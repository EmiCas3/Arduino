"""
Autenticación por API Key (header X-API-Key).

Para el prototipo se usa SHA-256 sin salt. No es apto para producción real
pero cumple con el contrato del openapi.yaml.
"""

import hashlib
from typing import Optional

from fastapi import Depends, Header, HTTPException, Path

import aiosqlite

from app.database import get_db


def hash_api_key(raw_key: str) -> str:
    """Genera el hash SHA-256 de una API key en texto plano."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def verify_api_key(
    device_id: str = Path(..., pattern=r"^[a-z0-9][a-z0-9-]{2,49}$"),
    x_api_key: Optional[str] = Header(None),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """
    Dependencia de FastAPI que:
    1. Verifica que el header X-API-Key esté presente.
    2. Busca un dispositivo cuyo api_key_hash coincida.
    3. Verifica que el device_id del path coincida con el de la key.
    4. Retorna los datos del dispositivo como dict.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_api_key", "message": "Header X-API-Key requerido"},
        )

    key_hash = hash_api_key(x_api_key)

    # Buscar el dispositivo por device_id
    cursor = await db.execute(
        "SELECT * FROM devices WHERE device_id = ?", (device_id,)
    )
    device = await cursor.fetchone()

    if device is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "device_not_registered",
                "message": f"El dispositivo {device_id} no está registrado",
            },
        )

    # Verificar que la API key corresponda a este dispositivo
    if device["api_key_hash"] != key_hash:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_api_key",
                "message": "API key inválida o no corresponde a este dispositivo",
            },
        )

    if device["status"] != "active":
        raise HTTPException(
            status_code=401,
            detail={
                "error": "device_inactive",
                "message": f"El dispositivo {device_id} está inactivo",
            },
        )

    return dict(device)
