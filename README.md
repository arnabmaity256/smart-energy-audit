# Dual-Tier IoT Smart Energy Auditing

PZEM-004T → Arduino Uno → (BSS138 5V↔3.3V) → ESP32 → **MQTT** → Python ingester → **InfluxDB** (readings) + **SQLite** (audit log) → **Grafana**

## Layout

```
server/            Docker stack: Mosquitto, InfluxDB 2.7, Grafana 11 (+ provisioned dashboard)
  .env             local credentials (git-ignored; template: .env.example)
  grafana/make_dashboard.py   regenerates grafana/dashboards/energy-audit.json
backend/           Python ingestion service
  ingester.py      MQTT -> InfluxDB / SQLite
  simulator.py     fake ESP32 for testing without hardware
  show_events.py   print the SQLite audit log
  data/events.db   SQLite audit log (created automatically)
firmware/
  arduino_pzem/    Tier 1: Uno reads PZEM-004T, sends checksummed frames on D1
  esp32_gateway/   Tier 2: ESP32 receives frames on GPIO16, publishes MQTT
                   (secrets.h is git-ignored; template: secrets.example.h)
  WIRING.md        wiring table, flashing steps, bring-up order, troubleshooting
docs/
  mains-wiring-guide.html   visual guide: PZEM to 230 V via extension board
```

## First-time setup (fresh clone)

Two files hold secrets and are git-ignored. Create them from the templates:

```powershell
copy server\.env.example server\.env                                         # then set passwords/token
copy firmware\esp32_gateway\secrets.example.h firmware\esp32_gateway\secrets.h   # then set Wi-Fi + PC IP
cd backend; python -m venv .venv; .venv\Scripts\pip install -r requirements.txt
```

## Daily use

Start Docker Desktop first, then:

```powershell
# 1. server stack (stays running in the background; restarts with Docker)
cd server;  docker compose up -d

# 2. ingester (keep this terminal open)
cd backend; .venv\Scripts\activate; python ingester.py

# 3. no hardware yet? in another terminal:
cd backend; .venv\Scripts\activate; python simulator.py            # add --loss 0.05 --bad 0.02 to test fault logging
```

| Service   | URL / address                     | Login                       |
|-----------|-----------------------------------|-----------------------------|
| Grafana   | http://localhost:3001             | `GRAFANA_USER` / `GRAFANA_PASSWORD` from `server/.env` |
| InfluxDB  | http://localhost:8086             | `INFLUX_USER` / `INFLUX_PASSWORD` from `server/.env`   |
| Mosquitto | `192.168.0.101:1883` (from ESP32) | anonymous                   |

Dashboard: Grafana → Dashboards → *Energy Audit* → *Smart Energy Audit*. Set the tariff (₹/kWh) box at the top for the cost panel.

Debug MQTT traffic: `docker exec -it mosquitto mosquitto_sub -t "energy/#" -v`

## MQTT contract (the ESP32 firmware must follow this)

| Topic                        | Payload                                                                                   |
|------------------------------|-------------------------------------------------------------------------------------------|
| `energy/<device>/telemetry`  | `{"seq":123,"v":229.8,"i":0.43,"p":98.7,"e":1.234,"f":50.0,"pf":0.99,"ts":1732350000123}` |
| `energy/<device>/status`     | `online` on connect (retained); set as **Last Will** = `offline` (retained, QoS 1)        |
| `energy/<device>/event`      | `{"type":"wifi_reconnected","severity":"WARN","detail":"back after 12s","uptime_s":3600}` → SQLite |
| `energy/<device>/diag`       | `{"rssi":-58,"uptime_s":3600,"heap":210000,"frames_ok":..,"frames_bad":..,"dropped":..,"wifi_drops":..,"mqtt_drops":..}` every 30 s → InfluxDB `gateway` |

`seq` = Arduino packet counter (packet-loss detection), `e` = cumulative kWh from PZEM,
`ts` = optional epoch ms from the ESP32's NTP clock (used for latency measurement).

## Audit events logged to SQLite

`ingester_started/stopped`, `broker_connected/disconnected`, `device_online/offline`,
`packet_loss`, `device_reset`, `data_timeout`, `data_resumed`, `invalid_payload`,
`out_of_range`, `influx_write_failed`, `ingest_error`, plus everything the gateway sends on
`event`: `gateway_boot` (with reset reason, e.g. brownout/watchdog), `wifi_connected/reconnected`,
`mqtt_reconnected`, `arduino_boot`, `arduino_silent/resumed`, `sensor_error/recovered`, `uart_frame_errors`.

## Notes

- If the PC's Wi-Fi IP changes, update the broker IP in the ESP32 firmware (or give the PC a DHCP reservation).
- Wipe all data and start fresh: `cd server; docker compose down -v` (also deletes InfluxDB/Grafana volumes).
