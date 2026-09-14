"""
Conexión a SQLite y creación de tablas.

Usa aiosqlite para operaciones async compatibles con FastAPI.
La BD se crea en backend/smartgreenai.db por defecto.
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
    """)
    await _db.commit()
    return _db


async def close_db():
    """Cierra la conexión a la DB."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
