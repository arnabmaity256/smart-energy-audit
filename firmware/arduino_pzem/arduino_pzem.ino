/*
 * ============================================================================
 *  Tier 1 - Edge Acquisition Node  (Arduino Uno R3, 5 V domain)
 *  Project : A Dual-Tier IoT Architecture for Smart Energy Auditing
 * ============================================================================
 *  Polls a PZEM-004T v3.0 (Modbus-RTU over TTL UART) once per second and
 *  forwards each reading to the ESP32 gateway as one checksummed text frame.
 *
 *  Wiring (see firmware/WIRING.md):
 *    PZEM TX  -> Uno D10 (SoftwareSerial RX)      PZEM 5V  -> Uno 5V
 *    PZEM RX  <- Uno D11 (SoftwareSerial TX)      PZEM GND -> Uno GND
 *    Uno D1 (hardware TX) -> level shifter HV1 -> LV1 -> ESP32 GPIO16 (RX2)
 *
 *  Link protocol (9600 baud, 8N1, one line per frame, NMEA-style checksum):
 *    $D,<seq>,<V>,<A>,<W>,<kWh>,<Hz>,<PF>*CS   data frame
 *    $E,<code>*CS                              sensor error (no seq used)
 *    $B,UNO*CS                                 Arduino has just booted
 *    # free text                               debug line, ignored by ESP32
 *  CS = XOR of every character between '$' and '*', as two hex digits.
 *
 *  Because hardware Serial (D1) also drives the USB port, every frame is
 *  visible in the Arduino IDE Serial Monitor at 9600 baud for debugging.
 *
 *  Library: "PZEM004Tv30" by Jakub Mandula (Arduino Library Manager).
 *  NOTE: the PZEM only answers when 230 V AC is connected to its L/N
 *  terminals - with no mains you will only see $E,PZEM_NO_RESPONSE frames.
 * ============================================================================
 */
#include <SoftwareSerial.h>
#include <PZEM004Tv30.h>

// ---------------------------------------------------------------- settings
const uint8_t       PZEM_RX_PIN        = 10;    // Uno receives  <- PZEM TX
const uint8_t       PZEM_TX_PIN        = 11;    // Uno transmits -> PZEM RX
const unsigned long LINK_BAUD          = 9600;  // Uno -> ESP32 link speed
const unsigned long SAMPLE_INTERVAL_MS = 1000;  // one reading per second
const unsigned long LED_BLINK_MS       = 30;

// ---------------------------------------------------------------- globals
SoftwareSerial pzemSerial(PZEM_RX_PIN, PZEM_TX_PIN);
PZEM004Tv30    pzem(pzemSerial);

uint32_t      seq        = 0;   // increments only for successful data frames
unsigned long nextSample = 0;
unsigned long ledOffAt   = 0;

// ---------------------------------------------------------------- framing
// Sends "$<body>*CS\r\n". The checksum lets the ESP32 reject frames that were
// corrupted on the wire (loose jumper, noise from the mains side, etc.).
void sendFrame(const char *body) {
  uint8_t cs = 0;
  for (const char *p = body; *p; ++p) cs ^= (uint8_t)*p;

  char tail[4];
  snprintf(tail, sizeof(tail), "*%02X", cs);
  Serial.print('$');
  Serial.print(body);
  Serial.println(tail);
}

// ---------------------------------------------------------------- sampling
void sampleAndSend() {
  // voltage() triggers one Modbus transaction; the library caches the full
  // register block, so the other getters below do not hit the bus again.
  float v = pzem.voltage();
  if (isnan(v)) {                       // no reply: mains off, wiring, or address
    sendFrame("E,PZEM_NO_RESPONSE");
    return;
  }
  float i  = pzem.current();
  float p  = pzem.power();
  float e  = pzem.energy();             // kWh, cumulative, stored inside PZEM
  float f  = pzem.frequency();
  float pf = pzem.pf();
  if (isnan(i) || isnan(p) || isnan(e) || isnan(f) || isnan(pf)) {
    sendFrame("E,PZEM_PARTIAL_READ");
    return;
  }

  // AVR printf has no %f support -> format floats with dtostrf.
  char sv[10], si[10], sp[12], se[12], sf[8], spf[8];
  dtostrf(v,  1, 1, sv);
  dtostrf(i,  1, 3, si);
  dtostrf(p,  1, 1, sp);
  dtostrf(e,  1, 3, se);
  dtostrf(f,  1, 1, sf);
  dtostrf(pf, 1, 2, spf);

  char body[80];
  snprintf(body, sizeof(body), "D,%lu,%s,%s,%s,%s,%s,%s",
           (unsigned long)++seq, sv, si, sp, se, sf, spf);
  sendFrame(body);

  digitalWrite(LED_BUILTIN, HIGH);      // short blink = one good reading
  ledOffAt = millis() + LED_BLINK_MS;
}

// ---------------------------------------------------------------- Arduino
void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  Serial.begin(LINK_BAUD);              // D1 -> level shifter -> ESP32 RX2 (and USB)
  delay(200);
  Serial.println(F("# Smart Energy Audit - Uno acquisition node"));
  sendFrame("B,UNO");                   // lets the gateway log Arduino restarts

  nextSample = millis() + SAMPLE_INTERVAL_MS;
}

void loop() {
  unsigned long now = millis();

  if (ledOffAt && (long)(now - ledOffAt) >= 0) {
    digitalWrite(LED_BUILTIN, LOW);
    ledOffAt = 0;
  }

  if ((long)(now - nextSample) >= 0) {
    nextSample += SAMPLE_INTERVAL_MS;   // fixed-rate schedule, no drift
    if ((long)(now - nextSample) >= 0)  // fell behind (e.g. PZEM timeouts)
      nextSample = now + SAMPLE_INTERVAL_MS;
    sampleAndSend();
  }
}
