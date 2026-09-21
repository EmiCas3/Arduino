/*
  SmartGreenAI: Radish Crop
  Sprint 1 - Lectura de sensores + evaluacion agronomica del rabano

  Que hace:
    Lee 4 sensores cada 2 segundos, los compara contra los rangos optimos
    del rabano e imprime UNA linea JSON por Serial (9600 baud).
    La Raspberry Pi lee esa linea con pyserial, la guarda en SQLite y la
    manda al endpoint de la nube. El Arduino NO habla con internet: la Pi
    es el gateway. Este archivo define el contrato con la Pi.

  Cubre: MON-01, MON-02 y la parte de firmware de MON-03.

  Cableado (todo al Arduino Uno, nada toca los GPIO de la Pi):
    DHT11 ............ VCC=5V  GND=GND  OUT=D2
    Humedad tierra ... VCC=5V  GND=GND  AO =A0
    Nivel de agua .... VCC=5V  GND=GND  AO =A1
    Sensor de luz .... VCC=5V  GND=GND  AO =A2

  Actuadores (via puente H L298N):
    Ventilador ....... D3 -> IN1  (IN2 a GND del Arduino)  OUT1/OUT2 al motor
    Bomba de riego ... D5 -> IN3  (IN4 a GND del Arduino)  OUT3/OUT4 a la bomba
    L298N alimentacion: +12V=Fuente externa, GND=comun con Arduino
    Jumpers ENA y ENB: puestos (habilitan canales A y B)

  Libreria necesaria: "DHT sensor library" de Adafruit
  (+ su dependencia "Adafruit Unified Sensor")

  ------------------------------------------------------------------
  CODIGOS DE ALERTA (se mandan en el arreglo "alertas")
  ------------------------------------------------------------------
    TEMP_ALTA / TEMP_BAJA .............. fuera de limite (>30C / <6C)
    TEMP_ALTA_LEVE / TEMP_BAJA_LEVE .... fuera del ideal (>25C / <20C)
    HUM_AIRE_ALTA / HUM_AIRE_BAJA ...... fuera de limite (>85% / <50%)
    HUM_AIRE_ALTA_LEVE / _BAJA_LEVE .... fuera del ideal (>80% / <60%)
    SUELO_ENCHARCADO / SUELO_SECO ...... fuera de limite (>80% / <50%)
    SUELO_HUMEDO_LEVE / SUELO_SECO_LEVE  fuera del ideal (>65% / <60%)
    TANQUE_MITAD ....................... <= 50% de agua
    TANQUE_UN_CUARTO ................... <= 25%, bomba bloqueada
    SOMBRA_PROLONGADA .................. es de dia y lleva rato sin sol pleno
    SOL_INSUFICIENTE ................... el dia cerro con menos de 6 h de sol
    FALLA_DHT / FALLA_SUELO / FALLA_NIVEL  sensor desconectado o en corto

  "estado" resume la linea: OK | AVISO | ALERTA
*/

#include <DHT.h>

// ---------------- PINES ----------------
#define PIN_DHT    2
#define PIN_SUELO  A0
#define PIN_NIVEL  A1
#define PIN_LUZ    A2

// Actuadores (via puente H L298N, logica Active HIGH)
#define PIN_VENT   3    // Ventilador: IN1 del L298N (IN2 a GND)
#define PIN_BOMBA  5    // Bomba de riego: IN3 del L298N (IN4 a GND)

DHT dht(PIN_DHT, DHT11);

// ---------------- CALIBRACION ----------------
const int SUELO_EN_AIRE = 555;   // medido
const int SUELO_EN_AGUA = 267;   // medido
const int NIVEL_VACIO   = 0;     // medido (oscila 0-6)
const int NIVEL_LLENO   = 200;   // medido

// Rango valido POR SENSOR, no global. El nivel puede leer 0 de forma
// legitima (tanque vacio); el suelo no: si lee casi 0 esta desconectado.
const int SUELO_MIN_VALIDO = 100;
const int SUELO_MAX_VALIDO = 1013;
const int NIVEL_MIN_VALIDO = 0;
const int NIVEL_MAX_VALIDO = 1013;

// ---------------- UMBRALES DEL RABANO ----------------
// Temperatura del aire (C)
const float TEMP_IDEAL_MIN  = 20.0;
const float TEMP_IDEAL_MAX  = 25.0;
const float TEMP_LIMITE_MIN = 6.0;
const float TEMP_LIMITE_MAX = 30.0;

// Humedad relativa del aire (%)
const float HUMA_IDEAL_MIN  = 60.0;
const float HUMA_IDEAL_MAX  = 80.0;
const float HUMA_LIMITE_MIN = 50.0;
const float HUMA_LIMITE_MAX = 85.0;

// Humedad de la tierra (% en la escala calibrada aire=0 / agua=100)
const int SUELO_IDEAL_MIN  = 60;
const int SUELO_IDEAL_MAX  = 65;
const int SUELO_LIMITE_MIN = 50;
const int SUELO_LIMITE_MAX = 80;

// Nivel del tanque (%)
const int NIVEL_PCT_AVISO   = 50;   // mitad
const int NIVEL_PCT_CRITICO = 25;   // un cuarto -> ademas bloquea la bomba

// Luz. El sensor no esta calibrado en lux, asi que se trabaja sobre una
// escala normalizada "mas alto = mas luz" (ver LUZ_INVERTIDA).
const bool LUZ_INVERTIDA    = true;  // true si al tapar el sensor el raw SUBE
const int  LUZ_UMBRAL_DIA   = 250;   // arriba de esto se considera de dia
const int  LUZ_HISTERESIS   = 40;    // margen para que no parpadee al atardecer
const int  LUZ_UMBRAL_SOL   = 600;   // arriba de esto cuenta como sol pleno
const unsigned int SOL_MIN_REQUERIDO = 360;          // 6 h en minutos
const unsigned long SOMBRA_MAX_MS    = 30UL * 60000; // 30 min sin sol pleno

// ---------------- CONFIG ----------------
const unsigned long INTERVALO_MS = 2000;  // el DHT11 no admite menos de 2s
unsigned long ultimaLectura = 0;

// Severidad
const byte OK = 0, AVISO = 1, ALERTA = 2;

// ---------------- ESTADO DEL CICLO DE LUZ ----------------
bool          esDia           = false;
bool          huboDiaCompleto = false;  // ya se cerro al menos un dia
unsigned long solHoyMs        = 0;      // sol pleno acumulado del dia actual
unsigned long sombraMs        = 0;      // rato continuo sin sol pleno, de dia
int           solPrevMin      = -1;     // minutos de sol del dia anterior

// ---------------- ESTADO DE LA LINEA ACTUAL ----------------
byte severidad = OK;
bool primeraAlerta = true;

void setup() {
  // Iniciar actuadores apagados ANTES de declararlos como salida
  // para evitar arranques involuntarios al encender el Arduino.
  digitalWrite(PIN_VENT, LOW);
  digitalWrite(PIN_BOMBA, LOW);
  pinMode(PIN_VENT, OUTPUT);
  pinMode(PIN_BOMBA, OUTPUT);

  Serial.begin(9600);
  dht.begin();
  delay(2000);  // el DHT11 tarda en estabilizarse al arrancar
}

// Convierte un valor crudo (0-1023) a porcentaje 0-100.
int aPorcentaje(int crudo, int enCero, int enCien) {
  long p = map(crudo, enCero, enCien, 0, 100);
  return constrain(p, 0, 100);
}

bool lecturaValida(int crudo, int minValido, int maxValido) {
  return crudo >= minValido && crudo <= maxValido;
}

// ---------------- IMPRESION DE CAMPOS ----------------
void campoFloat(const __FlashStringHelper* nombre, float valor, int decimales) {
  Serial.print(F(",\""));
  Serial.print(nombre);
  Serial.print(F("\":"));
  if (isnan(valor)) Serial.print(F("null"));
  else              Serial.print(valor, decimales);
}

// pct < 0 significa lectura invalida -> null
void campoAnalogico(const __FlashStringHelper* nombre, int crudo, int pct) {
  Serial.print(F(",\""));
  Serial.print(nombre);
  Serial.print(F("_raw\":"));
  Serial.print(crudo);

  Serial.print(F(",\""));
  Serial.print(nombre);
  Serial.print(F("_pct\":"));
  if (pct < 0) Serial.print(F("null"));
  else         Serial.print(pct);
}

void campoBool(const __FlashStringHelper* nombre, bool valor) {
  Serial.print(F(",\""));
  Serial.print(nombre);
  Serial.print(F("\":"));
  Serial.print(valor ? F("true") : F("false"));
}

void alerta(const __FlashStringHelper* codigo, byte nivel) {
  if (!primeraAlerta) Serial.print(',');
  Serial.print('"');
  Serial.print(codigo);
  Serial.print('"');
  primeraAlerta = false;
  if (nivel > severidad) severidad = nivel;
}

// ---------------- CICLO DIA / NOCHE ----------------
// El Arduino no tiene reloj, asi que el dia y la noche se deducen del propio
// sensor de luz. Asi no se mandan alertas de falta de sol a media noche.
void actualizarCicloDeLuz(int luzNivel) {
  if (esDia) {
    if (luzNivel < LUZ_UMBRAL_DIA - LUZ_HISTERESIS) {
      // anochecio: se cierra el dia y se guarda cuanto sol junto
      esDia = false;
      huboDiaCompleto = true;
      solPrevMin = solHoyMs / 60000UL;
    } else if (luzNivel >= LUZ_UMBRAL_SOL) {
      solHoyMs += INTERVALO_MS;
      sombraMs = 0;
    } else {
      sombraMs += INTERVALO_MS;
    }
  } else {
    if (luzNivel > LUZ_UMBRAL_DIA + LUZ_HISTERESIS) {
      // amanecio: arranca el conteo del dia nuevo
      esDia = true;
      solHoyMs = 0;
      sombraMs = 0;
    }
  }
}

// ---------------- EVALUACION AGRONOMICA ----------------
void evaluarTemperatura(float t) {
  if (isnan(t))                   alerta(F("FALLA_DHT"), ALERTA);
  else if (t > TEMP_LIMITE_MAX)   alerta(F("TEMP_ALTA"), ALERTA);
  else if (t < TEMP_LIMITE_MIN)   alerta(F("TEMP_BAJA"), ALERTA);
  else if (t > TEMP_IDEAL_MAX)    alerta(F("TEMP_ALTA_LEVE"), AVISO);
  else if (t < TEMP_IDEAL_MIN)    alerta(F("TEMP_BAJA_LEVE"), AVISO);
}

void evaluarHumedadAire(float h) {
  if (isnan(h)) return;  // ya lo reporto FALLA_DHT
  if (h > HUMA_LIMITE_MAX)        alerta(F("HUM_AIRE_ALTA"), ALERTA);
  else if (h < HUMA_LIMITE_MIN)   alerta(F("HUM_AIRE_BAJA"), ALERTA);
  else if (h > HUMA_IDEAL_MAX)    alerta(F("HUM_AIRE_ALTA_LEVE"), AVISO);
  else if (h < HUMA_IDEAL_MIN)    alerta(F("HUM_AIRE_BAJA_LEVE"), AVISO);
}

void evaluarSuelo(int pct) {
  if (pct < 0)                    alerta(F("FALLA_SUELO"), ALERTA);
  else if (pct > SUELO_LIMITE_MAX) alerta(F("SUELO_ENCHARCADO"), ALERTA);
  else if (pct < SUELO_LIMITE_MIN) alerta(F("SUELO_SECO"), ALERTA);
  else if (pct > SUELO_IDEAL_MAX)  alerta(F("SUELO_HUMEDO_LEVE"), AVISO);
  else if (pct < SUELO_IDEAL_MIN)  alerta(F("SUELO_SECO_LEVE"), AVISO);
}

void evaluarNivel(int pct) {
  if (pct < 0)                       alerta(F("FALLA_NIVEL"), ALERTA);
  else if (pct <= NIVEL_PCT_CRITICO) alerta(F("TANQUE_UN_CUARTO"), ALERTA);
  else if (pct <= NIVEL_PCT_AVISO)   alerta(F("TANQUE_MITAD"), AVISO);
}

void evaluarLuz() {
  if (esDia) {
    if (sombraMs >= SOMBRA_MAX_MS) alerta(F("SOMBRA_PROLONGADA"), AVISO);
  } else if (huboDiaCompleto && solPrevMin < (int)SOL_MIN_REQUERIDO) {
    // Solo de noche y solo si ya se cerro un dia completo: de otro modo
    // se alertaria de falta de sol a las 7 de la mañana, cuando todavia
    // le quedan horas de luz al dia.
    alerta(F("SOL_INSUFICIENTE"), AVISO);
  }
}

void loop() {
  if (millis() - ultimaLectura < INTERVALO_MS) return;
  ultimaLectura = millis();

  // ---- lectura ----
  float tempC    = dht.readTemperature();
  float humAire  = dht.readHumidity();
  int   sueloRaw = analogRead(PIN_SUELO);
  int   nivelRaw = analogRead(PIN_NIVEL);
  int   luzRaw   = analogRead(PIN_LUZ);

  int sueloPct = lecturaValida(sueloRaw, SUELO_MIN_VALIDO, SUELO_MAX_VALIDO)
                 ? aPorcentaje(sueloRaw, SUELO_EN_AIRE, SUELO_EN_AGUA) : -1;
  int nivelPct = lecturaValida(nivelRaw, NIVEL_MIN_VALIDO, NIVEL_MAX_VALIDO)
                 ? aPorcentaje(nivelRaw, NIVEL_VACIO, NIVEL_LLENO) : -1;

  int luzNivel = LUZ_INVERTIDA ? (1023 - luzRaw) : luzRaw;
  actualizarCicloDeLuz(luzNivel);

  // ---- decisiones (se recomiendan, la nube y el productor deciden) ----
  // Fail-safe: si el nivel esta bajo O el sensor fallo, la bomba no arranca.
  bool bombaHabilitada = (nivelPct > NIVEL_PCT_CRITICO);
  bool riegoSugerido   = (sueloPct >= 0 && sueloPct < SUELO_LIMITE_MIN);
  // La ventilacion es constante: BASE siempre, ALTA si hay calor o bochorno.
  bool ventAlta = (!isnan(tempC)   && tempC   > TEMP_IDEAL_MAX) ||
                  (!isnan(humAire) && humAire > HUMA_IDEAL_MAX);

  // ---- control fisico de actuadores (L298N, HIGH = encendido) ----
  // Ventilador: se enciende cuando la temperatura o humedad superan el ideal.
  digitalWrite(PIN_VENT, ventAlta ? HIGH : LOW);

  // Bomba: solo riega si el suelo esta seco Y el tanque tiene agua suficiente.
  bool bombaActiva = riegoSugerido && bombaHabilitada;
  digitalWrite(PIN_BOMBA, bombaActiva ? HIGH : LOW);

  // ---- salida JSON ----
  Serial.print(F("{\"ms\":"));
  Serial.print(millis());

  campoFloat(F("temp_c"),       tempC,   1);
  campoFloat(F("hum_aire_pct"), humAire, 1);
  campoAnalogico(F("suelo"), sueloRaw, sueloPct);
  campoAnalogico(F("nivel"), nivelRaw, nivelPct);

  // La luz se manda cruda; luz_nivel es la misma lectura normalizada a
  // "mas alto = mas luz" para que la nube no tenga que saber la polaridad.
  Serial.print(F(",\"luz_raw\":"));
  Serial.print(luzRaw);
  Serial.print(F(",\"luz_nivel\":"));
  Serial.print(luzNivel);

  campoBool(F("es_dia"), esDia);
  Serial.print(F(",\"sol_min_hoy\":"));
  Serial.print(solHoyMs / 60000UL);
  Serial.print(F(",\"sol_min_prev\":"));
  if (solPrevMin < 0) Serial.print(F("null"));
  else                Serial.print(solPrevMin);

  campoBool(F("riego_sugerido"),   riegoSugerido);
  campoBool(F("bomba_habilitada"), bombaHabilitada);
  campoBool(F("bomba_activa"),     bombaActiva);
  Serial.print(F(",\"vent\":\""));
  Serial.print(ventAlta ? F("ALTA") : F("BASE"));
  Serial.print('"');
  campoBool(F("vent_activo"), ventAlta);

  Serial.print(F(",\"alertas\":["));
  severidad = OK;
  primeraAlerta = true;
  evaluarTemperatura(tempC);
  evaluarHumedadAire(humAire);
  evaluarSuelo(sueloPct);
  evaluarNivel(nivelPct);
  evaluarLuz();
  Serial.print(F("],\"estado\":\""));
  Serial.print(severidad == OK ? F("OK") : (severidad == AVISO ? F("AVISO") : F("ALERTA")));
  Serial.println(F("\"}"));
}

/*
  ------------------------------------------------------------------
  EJEMPLO DE LINEA
  ------------------------------------------------------------------
  {"ms":148022,"temp_c":24.2,"hum_aire_pct":38.0,"suelo_raw":565,
   "suelo_pct":0,"nivel_raw":5,"nivel_pct":2,"luz_raw":306,"luz_nivel":717,
   "es_dia":true,"sol_min_hoy":42,"sol_min_prev":null,"riego_sugerido":true,
   "bomba_habilitada":false,"vent":"BASE",
   "alertas":["HUM_AIRE_BAJA","SUELO_SECO","TANQUE_UN_CUARTO"],"estado":"ALERTA"}

  (va todo en una sola linea, sin saltos)

  ------------------------------------------------------------------
  LO QUE FALTA CALIBRAR
  ------------------------------------------------------------------

  1. NIVEL_LLENO
     Llena el tanque hasta donde va a estar lleno de verdad, con el sensor
     ya montado vertical y fijo, y anota nivel_raw. Ese numero va arriba.
     Mientras siga inventado, las alertas TANQUE_MITAD y TANQUE_UN_CUARTO
     no significan nada.

  2. LUZ_INVERTIDA
     Abre el Monitor Serial y tapa el sensor con la mano.
       - Si luz_raw SUBE al taparlo  -> LUZ_INVERTIDA = true
       - Si luz_raw BAJA al taparlo  -> LUZ_INVERTIDA = false
     Verifica que luz_nivel se comporte como "mas alto = mas luz".

  3. LUZ_UMBRAL_DIA y LUZ_UMBRAL_SOL
     Con LUZ_INVERTIDA ya correcto, anota luz_nivel en tres momentos:
       - cuarto a oscuras (noche)      -> el umbral de dia va bien arriba de esto
       - sombra o interior con luz     -> entre los dos umbrales
       - vivero a pleno sol            -> LUZ_UMBRAL_SOL va un poco abajo
     Sin esto, sol_min_hoy cuenta mal y SOL_INSUFICIENTE es ruido.

  4. Recalibrar el suelo con TIERRA REAL antes de la demo
     SUELO_EN_AGUA = 267 se saco con el sensor en un vaso de agua, no en
     tierra. Un 60% en esa escala no es un 60% agronomico. Lo correcto:
     regar la maceta hasta capacidad de campo, esperar a que drene y tomar
     ESE raw como el 100%. Si no, los umbrales de 50/60/65/80 estan
     comparando contra una escala que no es la del rabano.

  Nota: el porcentaje del tanque es lineal contra el raw, pero el sensor
  solo mide el tramo que cubren las rayitas doradas. "Mitad" es mitad del
  tramo medido, no necesariamente mitad del volumen del tanque. Vale la
  pena anotarlo en el reporte como limitacion.
*/
