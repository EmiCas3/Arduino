# Protocolo Pi → Arduino (ACT-02)

Cómo le manda órdenes la plataforma al Arduino del vivero y cómo las confirma.
Lo implementan `vivero_sensores/vivero_sensoresV2/vivero_sensoresV2.ino`
(firmware), `gateway/protocol.py` + `gateway/serial_reader.py` (Pi) y
`backend/app/irrigation.py` (nube). El Arduino simulado (`gateway/simulator.py`)
se comporta igual que el firmware.

## Quién decide qué

- **El Arduino decide el riego automático** con sus constantes. La plataforma
  **no** duplica esos números: solo los muestra.

  | Constante | Valor | Qué es |
  |---|---|---|
  | `SUELO_LIMITE_MIN` | 50 % | Objetivo: riega abajo de esto y para al llegar |
  | `BOMBA_MAX_MS` | 60 s | Ningún riego (automático o manual) dura más |
  | `BOMBA_MIN_MS` | 10 s | Un riego automático dura al menos esto: si la tierra está justo en el límite, el ruido del sensor (49/50/49…) no hace que la bomba prenda y apague cada 2 s |
  | `BOMBA_DESCANSO_MS` | 5 min | Espera entre dos riegos automáticos |
  | `NIVEL_PCT_CRITICO` | 25 % | Con el tanque en o abajo de esto la bomba no arranca y se apaga |

- **La plataforma** guarda el modo que eligió una persona (automático o
  manual), manda las órdenes manuales y registra cada encendido/apagado como
  automático o manual.

## Camino de una orden

```
Persona ─clic─▶ backend ──(guarda "pending")
                   │
Pi ──POST lecturas cada 10 s──▶ backend
Pi ◀──202 {…, "commands": [{"id":18,"kind":"pump","value":"ON"}]}──   (pasa a "sent")
Pi ──"SG,18,BOMBA,ON\n" por USB──▶ Arduino (la aplica al instante)
Arduino ──siguiente línea JSON: "cmd_id":18, "bomba_activa":true──▶ Pi ──▶ backend ("confirmed")
```

Sin confirmación en `SMARTGREENAI_COMMAND_ACK_SECONDS` (45 s) la orden queda
`expired`; si la confirmación llega tarde, se corrige a `confirmed`.

`cmd_id` dice cuál fue la **última** orden aplicada, así que las anteriores
que iban en camino también cuentan como aplicadas (el gateway las escribe en
orden). El cambio de modo además se comprueba con `modo`: si la orden de modo
más nueva no coincide con lo que reporta el Arduino, se perdió y queda
`expired` (el backend manda otra enseguida).

## Órdenes (Pi → Arduino)

Una línea ASCII por orden, terminada en `\n` (también acepta `\r\n`), a la
misma velocidad del Serial (9600).

| Línea | Qué hace |
|---|---|
| `SG,<id>,MODO,AUTO` | Riega solo con sus constantes. Si estaba en manual con la bomba prendida, la apaga |
| `SG,<id>,MODO,MANUAL` | Ya **no** riega solo. Si estaba regando, apaga la bomba |
| `SG,<id>,BOMBA,ON` | Enciende la bomba. Solo en MANUAL y con agua en el tanque. Se apaga sola a los `BOMBA_MAX_MS` |
| `SG,<id>,BOMBA,OFF` | Apaga la bomba. Solo en MANUAL |

- `<id>` es un entero de 1 a 999 999 999 que pone el backend (el id de la orden).
  El firmware solo acepta de 1 a 9 dígitos (`esNumeroDeOrden()`): `-1`, `7x`
  o un número más largo se ignoran.
- Las líneas miden menos de 32 caracteres: caben en el búfer de 64 bytes del
  Uno. Una línea más larga se descarta completa.
- Una línea que no empieza con `SG,` o no trae un id mayor que 0 **se ignora**
  (ruido, el eco de una terminal, etc.).
- El gateway **solo escribe** líneas que pasan `protocol.to_line()`: nunca
  texto libre del backend. Una orden que esperó más de 30 s sin poder
  escribirse (Arduino desconectado) se descarta.
- Después de abrir el puerto, el gateway **no escribe nada hasta recibir la
  primera lectura válida**: el Uno se reinicia al abrir el puerto y lo que
  llegue mientras corre su bootloader se pierde.

## Respuesta (Arduino → Pi)

Campos nuevos en la línea JSON de siempre (cada 2 s):

| Campo | Valores | Qué es |
|---|---|---|
| `modo` | `AUTO` \| `MANUAL` | Modo actual. Al arrancar: `AUTO` (como siempre) |
| `cmd_id` | entero | La última orden que aplicó. `0` al arrancar |
| `cmd_err` | `null` \| `MODO` \| `TANQUE` \| `FORMATO` | Por qué no aplicó esa orden |
| `bomba_paro` | `null` \| `OBJETIVO` \| `TIEMPO` \| `TANQUE` \| `SENSOR` \| `MODO` \| `ORDEN` | Por qué se apagó la bomba la última vez |

`cmd_err`:

- `MODO`: llegó `BOMBA,ON/OFF` en modo automático (ahí decide el Arduino).
- `TANQUE`: `BOMBA,ON` con el tanque en crítico.
- `FORMATO`: tenía id pero el resto no cuadra (`SG,7,BOMBA,PRENDE`).

`bomba_paro`:

- `OBJETIVO`: en automático, la tierra llegó a `SUELO_LIMITE_MIN`.
- `TIEMPO`: se cumplió `BOMBA_MAX_MS`.
- `TANQUE`: el tanque bajó a crítico mientras regaba.
- `SENSOR`: en automático, el sensor de tierra dejó de responder.
- `MODO`: un cambio de modo la apagó.
- `ORDEN`: `BOMBA,OFF`.

Ejemplo:

```json
{"ms":148022,"temp_c":24.2,…,"co2_ppm":450,…,"bomba_habilitada":true,"bomba_activa":false,
 "vent":"BASE","vent_activo":false,"modo":"MANUAL","cmd_id":18,"cmd_err":null,
 "bomba_paro":"ORDEN","alertas":[],"estado":"OK"}
```

Un firmware que **no** manda `modo` (el de antes de ACT-02) no acepta
órdenes: el backend no le genera ninguna y la página lo explica. Su riego se
sigue registrando, como automático.

## Reinicios

Al abrir el puerto USB el Uno se reinicia: vuelve a `AUTO` con `cmd_id` 0 y la
bomba apagada. Si la persona había elegido MANUAL, el backend lo nota en la
siguiente lectura y le vuelve a mandar `SG,<id>,MODO,MANUAL` (orden "system").
Mientras tanto pueden pasar unos segundos en AUTO.

En "Últimas acciones" esos casos se registran como automáticos, con su
motivo: "El Arduino se reinició (la bomba arranca apagada)" o "Se restableció
el modo elegido (MANUAL)".

Si la Pi deja de mandar órdenes, el Arduino se queda en el último modo:
en manual no riega solo (decisión D11 del plan; un respaldo que vuelva a
automático si la Pi calla N minutos queda para después).

## Cómo probarlo sin el Arduino

- **Lógica del firmware en la computadora** (sin placa):

  ```bat
  cd vivero_sensores\pruebas_pc
  g++ -std=gnu++11 -I. -o prueba_riego prueba_riego.cpp
  prueba_riego
  ```

  Compila el `.ino` tal cual con un Arduino de mentira y revisa 35 casos
  (automático, objetivo, mínimo de 10 s, descanso, tiempo máximo, manual,
  tanque, cambio de modo, líneas raras y sensor de CO₂ I²C).

- **Cadena completa con el simulador:** `python -m gateway simulate --seco`
  (la tierra arranca en el límite y a los pocos segundos el Arduino simulado
  riega solo). En "Mi vivero" se cambia a Manual y se usa Regar / Detener.
  `--firmware-actual` simula el firmware de antes (no acepta órdenes).

- **Con el Arduino real:** el Monitor Serie del IDE también sirve para
  probar: escribe `SG,1,MODO,MANUAL` y Enter, luego `SG,2,BOMBA,ON`, y mira
  `modo`, `cmd_id` y `bomba_activa` en las líneas siguientes. (Cierra el
  Monitor antes de arrancar el gateway: el puerto es de uno a la vez.)
