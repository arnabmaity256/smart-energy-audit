// ---------------------------------------------------------------------------
//  TEMPLATE - copy this file to secrets.h and fill in your values.
//  secrets.h is git-ignored so your Wi-Fi password never gets committed.
// ---------------------------------------------------------------------------
#pragma once

// Wi-Fi network the ESP32 joins (2.4 GHz only - the ESP32 cannot use 5 GHz).
// Use the network's real SSID (`netsh wlan show interfaces`), not the Windows
// profile name, which can have a number appended.
#define WIFI_SSID      "your-ssid"
#define WIFI_PASSWORD  "your-wifi-password"

// IPv4 address of the PC running Mosquitto (run `ipconfig` -> Wi-Fi adapter).
#define MQTT_HOST      "192.168.0.101"
#define MQTT_PORT      1883

// Used in the MQTT topics: energy/<DEVICE_ID>/telemetry ...
#define DEVICE_ID      "meter1"

// Optional clock sync for latency measurement. If no NTP server is reachable
// (fully off-grid), readings are still published, just without the "ts" field.
#define NTP_SERVER_1   "pool.ntp.org"
#define NTP_SERVER_2   "time.google.com"
