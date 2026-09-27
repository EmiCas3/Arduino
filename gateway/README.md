# Gateway de la Raspberry Pi (MON-03)

Cliente que corre en la Raspberry Pi 5: lee al Arduino por USB, le pone la hora
a cada lectura, la guarda en un buffer local y la manda por lotes al backend
(`POST /api/v1/devices/{device_id}/readings`, MON-04). Si no hay red, **nada se
pierde**: las lecturas esperan en el buffer y salen con su hora original cuando
vuelve la conexión.

```
Arduino Uno ──USB 9600──▶ serial_reader ─▶ clock (hora UTC) ─▶ buffer.db ─▶ sender ──HTTP + X-API-Key──▶ backend
 (JSON cada 2 s)                                                (SQLite)     (lotes de 100 cada 10 s)
```

Solo usa `pyserial` y la librería estándar de Python. Nada que compilar.

## Qué cubre

| Criterio de MON-03 | Cómo |
|---|---|
| 1. Con red, la lectura llega con device, sensor, valor, unidad y hora | Reenvía los campos del Arduino **sin renombrarlos** y agrega `ts` en UTC con milisegundos. El `device_id` va en la URL y las unidades están fijas en el contrato |
| 2. Si falla el envío, se guarda y se reintenta; nada se descarta en silencio | Toda lectura se guarda en SQLite **antes** de enviarse y solo se marca `enviada` con un 202 |
| 3. Al volver la red, se vacía el buffer con la hora original | `ts` se pone al **leer** la línea, no al enviarla. El backend es idempotente por `(device_id, ts)` |

## Reglas que respeta

- **La hora la pone la Pi, solo si está sincronizada por internet (NTP).** La
  Pi 5 no tiene RTC con pila: al prender no sabe la hora. Mientras
  `timedatectl` diga que no está sincronizada, las lecturas se guardan como
  `sin_hora` con el tiempo desde el arranque, y en cuanto sincroniza se les
  calcula la hora exacta. **Nunca se inventa una hora.** Si la Pi se reinicia
  antes de sincronizar, esas lecturas ya no se pueden fechar: se quedan en el
  buffer y `status` lo avisa.
- `null` = sensor caído; se reenvía tal cual, nunca como 0.
- La primera línea tras abrir el puerto puede llegar cortada (el Arduino se
  reinicia al abrirlo): se ignora y queda en el log.
- Los campos extra del firmware (`bomba_activa`, `vent_activo`) se reenvían; el
  backend los ignora hasta ACT-04.

## Qué hace con cada respuesta

| Respuesta del backend | Qué hace |
|---|---|
| 202 | Marca `enviada`. Las que vengan en `rejected` quedan `rechazada` con su motivo (no se borran) |
| Sin red, timeout o 5xx | Conserva todo y reintenta con espera creciente: 5 s, 10 s, 20 s… hasta 5 min |
| 401 / 403 | Conserva todo y lo dice claro en el log: revisa `SG_API_KEY` |
| 404 | Conserva todo y lo dice claro: revisa `SG_DEVICE_ID` y `SG_API_URL` |
| 400 / 413 / 422 (lote completo) | Reenvía de una en una para aislar la lectura culpable |

Las enviadas se borran del buffer a los 7 días (`SG_PURGE_DAYS`). Las
pendientes, `sin_hora` y rechazadas **nunca** se borran solas.

## Comandos

Se corren desde la **carpeta raíz del repo** (la que contiene `gateway/`):

| Comando | Qué hace |
|---|---|
| `python -m gateway run` | Lee el Arduino real y envía. Es lo que corre el servicio de la Pi |
| `python -m gateway status` | Pendientes, enviadas, rechazadas, reloj, último envío y último error |
| `python -m gateway simulate` | Igual que `run`, pero con un Arduino simulado (para probar sin hardware) |
| `python -m gateway simulate --print` | Solo imprime líneas simuladas, como el Monitor Serie |

`simulate` acepta `--interval 0.5` (más rápido) y `--fallas` (el DHT11 simulado
manda `null` de vez en cuando).

## Configuración (`gateway/.env`)

Copia `.env.example` como `.env` y rellena `SG_API_KEY`. El `.env` **no se
sube a git**. Las variables del entorno ganan sobre las del archivo.

| Variable | Default | Para qué |
|---|---|---|
| `SG_API_URL` | `http://localhost:8000/api/v1` | Backend. En la Pi 5 corre en la misma máquina; si algún día se va a la nube, solo cambia esto |
| `SG_DEVICE_ID` | `pi-vivero-01` | El gateway registrado en el backend |
| `SG_API_KEY` | — (obligatoria) | La que imprime `python -m app.seed` una sola vez |
| `SG_SERIAL_PORT` | `auto` | `auto` usa `/dev/serial/by-id/...` (fijo aunque cambie `ttyACM0`/`ACM1`); en Windows busca el COM del Arduino |
| `SG_BAUD` | `9600` | Igual que el firmware |
| `SG_BATCH_SIZE` | `100` | Lecturas por envío (1 a 500) |
| `SG_SEND_INTERVAL` | `10` | Segundos entre envíos. Si quedan pendientes, manda el siguiente lote de inmediato |
| `SG_HTTP_TIMEOUT` | `10` | Segundos de espera por respuesta |
| `SG_BUFFER_DB` | `gateway/buffer.db` | Buffer SQLite (fuera de git) |
| `SG_CLOCK` | `auto` | `auto`: `timedatectl` en la Pi; si no existe (Windows), confía en el reloj del sistema. `timedatectl` o `system` para forzarlo |
| `SG_PURGE_DAYS` | `7` | Días que se guardan las enviadas |
| `SG_LOG_LEVEL` | `INFO` | `DEBUG` muestra también cada envío de rutina |

## Probarlo en la laptop (Windows, CMD)

Necesitas dos ventanas de **CMD** (no PowerShell).

**Ventana 1: el backend**, como siempre (ver `backend/README.md`):

```bat
cd C:\Users\emili\Documents\Arduino\backend
.venv\Scripts\activate.bat
set SMARTGREENAI_JWT_SECRET=cualquier-frase-larga-secreta
python -m uvicorn app.main:app --reload --port 8000
```

**Ventana 2: el gateway con el Arduino simulado.** Usa el mismo venv del backend:

```bat
cd C:\Users\emili\Documents\Arduino
backend\.venv\Scripts\activate.bat
rem Solo la primera vez:
pip install -r gateway\requirements.txt
copy gateway\.env.example gateway\.env
notepad gateway\.env
rem   → pega la API key en SG_API_KEY=, guarda y cierra
rem Siempre:
python -m gateway simulate
```

Debes ver `Leyendo el Arduino en arduino-simulado`, una línea "primera línea
tras abrir el puerto" ignorada y nada de errores. Entra a
<http://localhost:8000> como `productor`: las lecturas cambian cada pocos
segundos. Detén el gateway con **Ctrl+C** y corre `python -m gateway status`.

> **¿No tienes la API key?** El seed la muestra una sola vez, cuando crea el
> gateway. En desarrollo: detén el backend, borra `backend\smartgreenai.db`,
> vuelve a correr `python -m app.seed` y copia la key nueva (se pierden las
> lecturas de prueba).

Con el **Arduino real** conectado a la laptop: cierra el Monitor Serie del IDE
(solo un programa puede abrir el puerto) y usa `python -m gateway run`. Si no
lo encuentra solo, pon el puerto a mano en el `.env`: `SG_SERIAL_PORT=COM4`.

Fernando (Python de MSYS2): igual, pero con `backend\.venv\bin\python.exe -m ...`
en lugar de activar el venv.

### Demo del corte de red (criterios 2 y 3)

1. Con el gateway corriendo, detén el backend (Ctrl+C en la ventana 1).
2. El log dice `No se pudo enviar ... N lecturas pendientes a salvo en el buffer`
   y `python -m gateway status` (en una tercera ventana) muestra los pendientes
   subiendo.
3. Vuelve a arrancar el backend. En menos de un minuto aparece
   `Conexión con el backend recuperada` y `Lote enviado` hasta que
   `Pendientes: 0`. Las lecturas quedan en la BD con **su hora de lectura**,
   no la del envío.

## En la Raspberry Pi 5

La instalación como servicio (arranca solo al prender la Pi y se reinicia si
falla) está en [`deploy/pi5/README.md`](../deploy/pi5/README.md). Comandos
útiles ahí:

```bash
sudo systemctl status smartgreenai-gateway     # ¿está corriendo?
journalctl -u smartgreenai-gateway -f          # el log en vivo
cd ~/Arduino && gateway/.venv/bin/python -m gateway status
```

## Pruebas

Desde la raíz del repo, con el venv del backend (trae pytest y lo necesario
para la prueba de punta a punta):

```bat
python -m pytest gateway\tests -q
```

64 pruebas: lector Serial (17), reloj (11), buffer (10), configuración (11),
envío (12) y 3 de punta a punta contra el **backend real** (uvicorn en un
subproceso): lecturas que llegan a `/readings/latest`, backend apagado y
prendido con las horas originales, y reloj sin sincronizar que no manda nada
hasta que "llega" NTP. Si el venv no tiene FastAPI, las de punta a punta se
saltan.

## Archivos

```
gateway/
├── __main__.py       comandos run / status / simulate
├── config.py         lee gateway/.env
├── serial_reader.py  lectura del Arduino y reconexión
├── clock.py          NTP (timedatectl) y hora de lecturas sin hora
├── buffer.py         cola SQLite: sin_hora / pendiente / enviada / rechazada
├── sender.py         envío HTTP por lotes, reintentos y respuestas
├── simulator.py      Arduino simulado (mismas reglas que el firmware)
├── requirements.txt  pyserial
├── .env.example      plantilla del .env
└── tests/
```
