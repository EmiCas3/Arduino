"""
Tests del soporte de backend para los widgets de DASH-01.

- `X-SG-Background: 1`: el refresco automático del dashboard NO renueva la
  sesión, para que el cierre por inactividad de AUTH-01 siga funcionando con
  una pestaña abierta.
- `GET /greenhouses`: viveros que el usuario puede consultar (selector de admins).
- `area` en las últimas lecturas (el prototipo es un solo ambiente).
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from tests.conftest import (
    GATEWAY_API_KEY,
    GATEWAY_ID,
    GREENHOUSE_ID,
    OTHER_GREENHOUSE_ID,
    bearer,
    login,
    now_iso,
)

BACKGROUND = {"X-SG-Background": "1"}
LATEST_URL = f"/api/v1/greenhouses/{GREENHOUSE_ID}/readings/latest"
INGEST_URL = f"/api/v1/devices/{GATEWAY_ID}/readings"


def minutes_ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


async def last_seen(db) -> str:
    cursor = await db.execute("SELECT last_seen_at FROM sessions")
    return (await cursor.fetchone())["last_seen_at"]


async def set_last_seen(db, value: str) -> None:
    await db.execute("UPDATE sessions SET last_seen_at = ?", (value,))
    await db.commit()


# ── Refresco en segundo plano y sesión por inactividad ─────────────────

@pytest.mark.asyncio
async def test_background_request_does_not_renew_session(api, db):
    token = await login(api, "producer")
    before = minutes_ago(10)
    await set_last_seen(db, before)

    resp = await api.get(LATEST_URL, headers={**bearer(token), **BACKGROUND})

    assert resp.status_code == 200
    assert await last_seen(db) == before


@pytest.mark.asyncio
async def test_normal_request_still_renews_session(api, db):
    """Sin el header todo sigue como en AUTH-01: la actividad renueva."""
    token = await login(api, "producer")
    before = minutes_ago(10)
    await set_last_seen(db, before)

    resp = await api.get(LATEST_URL, headers=bearer(token))

    assert resp.status_code == 200
    assert await last_seen(db) > before


@pytest.mark.asyncio
async def test_polling_cannot_keep_an_idle_session_alive(api, db):
    """Una pestaña abierta que solo refresca termina cerrando sesión."""
    token = await login(api, "producer")
    headers = {**bearer(token), **BACKGROUND}

    # Casi al límite: los refrescos pasan, pero no "despiertan" la sesión.
    almost = minutes_ago(settings.session_idle_minutes - 1)
    await set_last_seen(db, almost)
    for _ in range(3):
        assert (await api.get(LATEST_URL, headers=headers)).status_code == 200
    assert await last_seen(db) == almost

    # Pasado el límite: el siguiente refresco recibe 401 y la sesión se revoca.
    await set_last_seen(db, minutes_ago(settings.session_idle_minutes + 1))
    resp = await api.get(LATEST_URL, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "session_expired"

    cursor = await db.execute("SELECT revoked_at FROM sessions")
    assert (await cursor.fetchone())["revoked_at"] is not None
    # Ya ni una petición normal la revive.
    assert (await api.get(LATEST_URL, headers=bearer(token))).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0", "true", "yes", ""])
async def test_only_the_exact_value_counts_as_background(api, db, value):
    token = await login(api, "producer")
    before = minutes_ago(10)
    await set_last_seen(db, before)

    resp = await api.get(LATEST_URL, headers={**bearer(token), "X-SG-Background": value})

    assert resp.status_code == 200
    assert await last_seen(db) > before


@pytest.mark.asyncio
async def test_background_header_gives_no_extra_access(api, tokens):
    """El header solo evita renovar; los permisos son los mismos."""
    other = f"/api/v1/greenhouses/{OTHER_GREENHOUSE_ID}/readings/latest"
    resp = await api.get(other, headers={**bearer(tokens["producer"]), **BACKGROUND})
    assert resp.status_code == 403
    assert (await api.get(LATEST_URL, headers=BACKGROUND)).status_code == 401


# ── GET /greenhouses ───────────────────────────────────────────────────

async def add_device(db, device_id: str, greenhouse_id: str) -> None:
    await db.execute(
        """
        INSERT INTO devices (device_id, greenhouse_id, type, model, location,
                             status, api_key_hash, registered_at)
        VALUES (?, ?, 'gateway', 'Test Pi', 'Otro lado', 'active', ?, ?)
        """,
        (device_id, greenhouse_id, hashlib.sha256(device_id.encode()).hexdigest(), now_iso()),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_producer_lists_only_own_greenhouse(api, db, tokens):
    await add_device(db, "pi-otro-02", OTHER_GREENHOUSE_ID)
    resp = await api.get("/api/v1/greenhouses", headers=bearer(tokens["producer"]))
    assert resp.status_code == 200
    assert [g["greenhouse_id"] for g in resp.json()] == [GREENHOUSE_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "super_admin"])
async def test_admins_list_every_greenhouse(api, db, tokens, role):
    await add_device(db, "pi-otro-02", OTHER_GREENHOUSE_ID)
    await add_device(db, "pi-test-02", GREENHOUSE_ID)
    resp = await api.get("/api/v1/greenhouses", headers=bearer(tokens[role]))
    assert resp.status_code == 200
    data = {g["greenhouse_id"]: g for g in resp.json()}
    assert sorted(data) == sorted([GREENHOUSE_ID, OTHER_GREENHOUSE_ID])
    assert data[GREENHOUSE_ID]["devices"] == 2
    assert data[OTHER_GREENHOUSE_ID]["devices"] == 1


@pytest.mark.asyncio
async def test_producer_without_greenhouse_gets_empty_list(api, db, tokens):
    await db.execute("UPDATE users SET greenhouse_id = NULL WHERE role = 'producer'")
    await db.commit()
    resp = await api.get("/api/v1/greenhouses", headers=bearer(tokens["producer"]))
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_greenhouse_last_seen_follows_ingest(api, tokens):
    headers = bearer(tokens["admin"])
    first = (await api.get("/api/v1/greenhouses", headers=headers)).json()
    assert first[0]["last_seen_at"] is None

    ts = datetime.now(timezone.utc).isoformat()
    resp = await api.post(INGEST_URL, json={"readings": [{"ts": ts, "temp_c": 24.0}]},
                          headers={"X-API-Key": GATEWAY_API_KEY})
    assert resp.status_code == 202

    after = (await api.get("/api/v1/greenhouses", headers=headers)).json()
    assert after[0]["last_seen_at"] is not None


# ── Área de las lecturas (prototipo de un solo ambiente) ───────────────

@pytest.mark.asyncio
async def test_latest_metrics_carry_default_area(api, tokens):
    resp = await api.get(LATEST_URL, headers=bearer(tokens["producer"]))
    assert resp.status_code == 200
    assert {m["area"] for m in resp.json()["metrics"]} == {"general"}


@pytest.mark.asyncio
async def test_default_area_is_configurable(api, tokens, monkeypatch):
    monkeypatch.setattr(settings, "default_area", "cama-1")
    ts = datetime.now(timezone.utc).isoformat()
    await api.post(INGEST_URL, json={"readings": [{"ts": ts, "temp_c": 24.0}]},
                   headers={"X-API-Key": GATEWAY_API_KEY})
    resp = await api.get(LATEST_URL, headers=bearer(tokens["producer"]))
    assert {m["area"] for m in resp.json()["metrics"]} == {"cama-1"}
