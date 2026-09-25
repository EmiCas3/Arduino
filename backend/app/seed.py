"""
Seed script: datos de prueba para desarrollo y para la demo.

Crea, SOLO si no existen (se puede correr las veces que quieras):
  1. El gateway pi-vivero-01 y su API key (MON-04).
  2. Un usuario por rol: productor, admin y superadmin (AUTH-01 / AUTH-02).

Las API keys y contraseñas se generan al azar y se muestran UNA sola vez.
Si prefieres una contraseña conocida para los usuarios de demo, define
SMARTGREENAI_SEED_PASSWORD antes de correrlo.

Uso:
    cd backend
    python -m app.seed
"""

import asyncio
import os
import secrets
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import aiosqlite

from app.auth import hash_api_key
from app.database import close_db, init_db
from app.security import hash_password


SEED_DEVICE = {
    "device_id": "pi-vivero-01",
    "greenhouse_id": "vivero-rabano-01",
    "type": "gateway",
    "model": "Raspberry Pi 1 Model B+ v1.2",
    "location": "Mesa del laboratorio, junto al tanque",
}

SEED_USERS = [
    {
        "username": "productor",
        "email": "productor@smartgreen.local",
        "full_name": "Productor de rábano",
        "role": "producer",
        "greenhouse_id": "vivero-rabano-01",
    },
    {
        "username": "admin",
        "email": "admin@smartgreen.local",
        "full_name": "Administrador del vivero",
        "role": "admin",
        "greenhouse_id": None,
    },
    {
        "username": "superadmin",
        "email": "superadmin@smartgreen.local",
        "full_name": "Super Administrador",
        "role": "super_admin",
        "greenhouse_id": None,
    },
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def seed_device(db: aiosqlite.Connection) -> Optional[str]:
    """Crea el gateway de prueba. Devuelve la API key nueva o None si ya existía."""
    cursor = await db.execute(
        "SELECT device_id FROM devices WHERE device_id = ?",
        (SEED_DEVICE["device_id"],),
    )
    if await cursor.fetchone():
        return None

    raw_key = secrets.token_urlsafe(32)
    await db.execute(
        """
        INSERT INTO devices (device_id, greenhouse_id, type, model, location,
                             status, api_key_hash, registered_at)
        VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        (
            SEED_DEVICE["device_id"],
            SEED_DEVICE["greenhouse_id"],
            SEED_DEVICE["type"],
            SEED_DEVICE["model"],
            SEED_DEVICE["location"],
            hash_api_key(raw_key),
            _now(),
        ),
    )
    await db.commit()
    return raw_key


async def seed_users(db: aiosqlite.Connection) -> List[Tuple[str, str, str]]:
    """Crea los usuarios de demo que falten. Devuelve [(usuario, rol, contraseña)]."""
    shared_password = os.environ.get("SMARTGREENAI_SEED_PASSWORD", "").strip() or None
    created = []
    for user in SEED_USERS:
        cursor = await db.execute(
            "SELECT id FROM users WHERE username = ? OR email = ?",
            (user["username"], user["email"]),
        )
        if await cursor.fetchone():
            continue

        password = shared_password or secrets.token_urlsafe(12)
        await db.execute(
            """
            INSERT INTO users (username, email, full_name, password_hash, role,
                               greenhouse_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                user["username"],
                user["email"],
                user["full_name"],
                hash_password(password),
                user["role"],
                user["greenhouse_id"],
                _now(),
            ),
        )
        created.append((user["username"], user["role"], password))
    await db.commit()
    return created


async def seed() -> None:
    db = await init_db()
    try:
        api_key = await seed_device(db)
        users = await seed_users(db)
    finally:
        await close_db()

    print("=" * 64)
    if api_key:
        print("Gateway de prueba creado:")
        print(f"  device_id:      {SEED_DEVICE['device_id']}")
        print(f"  greenhouse_id:  {SEED_DEVICE['greenhouse_id']}")
        print("  API Key (guárdala, se muestra UNA sola vez):")
        print(f"  {api_key}")
    else:
        print(f"El gateway '{SEED_DEVICE['device_id']}' ya existía. No se modificó.")
    print("-" * 64)
    if users:
        print("Usuarios creados (guarda las contraseñas, se muestran UNA sola vez):")
        for username, role, password in users:
            print(f"  {username:<12} rol={role:<12} contraseña={password}")
    else:
        print("Los usuarios de demo ya existían. No se modificaron.")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(seed())
