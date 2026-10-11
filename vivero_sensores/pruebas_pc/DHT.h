// DHT de mentira: la prueba decide la temperatura y la humedad.
#pragma once
extern float fake_temp, fake_hum;
class DHT { public: DHT(int, int) {} void begin() {} float readTemperature() { return fake_temp; } float readHumidity() { return fake_hum; } };
