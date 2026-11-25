# Wiring and Flashing Guide

> **Mains safety:** the PZEM-004T's L/N screw terminals carry 230 V AC. Wire them only
> with the plug pulled out, keep that side inside the closed enclosure, put a fuse
> (~1–2 A for lab loads) in the live line, and have a lab instructor check the wiring before
> the first power-on. Never touch the PZEM board while it is plugged in: its terminal side is live.

## Block diagram

```
 230 V AC ──L/N──► PZEM-004T v3.0 ◄── 100 A CT clamp (around the LIVE wire only)
                     │ TX  RX  5V  GND
                     │  │   │   │
                     ▼  ▲   │   │
 Arduino Uno        D10 D11 5V GND          (5 V domain)
                     D1 (TX) ──► HV1 ┐
                     5V ───────► HV  │ BSS138 logic level converter
                     GND ──────► GND │
                                     │
 ESP32 DevKit V1   GPIO16 (RX2) ◄─ LV1 ┘     (3.3 V domain)
                     3V3 ──────► LV
                     GND ──────► GND
```

## Connection table

| From | To | Notes |
|---|---|---|
| PZEM **TX** | Uno **D10** | SoftwareSerial RX |
| PZEM **RX** | Uno **D11** | SoftwareSerial TX |
| PZEM **5V** | Uno **5V** | powers the PZEM's opto-isolated TTL side |
| PZEM **GND** | Uno **GND** | |
| Uno **D1 (TX)** | Level shifter **HV1** | 5 V serial out |
| Level shifter **LV1** | ESP32 **GPIO16 (RX2)** | 3.3 V serial in |
| Uno **5V** | Level shifter **HV** | high-side reference |
| ESP32 **3V3** | Level shifter **LV** | low-side reference |
| Uno **GND**, ESP32 **GND**, shifter **GND** (both sides) | common ground | **required**: without it the serial data is garbage |
| CT clamp | PZEM CT terminals | clamp around the **live** wire only (not the whole cable) |
| Mains **L / N** | PZEM **L / N** screw terminals | needed for voltage *and* for the PZEM to reply at all |

Leave Uno **D0** and ESP32 **GPIO17** unconnected (the link is one-way, Uno → ESP32).

**Power:** run each board from its own USB cable to the PC. You then also get two Serial
Monitors for debugging.

## Flashing

Arduino IDE 2.x:

1. **Libraries** (Tools → Manage Libraries): `PZEM004Tv30` by Jakub Mandula, `PubSubClient` by Nick O'Leary.
2. **ESP32 boards:** File → Preferences → *Additional boards manager URLs* →
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`, then Boards Manager → install **esp32 by Espressif** (3.x or 4.x; tested to compile on 4.0.0).
3. **Uno:** open `arduino_pzem/arduino_pzem.ino`, Board = *Arduino Uno*, upload.
   If the upload fails with sync errors, unplug the D1 wire for the upload and reconnect it afterwards.
4. **ESP32:** edit `esp32_gateway/secrets.h` (Wi-Fi password; PC IP from `ipconfig`), open
   `esp32_gateway/esp32_gateway.ino`, Board = *ESP32 Dev Module*, upload. If it hangs at
   "Connecting....", hold the **BOOT** button until the upload starts.

## Bring-up order (test one link at a time)

| Step | What to do | Expected result |
|---|---|---|
| 1 | ESP32 alone, Serial Monitor 115200 | `[wifi] joining…`, `[mqtt] connecting … ok`; after 5 s `arduino_silent` (normal: no Uno yet). The dashboard's **Device** tile shows ONLINE. |
| 2 | Uno + PZEM **without mains**, Serial Monitor 9600 | `$B,UNO*..` then `$E,PZEM_NO_RESPONSE*..` every second (normal: the PZEM needs AC to reply) |
| 3 | Plug in mains with the bulb as load | `$D,1,229.8,0.431,98.7,0.002,50.0,1.00*..` lines |
| 4 | Connect D1 → shifter → GPIO16 + common GND | ESP32 monitor prints JSON lines; the Grafana graphs move |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `[mqtt] … failed (state -2)` | wrong `MQTT_HOST`, PC firewall, or ESP32 on a different Wi-Fi network / client isolation on the router |
| Wi-Fi never connects | 5 GHz-only network, wrong password, or an SSID typo (it is case- and space-sensitive) |
| `arduino_silent` never clears | D1/shifter/GPIO16 wiring, missing common GND, or HV/LV swapped on the shifter |
| `uart_frame_errors` events | loose jumper or long wires near the mains side; re-seat the wires and keep them short |
| `sensor_error PZEM_NO_RESPONSE` with mains on | PZEM TX/RX swapped, PZEM 5V/GND not connected |
| Power reads 0 with a load on | CT clamp around both conductors (fields cancel), or around neutral with the load on another circuit |
