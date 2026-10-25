# SmartGreenAI Backend

Backend en FastAPI del vivero de rábano. Hoy cubre:

| Item | Qué hace |
|---|---|
| MON-04 | Ingesta de lotes de lecturas desde la Raspberry Pi (`X-API-Key`), validadas lectura por lectura y con `ts` en UTC |
| AUTH-01 | Login con usuario o email, sesión con JWT y cierre por inactividad |
| AUTH-02 | Control de acceso por rol en todas las rutas (401 / 403) |
| CONF-05 | Registro de actuadores (bomba, ventilador, malla sombra, luz) |
| DASH-01 | API de últimas lecturas por vivero, lista de viveros y refresco en segundo plano para los widgets del dashboard |
| DASH-02 | Histórico de un rango y serie de una medición lista para graficar (promediada por bucket cuando hay demasiados puntos) |
| AI-01 | Análisis de la información histórica: tendencia por variable, con el periodo usado y la calidad del dato |
| AI-02 | Condiciones inusuales: lo que se sale del patrón reciente de su sensor, con valor, rango esperado y hora |

También sirve el frontend (`../frontend`) desde el mismo origen.

## Requisitos

- Python 3.10 o superior (el equipo usa 3.12).
- Nada más: todas las dependencias son puro Python o traen wheel. **No hace
  falta Rust ni compilador** (por eso se usa Pydantic v1, `hashlib.scrypt` de
  la librería estándar para las contraseñas y PyJWT para los tokens).

## Setup rápido

```bash
cd backend

# Crear y activar entorno virtual
python -m venv .venv
.venv\Scripts\activate      # Windows (MSYS2: source .venv/bin/activate)
# source .venv/bin/activate  # Linux/Mac

# Instalar dependencias
pip install -r requirements.txt

# Datos de demo: gateway + un usuario por rol + ventilador y bomba.
# Muestra la API key y las contraseñas UNA sola vez.
python -m app.seed

# Arrancar el servidor
uvicorn app.main:app --reload --port 8000
```

Abre <http://localhost:8000> para la interfaz y <http://localhost:8000/docs>
para probar la API (botón **Authorize** → pega el `access_token` del login).

Si ya tenías una `smartgreenai.db` de antes, no hay que borrarla: las tablas
nuevas se crean solas al arrancar.

## Correr en la Raspberry Pi 5

En la demo, el backend corre en la misma Pi 5 que el gateway (MON-03), como
servicio `smartgreenai-backend` en el puerto 8000. `deploy/pi5/install.sh` crea
el venv, un `backend/.env` con `SMARTGREENAI_JWT_SECRET` fijo y la ruta de la
BD, corre el seed y deja la API key del gateway en `gateway/.env`. La guía paso
a paso desde cero está en [`../deploy/pi5/README.md`](../deploy/pi5/README.md).

## Variables de entorno

| Variable | Default | Para qué |
|---|---|---|
| `SMARTGREENAI_DB` | `smartgreenai.db` | Ruta del archivo SQLite |
| `SMARTGREENAI_JWT_SECRET` | aleatorio al arrancar | Secreto para firmar los JWT. **Defínelo en la demo**: si no, las sesiones se cierran cada vez que reinicias el servidor |
| `SMARTGREENAI_SESSION_IDLE_MINUTES` | `30` | Minutos sin actividad antes de cerrar la sesión |
| `SMARTGREENAI_SESSION_MAX_HOURS` | `8` | Duración máxima de una sesión |
| `SMARTGREENAI_READING_STALE_MINUTES` | `15` | Una lectura más vieja que esto se marca `stale`. En la Pi se usa `2` (la Pi manda cada 10 s) |
| `SMARTGREENAI_DEFAULT_AREA` | `general` | Área que se reporta en las últimas lecturas. El prototipo es un solo ambiente; cuando exista CONF-04 vendrá del registro de sensores |
| `SMARTGREENAI_FRONTEND_DIR` | `../frontend` | Carpeta del frontend que se sirve en `/` |
| `SMARTGREENAI_SEED_PASSWORD` | aleatoria | Contraseña conocida para los usuarios del seed (solo demo) |
| `SMARTGREENAI_MIN_READING_TS` | `2026-01-01T00:00:00Z` | Lecturas con `ts` anterior se rechazan (una Pi sin hora cree que es 1970). Debe llevar zona horaria |
| `SMARTGREENAI_MAX_CLOCK_SKEW_SECONDS` | `300` | Segundos que un `ts` puede ir adelante del reloj del servidor antes de rechazarse |
| `SMARTGREENAI_ALERT_CLEAR_SECONDS` | `60` | Segundos seguidos sin el código de límite para cerrar una alerta (ALERT-01) |
| `SMARTGREENAI_COMMAND_ACK_SECONDS` | `45` | Segundos que se espera que el Arduino confirme una orden antes de marcarla `expired` (ACT-02) |

Puedes ponerlas en un archivo `.env` (está en `.gitignore`) o en la terminal:

```bash
# CMD de Windows
set SMARTGREENAI_JWT_SECRET=cambia-esto-por-algo-largo
# PowerShell
$env:SMARTGREENAI_JWT_SECRET = "cambia-esto-por-algo-largo"
# bash
export SMARTGREENAI_JWT_SECRET="cambia-esto-por-algo-largo"
```

## Endpoints (`/api/v1`)

| Método | Ruta | Autenticación | Roles |
|---|---|---|---|
| POST | `/devices/{device_id}/readings` | `X-API-Key` del gateway | — |
| POST | `/auth/login` | pública | — |
| POST | `/auth/logout` | Bearer | todos |
| GET | `/auth/me` | Bearer | todos |
| POST | `/actuators` | Bearer | admin, super_admin |
| GET | `/actuators` | Bearer | todos (productor: solo activos de su vivero) |
| GET | `/actuators/{actuator_id}` | Bearer | todos (productor: solo activos de su vivero) |
| PATCH | `/actuators/{actuator_id}` | Bearer | admin, super_admin |
| POST | `/actuators/{actuator_id}/commands` | Bearer | todos (productor: solo su vivero) |
| GET | `/greenhouses` | Bearer | todos (productor: solo su vivero) |
| GET | `/greenhouses/{greenhouse_id}/readings/latest` | Bearer | todos (productor: solo su vivero) |
| GET | `/greenhouses/{greenhouse_id}/actuators/status` | Bearer | todos (productor: solo su vivero) |
| GET | `/greenhouses/{greenhouse_id}/alerts` | Bearer | todos (productor: solo su vivero) |
| GET | `/greenhouses/{greenhouse_id}/irrigation` | Bearer | todos (productor: solo su vivero) |
| PUT | `/greenhouses/{greenhouse_id}/irrigation/mode` | Bearer | todos (productor: solo su vivero) |
| GET | `/greenhouses/{greenhouse_id}/actuators/events` | Bearer | todos (productor: solo su vivero) |
| GET | `/greenhouses/{greenhouse_id}/readings` | Bearer | todos (productor: solo su vivero) |
| GET | `/greenhouses/{greenhouse_id}/readings/series` | Bearer | todos (productor: solo su vivero) |
| GET | `/greenhouses/{greenhouse_id}/analysis` | Bearer | todos (productor: solo su vivero) |
| GET | `/greenhouses/{greenhouse_id}/anomalies` | Bearer | todos (productor: solo su vivero) |
| GET | `/health` (fuera de `/api/v1`) | pública | — |

El contrato completo está en `../openapi.yaml` (v1.7.0).

### Reglas de la ingesta (MON-04, ajustadas en Sprint 2)

- **Validación lectura por lectura.** Solo el sobre del lote (1 a 500
  lecturas) da 422. Cada lectura inválida regresa en `rejected` con su índice
  y motivo (`campo: motivo`), y las válidas del mismo lote se guardan.
- **`ts` con zona horaria obligatoria** (`Z` o `-06:00`). Se guarda en UTC con
  milisegundos: `2026-09-26T18:04:22.123+00:00`. El mismo instante escrito en
  dos zonas cuenta como duplicado.
- **Horas imposibles se rechazan:** antes de `SMARTGREENAI_MIN_READING_TS` o
  más de `SMARTGREENAI_MAX_CLOCK_SKEW_SECONDS` en el futuro.
- **El backend nunca pone la hora de la lectura**; solo agrega `received_at`.
- Los campos que el contrato no define se ignoran sin rechazar la lectura.
- **Después de guardar**, con cada lectura NUEVA y en orden de hora, se
  actualiza el estado de los actuadores (ACT-04), se procesa el riego
  (ACT-02) y se evalúan las alertas (ALERT-01). Si uno de esos pasos
  fallara, las lecturas ya quedaron guardadas y el lote se responde igual
  con 202 (el error va al log).
- **La respuesta 202 trae `commands`**: las órdenes pendientes de ese
  gateway para el Arduino (ACT-02). También en un lote de puras duplicadas.

### Estado de los actuadores (ACT-04)

`GET /greenhouses/{id}/actuators/status` devuelve cada actuador activo con
`state` y su motivo (`reason`):

| `state` | Cuándo |
|---|---|
| `on` / `off` | El Arduino lo confirmó hace menos de `SMARTGREENAI_READING_STALE_MINUTES` |
| `unavailable` | Bomba bloqueada por el firmware (tanque crítico) o gateway desactivado |
| `unknown` | Nunca lo reportó, la confirmación es vieja, no tiene gateway, o es malla/iluminación (el firmware no las reporta) |

Un equipo sin confirmación **nunca** sale como `off`. "Encendido" es lo que el
firmware ordenó en el pin: no mide si el motor gira.

### Alertas (ALERT-01)

Los umbrales del rábano viven en el firmware (`vivero_sensoresV2.ino`): el
Arduino compara cada lectura y manda códigos. `app/alerts.py` convierte los
**9 códigos de límite** en alertas con historia (`app/firmware.py` dice a qué
sensor y umbral corresponde cada uno): `TEMP_ALTA/BAJA`, `HUM_AIRE_ALTA/BAJA`,
`SUELO_ENCHARCADO/SECO`, `TANQUE_UN_CUARTO` y, desde NF-01, `CO2_ALTO/BAJO`
(límites 1500 y 300 ppm):

- Se **crea** en la misma petición de ingesta (cuando el gateway recibe el
  202, ya existe) con sensor, dispositivo, valor, umbral y hora.
- Mientras el código siga llegando se **actualiza la misma** alerta: no hay
  duplicados (índice único "una activa por sensor").
- Se **cierra** cuando el código lleva `SMARTGREENAI_ALERT_CLEAR_SECONDS`
  seguidos sin llegar. Un aviso `*_LEVE` cuenta como "de vuelta".
- Los avisos y las fallas de sensor no crean alertas; un `null` no abre ni
  cierra nada. Las lecturas atrasadas se ignoran.

**Si alguien cambia un umbral en el `.ino`**, `tests/test_firmware_mirror.py`
falla y dice qué número actualizar en `CONSTANTS` de `app/firmware.py` (es
una línea). El backend no vuelve a comparar valores: solo anota el número en
la alerta.

### Histórico y gráficas (DASH-02)

Dos formas de leer lo mismo, las dos con el rango en `from` / `to` (ISO 8601
**con zona**; sin ellos, las últimas 24 h). Un rango invertido, de más de 90
días, con una fecha sin zona o en los extremos del calendario → 422
`invalid_range` (sin zona se rechaza igual que en la ingesta: tomarla como UTC
correría 6 h el rango de quien escribió su hora de México).

| Endpoint | Qué devuelve |
|---|---|
| `GET /greenhouses/{id}/readings` | Las lecturas guardadas del rango, de la más nueva a la más vieja (`limit`, 200 por defecto). El dato crudo |
| `GET /greenhouses/{id}/readings/series?metric=temp_c` | UNA medición lista para dibujar |

**Por qué la serie va aparte.** El Arduino imprime una lectura cada 2 s, así
que un día son ~43 000 lecturas: mandarlas al navegador deja una mancha, no
una gráfica. `/readings/series` devuelve las lecturas tal cual solo si caben
en `max_points` (240 por defecto); si no, las promedia por bucket y cada
punto trae además el **mínimo y el máximo reales** de ese bucket, para que
promediar no esconda un pico. `bucket_seconds` dice de cuánto quedó el paso y
`aggregated` si hubo que promediar. **Nunca** devuelve más de `max_points`
puntos, aunque las lecturas lleguen más seguido que cada 2 s.

Los buckets se alinean con la época Unix (múltiplos exactos de su tamaño), no
con el inicio del rango: dos consultas del mismo periodo caen en los mismos
buckets, que es lo que hace reproducible el análisis de AI-01. Por eso un
rango de `n` buckets de largo toca `n + 1` (el primero y el último a medias):
`choose_bucket()` cuenta ese de más, y `history.grid()` es la rejilla que
cubre el rango completo — el análisis la usa para no dejar fuera el último
bucket, que es el de las lecturas más nuevas.

**Costo.** Agrupar por bucket obliga a SQLite a leer la hora de cada lectura:
30 días son ~1.3 M lecturas y ~1–1.5 s por consulta en una laptop (más en la
Pi), con la única conexión a la BD. Por eso la serie y el análisis hacen
**una** consulta cada uno (la cuenta sale de sumar los buckets) y la página
refresca los rangos largos más despacio (7 días cada 5 min, 30 días cada
15 min).

Un rango válido **sin lecturas no es un error**: responde 200 con
`has_data: false` y `points: []`, para que la página muestre un aviso en vez
de una gráfica vacía.

La elección del rango y del bucket vive en `app/history.py`, aparte de los
routers, y el catálogo de mediciones (columna, etiqueta, unidad, decimales)
en `app/metrics.py`: es la única fuente de esos nombres, y el nombre de
columna que entra a una consulta SQL sale **siempre** de ahí, nunca del
texto que mandó el cliente.

### Análisis histórico (AI-01)

`GET /greenhouses/{id}/analysis` devuelve, por cada medición: si va
`rising`, `falling`, `stable` o `insufficient_data`, la pendiente por hora,
el cambio estimado en el periodo, primer y último valor, promedio, mínimo,
máximo y la calidad del dato.

El cálculo vive en `app/analysis.py`, en funciones puras:

1. **Preprocesado** (`prepare`): las lecturas en `null` quedan fuera (es un
   sensor caído, no un cero), lo demás se promedia por bucket y los buckets
   sin lectura **no se rellenan**: se devuelven como `gaps`. Rellenar un
   hueco con el valor anterior inventaría una tendencia que nadie midió.
   Los huecos se recortan al periodo pedido (dicen cuánto faltó DENTRO de
   él) y el último bucket cuenta aunque vaya a medias: ahí están las
   lecturas más nuevas.
2. **Tendencia** (`trend`): mínimos cuadrados sobre esa serie. `stable` no es
   "pendiente cero": es que el cambio del periodo no pasa de `stable_band`,
   la banda de ruido de esa medición (`app/metrics.py`). Sin ella, una décima
   de grado en seis horas se reportaría como "la temperatura está subiendo".

Qué mirar en la respuesta:

| Campo | Para qué |
|---|---|
| `period` | El periodo y el bucket que se usaron: el indicador no significa nada sin ellos |
| `coverage_pct`, `gaps` | Qué parte del periodo se midió de verdad y dónde faltan datos |
| `readings_missing` | Lecturas del rango que traían esa medición en `null` |
| `fit_r2` | 0 a 1: qué tan recta es la serie |

**`fit_r2` bajo no es un error.** Una recta sobre 24 h de temperatura explica
poco, porque el día y la noche son un ciclo, no una línea: el `r2` lo dice en
vez de esconderlo. Para ventanas largas conviene mirar rangos más cortos; el
análisis que entiende el ciclo y cruza variables es AI-04.

**Reproducible** (criterio del item): el resultado sale solo de las lecturas
guardadas y del periodo pedido. No hay azar ni se mira el reloj;
`generated_at` es metadato de la respuesta y no entra en el cálculo.

Aquí **no** se redacta ninguna recomendación ni explicación: eso es AI-05.

Los umbrales del rábano siguen viviendo en el firmware. `stable_band` es un
parámetro de análisis, no un umbral agronómico, y está pendiente de ajustar
con datos de varios días.

### Condiciones inusuales (AI-02)

`GET /greenhouses/{id}/anomalies` devuelve las veces que una medición **se
salió de lo que su sensor venía haciendo**: sensor, valor, rango esperado y
hora (criterio 1). Una anomalía **no es una alerta**: la alerta dice "cruzó
el límite del rábano" (fijo, en el firmware); la anomalía dice "esto no es lo
de siempre", aunque siga dentro de los límites. Es el aviso temprano.

La regla vive en `app/anomalies.py`, en funciones puras, y trabaja sobre la
misma serie por bucket que deja `analysis.prepare` (AI-01):

1. **Patrón reciente**: los buckets con dato de los 12 anteriores (una
   ventana de tiempo; tras un hueco largo no hay patrón y no se juzga).
   Hacen falta al menos 6.
2. **Rango esperado**: mediana ± 3.5 × MAD × 1.4826 (el "z-score
   modificado"). Mediana y MAD porque una lectura rara dentro del patrón casi
   no los mueve. El ancho nunca baja de `anomaly_floor` (`app/metrics.py`):
   un sensor muy quieto tendría un rango de ±0.
3. **Anomalía**: un bucket fuera de su rango; dentro, nada (criterio 3). Los
   buckets seguidos fuera de rango son una sola anomalía, con su pico.

Qué **no** sale como anomalía:

| Caso | Por qué |
|---|---|
| Subida o bajada gradual (el día que se calienta) | Es un patrón: la dispersión del tramo cubre el retraso de la mediana |
| Un cambio de nivel que ya duró media ventana | Ya es "lo nuevo normal": sale una vez y luego no |
| Tierra que se moja / tanque que baja con la bomba encendida (o hasta 10 min después) | Lo explica el riego (`actuator_events` de ACT-02); se cuenta en `explained_by_irrigation` |
| Sensor en `null` | Es un sensor caído: eso lo dice `FALLA_*` en las alertas |
| Luz | No se revisa (`checked: false`): sube y baja sola con el sol y las nubes |

Para que los primeros minutos del periodo tengan patrón, se leen también las
lecturas de justo antes de `from`. **Reproducible** igual que AI-01: mismas
lecturas y mismo rango, mismas anomalías con el mismo `id`.

`anomaly_floor` (°C 1.0 · humedad del aire 4 % · tierra 4 % · tanque 4 % ·
CO₂ 60 ppm) también es un parámetro de análisis, pendiente de ajustar con
datos reales de varios días, igual que `stable_band`.

### Ejemplos

```bash
# Login (usuario o email)
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"login": "admin", "password": "<contraseña-del-seed>"}'
# → {"access_token": "eyJ...", "token_type": "bearer", "user": {"role": "admin", ...}}

# Registrar un actuador (solo admin / super_admin)
curl -X POST http://localhost:8000/api/v1/actuators \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"actuator_id": "malla-sombra-01", "name": "Malla sombra", "type": "shade",
       "greenhouse_id": "vivero-rabano-01", "area": "general", "control_channel": "SERVO/D9"}'

# Desactivar (no se borra: conserva su historial)
curl -X PATCH http://localhost:8000/api/v1/actuators/malla-sombra-01 \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" -d '{"status": "inactive"}'

# Últimas lecturas del vivero
curl http://localhost:8000/api/v1/greenhouses/vivero-rabano-01/readings/latest \
  -H "Authorization: Bearer <access_token>"

# Ingesta desde la Pi: ts con zona horaria; las lecturas malas van a "rejected"
curl -X POST http://localhost:8000/api/v1/devices/pi-vivero-01/readings \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <api-key-del-seed>" \
  -d '{"readings": [{"ts": "2026-09-13T21:04:22.123Z", "temp_c": 24.2, "suelo_pct": 62},
                    {"ts": "2026-09-13T21:04:24", "temp_c": 24.3}]}'
# → {"accepted": 1, "duplicates": 0,
#    "rejected": [{"index": 1, "reason": "ts: sin zona horaria: agrega Z (UTC) o un desfase como -06:00"}]}
```

### Riego (ACT-02)

El **Arduino** decide el riego automático con sus constantes (objetivo
`SUELO_LIMITE_MIN` 50 %, máximo `BOMBA_MAX_MS` 60 s, descanso
`BOMBA_DESCANSO_MS` 5 min). La **plataforma** (`app/irrigation.py`):

- **Guarda el modo** que eligió una persona (`irrigation_mode`; sin fila =
  `auto`, igual que el Arduino al arrancar) y se lo manda al Arduino.
- **Manda órdenes manuales** a la bomba, solo en modo manual. Cada orden se
  guarda `pending`, sale en el 202 del siguiente envío del gateway (`sent`)
  y el Arduino la confirma con `cmd_id` (`confirmed`) o la rechaza con
  `cmd_err` (`rejected`). Sin confirmación en 45 s: `expired`.
- **Re-sincroniza el modo**: si el Arduino reporta otro modo (se reinició al
  abrir el puerto), le vuelve a mandar el elegido (orden `system`). Un cambio
  de modo solo cuenta como confirmado si el `modo` que reporta el Arduino
  coincide; si no, se perdió (`expired`) y se manda otro.
- **Registra cada encendido/apagado** (`actuator_events`) con su origen:
  `auto` si lo decidió el firmware, `manual` si fue una persona (con su
  nombre), y el motivo (`bomba_paro` del Arduino o, con el firmware de antes,
  deducido de la lectura). Lo que provoca un reinicio del Arduino o una
  re-sincronización del modo queda como `auto`, con ese motivo.
- Si el firmware **no manda `modo`**, no acepta órdenes: no se genera
  ninguna y las rutas de control responden 409 `firmware_without_control`.
  El registro de acciones funciona igual (todo `auto`).

El protocolo completo con el Arduino está en `../gateway/PROTOCOLO_ARDUINO.md`.

## Cómo funciona la seguridad

- **Contraseñas:** `hashlib.scrypt` con salt (`scrypt$n=16384,r=8,p=1$...`).
  Nunca se guardan ni se devuelven en texto plano. `needs_rehash()` deja
  lista la migración a bcrypt/argon2 de NFR-03 (Sprint 4).
- **Sesión:** el login crea una fila en `sessions` y firma un JWT
  `{sub, sid, exp}`. En cada request `get_current_user` revisa la firma, que
  la sesión no esté revocada ni vencida y que no lleve más de 30 min sin
  actividad. Logout = revocar la sesión.
- **Login inválido:** usuario inexistente, contraseña incorrecta y usuario
  inactivo responden el mismo `401 invalid_credentials`, tardan lo mismo y
  no crean sesión.
- **Roles (AUTH-02):** cada endpoint declara sus roles con
  `require_roles(...)`, que corre antes del handler. Rol insuficiente → 403 y
  la acción no se ejecuta. Sin token o token vencido → 401. El rol se lee de
  la BD en cada request, nunca del token.
- **Todas las rutas protegidas:** `tests/test_roles.py` recorre `app.routes`
  y falla si una ruta nueva no exige sesión ni declara roles.
- **Refresco en segundo plano (DASH-01):** el dashboard pide lecturas solo
  cada 15 s. Esas peticiones traen `X-SG-Background: 1`: se validan igual
  (401 si la sesión ya venció) pero **no renuevan** `last_seen_at`. Sin eso,
  una pestaña abierta mantendría viva la sesión para siempre y el cierre por
  inactividad dejaría de funcionar. El header nunca da más acceso.

## Tests

```bash
cd backend
python -m pytest tests/ -v
```

425 tests: ingesta (9), horas y validación parcial de la ingesta (24),
login (22), roles (48, incluye el barrido de todas las rutas), actuadores
(36), últimas lecturas (11), soporte del dashboard (15), estado de los
actuadores (18), alertas (41), espejo de las constantes del firmware (5),
riego (31), frontend (19), histórico y gráficas (44), análisis (38), sensor
de CO₂ (23, `test_co2.py`: ingesta, alertas, dashboard y la BD vieja de la
Pi que gana la columna) y anomalías (41, `test_anomalies.py`: la regla pura,
el riego que las explica y el endpoint).

La lógica del frontend se prueba aparte, sin navegador ni `npm install`:

```bash
node --test frontend/tests/metrics.test.mjs frontend/tests/series.test.mjs
```

## Estructura

```
backend/
├── app/
│   ├── main.py          # FastAPI: routers + frontend estático
│   ├── config.py        # Variables de entorno SMARTGREENAI_*
│   ├── database.py      # SQLite async y creación de tablas
│   ├── models.py        # Schemas Pydantic v1
│   ├── auth.py          # Autenticación de DISPOSITIVOS (X-API-Key)
│   ├── security.py      # Hash de contraseñas (scrypt) y JWT
│   ├── dependencies.py  # get_current_user, require_roles (USUARIOS)
│   ├── seed.py          # Datos de demo
│   ├── actuator_state.py # Estado reportado de cada actuador (ACT-04)
│   ├── firmware.py      # Espejo de los umbrales del .ino (solo lectura)
│   ├── metrics.py       # Catálogo de mediciones: columna, etiqueta, unidad
│   ├── history.py       # Rango y resolución de una consulta histórica (DASH-02 / AI-01)
│   ├── analysis.py      # Preprocesado y tendencias, en funciones puras (AI-01)
│   ├── anomalies.py     # Detección de condiciones inusuales, en funciones puras (AI-02)
│   ├── alerts.py        # Motor de alertas (ALERT-01)
│   ├── irrigation.py    # Riego: modo, órdenes, confirmaciones y registro (ACT-02)
│   └── routers/
│       ├── ingest.py    # POST readings (MON-04)
│       ├── auth.py      # login / logout / me (AUTH-01)
│       ├── actuators.py # CRUD sin DELETE (CONF-05) y órdenes manuales (ACT-02)
│       ├── readings.py  # viveros, últimas lecturas (DASH-01) e histórico (DASH-02)
│       ├── analysis.py  # tendencias (AI-01) y anomalías (AI-02) del vivero
│       ├── control.py   # estado de los equipos (ACT-04) y riego (ACT-02)
│       └── alerts.py    # alertas del vivero (ALERT-01)
├── tests/
├── requirements.txt
└── README.md
```

## Base de datos

| Tabla | Item | Notas |
|---|---|---|
| `devices` | MON-04 | Sin cambios |
| `readings` | MON-04 | El índice `(greenhouse_id, ts)` sirve a DASH-01 y a las consultas por rango de DASH-02 / AI-01. **NF-01 agrega `co2_ppm`** (ppm, `null` = sensor caído): en una BD de antes del CO₂, `init_db()` la agrega con `ALTER TABLE … ADD COLUMN` una sola vez; es la única columna que se ha agregado a una tabla existente |
| `users` | AUTH-01 | `role` ∈ producer / admin / super_admin; `greenhouse_id` = vivero asignado |
| `sessions` | AUTH-01 | Timeout por inactividad, logout y revocación |
| `actuators` | CONF-05 | Desactivar = `status='inactive'`; no hay DELETE |
| `actuator_states` | ACT-04 | Último `bomba_activa` / `vent_activo` de cada actuador, con la hora de la lectura |
| `alerts` | ALERT-01 | Una fila por alerta; índice único: a lo más una activa por sensor y vivero |
| `controller_status` | ACT-02 | Lo último que reportó el firmware de cada gateway: `modo`, `cmd_id`, `cmd_err` |
| `irrigation_mode` | ACT-02 | Modo elegido por vivero, con quién y cuándo (sin fila = `auto`) |
| `actuator_commands` | ACT-02 | Órdenes al Arduino y su estado (pending → sent → confirmed / rejected / expired) |
| `actuator_events` | ACT-02 | Cada encendido/apagado con origen (`auto`/`manual`), motivo y usuario |

No hay migraciones: las tablas se crean con `CREATE TABLE IF NOT EXISTS`. Si
algún día cambia una tabla **existente**, habrá que borrar la BD local o
hacer un `ALTER TABLE` a mano.

## Pendientes conocidos

- **CONF-04** (registro de devices y sensores) es arrastre de Sprint 1: el
  spec define `POST/GET /devices`, pero todavía no están implementados.
- Los errores salen como `{"detail": {"error", "message"}}`, pero el spec
  v1.0.0 documenta `{"error", "message"}` (ver la nota en `openapi.yaml`).
- `test_batch_too_large` espera 422, pero el spec dice 413.
