/*
  SmartGreenAI: Radish Crop
  Sprint 1 - Lectura de sensores del mini vivero

  Que hace:
    Lee 4 sensores cada 2 segundos e imprime una linea JSON por Serial.
    La Raspberry Pi lee esas lineas con pyserial (siguiente paso).

  Cubre: MON-01 (temp/humedad) y MON-02 (humedad de suelo)

  Cableado (todo al Arduino Uno, nada toca los GPIO de la Pi):
    DHT11 ............ VCC=5V  GND=GND  OUT=D2
    Humedad tierra ... VCC=5V  GND=GND  AO =A0
    Nivel de agua .... VCC=5V  GND=GND  AO =A1
    Sensor de luz .... VCC=5V  GND=GND  AO =A2

  Libreria necesaria: "DHT sensor library" de Adafruit
  (+ su dependencia "Adafruit Unified Sensor")
*/

#include <DHT.h>

// ---------------- PINES ----------------
#define PIN_DHT    13
#define PIN_SUELO  A0
#define PIN_NIVEL  A1
#define PIN_LUZ    A2

DHT dht(PIN_DHT, DHT11);

// ---------------- CALIBRACION ----------------
// OJO: estos valores son un punto de partida, HAY QUE MEDIRLOS.
// Instrucciones de como sacarlos al final de este archivo.

//Capacitive Soil Moisture Sensor
const int SUELO_EN_AIRE  = 555;  // valor crudo con el sensor seco, al aire
const int SUELO_EN_AGUA  = 267;  // valor crudo con el sensor en un vaso de agua

// Sensor rojo nivel del tanque
const int NIVEL_VACIO    = 0;    // valor crudo con el tanque vacio 
const int NIVEL_LLENO    = 200;  // valor crudo con el tanque lleno

// ---------------- CONFIG ----------------
const unsigned long INTERVALO_MS = 2000;  // el DHT11 no admite menos de 2s
unsigned long ultimaLectura = 0;

// Un pin analogico desconectado flota y da basura. Si la lectura queda
// pegada a los extremos, asumimos sensor desconectado o en corto y
// reportamos error explicito (null) en vez de un 0 que parezca valido.
const int RAW_MIN_VALIDO = 10;
const int RAW_MAX_VALIDO = 1013;

void setup() {
  Serial.begin(9600);
  dht.begin();
  delay(2000);  // el DHT11 tarda en estabilizarse al arrancar
}

// Convierte un valor crudo (0-1023) a porcentaje 0-100.
// enCero y enCien son los valores crudos que corresponden a 0% y 100%.
int aPorcentaje(int crudo, int enCero, int enCien) {
  long p = map(crudo, enCero, enCien, 0, 100);
  return constrain(p, 0, 100);
}

bool lecturaValida(int crudo) {
  return crudo >= RAW_MIN_VALIDO && crudo <= RAW_MAX_VALIDO;
}

// Imprime un campo float, o null si la lectura fallo
void campoFloat(const char* nombre, float valor, int decimales) {
  Serial.print(",\"");
  Serial.print(nombre);
  Serial.print("\":");
  if (isnan(valor)) {
    Serial.print("null");
  } else {
    Serial.print(valor, decimales);
  }
}

// Imprime el par crudo + porcentaje de un sensor analogico
void campoAnalogico(const char* nombre, int crudo, int enCero, int enCien) {
  Serial.print(",\"");
  Serial.print(nombre);
  Serial.print("_raw\":");
  Serial.print(crudo);

  Serial.print(",\"");
  Serial.print(nombre);
  Serial.print("_pct\":");
  if (lecturaValida(crudo)) {
    Serial.print(aPorcentaje(crudo, enCero, enCien));
  } else {
    Serial.print("null");
  }
}

void loop() {
  if (millis() - ultimaLectura < INTERVALO_MS) return;
  ultimaLectura = millis();

  float tempC   = dht.readTemperature();
  float humAire = dht.readHumidity();
  int   suelo   = analogRead(PIN_SUELO);
  int   nivel   = analogRead(PIN_NIVEL);
  int   luz     = analogRead(PIN_LUZ);

  Serial.print("{\"ms\":");
  Serial.print(millis());

  campoFloat("temp_c",       tempC,   1);
  campoFloat("hum_aire_pct", humAire, 1);

  campoAnalogico("suelo", suelo, SUELO_EN_AIRE, SUELO_EN_AGUA);
  campoAnalogico("nivel", nivel, NIVEL_VACIO,   NIVEL_LLENO);

  // La luz se manda cruda: sin un sensor calibrado en lux, un porcentaje
  // seria un numero inventado. Que la nube decida como interpretarlo.
  Serial.print(",\"luz_raw\":");
  Serial.print(luz);

  Serial.println("}");
}

/*
  ------------------------------------------------------------------
  COMO CALIBRAR (hacer una sola vez, antes de sembrar)
  ------------------------------------------------------------------

  1. Sube el sketch y abre el Monitor Serial (lupa arriba a la derecha),
     con la velocidad puesta en 9600 baud.

  2. HUMEDAD DE TIERRA
     - Con el sensor seco al aire, anota "suelo_raw"  -> ese es SUELO_EN_AIRE
     - Metelo en un vaso con agua HASTA LA LINEA marcada en la placa,
       sin mojar la parte electronica. Anota "suelo_raw" -> SUELO_EN_AGUA
     - En el capacitivo, MAS SECO = numero MAS ALTO. Es normal que
       SUELO_EN_AIRE sea mayor que SUELO_EN_AGUA.

  3. NIVEL DE AGUA
     - Sensor fuera del agua, anota "nivel_raw"     -> NIVEL_VACIO
     - Sumergido hasta donde estara el tanque lleno -> NIVEL_LLENO

  4. Pon esos numeros arriba en la seccion CALIBRACION y vuelve a subir.

  Tip: el sensor de nivel se corroe si se deja energizado 24/7 dentro del
  agua. Para el prototipo esta bien, pero vale la pena mencionarlo en el
  reporte como limitacion conocida.
*/
