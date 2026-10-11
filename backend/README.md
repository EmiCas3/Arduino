# SmartGreenAI Backend

Backend en FastAPI del vivero de rábano. Hoy cubre:

| Item | Qué hace |
|---|---|
| MON-04 | Ingesta de lotes de lecturas desde la Raspberry Pi (`X-API-Key`), validadas lectura por lectura y con `ts` en UTC |
| AUTH-01 | Login con usuario o email, sesión con JWT y cierre por inactividad |
| AUTH-02 | Control de acceso por rol en todas las rutas (401 / 403) |
| CONF-05 | Registro de actuadores (bomba, ventilador, malla sombra, luz) |
| DASH-01 | API de últimas lecturas por vivero, lista de viveros y refresco en segundo plano para los widgets del dashboard |

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
| GET | `/greenhouses` | Bearer | todos (productor: solo su vivero) |
| GET | `/greenhouses/{greenhouse_id}/readings/latest` | Bearer | todos (productor: solo su vivero) |
| GET | `/greenhouses/{greenhouse_id}/actuators/status` | Bearer | todos (productor: solo su vivero) |
| GET | `/greenhouses/{greenhouse_id}/alerts` | Bearer | todos (productor: solo su vivero) |
| GET | `/health` (fuera de `/api/v1`) | pública | — |

El contrato completo está en `../openapi.yaml` (v1.4.0).

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
  actualiza el estado de los actuadores (ACT-04) y se evalúan las alertas
  (ALERT-01). Si uno de esos pasos fallara, las lecturas ya quedaron
  guardadas y el lote se responde igual con 202 (el error va al log).

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
**7 códigos de límite** en alertas con historia (`app/firmware.py` dice a qué
sensor y umbral corresponde cada uno):

- Se **crea** en la misma petición de ingesta (cuando el gateway recibe el
  202, ya existe) con sensor, dispositivo, valor, umbral y hora.
- Mientras el código siga llegando se **actualiza la misma** alerta: no hay
  duplicados (índice único "una activa por sensor").
- Se **cierra** cuando el código lleva `SMARTGREENAI_ALERT_CLEAR_SECONDS`
  seguidos sin llegar. Un aviso `*_LEVE` cuenta como "de vuelta".
- Los avisos y las fallas de sensor no crean alertas; un `null` no abre ni
  cierra nada. Las lecturas atrasadas se ignoran.

**Si Fernando cambia un umbral en el `.ino`**, `tests/test_firmware_mirror.py`
falla y dice qué número actualizar en `CONSTANTS` de `app/firmware.py` (es
una línea). El backend no vuelve a comparar valores: solo anota el número en
la alerta.

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

222 tests: ingesta (9), horas y validación parcial de la ingesta (24),
login (22), roles (32, incluye el barrido de todas las rutas), actuadores
(36), últimas lecturas (11), soporte del dashboard (15), estado de los
actuadores (18), alertas (36), espejo de umbrales del firmware (5) y
frontend (14).

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
│   ├── alerts.py        # Motor de alertas (ALERT-01)
│   └── routers/
│       ├── ingest.py    # POST readings (MON-04)
│       ├── auth.py      # login / logout / me (AUTH-01)
│       ├── actuators.py # CRUD sin DELETE de actuadores (CONF-05)
│       ├── readings.py  # viveros visibles y últimas lecturas (DASH-01)
│       ├── control.py   # estado de los equipos (ACT-04)
│       └── alerts.py    # alertas del vivero (ALERT-01)
├── tests/
├── requirements.txt
└── README.md
```

## Base de datos

| Tabla | Item | Notas |
|---|---|---|
| `devices` | MON-04 | Sin cambios |
| `readings` | MON-04 | Sin cambios. Nuevo índice `(greenhouse_id, ts)` para DASH-01 |
| `users` | AUTH-01 | `role` ∈ producer / admin / super_admin; `greenhouse_id` = vivero asignado |
| `sessions` | AUTH-01 | Timeout por inactividad, logout y revocación |
| `actuators` | CONF-05 | Desactivar = `status='inactive'`; no hay DELETE |
| `actuator_states` | ACT-04 | Último `bomba_activa` / `vent_activo` de cada actuador, con la hora de la lectura |
| `alerts` | ALERT-01 | Una fila por alerta; índice único: a lo más una activa por sensor y vivero |

No hay migraciones: las tablas se crean con `CREATE TABLE IF NOT EXISTS`. Si
algún día cambia una tabla **existente**, habrá que borrar la BD local o
hacer un `ALTER TABLE` a mano.

## Pendientes conocidos

- **CONF-04** (registro de devices y sensores) es arrastre de Sprint 1: el
  spec define `POST/GET /devices`, pero todavía no están implementados.
- Los errores salen como `{"detail": {"error", "message"}}`, pero el spec
  v1.0.0 documenta `{"error", "message"}` (ver la nota en `openapi.yaml`).
- `test_batch_too_large` espera 422, pero el spec dice 413.
