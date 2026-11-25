"""MQTT -> InfluxDB / SQLite ingestion service.

Telemetry payload (published by the ESP32 on energy/<device>/telemetry):
    {"seq": 123, "v": 229.8, "i": 0.43, "p": 98.7, "e": 1.234, "f": 50.0, "pf": 0.99, "ts": 1732350000123}
    seq = Arduino packet counter, e = cumulative kWh, ts = optional ESP32 epoch ms (for latency).

* Valid readings     -> InfluxDB measurement "power" (tag: device)
* Device online/off  -> InfluxDB measurement "device_status" + SQLite audit log
* Anomalies (bad payload, out-of-range values, packet loss, device reset,
  data timeout, broker connection changes) -> SQLite audit log

Run:  python ingester.py
"""
import json
import logging
import signal
import sqlite3
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

import config

log = logging.getLogger("ingester")

FIELD_NAMES = {"v": "voltage", "i": "current", "p": "power", "e": "energy", "f": "frequency", "pf": "pf"}


# --------------------------------------------------------------------------- SQLite audit log
class EventLog:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS events (
                   id         INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts_utc     TEXT NOT NULL,
                   device     TEXT,
                   event_type TEXT NOT NULL,
                   severity   TEXT NOT NULL,
                   detail     TEXT
               )"""
        )
        self.db.commit()

    def add(self, event_type, detail="", device=None, severity="INFO"):
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self.lock:
            self.db.execute(
                "INSERT INTO events (ts_utc, device, event_type, severity, detail) VALUES (?,?,?,?,?)",
                (ts, device, event_type, severity, detail),
            )
            self.db.commit()
        level = {"INFO": logging.INFO, "WARN": logging.WARNING, "ERROR": logging.ERROR}[severity]
        log.log(level, "[event] %s %s %s", device or "-", event_type, detail)

    def close(self):
        with self.lock:
            self.db.close()


# --------------------------------------------------------------------------- ingester
class Ingester:
    def __init__(self):
        self.events = EventLog(config.SQLITE_PATH)
        self.influx = InfluxDBClient(url=config.INFLUX_URL, token=config.INFLUX_TOKEN, org=config.INFLUX_ORG)
        self.write_api = self.influx.write_api(write_options=SYNCHRONOUS)
        self.last_seq = {}      # device -> last seq number
        self.last_seen = {}     # device -> monotonic time of last telemetry
        self.silent = set()     # devices already flagged as timed out
        self.stats = {"ok": 0, "rejected": 0, "lost": 0}
        self.stop = threading.Event()

        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="energy-ingester")
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_disconnect = self.on_disconnect
        self.mqtt.on_message = self.on_message
        self.mqtt.reconnect_delay_set(min_delay=1, max_delay=10)

    # ---- MQTT callbacks
    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.events.add("broker_connect_failed", str(reason_code), severity="ERROR")
            return
        self.events.add("broker_connected", f"{config.MQTT_HOST}:{config.MQTT_PORT}")
        client.subscribe([(config.TOPIC_TELEMETRY, 0), (config.TOPIC_STATUS, 1),
                          (config.TOPIC_EVENT, 1), (config.TOPIC_DIAG, 0)])

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        if not self.stop.is_set():
            self.events.add("broker_disconnected", str(reason_code), severity="WARN")

    def on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        if len(parts) != 3:
            return
        _, device, kind = parts
        try:
            if kind == "telemetry":
                self.handle_telemetry(device, msg.payload)
            elif kind == "status":
                self.handle_status(device, msg.payload.decode(errors="replace").strip().lower())
            elif kind == "event":
                self.handle_event(device, msg.payload)
            elif kind == "diag":
                self.handle_diag(device, msg.payload)
        except Exception as exc:  # never let one bad message kill the loop
            self.events.add("ingest_error", f"{type(exc).__name__}: {exc}", device, "ERROR")

    # ---- handlers
    def handle_status(self, device, status):
        if status not in ("online", "offline"):
            self.events.add("unknown_status", status, device, "WARN")
            return
        self.events.add(f"device_{status}", "", device, "INFO" if status == "online" else "WARN")
        self.write_influx(Point("device_status").tag("device", device).field("online", int(status == "online")))

    def handle_event(self, device, raw):
        """Gateway-side events (Wi-Fi drops, sensor errors, reboots...) -> SQLite."""
        try:
            ev = json.loads(raw)
            etype = str(ev["type"])[:40]
        except (ValueError, KeyError, TypeError):
            self.events.add("invalid_event", f"raw={raw[:120]!r}", device, "WARN")
            return
        severity = str(ev.get("severity", "WARN")).upper()
        if severity not in ("INFO", "WARN", "ERROR"):
            severity = "WARN"
        detail = str(ev.get("detail", ""))
        if "uptime_s" in ev:
            detail = f"{detail} (gateway uptime {ev['uptime_s']}s)".strip()
        self.events.add(etype, detail, device, severity)

    def handle_diag(self, device, raw):
        """Gateway health snapshot -> InfluxDB measurement "gateway" (for the 24 h endurance test)."""
        try:
            diag = json.loads(raw)
        except ValueError:
            self.events.add("invalid_diag", f"raw={raw[:120]!r}", device, "WARN")
            return
        point = Point("gateway").tag("device", device)
        fields = 0
        for key, val in diag.items():
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                point = point.field(key, float(val))
                fields += 1
        if fields:
            self.write_influx(point)

    def handle_telemetry(self, device, raw):
        recv_ns = time.time_ns()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("payload is not a JSON object")
        except ValueError as exc:
            self.stats["rejected"] += 1
            self.events.add("invalid_payload", f"{exc}; raw={raw[:120]!r}", device, "WARN")
            return

        # Sanitise: all metrics must be present, numeric and plausible.
        values, problems = {}, []
        for key, (lo, hi) in config.LIMITS.items():
            val = data.get(key)
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                problems.append(f"{key} missing/non-numeric")
            elif not lo <= val <= hi:
                problems.append(f"{key}={val} outside [{lo}, {hi}]")
            else:
                values[key] = float(val)
        if problems:
            self.stats["rejected"] += 1
            self.events.add("out_of_range", "; ".join(problems), device, "WARN")
            return

        self.track_sequence(device, data.get("seq"))
        self.mark_seen(device)

        point = Point("power").tag("device", device).time(recv_ns, WritePrecision.NS)
        for key, val in values.items():
            point = point.field(FIELD_NAMES[key], val)
        if isinstance(data.get("seq"), int):
            point = point.field("seq", data["seq"])
        ts = data.get("ts")
        if isinstance(ts, (int, float)) and ts > 1_600_000_000_000:  # only if ESP32 clock is synced
            point = point.field("latency_ms", recv_ns / 1e6 - ts)
        if self.write_influx(point):
            self.stats["ok"] += 1
            log.debug("%s %s", device, values)

    def track_sequence(self, device, seq):
        if not isinstance(seq, int):
            return
        prev = self.last_seq.get(device)
        self.last_seq[device] = seq
        if prev is None:
            return
        if seq <= prev:
            self.events.add("device_reset", f"seq went {prev} -> {seq}", device, "WARN")
        elif seq > prev + 1:
            lost = seq - prev - 1
            self.stats["lost"] += lost
            self.events.add("packet_loss", f"{lost} packet(s) missing between seq {prev} and {seq}", device, "WARN")

    def mark_seen(self, device):
        self.last_seen[device] = time.monotonic()
        if device in self.silent:
            self.silent.discard(device)
            self.events.add("data_resumed", "", device)

    def write_influx(self, point):
        try:
            self.write_api.write(bucket=config.INFLUX_BUCKET, record=point)
            return True
        except Exception as exc:
            self.events.add("influx_write_failed", f"{type(exc).__name__}: {exc}"[:300], severity="ERROR")
            return False

    # ---- background watchdog + periodic stats
    def watchdog(self):
        last_report = time.monotonic()
        while not self.stop.wait(1.0):
            now = time.monotonic()
            for device, seen in list(self.last_seen.items()):
                if device not in self.silent and now - seen > config.DATA_TIMEOUT_S:
                    self.silent.add(device)
                    self.events.add("data_timeout", f"no telemetry for {now - seen:.1f}s", device, "WARN")
            if now - last_report >= 10:
                last_report = now
                log.info("stats: stored=%(ok)d rejected=%(rejected)d lost=%(lost)d", self.stats)

    def run(self):
        if not self.influx.ping():
            log.warning("InfluxDB at %s is not reachable yet - writes will be retried per message", config.INFLUX_URL)
        self.events.add("ingester_started")
        threading.Thread(target=self.watchdog, daemon=True).start()
        self.mqtt.connect_async(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
        self.mqtt.loop_start()
        try:
            while not self.stop.wait(0.5):
                pass
        finally:
            self.mqtt.loop_stop()
            self.mqtt.disconnect()
            self.events.add("ingester_stopped", f"stored={self.stats['ok']} rejected={self.stats['rejected']} lost={self.stats['lost']}")
            self.influx.close()
            self.events.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    ing = Ingester()
    signal.signal(signal.SIGINT, lambda *_: ing.stop.set())
    signal.signal(signal.SIGTERM, lambda *_: ing.stop.set())
    log.info("Ingester running - MQTT %s:%s -> InfluxDB %s, SQLite %s (Ctrl+C to stop)",
             config.MQTT_HOST, config.MQTT_PORT, config.INFLUX_URL, config.SQLITE_PATH)
    ing.run()


if __name__ == "__main__":
    main()
