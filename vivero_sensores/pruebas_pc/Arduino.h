// Arduino de mentira para probar la logica del firmware en la computadora
// (prueba_riego.cpp). Solo trae lo que usa vivero_sensoresV2.ino.
#pragma once
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <deque>
typedef unsigned char byte;
class __FlashStringHelper;
#define F(s) (reinterpret_cast<const __FlashStringHelper *>(s))
#define HIGH 1
#define LOW 0
#define OUTPUT 1
#define INPUT_PULLUP 2
#define A0 14
#define A1 15
#define A2 16
#define A4 18
#define A5 19
#define DHT11 11
extern unsigned long fake_ms;
extern int fake_analog[32];
extern int fake_pins[32];
inline unsigned long millis() { return fake_ms; }
inline void delay(unsigned long ms) { fake_ms += ms; }
inline void delayMicroseconds(unsigned int) {}
inline int analogRead(int pin) { return fake_analog[pin]; }
inline int digitalRead(int pin) { return fake_pins[pin]; }
inline void digitalWrite(int pin, int v) { fake_pins[pin] = v; }
inline void pinMode(int pin, int mode) { if (mode == INPUT_PULLUP) fake_pins[pin] = HIGH; }
#define constrain(x, a, b) ((x) < (a) ? (a) : ((x) > (b) ? (b) : (x)))
inline long map(long x, long in_min, long in_max, long out_min, long out_max) {
  return (x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min;
}
using std::isnan;
struct FakeSerial {
  std::string out;
  std::deque<char> in;
  void begin(long) {}
  int available() { return (int)in.size(); }
  int read() { char c = in.front(); in.pop_front(); return c; }
  void print(const __FlashStringHelper *s) { out += reinterpret_cast<const char *>(s); }
  void print(const char *s) { out += s; }
  void print(char c) { out += c; }
  void print(int v) { out += std::to_string(v); }
  void print(unsigned int v) { out += std::to_string(v); }
  void print(long v) { out += std::to_string(v); }
  void print(unsigned long v) { out += std::to_string(v); }
  void print(float v, int d) { char b[32]; snprintf(b, sizeof b, "%.*f", d, v); out += b; }
  void println(const __FlashStringHelper *s) { print(s); out += "\n"; }
};
extern FakeSerial Serial;
