"""Fake ESP32 gateway: publishes realistic PZEM-004T readings so the whole
backend + dashboard can be built and tested before the hardware is ready.

It behaves like the real firmware will: JSON telemetry once per second on
energy/<device>/telemetry, "online" on energy/<device>/status, and an MQTT
Last-Will "offline" message if it dies unexpectedly.

    python simulator.py                    # 1 reading/s, load switches every 20 s
    python simulator.py --loss 0.05        # drop 5 % of packets (tests packet-loss logging)
    python simulator.py --bad 0.02         # send 2 % corrupted payloads (tests sanitisation)
"""
import argparse
import json
import math
import random
import time

import paho.mqtt.client as mqtt

import config

# (name, watts, power factor) - loads cycled through to make the graphs interesting
LOADS = [("idle", 0.0, 0.0), ("100 W bulb", 100.0, 1.0), ("soldering iron", 60.0, 0.99),
         ("bulb + iron", 160.0, 0.99), ("fan", 55.0, 0.85)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="meter1")
    ap.add_argument("--host", default=config.MQTT_HOST)
    ap.add_argument("--port", type=int, default=config.MQTT_PORT)
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between readings")
    ap.add_argument("--switch", type=float, default=20.0, help="seconds between load changes")
    ap.add_argument("--loss", type=float, default=0.0, help="fraction of packets to drop")
    ap.add_argument("--bad", type=float, default=0.0, help="fraction of corrupted payloads")
    args = ap.parse_args()

    status_topic = f"energy/{args.device}/status"
    tele_topic = f"energy/{args.device}/telemetry"

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{args.device}")
    client.will_set(status_topic, "offline", qos=1, retain=True)
    client.connect(args.host, args.port, keepalive=10)
    client.loop_start()
    client.publish(status_topic, "online", qos=1, retain=True)
    client.publish(f"energy/{args.device}/event", json.dumps(
        {"type": "gateway_boot", "severity": "INFO", "detail": "simulator start", "uptime_s": 0}), qos=1)
    diag_topic = f"energy/{args.device}/diag"
    started = time.monotonic()
    print(f"Simulating {args.device} -> {args.host}:{args.port}  (Ctrl+C to stop)")

    seq, energy_kwh, load_idx, last_switch = 0, 12.345, 1, time.monotonic()
    try:
        while True:
            if time.monotonic() - last_switch >= args.switch:
                load_idx = (load_idx + 1) % len(LOADS)
                last_switch = time.monotonic()
                print(f"  load -> {LOADS[load_idx][0]}")
            name, watts, pf = LOADS[load_idx]

            v = 230.0 + 4.0 * math.sin(time.time() / 60) + random.gauss(0, 0.4)
            p = max(0.0, watts * (v / 230.0) ** 2 + (random.gauss(0, 0.6) if watts else 0.0))
            i = p / (v * pf) if pf else 0.0
            energy_kwh += p * args.interval / 3_600_000
            seq += 1

            payload = {"seq": seq, "v": round(v, 1), "i": round(i, 3), "p": round(p, 1),
                       "e": round(energy_kwh, 3), "f": round(50.0 + random.gauss(0, 0.03), 1),
                       "pf": round(pf, 2), "ts": int(time.time() * 1000)}

            if random.random() < args.loss:
                print(f"  (dropped seq {seq})")
            elif random.random() < args.bad:
                client.publish(tele_topic, random.choice(["{garbage", json.dumps({**payload, "v": 9999})]))
                print(f"  (sent corrupted seq {seq})")
            else:
                client.publish(tele_topic, json.dumps(payload))
            if seq % 10 == 0:   # the real gateway sends this every 30 s
                client.publish(diag_topic, json.dumps({
                    "rssi": random.randint(-62, -52), "uptime_s": int(time.monotonic() - started),
                    "heap": 210_000 + random.randint(-2000, 2000), "frames_ok": seq, "frames_bad": 0,
                    "dropped": 0, "wifi_drops": 0, "mqtt_drops": 0}))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.publish(status_topic, "offline", qos=1, retain=True).wait_for_publish(2)
        client.loop_stop()
        client.disconnect()
        print("stopped")


if __name__ == "__main__":
    main()
