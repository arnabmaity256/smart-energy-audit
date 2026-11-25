/*
 * ============================================================================
 *  Tier 2 - Wi-Fi / MQTT Gateway  (ESP32 DevKit V1, 3.3 V domain)
 *  Project : A Dual-Tier IoT Architecture for Smart Energy Auditing
 * ============================================================================
 *  Receives checksummed frames from the Arduino on UART2 (GPIO16), converts
 *  them to JSON and publishes them to the local Mosquitto broker.
 *
 *  MQTT topics (DEVICE_ID from secrets.h, e.g. "meter1"):
 *    energy/<id>/telemetry  {"seq":..,"v":..,"i":..,"p":..,"e":..,"f":..,"pf":..,"ts":..}
 *    energy/<id>/status     "online" (retained)  /  "offline" (retained Last Will)
 *    energy/<id>/event      {"type":"..","severity":"INFO|WARN|ERROR","detail":"..","uptime_s":..}
 *    energy/<id>/diag       {"rssi":..,"uptime_s":..,"heap":..,"frames_ok":..,...} every 30 s
 *
 *  Events are queued while Wi-Fi/MQTT is down and delivered after reconnect,
 *  so outages still end up in the SQLite audit log.
 *
 *  Libraries: "PubSubClient" by Nick O'Leary (Library Manager).
 *  Board    : "ESP32 Dev Module" (Espressif esp32 core 3.x / 4.x).
 *  USB Serial Monitor at 115200 baud shows everything the gateway does.
 * ============================================================================
 */
#include <WiFi.h>
#include <PubSubClient.h>
#include <sys/time.h>
#include <time.h>
#include "esp_system.h"
#include "secrets.h"

// ---------------------------------------------------------------- settings
constexpr int      LINK_RX_PIN        = 16;      // <- level shifter LV1 <- Uno D1
constexpr int      LINK_TX_PIN        = 17;      // not used (link is one-way)
constexpr uint32_t LINK_BAUD          = 9600;
constexpr int      LED_PIN            = 2;       // on-board blue LED
constexpr uint32_t MQTT_RETRY_MS      = 3000;
constexpr uint32_t WIFI_KICK_MS       = 15000;   // force re-join if auto-reconnect stalls
constexpr uint32_t DIAG_INTERVAL_MS   = 30000;
constexpr uint32_t ARDUINO_SILENT_MS  = 5000;    // no frame for this long -> event
constexpr size_t   FRAME_MAX_LEN           = 96;
constexpr size_t   EVENT_QUEUE_LEN    = 12;

// ---------------------------------------------------------------- globals
WiFiClient   net;
PubSubClient mqtt(net);

char topicTelemetry[64], topicStatus[64], topicEvent[64], topicDiag[64];

// UART line assembly
char   line[FRAME_MAX_LEN];
size_t lineLen      = 0;
bool   lineOverflow = false;

// State tracking
bool     wifiUp          = false;
uint32_t wifiDownSince   = 0;
uint32_t lastWifiKick    = 0;
bool     mqttWasUp       = false;
uint32_t mqttDownSince   = 0;
uint32_t lastMqttAttempt = 0;
uint32_t lastFrameAt     = 0;
bool     arduinoSilent   = false;
bool     sensorFault     = false;
uint32_t lastDiag        = 0;
uint32_t ledOffAt        = 0;

// Counters (reported in diag)
uint32_t framesOk = 0, framesBad = 0, framesDropped = 0, badSinceReport = 0;
uint32_t wifiDrops = 0, mqttDrops = 0;

// ---------------------------------------------------------------- event queue
struct PendingEvent { char json[200]; };
PendingEvent eventQueue[EVENT_QUEUE_LEN];
size_t eventHead = 0, eventCount = 0;

uint32_t uptimeS() { return millis() / 1000; }

void queueEvent(const char *type, const char *severity, const char *detail = "") {
  if (eventCount == EVENT_QUEUE_LEN) {               // full: drop the oldest
    eventHead = (eventHead + 1) % EVENT_QUEUE_LEN;
    eventCount--;
  }
  PendingEvent &ev = eventQueue[(eventHead + eventCount) % EVENT_QUEUE_LEN];
  snprintf(ev.json, sizeof(ev.json),
           "{\"type\":\"%s\",\"severity\":\"%s\",\"detail\":\"%s\",\"uptime_s\":%lu}",
           type, severity, detail, (unsigned long)uptimeS());
  eventCount++;
  Serial.printf("[event] %-5s %s %s\n", severity, type, detail);
}

void flushEvents() {
  while (eventCount && mqtt.connected()) {
    if (!mqtt.publish(topicEvent, eventQueue[eventHead].json)) break;
    eventHead = (eventHead + 1) % EVENT_QUEUE_LEN;
    eventCount--;
  }
}

// ---------------------------------------------------------------- helpers
const char *resetReasonText(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON:   return "power-on";
    case ESP_RST_EXT:       return "external reset pin";
    case ESP_RST_SW:        return "software restart";
    case ESP_RST_PANIC:     return "crash (panic)";
    case ESP_RST_INT_WDT:   return "interrupt watchdog";
    case ESP_RST_TASK_WDT:  return "task watchdog";
    case ESP_RST_WDT:       return "other watchdog";
    case ESP_RST_DEEPSLEEP: return "deep-sleep wake";
    case ESP_RST_BROWNOUT:  return "brownout (supply voltage dip)";
    default:                return "unknown";
  }
}

bool clockSynced() { return time(nullptr) > 1700000000; }   // after Nov 2023

uint64_t epochMs() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  return (uint64_t)tv.tv_sec * 1000ULL + tv.tv_usec / 1000;
}

void blink() {
  digitalWrite(LED_PIN, LOW);           // LED is normally ON while MQTT is up
  ledOffAt = millis() + 40;
}

// Validates a numeric token ("229.8", "0.000", "-1") without converting it,
// so the exact value the Arduino formatted is forwarded unchanged.
bool isNumber(const char *s) {
  if (!s || !*s) return false;
  char *end;
  strtod(s, &end);
  return *end == '\0';
}

// ---------------------------------------------------------------- frame handling
void handleData(char *payload) {          // payload: "<seq>,<V>,<A>,<W>,<kWh>,<Hz>,<PF>"
  char *tok[7];
  char *save = nullptr;
  int n = 0;
  for (char *t = strtok_r(payload, ",", &save); t && n < 8; t = strtok_r(nullptr, ",", &save)) {
    if (n < 7) tok[n] = t;
    n++;
  }
  if (n != 7) { framesBad++; badSinceReport++; return; }
  for (int k = 0; k < 7; k++)
    if (!isNumber(tok[k])) { framesBad++; badSinceReport++; return; }

  framesOk++;
  if (sensorFault) {
    sensorFault = false;
    queueEvent("sensor_recovered", "INFO", "PZEM responding again");
  }

  if (!mqtt.connected()) { framesDropped++; return; }   // ingester sees the seq gap

  char json[256];
  int len = snprintf(json, sizeof(json),
                     "{\"seq\":%s,\"v\":%s,\"i\":%s,\"p\":%s,\"e\":%s,\"f\":%s,\"pf\":%s",
                     tok[0], tok[1], tok[2], tok[3], tok[4], tok[5], tok[6]);
  if (clockSynced())
    len += snprintf(json + len, sizeof(json) - len, ",\"ts\":%llu", (unsigned long long)epochMs());
  snprintf(json + len, sizeof(json) - len, "}");

  if (mqtt.publish(topicTelemetry, json)) {
    blink();
    Serial.println(json);
  } else {
    framesDropped++;
  }
}

void handleLine(char *s, bool overflow) {
  if (s[0] == '#') {                                   // Arduino debug text
    Serial.printf("[uno] %s\n", s);
    return;
  }
  // Expect "$<body>*HH"
  size_t n = strlen(s);
  if (overflow || n < 5 || s[0] != '$' || s[n - 3] != '*') {
    framesBad++; badSinceReport++;
    return;
  }
  s[n - 3] = '\0';
  char *body = s + 1;
  uint8_t cs = 0;
  for (char *p = body; *p; ++p) cs ^= (uint8_t)*p;
  if (cs != (uint8_t)strtoul(s + n - 2, nullptr, 16)) {
    framesBad++; badSinceReport++;
    return;
  }

  lastFrameAt = millis();
  if (arduinoSilent) {
    arduinoSilent = false;
    queueEvent("arduino_resumed", "INFO", "frames arriving again");
  }

  if (body[0] == 'D' && body[1] == ',') {
    handleData(body + 2);
  } else if (body[0] == 'E' && body[1] == ',') {
    if (!sensorFault) {                                // report on state change only
      sensorFault = true;
      queueEvent("sensor_error", "ERROR", body + 2);
    }
  } else if (body[0] == 'B') {
    queueEvent("arduino_boot", "WARN", "Arduino (re)started - seq counter reset");
  } else {
    framesBad++; badSinceReport++;
  }
}

void pollLink() {
  while (Serial2.available()) {
    char c = (char)Serial2.read();
    if (c == '\n') {
      line[lineLen] = '\0';
      if (lineLen) handleLine(line, lineOverflow);
      lineLen = 0;
      lineOverflow = false;
    } else if (c == '\r') {
      // ignore
    } else if (lineLen < FRAME_MAX_LEN - 1) {
      line[lineLen++] = c;
    } else {
      lineOverflow = true;
    }
  }
}

// ---------------------------------------------------------------- connectivity
void maintainWifi() {
  bool up = WiFi.status() == WL_CONNECTED;
  uint32_t now = millis();

  if (up && !wifiUp) {
    wifiUp = true;
    char d[96];
    if (wifiDownSince) {
      snprintf(d, sizeof(d), "back after %lus, ip %s, rssi %d dBm",
               (unsigned long)((now - wifiDownSince) / 1000), WiFi.localIP().toString().c_str(), WiFi.RSSI());
      queueEvent("wifi_reconnected", "WARN", d);
    } else {
      snprintf(d, sizeof(d), "ip %s, rssi %d dBm", WiFi.localIP().toString().c_str(), WiFi.RSSI());
      queueEvent("wifi_connected", "INFO", d);
    }
  } else if (!up && wifiUp) {
    wifiUp = false;
    wifiDownSince = now;
    lastWifiKick = now;
    wifiDrops++;
    Serial.println("[wifi] connection lost");
  } else if (!up && now - lastWifiKick > WIFI_KICK_MS) {
    lastWifiKick = now;                                // auto-reconnect stalled: re-join
    Serial.println("[wifi] re-joining...");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }
}

void maintainMqtt() {
  uint32_t now = millis();
  if (mqtt.connected()) {
    mqtt.loop();
    return;
  }
  if (mqttWasUp) {
    mqttWasUp = false;
    mqttDownSince = now;
    mqttDrops++;
    Serial.println("[mqtt] connection lost");
  }
  if (!wifiUp || now - lastMqttAttempt < MQTT_RETRY_MS) return;
  lastMqttAttempt = now;

  Serial.printf("[mqtt] connecting to %s:%d ... ", MQTT_HOST, MQTT_PORT);
  // Last Will: broker publishes retained "offline" if we vanish without saying goodbye.
  if (mqtt.connect(DEVICE_ID, topicStatus, 1, true, "offline")) {
    Serial.println("ok");
    mqttWasUp = true;
    mqtt.publish(topicStatus, "online", true);
    if (mqttDownSince) {
      char d[48];
      snprintf(d, sizeof(d), "back after %lus", (unsigned long)((now - mqttDownSince) / 1000));
      queueEvent("mqtt_reconnected", "WARN", d);
    }
  } else {
    Serial.printf("failed (state %d)\n", mqtt.state());
  }
}

// ---------------------------------------------------------------- periodic tasks
void periodic() {
  uint32_t now = millis();

  if (!arduinoSilent && now - lastFrameAt > ARDUINO_SILENT_MS) {
    arduinoSilent = true;
    queueEvent("arduino_silent", "ERROR", "no frames from Arduino - check D1/shifter/GPIO16 wiring and GND");
  }

  if (now - lastDiag >= DIAG_INTERVAL_MS) {
    lastDiag = now;
    if (badSinceReport) {
      char d[64];
      snprintf(d, sizeof(d), "%lu corrupted UART frame(s) in last %lus",
               (unsigned long)badSinceReport, (unsigned long)(DIAG_INTERVAL_MS / 1000));
      queueEvent("uart_frame_errors", "WARN", d);
      badSinceReport = 0;
    }
    if (mqtt.connected()) {
      char json[256];
      snprintf(json, sizeof(json),
               "{\"rssi\":%d,\"uptime_s\":%lu,\"heap\":%lu,\"frames_ok\":%lu,\"frames_bad\":%lu,"
               "\"dropped\":%lu,\"wifi_drops\":%lu,\"mqtt_drops\":%lu}",
               WiFi.RSSI(), (unsigned long)uptimeS(), (unsigned long)ESP.getFreeHeap(),
               (unsigned long)framesOk, (unsigned long)framesBad, (unsigned long)framesDropped,
               (unsigned long)wifiDrops, (unsigned long)mqttDrops);
      mqtt.publish(topicDiag, json);
    }
  }

  if (ledOffAt && (int32_t)(now - ledOffAt) >= 0) ledOffAt = 0;
  digitalWrite(LED_PIN, (mqtt.connected() && !ledOffAt) ? HIGH : LOW);
}

// ---------------------------------------------------------------- Arduino
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== Smart Energy Audit - ESP32 gateway ===");
  pinMode(LED_PIN, OUTPUT);

  Serial2.setRxBufferSize(1024);        // ~20 s of frames buffered during blocking reconnects
  Serial2.begin(LINK_BAUD, SERIAL_8N1, LINK_RX_PIN, LINK_TX_PIN);

  snprintf(topicTelemetry, sizeof(topicTelemetry), "energy/%s/telemetry", DEVICE_ID);
  snprintf(topicStatus,    sizeof(topicStatus),    "energy/%s/status",    DEVICE_ID);
  snprintf(topicEvent,     sizeof(topicEvent),     "energy/%s/event",     DEVICE_ID);
  snprintf(topicDiag,      sizeof(topicDiag),      "energy/%s/diag",      DEVICE_ID);

  esp_reset_reason_t rr = esp_reset_reason();
  bool badReset = rr == ESP_RST_PANIC || rr == ESP_RST_INT_WDT || rr == ESP_RST_TASK_WDT ||
                  rr == ESP_RST_WDT || rr == ESP_RST_BROWNOUT;
  queueEvent("gateway_boot", badReset ? "ERROR" : "INFO", resetReasonText(rr));

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                 // lower latency, steadier link
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  lastWifiKick = millis();
  Serial.printf("[wifi] joining \"%s\"...\n", WIFI_SSID);

  configTime(0, 0, NTP_SERVER_1, NTP_SERVER_2);   // UTC; silently skipped if unreachable

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(15);
  mqtt.setSocketTimeout(3);
  mqtt.setBufferSize(512);

  lastFrameAt = millis();
  lastDiag = millis();
}

void loop() {
  pollLink();
  maintainWifi();
  maintainMqtt();
  flushEvents();
  periodic();
}
