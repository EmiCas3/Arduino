# SmartGreenAI Backend

Backend en FastAPI del vivero de rábano. Hoy cubre:

| Item | Qué hace |
|---|---|
| MON-04 | Ingesta de lotes de lecturas desde la Raspberry Pi (`X-API-Key`) |
| AUTH-01 | Login con usuario o email, sesión con JWT y cierre por inactividad |
| AUTH-02 | Control de acceso por rol en todas las rutas (401 / 403) |
| CONF-05 | Registro de actuadores (bomba, ventilador, malla sombra, luz) |
| DASH-01 | API de últimas lecturas por vivero (la UI de widgets sigue pendiente) |

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

## Variables de entorno

| Variable | Default | Para qué |
|---|---|---|
| `SMARTGREENAI_DB` | `smartgreenai.db` | Ruta del archivo SQLite |
| `SMARTGREENAI_JWT_SECRET` | aleatorio al arrancar | Secreto para firmar los JWT. **Defínelo en la demo**: si no, las sesiones se cierran cada vez que reinicias el servidor |
| `SMARTGREENAI_SESSION_IDLE_MINUTES` | `30` | Minutos sin actividad antes de cerrar la sesión |
| `SMARTGREENAI_SESSION_MAX_HOURS` | `8` | Duración máxima de una sesión |
| `SMARTGREENAI_READING_STALE_MINUTES` | `15` | Una lectura más vieja que esto se marca `stale` |
| `SMARTGREENAI_FRONTEND_DIR` | `../frontend` | Carpeta del frontend que se sirve en `/` |
| `SMARTGREENAI_SEED_PASSWORD` | aleatoria | Contraseña conocida para los usuarios del seed (solo demo) |

Puedes ponerlas en un archivo `.env` (está en `.gitignore`) o en la terminal:

```bash
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
| GET | `/greenhouses/{greenhouse_id}/readings/latest` | Bearer | todos (productor: solo su vivero) |
| GET | `/health` (fuera de `/api/v1`) | pública | — |

El contrato completo está en `../openapi.yaml` (v1.1.0).

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

# Ingesta desde la Pi (sin cambios respecto a MON-04)
curl -X POST http://localhost:8000/api/v1/devices/pi-vivero-01/readings \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <api-key-del-seed>" \
  -d '{"readings": [{"ts": "2026-09-13T21:04:22Z", "temp_c": 24.2, "suelo_pct": 62}]}'
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

## Tests

```bash
cd backend
python -m pytest tests/ -v
```

112 tests: ingesta (9), login (22), roles (16), actuadores (44), últimas
lecturas (13) y frontend (9).

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
│   └── routers/
│       ├── ingest.py    # POST readings (MON-04)
│       ├── auth.py      # login / logout / me (AUTH-01)
│       ├── actuators.py # CRUD sin DELETE de actuadores (CONF-05)
│       └── readings.py  # últimas lecturas (DASH-01)
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

No hay migraciones: las tablas se crean con `CREATE TABLE IF NOT EXISTS`. Si
algún día cambia una tabla **existente**, habrá que borrar la BD local o
hacer un `ALTER TABLE` a mano.

## Pendientes conocidos

- **CONF-04** (registro de devices y sensores) es arrastre de Sprint 1: el
  spec define `POST/GET /devices`, pero todavía no están implementados.
- Los errores salen como `{"detail": {"error", "message"}}`, pero el spec
  v1.0.0 documenta `{"error", "message"}` (ver la nota en `openapi.yaml`).
- `test_batch_too_large` espera 422, pero el spec dice 413.
- El spec publicado en SwaggerHub sigue en 1.0.0: hay que subir el
  `openapi.yaml` 1.1.0 a mano.
