"""
Conexión a SQLite y creación de tablas.

Usa aiosqlite para operaciones async compatibles con FastAPI.
La BD se crea en backend/smartgreenai.db por defecto.

No hay herramienta de migraciones: las tablas se crean con
CREATE TABLE IF NOT EXISTS, que NO altera tablas que ya existen. Por eso los
items nuevos solo AGREGAN tablas e índices; devices y readings no se tocan.
"""

from typing import Optional

import aiosqlite
import os

DB_PATH = os.environ.get("SMARTGREENAI_DB", "smartgreenai.db")

# Referencia global a la conexión (se inicializa en lifespan)
_db: Optional[aiosqlite.Connection] = None


async def get_db() -> aiosqlite.Connection:
    """Dependencia de FastAPI para obtener la conexión a la DB."""
    if _db is None:
        raise RuntimeError("La base de datos no está inicializada")
    return _db


async def init_db(db_path: Optional[str] = None) -> aiosqlite.Connection:
    """Crea la conexión y las tablas si no existen."""
    global _db
    path = db_path or DB_PATH
    _db = await aiosqlite.connect(path)
    _db.row_factory = aiosqlite.Row

    # Habilitar WAL para mejor rendimiento con lecturas concurrentes
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA foreign_keys=ON")

    await _db.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            device_id     TEXT PRIMARY KEY,
            greenhouse_id TEXT NOT NULL,
            type          TEXT NOT NULL CHECK(type IN ('gateway','sensor','actuator')),
            model         TEXT,
            location      TEXT,
            status        TEXT NOT NULL DEFAULT 'active'
                          CHECK(status IN ('active','inactive')),
            api_key_hash  TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            last_seen_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS readings (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id        TEXT    NOT NULL REFERENCES devices(device_id),
            greenhouse_id    TEXT    NOT NULL,
            received_at      TEXT    NOT NULL,
            ts               TEXT    NOT NULL,
            ms               INTEGER,
            temp_c           REAL,
            hum_aire_pct     REAL,
            suelo_raw        INTEGER,
            suelo_pct        INTEGER,
            nivel_raw        INTEGER,
            nivel_pct        INTEGER,
            luz_raw          INTEGER,
            luz_nivel        INTEGER,
            es_dia           INTEGER,
            sol_min_hoy      INTEGER,
            sol_min_prev     INTEGER,
            riego_sugerido   INTEGER,
            bomba_habilitada INTEGER,
            vent             TEXT,
            estado           TEXT,
            alertas          TEXT,
            UNIQUE(device_id, ts)
        );

        -- Usuarios de la plataforma (AUTH-01 / AUTH-02).
        -- greenhouse_id: vivero asignado a un productor (NULL = todos, para admins).
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
            email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
            full_name     TEXT,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL
                          CHECK(role IN ('producer','admin','super_admin')),
            greenhouse_id TEXT,
            status        TEXT NOT NULL DEFAULT 'active'
                          CHECK(status IN ('active','inactive')),
            created_at    TEXT NOT NULL,
            last_login_at TEXT
        );

        -- Sesiones: permiten el timeout por inactividad, el logout real y,
        -- mas adelante, revocar sesiones al desactivar usuarios (AUTH-03).
        CREATE TABLE IF NOT EXISTS sessions (
            session_id   TEXT PRIMARY KEY,
            user_id      INTEGER NOT NULL REFERENCES users(id),
            created_at   TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            expires_at   TEXT NOT NULL,
            revoked_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);

        -- Actuadores registrados por un Administrador (CONF-05).
        -- Tabla propia: los actuadores no se autentican (los maneja el Arduino
        -- via L298N) y devices.api_key_hash es NOT NULL.
        -- Desactivar = status 'inactive'. No hay DELETE ni ON DELETE CASCADE
        -- para conservar el historial de eventos (ACT-02 / ACT-04 / ACT-06).
        CREATE TABLE IF NOT EXISTS actuators (
            actuator_id       TEXT PRIMARY KEY,
            name              TEXT NOT NULL,
            type              TEXT NOT NULL
                              CHECK(type IN ('pump','fan','shade','light')),
            greenhouse_id     TEXT NOT NULL,
            area              TEXT NOT NULL,
            control_channel   TEXT NOT NULL,
            gateway_device_id TEXT REFERENCES devices(device_id),
            model             TEXT,
            status            TEXT NOT NULL DEFAULT 'active'
                              CHECK(status IN ('active','inactive')),
            registered_at     TEXT NOT NULL,
            registered_by     INTEGER REFERENCES users(id),
            updated_at        TEXT,
            deactivated_at    TEXT
        );
        -- Un canal fisico no puede estar ocupado por dos actuadores ACTIVOS
        -- del mismo vivero.
        CREATE UNIQUE INDEX IF NOT EXISTS ux_actuators_active_channel
            ON actuators(greenhouse_id, control_channel) WHERE status = 'active';
    """)
    await _db.commit()
    return _db


async def close_db():
    """Cierra la conexión a la DB."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
