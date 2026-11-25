"""Shared settings. Values come from server/.env (same file docker compose uses),
and can be overridden with normal environment variables."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / "server" / ".env")

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
TOPIC_TELEMETRY = "energy/+/telemetry"   # energy/<device>/telemetry  (JSON)
TOPIC_STATUS = "energy/+/status"         # energy/<device>/status     ("online"/"offline", LWT)
TOPIC_EVENT = "energy/+/event"           # energy/<device>/event      (JSON, gateway-side events)
TOPIC_DIAG = "energy/+/diag"             # energy/<device>/diag       (JSON, gateway health)

INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "iot-lab")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "energy")

SQLITE_PATH = Path(os.getenv("SQLITE_PATH", ROOT / "backend" / "data" / "events.db"))

# Seconds without telemetry before a device is flagged as silent.
DATA_TIMEOUT_S = float(os.getenv("DATA_TIMEOUT_S", "5"))

# Plausibility limits for sanitisation (PZEM-004T v3.0 ranges, 230 V / 50 Hz grid).
LIMITS = {
    "v": (80.0, 260.0),
    "i": (0.0, 100.0),
    "p": (0.0, 23000.0),
    "e": (0.0, 9999.999),
    "f": (45.0, 65.0),
    "pf": (0.0, 1.0),
}
