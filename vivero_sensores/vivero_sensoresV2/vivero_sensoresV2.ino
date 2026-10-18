/*
  SmartGreenAI: Radish Crop
  Sprint 1 - Lectura de sensores + evaluacion agronomica del rabano

  Que hace:
    Lee 4 sensores cada 2 segundos, los compara contra los rangos optimos
    del rabano e imprime UNA linea JSON por Serial (9600 baud).
    La Raspberry Pi lee esa linea con pyserial, la guarda en SQLite y la
    manda al endpoint de la nube. El Arduino NO habla con internet: la Pi
    es el gateway. Este archivo define el contrato con la Pi.

  Cubre: MON-01, MON-02, la parte de firmware de MON-03 y, desde el
  Sprint 2, la de ACT-02 (modo automatico/manual y ordenes de la Pi).

  Cableado (todo al Arduino Uno, nada toca los GPIO de la Pi):
    DHT11 ............ VCC=5V  GND=GND  OUT=D2
    Humedad tierra ... VCC=5V  GND=GND  AO =A0
    Nivel de agua .... VCC=5V  GND=GND  AO =A1
    Sensor de luz .... VCC=5V  GND=GND  AO =A2
    Sensor CO2 SCD4x . VDD=5V  GND=GND  SDA=A4  SCL=A5  (I2C 0x62)

  Actuadores (via puente H L298N):
    Ventilador ....... D3 -> IN1  (IN2 a GND del Arduino)  OUT1/OUT2 al motor
    Bomba de riego ... D5 -> IN3  (IN4 a GND del Arduino)  OUT3/OUT4 a la bomba
    L298N alimentacion: +12V=Fuente externa, GND=comun con Arduino
    Jumpers ENA y ENB: puestos (habilitan canales A y B)

  Libreria necesaria: "DHT sensor library" de Adafruit
  (+ su dependencia "Adafruit Unified Sensor"). El sensor de CO2 SCD40/SCD41
  se lee directo con <Wire.h> (incluida en el IDE), sin librerias externas.

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
    CO2_ALTO / CO2_BAJO ................ fuera de limite (>1500 / <300 ppm)
    CO2_ALTO_LEVE / CO2_BAJO_LEVE ...... fuera del ideal (>1000 / <400 ppm)
    FALLA_DHT / FALLA_SUELO / FALLA_NIVEL / FALLA_CO2  sensor desconectado
      (FALLA_CO2 no sale en los primeros 12 s tras arrancar: el SCD4x aun
      no tiene su primera muestra; co2_ppm va null sin codigo)

  "estado" resume la linea: OK | AVISO | ALERTA

  ------------------------------------------------------------------
  ORDENES DESDE LA PI (ACT-02) - una linea por orden, misma velocidad
  ------------------------------------------------------------------
    SG,<id>,MODO,AUTO ...... riega solo: arranca con la tierra abajo de
                             SUELO_LIMITE_MIN y para al llegar a ese valor
    SG,<id>,MODO,MANUAL .... NO riega solo: solo obedece BOMBA,ON / BOMBA,OFF
    SG,<id>,BOMBA,ON ....... enciende la bomba (solo en MANUAL y con agua)
    SG,<id>,BOMBA,OFF ...... apaga la bomba (solo en MANUAL)

  <id> es un numero mayor que 0 que pone el backend. El Arduino contesta en
  su siguiente linea JSON con:
    "modo" ........ AUTO | MANUAL (al arrancar: AUTO, como siempre)
    "cmd_id" ...... la ultima orden que aplico (0 al arrancar)
    "cmd_err" ..... null, o por que no la aplico: MODO | TANQUE | FORMATO
    "bomba_paro" .. por que se apago la bomba la ultima vez:
                    OBJETIVO | TIEMPO | TANQUE | SENSOR | MODO | ORDEN
  En cualquier modo la bomba se apaga sola al cumplir BOMBA_MAX_MS o si el
  tanque llega a critico. Un riego automatico dura al menos BOMBA_MIN_MS y
  entre dos espera BOMBA_DESCANSO_MS. Las lineas que no empiezan con "SG,"
  o cuyo id no es un numero de 1 a 9 digitos se ignoran.
  Detalle completo: gateway/PROTOCOLO_ARDUINO.md
*/

#include <DHT.h>
#include <Wire.h>

// ---------------- PINES ----------------
#define PIN_DHT 2
#define PIN_SUELO A0
#define PIN_NIVEL A1
#define PIN_LUZ A2
// I2C del Arduino Uno para el sensor de CO2 (SCD40/SCD41): SDA=A4, SCL=A5
const byte SCD4X_ADDR = 0x62;

// Actuadores (via puente H L298N, logica Active HIGH)
#define PIN_VENT 3  // Ventilador: IN1 del L298N (IN2 a GND)
#define PIN_BOMBA 5 // Bomba de riego: IN3 del L298N (IN4 a GND)

DHT dht(PIN_DHT, DHT11);

// ---------------- CALIBRACION ----------------
const int SUELO_EN_AIRE = 555; // medido
const int SUELO_EN_AGUA = 267; // medido
const int NIVEL_VACIO = 0;     // medido (oscila 0-6)
const int NIVEL_LLENO = 150;   // medido

// Rango valido POR SENSOR, no global. El nivel puede leer 0 de forma
// legitima (tanque vacio); el suelo no: si lee casi 0 esta desconectado.
const int SUELO_MIN_VALIDO = 100;
const int SUELO_MAX_VALIDO = 1013;
const int NIVEL_MIN_VALIDO = 0;
const int NIVEL_MAX_VALIDO = 1013;
const int CO2_MIN_VALIDO = 100;
const int CO2_MAX_VALIDO = 40000;

// ---------------- UMBRALES DEL RABANO ----------------
// Temperatura del aire (C)
const float TEMP_IDEAL_MIN = 20.0;
const float TEMP_IDEAL_MAX = 25.0;
const float TEMP_LIMITE_MIN = 6.0;
const float TEMP_LIMITE_MAX = 30.0;

// Humedad relativa del aire (%)
const float HUMA_IDEAL_MIN = 60.0;
const float HUMA_IDEAL_MAX = 80.0;
const float HUMA_LIMITE_MIN = 50.0;
const float HUMA_LIMITE_MAX = 85.0;

// Humedad de la tierra (% en la escala calibrada aire=0 / agua=100)
const int SUELO_IDEAL_MIN = 60;
const int SUELO_IDEAL_MAX = 65;
const int SUELO_LIMITE_MIN = 50;
const int SUELO_LIMITE_MAX = 80;

// Nivel del tanque (%)
const int NIVEL_PCT_AVISO = 50;   // mitad
const int NIVEL_PCT_CRITICO = 25; // un cuarto -> ademas bloquea la bomba

// CO2 en el aire (ppm, sensor SCD40/SCD41 por I2C)
const int CO2_IDEAL_MIN = 400;
const int CO2_IDEAL_MAX = 1000;
const int CO2_LIMITE_MIN = 300;
const int CO2_LIMITE_MAX = 1500;
// El SCD4x entrega una muestra nueva cada 5 s. Si pasan mas de 15 s sin
// muestra nueva (o si no responde en el bus I2C), se reporta FALLA_CO2.
const unsigned long CO2_MAX_EDAD_MS = 15000UL;
// Al arrancar, la primera muestra tarda ~5 s despues de start (mas los 2.5 s
// de setup). Mientras no haya llegado ninguna, co2_ppm va null SIN FALLA_CO2:
// todavia no hay dato, pero no es una falla. Pasado este tiempo, si.
const unsigned long CO2_ARRANQUE_MS = 12000UL;

// Luz. El sensor no esta calibrado en lux, asi que se trabaja sobre una
// escala normalizada "mas alto = mas luz" (ver LUZ_INVERTIDA).
const bool LUZ_INVERTIDA = true; // true si al tapar el sensor el raw SUBE
const int LUZ_UMBRAL_DIA = 250;  // arriba de esto se considera de dia
const int LUZ_HISTERESIS = 40;   // margen para que no parpadee al atardecer
const int LUZ_UMBRAL_SOL = 600;  // arriba de esto cuenta como sol pleno
const unsigned int SOL_MIN_REQUERIDO = 360;       // 6 h en minutos
const unsigned long SOMBRA_MAX_MS = 30UL * 60000; // 30 min sin sol pleno

// ---------------- RIEGO (ACT-02) ----------------
// El objetivo del riego automatico es SUELO_LIMITE_MIN (arriba).
const unsigned long BOMBA_MAX_MS = 60UL * 1000;      // ningun riego dura mas de 1 min
const unsigned long BOMBA_MIN_MS = 10UL * 1000;      // un riego automatico dura al menos 10 s
const unsigned long BOMBA_DESCANSO_MS = 5UL * 60000; // espera entre riegos automaticos

// ---------------- CONFIG ----------------
const unsigned long INTERVALO_MS = 2000; // el DHT11 no admite menos de 2s
unsigned long ultimaLectura = 0;

// Severidad
const byte OK = 0, AVISO = 1, ALERTA = 2;

// ---------------- ESTADO DEL CICLO DE LUZ ----------------
bool esDia = false;
bool huboDiaCompleto = false; // ya se cerro al menos un dia
unsigned long solHoyMs = 0;   // sol pleno acumulado del dia actual
unsigned long sombraMs = 0;   // rato continuo sin sol pleno, de dia
int solPrevMin = -1;          // minutos de sol del dia anterior

// ---------------- ESTADO DEL SENSOR DE CO2 (SCD4x) ----------------
int co2Ppm = -1;                  // ultimo valor valido en ppm (-1 = sin lectura)
unsigned long co2UltimoMs = 0;    // millis() de la ultima muestra recibida
bool co2TieneLectura = false;     // ya entrego al menos una muestra valida
bool co2Iniciado = false;         // ya se envio start_periodic_measurement

// ---------------- ESTADO DE LA LINEA ACTUAL ----------------
byte severidad = OK;
bool primeraAlerta = true;

// ---------------- ESTADO DEL RIEGO (ACT-02) ----------------
bool modoAuto = true;              // al arrancar riega solo, como siempre
bool bombaEncendida = false;
unsigned long bombaDesdeMs = 0;    // cuando se encendio
unsigned long bombaParoMs = 0;     // cuando se apago la ultima vez
bool huboParo = false;
bool bombaHabilitadaActual = false; // ultimo valor calculado en loop()
const __FlashStringHelper *bombaParo = NULL; // por que se apago (o NULL)
unsigned long cmdId = 0;                     // ultima orden aplicada
const __FlashStringHelper *cmdErr = NULL;    // por que no se aplico (o NULL)

// Linea de orden que va llegando por Serial
char orden[32];
byte ordenLen = 0;
bool ordenDesbordada = false;

// Comprueba que las lineas I2C (SDA=A4, SCL=A5) no esten pegadas a GND (LOW).
// Si al sensor le falta VDD (5V) o hay un corto en la protoboard, SDA o SCL
// caen a 0V y Wire.endTransmission() congelaria al Arduino esperando el bus.
bool busI2cLibre() {
  return digitalRead(A4) == HIGH && digitalRead(A5) == HIGH;
}

// Envia un comando de 16 bits al SCD4x por I2C. Devuelve true si hubo ACK.
bool scd4xComando(unsigned int cmd) {
  if (!busI2cLibre())
    return false;
  Wire.beginTransmission(SCD4X_ADDR);
  Wire.write((byte)(cmd >> 8));
  Wire.write((byte)(cmd & 0xFF));
  return Wire.endTransmission() == 0;
}

void setup() {
  // Iniciar actuadores apagados ANTES de declararlos como salida
  // para evitar arranques involuntarios al encender el Arduino.
  digitalWrite(PIN_VENT, LOW);
  digitalWrite(PIN_BOMBA, LOW);
  pinMode(PIN_VENT, OUTPUT);
  pinMode(PIN_BOMBA, OUTPUT);

  Serial.begin(9600);
  dht.begin();
  // Activa pull-ups en A4/A5 antes de comprobar si el bus I2C esta libre.
  pinMode(A4, INPUT_PULLUP);
  pinMode(A5, INPUT_PULLUP);
  Wire.begin();
  // Tiempo limite del bus I2C: sin el, un falso contacto en SDA/SCL a media
  // transmision deja al Uno esperando para siempre (sin lecturas y con la
  // bomba en el estado en que estaba: BOMBA_MAX_MS ya no la apagaria).
  // 25 ms y reinicio del bus. setWireTimeout() existe desde el core AVR 1.8.3
  // (el del IDE es 1.8.6) pero ese core NO define WIRE_HAS_TIMEOUT: dentro de
  // un #if con esa macro nunca se compilaba.
  Wire.setWireTimeout(25000, true);
  // Esperar 2 s a que se estabilicen el DHT11 y el SCD4x (tarda hasta 1 s en
  // encender antes de aceptar comandos I2C).
  delay(2000);
  // Si el Uno se reinicio (pasa cada vez que la Pi abre el puerto USB) el
  // SCD4x NO se apago: sigue midiendo y rechaza start (NACK). Por eso primero
  // stop_periodic_measurement (0x3F86) y los 500 ms que pide el sensor; si ya
  // estaba detenido, el stop no hace nada.
  scd4xComando(0x3F86);
  delay(500);
  // Pone al SCD40/SCD41 a medir cada 5 s (0x21B1 = start_periodic_measurement).
  co2Iniciado = scd4xComando(0x21B1);
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
void campoFloat(const __FlashStringHelper *nombre, float valor, int decimales) {
  Serial.print(F(",\""));
  Serial.print(nombre);
  Serial.print(F("\":"));
  if (isnan(valor))
    Serial.print(F("null"));
  else
    Serial.print(valor, decimales);
}

// pct < 0 significa lectura invalida -> null
void campoAnalogico(const __FlashStringHelper *nombre, int crudo, int pct) {
  Serial.print(F(",\""));
  Serial.print(nombre);
  Serial.print(F("_raw\":"));
  Serial.print(crudo);

  Serial.print(F(",\""));
  Serial.print(nombre);
  Serial.print(F("_pct\":"));
  if (pct < 0)
    Serial.print(F("null"));
  else
    Serial.print(pct);
}

void campoBool(const __FlashStringHelper *nombre, bool valor) {
  Serial.print(F(",\""));
  Serial.print(nombre);
  Serial.print(F("\":"));
  Serial.print(valor ? F("true") : F("false"));
}

// valor < 0 significa lectura invalida -> null
void campoEntero(const __FlashStringHelper *nombre, int valor) {
  Serial.print(F(",\""));
  Serial.print(nombre);
  Serial.print(F("\":"));
  if (valor < 0)
    Serial.print(F("null"));
  else
    Serial.print(valor);
}

// Texto entre comillas, o null si no hay
void campoTexto(const __FlashStringHelper *nombre, const __FlashStringHelper *valor) {
  Serial.print(F(",\""));
  Serial.print(nombre);
  Serial.print(F("\":"));
  if (valor == NULL) {
    Serial.print(F("null"));
  } else {
    Serial.print('"');
    Serial.print(valor);
    Serial.print('"');
  }
}

// ---------------- LECTURA DEL SENSOR DE CO2 (SCD40 / SCD41) ----------------
// CRC-8 de Sensirion (polinomio 0x31, inicio 0xFF) para validar cada palabra
// de 16 bits que llega por I2C desde el SCD4x.
byte scd4xCrc(byte msb, byte lsb) {
  byte crc = 0xFF;
  byte datos[2] = {msb, lsb};
  for (byte i = 0; i < 2; ++i) {
    crc ^= datos[i];
    for (byte bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x80) ? (byte)((crc << 1) ^ 0x31) : (byte)(crc << 1);
    }
  }
  return crc;
}

// Lee el SCD40/SCD41 sin bloquear el loop(). Como el sensor mide cada 5 s y el
// loop() corre cada 2 s, entre muestra y muestra devuelve el ultimo valor
// valido siempre que el sensor siga respondiendo en el bus I2C (A4/A5).
int leerCo2() {
  unsigned long ahora = millis();

  // Si estaba desconectado o nunca arranco, reintentar start_periodic_measurement.
  if (!co2Iniciado) {
    co2Iniciado = scd4xComando(0x21B1);
    if (!co2Iniciado) {
      // Puede que siga midiendo (rechaza start): se le manda stop y start se
      // reintenta en la siguiente vuelta, 2 s despues (pide 500 ms). Sin
      // delay() aqui, para no frenar el loop().
      scd4xComando(0x3F86);
      co2Ppm = -1;
      return -1;
    }
  }

  // 0xE4B8 = get_data_ready_status: ademas de decir si hay muestra nueva,
  // comprueba que el sensor sigue conectado en SDA (A4) y SCL (A5).
  if (!scd4xComando(0xE4B8)) {
    co2Iniciado = false;
    co2Ppm = -1;
    return -1;
  }
  delayMicroseconds(1000);
  if (Wire.requestFrom(SCD4X_ADDR, (byte)3) != 3) {
    co2Iniciado = false;
    co2Ppm = -1;
    return -1;
  }
  byte r0 = (byte)Wire.read();
  byte r1 = (byte)Wire.read();
  byte rCrc = (byte)Wire.read();
  if (scd4xCrc(r0, r1) != rCrc) {
    co2Ppm = -1;
    return -1;
  }

  // Segun la hoja de datos del SCD4x, los 11 bits menos significativos en 0
  // significan que aun no termina el ciclo de 5 s.
  bool datoListo = ((((unsigned int)r0 << 8) | r1) & 0x07FF) != 0;
  if (!datoListo) {
    if (co2TieneLectura && (ahora - co2UltimoMs <= CO2_MAX_EDAD_MS)) {
      return co2Ppm;
    }
    // Si lleva demasiado tiempo sin dar muestra (ej. se desconecto y volvio a
    // conectar solo VDD), le vuelve a pedir que inicie medicion periodica.
    if (ahora - co2UltimoMs > CO2_MAX_EDAD_MS) {
      scd4xComando(0x21B1);
    }
    co2Ppm = -1;
    return -1;
  }

  // 0xEC05 = read_measurement: devuelve 9 bytes (CO2 + temp + hum con sus CRC).
  // Se leen los 9 para vaciar el buffer interno del SCD4x.
  if (!scd4xComando(0xEC05)) {
    co2Iniciado = false;
    co2Ppm = -1;
    return -1;
  }
  delayMicroseconds(1000);
  if (Wire.requestFrom(SCD4X_ADDR, (byte)9) != 9) {
    co2Iniciado = false;
    co2Ppm = -1;
    return -1;
  }
  byte c0 = (byte)Wire.read();
  byte c1 = (byte)Wire.read();
  byte cCrc = (byte)Wire.read();
  for (byte i = 0; i < 6; ++i) {
    Wire.read(); // descartar temp y humedad del SCD4x (ya las mide el DHT11)
  }

  if (scd4xCrc(c0, c1) != cCrc) {
    co2Ppm = -1;
    return -1;
  }
  unsigned int ppm = ((unsigned int)c0 << 8) | c1;
  if (ppm < (unsigned int)CO2_MIN_VALIDO || ppm > (unsigned int)CO2_MAX_VALIDO) {
    co2Ppm = -1;
    return -1;
  }

  co2Ppm = (int)ppm;
  co2UltimoMs = ahora;
  co2TieneLectura = true;
  return co2Ppm;
}

// ---------------- BOMBA Y ORDENES DE LA PI (ACT-02) ----------------
void encenderBomba() {
  bombaEncendida = true;
  bombaDesdeMs = millis();
  digitalWrite(PIN_BOMBA, HIGH);
}

void apagarBomba(const __FlashStringHelper *motivo) {
  if (!bombaEncendida)
    return;
  bombaEncendida = false;
  bombaParoMs = millis();
  huboParo = true;
  bombaParo = motivo;
  digitalWrite(PIN_BOMBA, LOW);
}

// Se llama cada 2 s con la lectura nueva.
// AUTO: arranca con la tierra abajo de SUELO_LIMITE_MIN (si ya paso el
// descanso) y para al llegar, pero no antes de BOMBA_MIN_MS: si la tierra
// esta justo en el limite, el ruido del sensor (49/50/49...) no hace que la
// bomba prenda y apague cada 2 s. MANUAL: no decide nada, solo obedece
// ordenes. En los dos modos: se apaga al cumplir BOMBA_MAX_MS o si el tanque
// llega a critico (la bomba es sumergible y se quema en seco).
void controlarBomba(int sueloPct, bool riegoSugerido, bool bombaHabilitada) {
  unsigned long ahora = millis();
  if (bombaEncendida) {
    if (!bombaHabilitada)
      apagarBomba(F("TANQUE"));
    else if (ahora - bombaDesdeMs >= BOMBA_MAX_MS)
      apagarBomba(F("TIEMPO"));
    else if (modoAuto && sueloPct < 0)
      apagarBomba(F("SENSOR"));
    else if (modoAuto && !riegoSugerido && ahora - bombaDesdeMs >= BOMBA_MIN_MS)
      apagarBomba(F("OBJETIVO"));
  } else if (modoAuto && riegoSugerido && bombaHabilitada &&
             (!huboParo || ahora - bombaParoMs >= BOMBA_DESCANSO_MS)) {
    encenderBomba();
  }
}

// true si el texto son solo digitos (1 a 9): "17" si, "-1", "7x" o "" no.
bool esNumeroDeOrden(const char *texto) {
  if (texto == NULL || texto[0] == '\0' || strlen(texto) > 9)
    return false;
  for (const char *c = texto; *c; ++c)
    if (*c < '0' || *c > '9')
      return false;
  return true;
}

// Aplica una orden "SG,<id>,<equipo>,<accion>" (ya sin el salto de linea).
void aplicarOrden(char *linea) {
  char *prefijo = strtok(linea, ",");
  if (prefijo == NULL || strcmp(prefijo, "SG") != 0)
    return; // ruido o eco de una terminal: se ignora
  char *textoId = strtok(NULL, ",");
  char *equipo = strtok(NULL, ",");
  char *accion = strtok(NULL, ",");
  char *sobra = strtok(NULL, ",");
  unsigned long id = esNumeroDeOrden(textoId) ? strtoul(textoId, NULL, 10) : 0;
  if (id == 0)
    return; // sin numero de orden valido no hay a quien contestarle

  cmdId = id;
  cmdErr = NULL;
  if (equipo == NULL || accion == NULL || sobra != NULL) {
    cmdErr = F("FORMATO");
  } else if (strcmp(equipo, "MODO") == 0 && strcmp(accion, "AUTO") == 0) {
    if (!modoAuto)
      apagarBomba(F("MODO")); // la decision vuelve a ser del Arduino
    modoAuto = true;
  } else if (strcmp(equipo, "MODO") == 0 && strcmp(accion, "MANUAL") == 0) {
    apagarBomba(F("MODO")); // un riego automatico no sigue en manual
    modoAuto = false;
  } else if (strcmp(equipo, "BOMBA") == 0 &&
             (strcmp(accion, "ON") == 0 || strcmp(accion, "OFF") == 0)) {
    if (modoAuto)
      cmdErr = F("MODO"); // en automatico la bomba la decide el Arduino
    else if (strcmp(accion, "OFF") == 0)
      apagarBomba(F("ORDEN"));
    else if (!bombaHabilitadaActual)
      cmdErr = F("TANQUE");
    else if (!bombaEncendida)
      encenderBomba();
  } else {
    cmdErr = F("FORMATO");
  }
}

// Lee lo que mande la Pi sin bloquear. Una linea demasiado larga se
// descarta completa.
void leerOrdenes() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (ordenLen > 0 && !ordenDesbordada) {
        orden[ordenLen] = '\0';
        aplicarOrden(orden);
      }
      ordenLen = 0;
      ordenDesbordada = false;
    } else if (ordenLen < sizeof(orden) - 1) {
      orden[ordenLen++] = c;
    } else {
      ordenDesbordada = true;
    }
  }
}

void alerta(const __FlashStringHelper *codigo, byte nivel) {
  if (!primeraAlerta)
    Serial.print(',');
  Serial.print('"');
  Serial.print(codigo);
  Serial.print('"');
  primeraAlerta = false;
  if (nivel > severidad)
    severidad = nivel;
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
  if (isnan(t))
    alerta(F("FALLA_DHT"), ALERTA);
  else if (t > TEMP_LIMITE_MAX)
    alerta(F("TEMP_ALTA"), ALERTA);
  else if (t < TEMP_LIMITE_MIN)
    alerta(F("TEMP_BAJA"), ALERTA);
  else if (t > TEMP_IDEAL_MAX)
    alerta(F("TEMP_ALTA_LEVE"), AVISO);
  else if (t < TEMP_IDEAL_MIN)
    alerta(F("TEMP_BAJA_LEVE"), AVISO);
}

void evaluarHumedadAire(float h) {
  if (isnan(h))
    return; // ya lo reporto FALLA_DHT
  if (h > HUMA_LIMITE_MAX)
    alerta(F("HUM_AIRE_ALTA"), ALERTA);
  else if (h < HUMA_LIMITE_MIN)
    alerta(F("HUM_AIRE_BAJA"), ALERTA);
  else if (h > HUMA_IDEAL_MAX)
    alerta(F("HUM_AIRE_ALTA_LEVE"), AVISO);
  else if (h < HUMA_IDEAL_MIN)
    alerta(F("HUM_AIRE_BAJA_LEVE"), AVISO);
}

void evaluarSuelo(int pct) {
  if (pct < 0)
    alerta(F("FALLA_SUELO"), ALERTA);
  else if (pct > SUELO_LIMITE_MAX)
    alerta(F("SUELO_ENCHARCADO"), ALERTA);
  else if (pct < SUELO_LIMITE_MIN)
    alerta(F("SUELO_SECO"), ALERTA);
  else if (pct > SUELO_IDEAL_MAX)
    alerta(F("SUELO_HUMEDO_LEVE"), AVISO);
  else if (pct < SUELO_IDEAL_MIN)
    alerta(F("SUELO_SECO_LEVE"), AVISO);
}

void evaluarNivel(int pct) {
  if (pct < 0)
    alerta(F("FALLA_NIVEL"), ALERTA);
  else if (pct <= NIVEL_PCT_CRITICO)
    alerta(F("TANQUE_UN_CUARTO"), ALERTA);
  else if (pct <= NIVEL_PCT_AVISO)
    alerta(F("TANQUE_MITAD"), AVISO);
}

void evaluarLuz() {
  if (esDia) {
    if (sombraMs >= SOMBRA_MAX_MS)
      alerta(F("SOMBRA_PROLONGADA"), AVISO);
  } else if (huboDiaCompleto && solPrevMin < (int)SOL_MIN_REQUERIDO) {
    // Solo de noche y solo si ya se cerro un dia completo: de otro modo
    // se alertaria de falta de sol a las 7 de la mañana, cuando todavia
    // le quedan horas de luz al dia.
    alerta(F("SOL_INSUFICIENTE"), AVISO);
  }
}

void evaluarCo2(int co2) {
  if (co2 < 0) {
    // En el arranque todavia no hay muestra: null, pero no es falla.
    if (co2TieneLectura || millis() >= CO2_ARRANQUE_MS)
      alerta(F("FALLA_CO2"), ALERTA);
  } else if (co2 > CO2_LIMITE_MAX)
    alerta(F("CO2_ALTO"), ALERTA);
  else if (co2 < CO2_LIMITE_MIN)
    alerta(F("CO2_BAJO"), ALERTA);
  else if (co2 > CO2_IDEAL_MAX)
    alerta(F("CO2_ALTO_LEVE"), AVISO);
  else if (co2 < CO2_IDEAL_MIN)
    alerta(F("CO2_BAJO_LEVE"), AVISO);
}

void loop() {
  leerOrdenes(); // las ordenes de la Pi se atienden en cuanto llegan

  if (millis() - ultimaLectura < INTERVALO_MS)
    return;
  ultimaLectura = millis();

  // ---- lectura ----
  float tempC = dht.readTemperature();
  float humAire = dht.readHumidity();
  int sueloRaw = analogRead(PIN_SUELO);
  int nivelRaw = analogRead(PIN_NIVEL);
  int luzRaw = analogRead(PIN_LUZ);
  int co2 = leerCo2();

  int sueloPct = lecturaValida(sueloRaw, SUELO_MIN_VALIDO, SUELO_MAX_VALIDO)
                     ? aPorcentaje(sueloRaw, SUELO_EN_AIRE, SUELO_EN_AGUA)
                     : -1;
  int nivelPct = lecturaValida(nivelRaw, NIVEL_MIN_VALIDO, NIVEL_MAX_VALIDO)
                     ? aPorcentaje(nivelRaw, NIVEL_VACIO, NIVEL_LLENO)
                     : -1;

  int luzNivel = LUZ_INVERTIDA ? (1023 - luzRaw) : luzRaw;
  actualizarCicloDeLuz(luzNivel);

  // ---- decisiones (se recomiendan, la nube y el productor deciden) ----
  // Fail-safe: si el nivel esta bajo O el sensor fallo, la bomba no arranca.
  bool bombaHabilitada = (nivelPct > NIVEL_PCT_CRITICO);
  bool riegoSugerido = (sueloPct >= 0 && sueloPct < SUELO_LIMITE_MIN);
  // La ventilacion es constante: BASE siempre, ALTA si hay calor o bochorno.
  bool ventAlta = (!isnan(tempC) && tempC > TEMP_IDEAL_MAX) ||
                  (!isnan(humAire) && humAire > HUMA_IDEAL_MAX);

  // ---- control fisico de actuadores (L298N, HIGH = encendido) ----
  // Ventilador: se enciende cuando la temperatura o humedad superan el ideal.
  digitalWrite(PIN_VENT, ventAlta ? HIGH : LOW);

  // Bomba (ACT-02): ver controlarBomba(). Solo riega con agua en el tanque.
  bombaHabilitadaActual = bombaHabilitada;
  controlarBomba(sueloPct, riegoSugerido, bombaHabilitada);
  bool bombaActiva = bombaEncendida;
  digitalWrite(PIN_BOMBA, bombaActiva ? HIGH : LOW);

  // ---- salida JSON ----
  Serial.print(F("{\"ms\":"));
  Serial.print(millis());

  campoFloat(F("temp_c"), tempC, 1);
  campoFloat(F("hum_aire_pct"), humAire, 1);
  campoAnalogico(F("suelo"), sueloRaw, sueloPct);
  campoAnalogico(F("nivel"), nivelRaw, nivelPct);

  // La luz se manda cruda; luz_nivel es la misma lectura normalizada a
  // "mas alto = mas luz" para que la nube no tenga que saber la polaridad.
  Serial.print(F(",\"luz_raw\":"));
  Serial.print(luzRaw);
  Serial.print(F(",\"luz_nivel\":"));
  Serial.print(luzNivel);
  campoEntero(F("co2_ppm"), co2);

  campoBool(F("es_dia"), esDia);
  Serial.print(F(",\"sol_min_hoy\":"));
  Serial.print(solHoyMs / 60000UL);
  Serial.print(F(",\"sol_min_prev\":"));
  if (solPrevMin < 0)
    Serial.print(F("null"));
  else
    Serial.print(solPrevMin);

  campoBool(F("riego_sugerido"), riegoSugerido);
  campoBool(F("bomba_habilitada"), bombaHabilitada);
  campoBool(F("bomba_activa"), bombaActiva);
  Serial.print(F(",\"vent\":\""));
  Serial.print(ventAlta ? F("ALTA") : F("BASE"));
  Serial.print('"');
  campoBool(F("vent_activo"), ventAlta);

  // Riego (ACT-02): modo, ultima orden aplicada y por que paro la bomba
  Serial.print(F(",\"modo\":\""));
  Serial.print(modoAuto ? F("AUTO") : F("MANUAL"));
  Serial.print(F("\",\"cmd_id\":"));
  Serial.print(cmdId);
  campoTexto(F("cmd_err"), cmdErr);
  campoTexto(F("bomba_paro"), bombaParo);

  Serial.print(F(",\"alertas\":["));
  severidad = OK;
  primeraAlerta = true;
  evaluarTemperatura(tempC);
  evaluarHumedadAire(humAire);
  evaluarSuelo(sueloPct);
  evaluarNivel(nivelPct);
  evaluarLuz();
  evaluarCo2(co2);
  Serial.print(F("],\"estado\":\""));
  Serial.print(severidad == OK
                   ? F("OK")
                   : (severidad == AVISO ? F("AVISO") : F("ALERTA")));
  Serial.println(F("\"}"));
}

/*
  ------------------------------------------------------------------
  EJEMPLO DE LINEA
  ------------------------------------------------------------------
  {"ms":148022,"temp_c":24.2,"hum_aire_pct":38.0,"suelo_raw":565,
   "suelo_pct":0,"nivel_raw":5,"nivel_pct":2,"luz_raw":306,"luz_nivel":717,
   "co2_ppm":450,"es_dia":true,"sol_min_hoy":42,"sol_min_prev":null,
   "riego_sugerido":true,"bomba_habilitada":false,"bomba_activa":false,
   "vent":"BASE","vent_activo":false,"modo":"AUTO","cmd_id":0,"cmd_err":null,
   "bomba_paro":null,
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
       - cuarto a oscuras (noche)      -> el umbral de dia va bien arriba de
  esto
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
