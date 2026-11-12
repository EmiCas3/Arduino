# Firmware del Arduino Uno (MON-01, MON-02, MON-03, ACT-02, NF-01)

Lee los sensores del mini vivero cada 2 s, los compara contra los rangos del
rábano y manda **una línea JSON por Serial** (USB, 9600 baud). También controla
el ventilador y la bomba de riego, y obedece las órdenes de riego que le manda
la Pi.

El Arduino **no habla con internet**: la Raspberry Pi 5 es el gateway
(`gateway/`), lee estas líneas y las sube al backend.

```
Sensores ─▶ Arduino Uno ──USB 9600, JSON cada 2 s──▶ gateway (Pi 5) ─▶ backend ─▶ dashboard
                 ▲                                         │
                 └──────── "SG,<id>,MODO|BOMBA,..." ◀──────┘   (ACT-02)
```

## Carpetas

```
vivero_sensores/
├── vivero_sensoresV2/vivero_sensoresV2.ino   El firmware que va en el Arduino
├── pruebas_pc/                               Pruebas del firmware en la compu (sin Arduino)
│   ├── prueba_riego.cpp                      Las pruebas (riego ACT-02 y CO₂)
│   └── Arduino.h, DHT.h, Wire.h              Versiones de mentira de las librerías
└── Preparacion/                              Sketches sueltos de cuando se armó el vivero
    ├── vivero_sensoresCalibracion.ino        Lectura cruda de los 4 sensores (versión vieja: DHT en D13)
    └── prueba_motores/prueba_motores.ino     Prueba de un motor en D3
```

Solo `vivero_sensoresV2.ino` va en el vivero. Lo de `Preparacion/` sirve para
probar el hardware por partes.

## Cableado

Todo va al Arduino Uno; nada se conecta a los GPIO de la Pi.

| Equipo | Conexión |
|---|---|
| DHT11 (temp. y humedad del aire) | VCC 5V · GND · OUT → **D2** |
| Humedad de la tierra | VCC 5V · GND · AO → **A0** |
| Nivel de agua del tanque | VCC 5V · GND · AO → **A1** |
| Sensor de luz | VCC 5V · GND · AO → **A2** |
| CO₂ SCD40/SCD41 (I²C, dirección 0x62) | VDD 5V · GND · SDA → **A4** · SCL → **A5** |
| Ventilador (L298N canal A) | **D3** → IN1, IN2 a GND |
| Bomba de riego (L298N canal B) | **D5** → IN3, IN4 a GND |
| L298N | +12V de fuente externa, GND común con el Arduino, jumpers ENA y ENB puestos |

No alimentes los motores desde el 5V del Arduino: el pico de corriente lo reinicia.

## Cómo cargarlo

1. Arduino IDE → *Library Manager* → instala **DHT sensor library** (Adafruit),
   que pide también **Adafruit Unified Sensor**. También están copiadas en
   `libraries/` en la raíz del repo. `Wire.h` ya viene con el IDE; el SCD4x se
   lee sin librerías externas.
2. Abre `vivero_sensoresV2/vivero_sensoresV2.ino`, placa **Arduino Uno**, y súbelo.
3. Abre el Monitor Serial a **9600** baud: cada 2 s debe salir una línea JSON.
   Ciérralo antes de arrancar el gateway; el puerto solo lo puede tener un programa.

Cada vez que la Pi abre el puerto USB, el Uno se reinicia. Es normal: el
firmware detiene y vuelve a arrancar el SCD4x al empezar.

## La línea que manda (contrato con la Pi)

Ejemplo (va en una sola línea):

```json
{"ms":14500,"temp_c":23.0,"hum_aire_pct":70.0,"suelo_raw":377,"suelo_pct":61,
 "nivel_raw":120,"nivel_pct":80,"luz_raw":300,"luz_nivel":723,"co2_ppm":450,
 "es_dia":true,"sol_min_hoy":3,"sol_min_prev":null,"riego_sugerido":false,
 "bomba_habilitada":true,"bomba_activa":false,"vent":"BASE","vent_activo":false,
 "modo":"AUTO","cmd_id":31,"cmd_err":null,"bomba_paro":"SENSOR",
 "alertas":[],"estado":"OK"}
```

- Un sensor que falla o que todavía no tiene dato manda `null`, nunca un número inventado.
- `luz_nivel` es la luz normalizada a "más alto = más luz".
- `estado` resume las alertas: `OK`, `AVISO` (fuera del ideal) o `ALERTA` (fuera del límite o sensor caído).
- El gateway reenvía los campos **sin renombrarlos**. Si agregas o cambias uno,
  actualiza también `gateway/PROTOCOLO_ARDUINO.md`, el simulador del gateway y `openapi.yaml`.

## Rangos del rábano y alertas

| Variable | Ideal (fuera → `AVISO`) | Límite (fuera → `ALERTA`) | Códigos |
|---|---|---|---|
| Temperatura | 20–25 °C | 6–30 °C | `TEMP_ALTA`, `TEMP_BAJA` y sus `_LEVE` |
| Humedad del aire | 60–80 % | 50–85 % | `HUM_AIRE_ALTA`, `HUM_AIRE_BAJA` y sus `_LEVE` |
| Humedad de la tierra | 60–65 % | 50–80 % | `SUELO_ENCHARCADO`, `SUELO_SECO`, `SUELO_HUMEDO_LEVE`, `SUELO_SECO_LEVE` |
| CO₂ | 400–1000 ppm | 300–1500 ppm | `CO2_ALTO`, `CO2_BAJO` y sus `_LEVE` |
| Tanque | — | ≤ 50 % aviso, ≤ 25 % alerta | `TANQUE_MITAD`, `TANQUE_UN_CUARTO` (bloquea la bomba) |
| Luz | — | — | `SOMBRA_PROLONGADA` (30 min de día sin sol pleno), `SOL_INSUFICIENTE` (el día cerró con < 6 h de sol) |
| Sensor caído | — | — | `FALLA_DHT`, `FALLA_SUELO`, `FALLA_NIVEL`, `FALLA_CO2` |

`FALLA_CO2` no sale en los primeros 12 s tras arrancar: el SCD4x tarda en dar
su primera muestra y mientras tanto `co2_ppm` va `null` sin alerta.

Los valores están como constantes al inicio del `.ino` (sección *UMBRALES DEL RÁBANO*).

## Actuadores

- **Ventilador:** se enciende (`vent: "ALTA"`) si la temperatura pasa de 25 °C o
  la humedad del aire de 80 %.
- **Bomba (ACT-02):** dos modos, y al arrancar siempre está en `AUTO`.
  - `AUTO`: riega sola cuando la tierra baja de 50 %, y para al llegar. Cada riego
    dura al menos 10 s y entre riegos espera 5 min.
  - `MANUAL`: no riega sola; solo obedece `BOMBA,ON` / `BOMBA,OFF`.
  - En cualquier modo se apaga a los 60 s o si el tanque baja de 25 % (es
    sumergible y se quema en seco). `bomba_paro` dice por qué paró:
    `OBJETIVO`, `TIEMPO`, `TANQUE`, `SENSOR`, `MODO` u `ORDEN`.

### Órdenes que acepta por Serial

| Orden | Qué hace |
|---|---|
| `SG,<id>,MODO,AUTO` | Pasa a riego automático |
| `SG,<id>,MODO,MANUAL` | Pasa a manual (apaga la bomba si estaba regando) |
| `SG,<id>,BOMBA,ON` | Enciende la bomba (solo en `MANUAL` y con agua) |
| `SG,<id>,BOMBA,OFF` | Apaga la bomba (solo en `MANUAL`) |

`<id>` es un número de 1 a 9 dígitos que pone el backend. El Arduino contesta en
su siguiente línea con `cmd_id` = la última orden aplicada y `cmd_err` = `null`
o el motivo para no aplicarla (`MODO`, `TANQUE`, `FORMATO`). Las líneas que no
empiezan con `SG,` se ignoran. Detalle completo: [`gateway/PROTOCOLO_ARDUINO.md`](../gateway/PROTOCOLO_ARDUINO.md).

Puedes probarlas a mano escribiéndolas en el Monitor Serial (con *Newline* activado).

## Pruebas en la computadora

`pruebas_pc/prueba_riego.cpp` compila el `.ino` tal cual, con un Arduino de
mentira (reloj, pines, Serial, DHT y bus I²C controlados por la prueba). Cubre
los modos de riego, los apagados de seguridad de la bomba, las órdenes mal
formadas y el CO₂ (arranque, desconexión y reinicio del Uno con el sensor midiendo).

```bash
cd vivero_sensores/pruebas_pc
g++ -std=gnu++11 -I. -o prueba_riego prueba_riego.cpp
./prueba_riego
```

Al final debe decir `TODO OK` (en Windows el ejecutable es `prueba_riego.exe`).
Córrelas antes de abrir un PR que toque el `.ino`.

## Calibración

Los valores medidos están al inicio del `.ino` (sección *CALIBRACIÓN*):
tierra en aire = 555, tierra en agua = 267, tanque vacío = 0, tanque lleno = 150.

Pendiente antes de la demo (pasos completos al final del `.ino`):

1. **`NIVEL_LLENO`**: medirlo con el tanque lleno de verdad y el sensor fijo.
2. **`LUZ_INVERTIDA`**: tapar el sensor; si `luz_raw` sube, va `true`.
3. **`LUZ_UMBRAL_DIA` y `LUZ_UMBRAL_SOL`**: anotar `luz_nivel` a oscuras, en sombra y a pleno sol.
4. **Tierra**: el 100 % se midió en un vaso de agua. Hay que medirlo en la maceta
   regada a capacidad de campo, o los rangos 50/60/65/80 % no son agronómicos.

El porcentaje del tanque solo cubre el tramo que mide el sensor, no el volumen total del tanque.
