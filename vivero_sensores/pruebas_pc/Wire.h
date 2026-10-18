// Bus I2C (Wire) y sensor SCD40/SCD41 de mentira para prueba_riego.cpp.
// Si fake_co2 >= 0, responde en 0x62 con ese valor en ppm y su CRC-8 real.
// Si fake_co2 < 0, simula el sensor desconectado (NACK en endTransmission);
// al quedarse sin corriente deja de medir.
// fake_scd_running es el modo del sensor, como el real: start (0x21B1) con el
// sensor ya midiendo responde NACK; stop (0x3F86) lo pasa a reposo, y en
// reposo no hay muestras.
#pragma once
#include "Arduino.h"

extern int fake_co2;
extern bool fake_scd_running;

class FakeWire {
  byte last_cmd_msb = 0;
  byte last_cmd_lsb = 0;
  std::deque<byte> rx;

  static byte crc8(byte msb, byte lsb) {
    byte crc = 0xFF;
    byte d[2] = {msb, lsb};
    for (int i = 0; i < 2; ++i) {
      crc ^= d[i];
      for (int b = 0; b < 8; ++b)
        crc = (crc & 0x80) ? (byte)((crc << 1) ^ 0x31) : (byte)(crc << 1);
    }
    return crc;
  }

public:
  unsigned long timeout_us = 0;     // lo que pidio setWireTimeout() (0 = nunca)
  bool timeout_reset = false;

  void begin() {}
  void setWireTimeout(unsigned long us = 25000, bool reset = false) {
    timeout_us = us;
    timeout_reset = reset;
  }
  void beginTransmission(byte) {
    last_cmd_msb = 0;
    last_cmd_lsb = 0;
  }
  void write(byte b) {
    last_cmd_msb = last_cmd_lsb;
    last_cmd_lsb = b;
  }
  byte endTransmission() {
    if (fake_co2 < 0) {
      fake_scd_running = false; // sin corriente deja de medir
      return 2;                 // NACK: sensor desconectado en A4/A5
    }
    unsigned int cmd = ((unsigned int)last_cmd_msb << 8) | last_cmd_lsb;
    if (cmd == 0x21B1) {        // start_periodic_measurement
      if (fake_scd_running)
        return 2;               // ya estaba midiendo: no se permite
      fake_scd_running = true;
    } else if (cmd == 0x3F86) { // stop_periodic_measurement
      fake_scd_running = false;
    }
    return 0;
  }
  byte requestFrom(byte, byte count) {
    rx.clear();
    if (fake_co2 < 0)
      return 0;
    unsigned int cmd = ((unsigned int)last_cmd_msb << 8) | last_cmd_lsb;
    if (cmd == 0xE4B8 && count == 3) {
      // get_data_ready_status -> dato listo (bits 0..10 != 0) si esta midiendo
      byte lsb = fake_scd_running ? 0x01 : 0x00;
      rx.push_back(0x80);
      rx.push_back(lsb);
      rx.push_back(crc8(0x80, lsb));
      return 3;
    }
    if (cmd == 0xEC05 && count == 9) {
      // read_measurement -> 3 bytes de CO2 + 6 bytes de temp/hum
      byte msb = (byte)((fake_co2 >> 8) & 0xFF);
      byte lsb = (byte)(fake_co2 & 0xFF);
      rx.push_back(msb);
      rx.push_back(lsb);
      rx.push_back(crc8(msb, lsb));
      for (int i = 0; i < 2; ++i) {
        rx.push_back(0x00);
        rx.push_back(0x00);
        rx.push_back(crc8(0x00, 0x00));
      }
      return 9;
    }
    return 0;
  }
  int read() {
    if (rx.empty())
      return -1;
    byte b = rx.front();
    rx.pop_front();
    return b;
  }
};

extern FakeWire Wire;
