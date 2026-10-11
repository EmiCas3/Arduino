"""
Tests de ACT-04 — task "Build actuator-status endpoint + UI badge".

Criterios de aceptación:
  1. Con actuadores registrados, el dashboard muestra cada uno como
     encendido, apagado o no disponible.
  2. Tras una acción manual o automática, el estado se refresca sin recargar
     (la API devuelve el nuevo estado en cuanto llega la lectura).
  3. Un actuador que no confirma su estado se muestra como desconocido,
     nunca como apagado.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from tests.conftest import (
    GATEWAY_API_KEY,
    GATEWAY_ID,
    GREENHOUSE_ID,
    OTHER_GREENHOUSE_ID,
    bearer,
    now_iso,
)

INGEST_URL = f"/api/v1/devices/{GATEWAY_ID}/readings"
STATUS_URL = f"/api/v1/greenhouses/{GREENHOUSE_ID}/actuators/status"

ACTUATORS = [
    # (id, nombre, tipo, vivero, canal, gateway, status)
    ("bomba-test-01", "Bomba de riego", "pump", GREENHOUSE_ID, "L298N-B/D5", GATEWAY_ID, "active"),
    ("vent-test-01", "Ventilador", "fan", GREENHOUSE_ID, "L298N-A/D3", GATEWAY_ID, "active"),
    ("malla-test-01", "Malla sombra", "shade", GREENHOUSE_ID, "SERVO/D9", GATEWAY_ID, "active"),
    ("luz-test-01", "Lámpara", "light", GREENHOUSE_ID, "RELE/D7", None, "active"),
    ("bomba-vieja-01", "Bomba vieja", "pump", GREENHOUSE_ID, "L298N-B/D6", GATEWAY_ID, "inactive"),
    ("bomba-otro-01", "Bomba de otro vivero", "pump", OTHER_GREENHOUSE_ID, "L298N-B/D5", None, "active"),
]


def ts_ago(**delta) -> str:
    moment = datetime.now(timezone.utc) - timedelta(**delta)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def reading(ts: str, **overrides) -> dict:
    base = {
        "ts": ts, "ms": 1000, "temp_c": 24.2, "hum_aire_pct": 72.0,
        "suelo_raw": 380, "suelo_pct": 62, "nivel_raw": 120, "nivel_pct": 80,
        "luz_raw": 306, "luz_nivel": 717, "es_dia": True, "sol_min_hoy": 42,
        "sol_min_prev": None, "riego_sugerido": False, "bomba_habilitada": True,
        "bomba_activa": False, "vent": "BASE", "vent_activo": False,
        "estado": "OK", "alertas": [],
    }
    base.update(overrides)
    return base


async def ingest(api, *readings) -> dict:
    resp = await api.post(INGEST_URL, json={"readings": list(readings)},
                          headers={"X-API-Key": GATEWAY_API_KEY})
    assert resp.status_code == 202, resp.text
    return resp.json()


async def status(api, token, url=STATUS_URL) -> dict:
    resp = await api.get(url, headers=bearer(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    data["by_id"] = {a["actuator_id"]: a for a in data["actuators"]}
    return data


@pytest.fixture
async def actuators(db):
    for actuator_id, name, kind, greenhouse, channel, gateway, state in ACTUATORS:
        await db.execute(
            """
            INSERT INTO actuators (actuator_id, name, type, greenhouse_id, area,
                                   control_channel, gateway_device_id, status, registered_at)
            VALUES (?, ?, ?, ?, 'general', ?, ?, ?, ?)
            """,
            (actuator_id, name, kind, greenhouse, channel, gateway, state, now_iso()),
        )
    await db.commit()


# ── Criterio 1: encendido, apagado o no disponible ─────────────────────

@pytest.mark.asyncio
async def test_status_on_off_from_reading(api, tokens, actuators):
    await ingest(api, reading(ts_ago(seconds=5), bomba_activa=True, vent_activo=False))
    data = await status(api, tokens["producer"])
    pump, fan = data["by_id"]["bomba-test-01"], data["by_id"]["vent-test-01"]
    assert (pump["state"], pump["reason"]) == ("on", "reported")
    assert (fan["state"], fan["reason"]) == ("off", "reported")
    assert 0 <= pump["age_seconds"] < 60
    assert pump["reported_at"].startswith(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    assert pump["detail"]
    assert data["stale_after_minutes"] == settings.reading_stale_minutes


@pytest.mark.asyncio
async def test_pump_unavailable_when_tank_blocked(api, tokens, actuators):
    """El firmware bloquea la bomba con el tanque crítico: no disponible."""
    await ingest(api, reading(ts_ago(seconds=5), bomba_activa=False, bomba_habilitada=False,
                              nivel_pct=10, alertas=["TANQUE_UN_CUARTO"], estado="ALERTA"))
    pump = (await status(api, tokens["producer"]))["by_id"]["bomba-test-01"]
    assert (pump["state"], pump["reason"]) == ("unavailable", "tank_low")


@pytest.mark.asyncio
async def test_gateway_inactive_means_unavailable(api, db, tokens, actuators):
    await ingest(api, reading(ts_ago(seconds=5), bomba_activa=True))
    await db.execute("UPDATE devices SET status = 'inactive' WHERE device_id = ?", (GATEWAY_ID,))
    await db.commit()
    pump = (await status(api, tokens["admin"]))["by_id"]["bomba-test-01"]
    assert (pump["state"], pump["reason"]) == ("unavailable", "gateway_inactive")


@pytest.mark.asyncio
async def test_only_active_actuators_of_the_greenhouse(api, tokens, actuators):
    data = await status(api, tokens["admin"])
    assert set(data["by_id"]) == {"bomba-test-01", "vent-test-01", "malla-test-01", "luz-test-01"}


# ── Criterio 2: el estado cambia en cuanto llega la lectura ────────────

@pytest.mark.asyncio
async def test_state_refreshes_with_next_reading(api, tokens, actuators):
    await ingest(api, reading(ts_ago(seconds=20), bomba_activa=False))
    assert (await status(api, tokens["producer"]))["by_id"]["bomba-test-01"]["state"] == "off"
    await ingest(api, reading(ts_ago(seconds=10), bomba_activa=True))
    assert (await status(api, tokens["producer"]))["by_id"]["bomba-test-01"]["state"] == "on"


@pytest.mark.asyncio
async def test_last_reading_of_batch_wins(api, tokens, actuators):
    """Dentro de un lote manda la lectura más reciente, sin importar su posición."""
    await ingest(api,
                 reading(ts_ago(seconds=4), bomba_activa=False),
                 reading(ts_ago(seconds=8), bomba_activa=True))
    assert (await status(api, tokens["producer"]))["by_id"]["bomba-test-01"]["state"] == "off"


@pytest.mark.asyncio
async def test_older_reading_does_not_overwrite(api, tokens, actuators):
    """Una lectura atrasada (llega después pero es de antes) no pisa el estado."""
    await ingest(api, reading(ts_ago(seconds=5), bomba_activa=True))
    await ingest(api, reading(ts_ago(minutes=3), bomba_activa=False))
    assert (await status(api, tokens["producer"]))["by_id"]["bomba-test-01"]["state"] == "on"


@pytest.mark.asyncio
async def test_duplicate_reading_changes_nothing(api, db, tokens, actuators):
    first = reading(ts_ago(seconds=5), bomba_activa=True)
    await ingest(api, first)
    cursor = await db.execute("SELECT updated_at FROM actuator_states WHERE actuator_id = 'bomba-test-01'")
    before = (await cursor.fetchone())["updated_at"]
    data = await ingest(api, dict(first, bomba_activa=False))   # mismo ts = duplicada
    assert data["duplicates"] == 1
    cursor = await db.execute("SELECT is_on, updated_at FROM actuator_states WHERE actuator_id = 'bomba-test-01'")
    row = await cursor.fetchone()
    assert (row["is_on"], row["updated_at"]) == (1, before)


@pytest.mark.asyncio
async def test_inactive_actuator_state_is_not_tracked(api, db, actuators):
    await ingest(api, reading(ts_ago(seconds=5), bomba_activa=True))
    cursor = await db.execute("SELECT actuator_id FROM actuator_states ORDER BY actuator_id")
    assert [row["actuator_id"] for row in await cursor.fetchall()] == ["bomba-test-01", "vent-test-01"]


# ── Criterio 3: sin confirmación → desconocido, nunca apagado ──────────

@pytest.mark.asyncio
async def test_unknown_without_report(api, tokens, actuators):
    data = await status(api, tokens["producer"])
    for actuator_id in ("bomba-test-01", "vent-test-01"):
        item = data["by_id"][actuator_id]
        assert (item["state"], item["reason"]) == ("unknown", "never_reported")
        assert item["reported_at"] is None and item["age_seconds"] is None


@pytest.mark.asyncio
async def test_never_off_when_firmware_does_not_send_the_field(api, tokens, actuators):
    """Un firmware viejo sin bomba_activa: la bomba NO aparece apagada."""
    old = reading(ts_ago(seconds=5))
    del old["bomba_activa"], old["vent_activo"]
    await ingest(api, old)
    pump = (await status(api, tokens["producer"]))["by_id"]["bomba-test-01"]
    assert pump["state"] == "unknown"


@pytest.mark.asyncio
async def test_unknown_when_stale(api, tokens, actuators):
    minutes = settings.reading_stale_minutes + 5
    await ingest(api, reading(ts_ago(minutes=minutes), bomba_activa=False, vent_activo=True))
    data = await status(api, tokens["producer"])
    for actuator_id in ("bomba-test-01", "vent-test-01"):
        item = data["by_id"][actuator_id]
        assert (item["state"], item["reason"]) == ("unknown", "stale")
        assert item["age_seconds"] >= minutes * 60 - 5


@pytest.mark.asyncio
async def test_stale_blocked_pump_is_unknown_too(api, tokens, actuators):
    minutes = settings.reading_stale_minutes + 1
    await ingest(api, reading(ts_ago(minutes=minutes), bomba_activa=False, bomba_habilitada=False))
    pump = (await status(api, tokens["producer"]))["by_id"]["bomba-test-01"]
    assert pump["state"] == "unknown"


@pytest.mark.asyncio
async def test_unknown_for_shade_and_light(api, tokens, actuators):
    await ingest(api, reading(ts_ago(seconds=5), bomba_activa=True, vent_activo=True))
    data = await status(api, tokens["producer"])
    assert (data["by_id"]["malla-test-01"]["state"], data["by_id"]["malla-test-01"]["reason"]) == (
        "unknown", "not_reported")
    assert (data["by_id"]["luz-test-01"]["state"], data["by_id"]["luz-test-01"]["reason"]) == (
        "unknown", "no_gateway")


@pytest.mark.asyncio
async def test_no_state_is_ever_off_without_report(api, tokens, actuators):
    """Barrido: sin ninguna lectura, ningún actuador sale como off."""
    data = await status(api, tokens["admin"])
    assert all(item["state"] != "off" for item in data["actuators"])


# ── Permisos (AUTH-02) ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_producer_cannot_see_other_greenhouse(api, tokens, actuators):
    resp = await api.get(f"/api/v1/greenhouses/{OTHER_GREENHOUSE_ID}/actuators/status",
                         headers=bearer(tokens["producer"]))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_sees_any_greenhouse(api, tokens, actuators):
    data = await status(api, tokens["admin"],
                        f"/api/v1/greenhouses/{OTHER_GREENHOUSE_ID}/actuators/status")
    assert list(data["by_id"]) == ["bomba-otro-01"]
    assert data["by_id"]["bomba-otro-01"]["state"] == "unknown"


@pytest.mark.asyncio
async def test_requires_session(api):
    assert (await api.get(STATUS_URL)).status_code == 401
