"""
Seed script: inserta un dispositivo de prueba para poder probar MON-04
sin necesitar CONF-04 implementado.

Uso:
    cd backend
    python -m app.seed
"""

import asyncio
import secrets

from app.auth import hash_api_key
from app.database import init_db, close_db


SEED_DEVICE = {
    "device_id": "pi-vivero-01",
    "greenhouse_id": "vivero-rabano-01",
    "type": "gateway",
    "model": "Raspberry Pi 1 Model B+ v1.2",
    "location": "Mesa del laboratorio, junto al tanque",
}


async def seed():
    db = await init_db()

    # Verificar si ya existe
    cursor = await db.execute(
        "SELECT device_id FROM devices WHERE device_id = ?",
        (SEED_DEVICE["device_id"],),
    )
    existing = await cursor.fetchone()

    if existing:
        print(f"El dispositivo '{SEED_DEVICE['device_id']}' ya existe. No se insertó.")
        await close_db()
        return

    # Generar API key
    raw_key = secrets.token_urlsafe(32)
    key_hash = hash_api_key(raw_key)

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

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
            key_hash,
            now,
        ),
    )
    await db.commit()
    await close_db()

    print("=" * 60)
    print("Dispositivo de prueba creado:")
    print(f"  device_id:      {SEED_DEVICE['device_id']}")
    print(f"  greenhouse_id:  {SEED_DEVICE['greenhouse_id']}")
    print(f"  type:           {SEED_DEVICE['type']}")
    print()
    print("  API Key (guardar, se muestra UNA sola vez):")
    print(f"  {raw_key}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(seed())
