"""Generates the provisioned Grafana dashboard (Flux queries against InfluxDB).

    python make_dashboard.py dashboards/energy-audit.json

Grafana picks up the rewritten file within ~10 s (no restart needed).
"""
import json, sys

DS = {"type": "influxdb", "uid": "influxdb-energy"}
B = 'from(bucket: "energy")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'


def series(field, measurement="power"):
    return (B + f'  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}")\n'
            '  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)\n'
            f'  |> map(fn: (r) => ({{r with _field: "{field}"}}))\n  |> keep(columns: ["_time", "_value", "_field"])')


def last(field):
    return (B + f'  |> filter(fn: (r) => r._measurement == "power" and r._field == "{field}")\n'
            '  |> last()\n  |> keep(columns: ["_time", "_value"])')


ENERGY_USED = (B + '  |> filter(fn: (r) => r._measurement == "power" and r._field == "energy")\n'
               '  |> spread()\n  |> keep(columns: ["_value"])')
COST = ENERGY_USED + '\n  |> map(fn: (r) => ({r with _value: r._value * float(v: "${tariff}")}))'
STATUS = (B.replace("v.timeRangeStart", "-30d") +
          '  |> filter(fn: (r) => r._measurement == "device_status" and r._field == "online")\n'
          '  |> last()\n  |> keep(columns: ["_time", "_value"])')

pid = 0


def panel(kind, title, queries, x, y, w, h, unit="none", decimals=None, extra=None):
    global pid
    pid += 1
    defaults = {"unit": unit}
    if decimals is not None:
        defaults["decimals"] = decimals
    p = {
        "id": pid, "type": kind, "title": title, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"refId": chr(65 + i), "datasource": DS, "query": q} for i, q in enumerate(queries)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
    }
    if kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "value", "graphMode": "none", "textMode": "value"}
    if kind == "timeseries":
        defaults["custom"] = {"lineWidth": 2, "fillOpacity": 10, "showPoints": "never"}
        p["options"] = {"legend": {"displayMode": "list", "placement": "bottom"},
                        "tooltip": {"mode": "multi"}}
    if extra:
        for k, v in extra.items():
            if k == "defaults":
                defaults.update(v)
            else:
                p[k] = v
    return p


status_extra = {"defaults": {
    "mappings": [{"type": "value", "options": {
        "1": {"text": "ONLINE", "color": "green"}, "0": {"text": "OFFLINE", "color": "red"}}}],
    "color": {"mode": "fixed", "fixedColor": "text"},
    "noValue": "NO DATA"}}

panels = [
    panel("stat", "Device", [STATUS], 0, 0, 4, 4, extra=status_extra),
    panel("stat", "Voltage", [last("voltage")], 4, 0, 4, 4, "volt", 1),
    panel("stat", "Current", [last("current")], 8, 0, 4, 4, "amp", 3),
    panel("stat", "Active Power", [last("power")], 12, 0, 4, 4, "watt", 1),
    panel("stat", "Power Factor", [last("pf")], 16, 0, 4, 4, "none", 2),
    panel("stat", "Frequency", [last("frequency")], 20, 0, 4, 4, "hertz", 1),
    panel("timeseries", "Active Power", [series("power")], 0, 4, 16, 9, "watt"),
    panel("stat", "Energy used (selected range)", [ENERGY_USED], 16, 4, 8, 4, "kwatth", 3),
    panel("stat", "Estimated cost (selected range)", [COST], 16, 8, 8, 5, "currencyINR", 2),
    panel("timeseries", "Voltage", [series("voltage")], 0, 13, 12, 8, "volt"),
    panel("timeseries", "Current", [series("current")], 12, 13, 12, 8, "amp"),
    panel("timeseries", "Power Factor", [series("pf")], 0, 21, 12, 7, "none"),
    panel("timeseries", "Frequency", [series("frequency")], 12, 21, 12, 7, "hertz"),
    panel("timeseries", "Gateway Wi-Fi signal (RSSI)", [series("rssi", "gateway")], 0, 28, 10, 7, "dBm"),
    panel("timeseries", "Gateway free heap", [series("heap", "gateway")], 10, 28, 8, 7, "bytes"),
    panel("stat", "Gateway uptime", [B.replace("v.timeRangeStart", "-1d") +
          '  |> filter(fn: (r) => r._measurement == "gateway" and r._field == "uptime_s")\n'
          '  |> last()\n  |> keep(columns: ["_time", "_value"])'], 18, 28, 6, 7, "s"),
]

dashboard = {
    "uid": "energy-audit",
    "title": "Smart Energy Audit",
    "tags": ["iot", "energy"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "1s",
    "time": {"from": "now-15m", "to": "now"},
    "timepicker": {"refresh_intervals": ["1s", "5s", "10s", "30s", "1m", "5m"]},
    "templating": {"list": [{
        "type": "textbox", "name": "tariff", "label": "Tariff (₹/kWh)",
        "query": "8", "current": {"text": "8", "value": "8"}, "hide": 0}]},
    "panels": panels,
}

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(dashboard, f, indent=2, ensure_ascii=False)
print("wrote", sys.argv[1])
