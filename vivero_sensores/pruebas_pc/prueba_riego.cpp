// Pruebas de la logica de riego (ACT-02) del firmware, en la computadora.
// No necesita el Arduino: Arduino.h y DHT.h de esta carpeta son de mentira
// (reloj, pines, Serial y sensores controlados por la prueba).
//
//   g++ -std=gnu++11 -I. -o prueba_riego prueba_riego.cpp
//   ./prueba_riego            (en Windows: prueba_riego.exe)
//
// Compila el .ino tal cual, sin copiarlo.
#include "Arduino.h"
#include "DHT.h"
unsigned long fake_ms = 0;
int fake_analog[32];
int fake_pins[32];
float fake_temp = 23, fake_hum = 70;
FakeSerial Serial;
#include "../vivero_sensoresV2/vivero_sensoresV2.ino"

static int fails = 0;
#define CHECK(cond, msg) do { if (!(cond)) { printf("FALLA: %s (linea %d)\n", msg, __LINE__); fails++; } else printf("ok: %s\n", msg); } while (0)

// suelo: 555 = 0 % (aire), 267 = 100 % (agua). nivel: 0..150.
int sueloRawPara(int pct) { return 555 - (555 - 267) * pct / 100; }
std::string lastLine;
void step(unsigned long ms = 2000) {
  fake_ms += ms;
  Serial.out.clear();
  loop();
  if (!Serial.out.empty()) lastLine = Serial.out;
}
void send(const char *line) { for (const char *p = line; *p; ++p) Serial.in.push_back(*p); Serial.in.push_back('\n'); }
bool has(const char *s) { return lastLine.find(s) != std::string::npos; }
bool pump() { return fake_pins[5] == HIGH; }

int main() {
  fake_analog[A0] = sueloRawPara(62); fake_analog[A1] = 120; fake_analog[A2] = 300;
  setup();
  step();
  CHECK(has("\"modo\":\"AUTO\"") && has("\"cmd_id\":0") && has("\"cmd_err\":null") && has("\"bomba_paro\":null"), "arranca en AUTO, cmd_id 0");
  CHECK(!pump(), "tierra al 62 %: bomba apagada");

  // AUTO: tierra seca -> riega sola, para al llegar al objetivo (50 %)
  fake_analog[A0] = sueloRawPara(40); step();
  CHECK(pump() && has("\"bomba_activa\":true"), "AUTO + tierra 40 %: riega sola");
  fake_analog[A0] = sueloRawPara(52); step();
  CHECK(pump(), "llega al objetivo a los 2 s: sigue (minimo 10 s, el ruido no la hace parpadear)");
  for (int i = 0; i < 3; ++i) step();
  CHECK(pump(), "a los 8 s sigue");
  step();
  CHECK(!pump() && has("\"bomba_paro\":\"OBJETIVO\""), "a los 10 s y en el objetivo: para (OBJETIVO)");

  // descanso: vuelve a secarse enseguida y NO arranca hasta pasar 5 min
  fake_analog[A0] = sueloRawPara(40); step();
  CHECK(!pump(), "descanso de 5 min entre riegos automaticos");
  step(5UL * 60000); 
  CHECK(pump(), "pasado el descanso vuelve a regar");

  // tiempo maximo: la tierra no llega al objetivo -> para a los 60 s
  for (int i = 0; i < 29; ++i) step();
  CHECK(pump(), "a los 58 s sigue regando");
  step();
  CHECK(!pump() && has("\"bomba_paro\":\"TIEMPO\""), "a los 60 s para (TIEMPO)");

  // MANUAL: no riega solo
  send("SG,17,MODO,MANUAL"); step(5UL * 60000);
  CHECK(has("\"modo\":\"MANUAL\"") && has("\"cmd_id\":17") && has("\"cmd_err\":null"), "orden 17 MODO,MANUAL confirmada");
  for (int i = 0; i < 5; ++i) step();
  CHECK(!pump(), "MANUAL + tierra seca: no riega solo");

  // orden manual ON / OFF
  send("SG,18,BOMBA,ON"); step();
  CHECK(pump() && has("\"cmd_id\":18"), "BOMBA,ON en manual: enciende");
  send("SG,19,BOMBA,OFF"); step();
  CHECK(!pump() && has("\"cmd_id\":19") && has("\"bomba_paro\":\"ORDEN\""), "BOMBA,OFF: apaga (ORDEN)");

  // manual tambien tiene tiempo maximo
  send("SG,20,BOMBA,ON"); step();
  for (int i = 0; i < 30; ++i) step();
  CHECK(!pump() && has("\"bomba_paro\":\"TIEMPO\""), "manual: tambien para a los 60 s");

  // tanque critico: no enciende y avisa TANQUE
  fake_analog[A1] = 20; step();
  send("SG,21,BOMBA,ON"); step();
  CHECK(!pump() && has("\"cmd_id\":21") && has("\"cmd_err\":\"TANQUE\""), "sin agua: rechaza ON (TANQUE)");
  fake_analog[A1] = 120; step();

  // tanque se vacia mientras riega: apaga
  send("SG,22,BOMBA,ON"); step();
  CHECK(pump(), "con agua enciende");
  fake_analog[A1] = 20; step();
  CHECK(!pump() && has("\"bomba_paro\":\"TANQUE\""), "se vacia el tanque: apaga (TANQUE)");
  fake_analog[A1] = 120; step();

  // pasar a AUTO con la bomba manual encendida: apaga
  send("SG,23,BOMBA,ON"); step();
  send("SG,24,MODO,AUTO"); step();
  CHECK(has("\"modo\":\"AUTO\"") && has("\"bomba_paro\":\"MODO\""), "MANUAL -> AUTO apaga el riego manual (MODO)");

  // en AUTO no acepta ordenes de bomba
  send("SG,25,BOMBA,ON"); step();
  CHECK(has("\"cmd_id\":25") && has("\"cmd_err\":\"MODO\""), "en AUTO rechaza BOMBA (MODO)");

  // AUTO regando y pasa a MANUAL: apaga
  step(5UL * 60000);
  CHECK(pump(), "AUTO regando otra vez tras el descanso");
  send("SG,26,MODO,MANUAL"); step();
  CHECK(!pump() && has("\"bomba_paro\":\"MODO\""), "AUTO -> MANUAL apaga el riego automatico");

  // ruido, eco y formatos raros
  send("=hola"); send("SG,abc,BOMBA,ON"); send("SG,-1,BOMBA,ON"); send("SG,7x,BOMBA,ON");
  send("SG,1234567890,BOMBA,ON"); send(" SG,40,BOMBA,ON"); step();
  CHECK(has("\"cmd_id\":26") && !pump(), "ruido o id invalido (-1, 7x, 10 digitos): se ignora");
  send("SG,27,BOMBA,PRENDE"); step();
  CHECK(has("\"cmd_id\":27") && has("\"cmd_err\":\"FORMATO\""), "accion desconocida: FORMATO");
  send("SG,28,MODO,MANUAL,EXTRA"); step();
  CHECK(has("\"cmd_err\":\"FORMATO\""), "campos de mas: FORMATO");
  send("SG,29,BOMBA,ON,AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"); step();
  CHECK(has("\"cmd_id\":28") && !pump(), "linea demasiado larga: se descarta completa");
  send("SG,30,BOMBA,ON\r"); step();
  CHECK(has("\"cmd_id\":30") && pump(), "acepta fin de linea \\r\\n");

  // sensor de suelo desconectado en AUTO: apaga (SENSOR)
  send("SG,31,MODO,AUTO"); step(); step(5UL * 60000);
  CHECK(pump(), "AUTO regando");
  fake_analog[A0] = 20; step();
  CHECK(!pump() && has("\"bomba_paro\":\"SENSOR\""), "falla el sensor de suelo: apaga (SENSOR)");

  printf("\nlinea de ejemplo: %s", lastLine.c_str());
  printf("%s\n", fails ? "HAY FALLAS" : "TODO OK");
  return fails ? 1 : 0;
}
