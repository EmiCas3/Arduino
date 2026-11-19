/*
  SmartGreenAI: Mini Vivero Automatizado
  Carpeta de Preparación - Prueba secuencial de Ventilador y Bomba

  Mantiene exactamente el mismo cableado que vivero_sensoresV2.ino:
    - Ventilador (L298N canal A): D3 -> IN1 (IN2 a GND del Arduino), OUT1/OUT2
    - Bomba      (L298N canal B): D5 -> IN3 (IN4 a GND del Arduino), OUT3/OUT4
    - L298N: fuente externa en +12V, GND común con Arduino, jumpers ENA y ENB puestos.

  Secuencia al arrancar (una sola vez para proteger la bomba en seco):
    1. Enciende ventilador 5 s y lo apaga.
    2. Enciende bomba 5 s y la apaga.
    3. Pausa de 2 s.
    4. Enciende ambos 5 s y los apaga.

  Para repetir la secuencia: presiona RESET en el Arduino o envía 'r' en el
  Monitor Serial (9600 baud).
*/

#define PIN_VENT 3  // Ventilador: IN1 del L298N (IN2 a GND)
#define PIN_BOMBA 5 // Bomba de riego: IN3 del L298N (IN4 a GND)

const unsigned long DURACION_PRUEBA_MS = 5000UL;
const unsigned long PAUSA_AMBOS_MS = 2000UL;

void ejecutarPrueba() {
  Serial.println(F("\n=== Inicio de prueba (D3=Ventilador, D5=Bomba) ==="));

  // 1. Ventilador 5 segundos
  Serial.println(F("1/4 -> Ventilador ON (5 s)"));
  digitalWrite(PIN_VENT, HIGH);
  delay(DURACION_PRUEBA_MS);
  digitalWrite(PIN_VENT, LOW);
  Serial.println(F("1/4 -> Ventilador OFF"));

  // 2. Bomba 5 segundos
  Serial.println(F("2/4 -> Bomba ON (5 s)"));
  digitalWrite(PIN_BOMBA, HIGH);
  delay(DURACION_PRUEBA_MS);
  digitalWrite(PIN_BOMBA, LOW);
  Serial.println(F("2/4 -> Bomba OFF"));

  // 3. Espera 2 segundos
  Serial.println(F("3/4 -> Espera 2 s..."));
  delay(PAUSA_AMBOS_MS);

  // 4. Ambos 5 segundos
  Serial.println(F("4/4 -> Ventilador y Bomba ON (5 s)"));
  digitalWrite(PIN_VENT, HIGH);
  digitalWrite(PIN_BOMBA, HIGH);
  delay(DURACION_PRUEBA_MS);
  digitalWrite(PIN_VENT, LOW);
  digitalWrite(PIN_BOMBA, LOW);
  Serial.println(F("4/4 -> Ambos OFF"));

  Serial.println(F("=== Fin de prueba. Envia 'r' o presiona RESET para repetir. ==="));
}

void setup() {
  digitalWrite(PIN_VENT, LOW);
  digitalWrite(PIN_BOMBA, LOW);
  pinMode(PIN_VENT, OUTPUT);
  pinMode(PIN_BOMBA, OUTPUT);

  Serial.begin(9600);
  delay(1000);

  ejecutarPrueba();
}

void loop() {
  if (Serial.available() > 0) {
    char c = Serial.read();
    if (c == 'r' || c == 'R') {
      ejecutarPrueba();
    }
  }
}
