/*
  SmartGreenAI: Mini Vivero Automatizado
  Carpeta de Preparación - Programa de Prueba de 1 Motor

  Propósito:
    Verificar de forma rápida y sencilla el funcionamiento de un motor
    (bomba de agua o ventilador) conectado al Pin Digital 3.

  Conexión recomendada (Arduino Uno):
    - Pin de control (IN / Señal)  -> Pin Digital D3
    - GND del módulo / driver      -> GND del Arduino
    - Alimentación del motor       -> Fuente externa recomendada (5V / 12V)
      (Unir GND de la fuente externa con GND del Arduino)

  ADVERTENCIA DE ALIMENTACIÓN:
    No alimentes el motor directamente desde el pin de 5V del Arduino,
    ya que el pico de corriente puede provocar caídas de tensión o resetear la
  placa.
*/

// ====================================================================
// 1. CONFIGURACIÓN DE PINES Y PARÁMETROS
// ====================================================================

// Pin de control del motor
const int PIN_MOTOR = 3;

// TIPO DE MÓDULO (Polaridad):
// Para L298N, transistores o MOSFETs debe ser false (HIGH = Encendido).
// Para módulos de relé típicos suele ser true.
const bool MODULO_ACTIVE_LOW = false;

// Tiempos del ciclo automático (en milisegundos)
const unsigned long TIEMPO_ENCENDIDO_MS = 3000; // 3 segundos encendido
const unsigned long TIEMPO_PAUSA_MS = 2000;     // 2 segundos apagado

// ====================================================================
// 2. VARIABLES DE ESTADO
// ====================================================================

bool motorActivo = false;
bool modoAutomatico = true; // Inicia en ciclo automático

unsigned long tiempoUltimoCambio = 0;

// ====================================================================
// 3. FUNCIONES DE CONTROL
// ====================================================================

// Aplica el nivel lógico correcto según la polaridad configurada
void setMotor(bool encender) {
  if (MODULO_ACTIVE_LOW) {
    digitalWrite(PIN_MOTOR, encender ? LOW : HIGH);
  } else {
    digitalWrite(PIN_MOTOR, encender ? HIGH : LOW);
  }
}

void encenderMotor() {
  motorActivo = true;
  setMotor(true);
  Serial.println(F("[MOTOR - PIN 3]  >>> ENCENDIDO <<<"));
}

void apagarMotor() {
  motorActivo = false;
  setMotor(false);
  Serial.println(F("[MOTOR - PIN 3]  --- APAGADO ---"));
}

void mostrarMenu() {
  Serial.println(
      F("\n========================================================"));
  Serial.println(
      F("       SmartGreenAI - Diagnostico de Motor (Pin 3)       "));
  Serial.println(F("========================================================"));
  Serial.print(F("Pin asignado al motor:     D"));
  Serial.println(PIN_MOTOR);
  Serial.print(F("Tipo de logica de modulo:  "));
  Serial.println(MODULO_ACTIVE_LOW ? F("Active LOW (LOW = ON)")
                                   : F("Active HIGH (HIGH = ON)"));
  Serial.println(F("--------------------------------------------------------"));
  Serial.println(
      F("Comandos del Monitor Serial (ingresa caracter y presiona Enter):"));
  Serial.println(F("  '1' o 'e' : Encender motor"));
  Serial.println(F("  '0' o 's' : Apagar motor"));
  Serial.println(F("  'a'       : Alternar Modo Automatico <-> Modo Manual"));
  Serial.println(F("  'm' o '?' : Mostrar este menu"));
  Serial.println(
      F("========================================================\n"));
  if (modoAutomatico) {
    Serial.println(
        F("Estado: MODO AUTOMATICO activo. Encendiendo/apagando en ciclo..."));
  } else {
    Serial.println(F("Estado: MODO MANUAL activo. Esperando comandos..."));
  }
}

// ====================================================================
// 4. SETUP
// ====================================================================

void setup() {
  Serial.begin(9600);

  // Asegurar que el pin inicie apagado antes de declararlo salida
  setMotor(false);
  pinMode(PIN_MOTOR, OUTPUT);

  delay(1000); // Pequeña pausa de inicio
  mostrarMenu();
  tiempoUltimoCambio = millis();
}

// ====================================================================
// 5. ATENCIÓN DE COMANDOS POR SERIAL (MODO MANUAL)
// ====================================================================

void procesarComandosSerial() {
  while (Serial.available() > 0) {
    char cmd = Serial.read();

    // Ignorar saltos de línea y espacios
    if (cmd == '\r' || cmd == '\n' || cmd == ' ')
      continue;

    switch (cmd) {
    case '1':
    case 'e':
    case 'E':
      modoAutomatico = false;
      encenderMotor();
      break;

    case '0':
    case 's':
    case 'S':
      modoAutomatico = false;
      apagarMotor();
      break;

    case 'a':
    case 'A':
      modoAutomatico = !modoAutomatico;
      if (modoAutomatico) {
        Serial.println(F("\n>> Modo AUTOMATICO activado <<"));
        apagarMotor();
        tiempoUltimoCambio = millis();
      } else {
        Serial.println(F("\n>> Modo MANUAL activado <<"));
        apagarMotor();
      }
      break;

    case 'm':
    case 'M':
    case '?':
      mostrarMenu();
      break;

    default:
      Serial.print(F("Comando no reconocido: '"));
      Serial.print(cmd);
      Serial.println(F("'. Envia '?' para ver el menu."));
      break;
    }
  }
}

// ====================================================================
// 6. CICLO AUTOMÁTICO DE PRUEBA
// ====================================================================

void ejecutarCicloAutomatico() {
  unsigned long ahora = millis();

  if (motorActivo) {
    // Si esta encendido, verificar si ya cumplio el tiempo de encendido
    if (ahora - tiempoUltimoCambio >= TIEMPO_ENCENDIDO_MS) {
      apagarMotor();
      tiempoUltimoCambio = ahora;
    }
  } else {
    // Si esta apagado, verificar si ya cumplio el tiempo de pausa
    if (ahora - tiempoUltimoCambio >= TIEMPO_PAUSA_MS) {
      encenderMotor();
      tiempoUltimoCambio = ahora;
    }
  }
}

// ====================================================================
// 7. BUCLE PRINCIPAL
// ====================================================================

void loop() {
  // Escuchar comandos del usuario por Monitor Serial
  procesarComandosSerial();

  // Si esta en modo automatico, gestionar el parpadeo/ciclo del motor
  if (modoAutomatico) {
    ejecutarCicloAutomatico();
  }
}
