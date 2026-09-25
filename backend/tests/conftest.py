"""
Fixtures compartidas por los tests de AUTH-01, AUTH-02, CONF-05 y DASH-01.

test_ingest.py conserva su propio fixture `client`; aquí el cliente se llama
`api` para no chocar con él.
"""

import hashlib
from datetime import datetime, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.database import close_db, init_db
from app.security import hash_password

GREENHOUSE_ID = "vivero-test-01"
OTHER_GREENHOUSE_ID = "vivero-otro-02"
GATEWAY_ID = "pi-test-01"
GATEWAY_API_KEY = "test-secret-key-12345"

PASSWORDS = {
    "producer": "Rabano-Prod-2026!",
    "admin": "Rabano-Admin-2026!",
    "super_admin": "Rabano-Super-2026!",
}

USERS = {
    "producer": {
        "username": "productor",
        "email": "productor@test.local",
        "full_name": "Productor de prueba",
        "greenhouse_id": GREENHOUSE_ID,
    },
    "admin": {
        "username": "admin",
        "email": "admin@test.local",
        "full_name": "Admin de prueba",
        "greenhouse_id": None,
    },
    "super_admin": {
        "username": "superadmin",
        "email": "superadmin@test.local",
        "full_name": "Super de prueba",
        "greenhouse_id": None,
    },
}

# scrypt es lento a propósito: se calcula una sola vez por sesión de tests.
_HASHES = {role: hash_password(pw) for role, pw in PASSWORDS.items()}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def login(api: AsyncClient, role: str) -> str:
    """Inicia sesión con el usuario de prueba del rol y devuelve el JWT."""
    resp = await api.post(
        "/api/v1/auth/login",
        json={"login": USERS[role]["username"], "password": PASSWORDS[role]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def db():
    """BD en memoria con un gateway y un usuario activo por rol."""
    conn = await init_db(":memory:")
    await conn.execute(
        """
        INSERT INTO devices (device_id, greenhouse_id, type, model, location,
                             status, api_key_hash, registered_at)
        VALUES (?, ?, 'gateway', 'Test Pi', 'Mesa de pruebas', 'active', ?, ?)
        """,
        (GATEWAY_ID, GREENHOUSE_ID,
         hashlib.sha256(GATEWAY_API_KEY.encode()).hexdigest(), now_iso()),
    )
    for role, user in USERS.items():
        await conn.execute(
            """
            INSERT INTO users (username, email, full_name, password_hash, role,
                               greenhouse_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (user["username"], user["email"], user["full_name"], _HASHES[role],
             role, user["greenhouse_id"], now_iso()),
        )
    await conn.commit()
    yield conn
    await close_db()


@pytest_asyncio.fixture
async def api(db):
    """Cliente HTTP contra la app real, usando la BD en memoria."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def tokens(api):
    """Un JWT válido por rol: {'producer': ..., 'admin': ..., 'super_admin': ...}."""
    return {role: await login(api, role) for role in USERS}
