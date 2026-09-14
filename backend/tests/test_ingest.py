"""
Tests para el endpoint POST /api/v1/devices/{device_id}/readings (MON-04).

Cubre: autenticación, validación, idempotencia, validación parcial,
campos nullable y límite de lote.
"""

import hashlib
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.database import init_db, close_db


# ── Helpers ────────────────────────────────────────────────────────────

TEST_DEVICE_ID = "pi-test-01"
TEST_GREENHOUSE = "vivero-test-01"
TEST_API_KEY = "test-secret-key-12345"
TEST_API_KEY_HASH = hashlib.sha256(TEST_API_KEY.encode()).hexdigest()

INGEST_URL = f"/api/v1/devices/{TEST_DEVICE_ID}/readings"


def make_reading(ts: str = "2026-09-13T21:04:22Z", **overrides) -> dict:
    """Genera una lectura válida de ejemplo."""
    base = {
        "ts": ts,
        "ms": 148022,
        "temp_c": 24.2,
        "hum_aire_pct": 72.0,
        "suelo_raw": 380,
        "suelo_pct": 62,
        "nivel_raw": 150,
        "nivel_pct": 75,
        "luz_raw": 306,
        "luz_nivel": 717,
        "es_dia": True,
        "sol_min_hoy": 42,
        "sol_min_prev": None,
        "riego_sugerido": False,
        "bomba_habilitada": True,
        "vent": "BASE",
        "estado": "OK",
        "alertas": [],
    }
    base.update(overrides)
    return base


# ── Fixtures ───────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client():
    """Crea un cliente de test con DB in-memory y un dispositivo seed."""
    # Inicializar DB en memoria
    db = await init_db(":memory:")

    # Insertar dispositivo de prueba
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO devices (device_id, greenhouse_id, type, model, location,
                             status, api_key_hash, registered_at)
        VALUES (?, ?, 'gateway', 'Test Pi', 'Lab', 'active', ?, ?)
        """,
        (TEST_DEVICE_ID, TEST_GREENHOUSE, TEST_API_KEY_HASH, now),
    )
    await db.commit()

    # Importar app DESPUÉS de init_db para que use la conexión in-memory
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await close_db()


# ── Tests ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_batch(client):
    """Caso 1: Lote válido de 3 lecturas → 202, accepted=3."""
    readings = [
        make_reading(ts=f"2026-09-13T21:04:{22 + i:02d}Z")
        for i in range(3)
    ]

    resp = await client.post(
        INGEST_URL,
        json={"readings": readings},
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert resp.status_code == 202
    data = resp.json()
    assert data["accepted"] == 3
    assert data["duplicates"] == 0
    assert data["rejected"] == []


@pytest.mark.asyncio
async def test_missing_api_key(client):
    """Caso 2: Sin header X-API-Key → 401."""
    resp = await client.post(
        INGEST_URL,
        json={"readings": [make_reading()]},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_wrong_api_key(client):
    """Caso 3: API key inválida → 401."""
    resp = await client.post(
        INGEST_URL,
        json={"readings": [make_reading()]},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_nonexistent_device(client):
    """Caso 4: device_id que no existe → 404."""
    resp = await client.post(
        "/api/v1/devices/pi-no-existe/readings",
        json={"readings": [make_reading()]},
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_batch_too_large(client):
    """Caso 5: Lote > 500 lecturas → 422 (Pydantic valida max_items)."""
    readings = [
        make_reading(ts=f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z")
        for i in range(501)
    ]

    resp = await client.post(
        INGEST_URL,
        json={"readings": readings},
        headers={"X-API-Key": TEST_API_KEY},
    )
    # Pydantic rechaza con 422 antes de llegar al handler
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_readings(client):
    """Caso 6: Enviar el mismo ts dos veces → duplicates > 0."""
    reading = make_reading(ts="2026-09-13T22:00:00Z")

    # Primera vez
    resp1 = await client.post(
        INGEST_URL,
        json={"readings": [reading]},
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert resp1.status_code == 202
    assert resp1.json()["accepted"] == 1

    # Segunda vez (mismo ts)
    resp2 = await client.post(
        INGEST_URL,
        json={"readings": [reading]},
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert resp2.status_code == 202
    data = resp2.json()
    assert data["accepted"] == 0
    assert data["duplicates"] == 1


@pytest.mark.asyncio
async def test_nullable_fields(client):
    """Caso 8: Campos en null (sensor caído) → se almacenan sin error."""
    reading = make_reading(
        ts="2026-09-13T23:00:00Z",
        temp_c=None,
        hum_aire_pct=None,
        suelo_raw=None,
        suelo_pct=None,
        nivel_raw=None,
        nivel_pct=None,
    )

    resp = await client.post(
        INGEST_URL,
        json={"readings": [reading]},
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert resp.status_code == 202
    assert resp.json()["accepted"] == 1


@pytest.mark.asyncio
async def test_empty_batch(client):
    """Caso 9: Lote vacío (0 readings) → 422 (Pydantic min_items=1)."""
    resp = await client.post(
        INGEST_URL,
        json={"readings": []},
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_reading_with_alerts(client):
    """Lectura con alertas activas se almacena correctamente."""
    reading = make_reading(
        ts="2026-09-13T23:30:00Z",
        estado="ALERTA",
        alertas=["SUELO_SECO", "TANQUE_MITAD"],
        riego_sugerido=True,
    )

    resp = await client.post(
        INGEST_URL,
        json={"readings": [reading]},
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert resp.status_code == 202
    assert resp.json()["accepted"] == 1
