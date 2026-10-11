"""
Tests de ALERT-01 — task "Build alert engine (compare readings vs. thresholds)".

Los umbrales viven en el firmware: el Arduino compara y manda códigos de
límite (`TEMP_ALTA`, `SUELO_SECO`…). El motor los convierte en alertas.

Criterios de aceptación:
  1. Con un umbral configurado, cuando una medición lo cruza se crea una
     alerta dentro de un ciclo de sondeo, con sensor, valor, umbral y hora.
  2. (UI) La alerta se ve en el dashboard sin recargar: la API la devuelve
     en cuanto la ingesta responde.
  3. Mientras la condición siga, no se crea una alerta duplicada hasta que
     la condición se normalice.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.firmware import LIMIT_ALERTS
from tests.conftest import (
    GATEWAY_API_KEY,
    GATEWAY_ID,
    GREENHOUSE_ID,
    OTHER_GREENHOUSE_ID,
    bearer,
)

INGEST_URL = f"/api/v1/devices/{GATEWAY_ID}/readings"
ALERTS_URL = f"/api/v1/greenhouses/{GREENHOUSE_ID}/alerts"
T0 = (datetime.now(timezone.utc) - timedelta(minutes=30)).replace(microsecond=0)

# Valor que pone el Arduino fuera de cada límite (según el firmware).
OUTSIDE = {
    "TEMP_ALTA": ("temp_c", 31.2),
    "TEMP_BAJA": ("temp_c", 4.5),
    "HUM_AIRE_ALTA": ("hum_aire_pct", 90.0),
    "HUM_AIRE_BAJA": ("hum_aire_pct", 41.0),
    "SUELO_ENCHARCADO": ("suelo_pct", 88),
    "SUELO_SECO": ("suelo_pct", 31),
    "TANQUE_UN_CUARTO": ("nivel_pct", 20),
}


def at(seconds: float) -> str:
    """Hora T0 + n segundos, como la manda la Pi."""
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stored(seconds: float) -> str:
    """La misma hora como la guarda y devuelve el backend."""
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def reading(seconds: float, *codes: str, **overrides) -> dict:
    base = {
        "ts": at(seconds), "ms": 1000, "temp_c": 23.0, "hum_aire_pct": 70.0,
        "suelo_raw": 380, "suelo_pct": 62, "nivel_raw": 120, "nivel_pct": 80,
        "luz_raw": 306, "luz_nivel": 717, "es_dia": True, "sol_min_hoy": 42,
        "sol_min_prev": None, "riego_sugerido": False, "bomba_habilitada": True,
        "vent": "BASE", "estado": "ALERTA" if codes else "OK", "alertas": list(codes),
    }
    for code in codes:
        if code in OUTSIDE:
            metric, value = OUTSIDE[code]
            base[metric] = value
    base.update(overrides)
    return base


async def ingest(api, *readings) -> dict:
    resp = await api.post(INGEST_URL, json={"readings": list(readings)},
                          headers={"X-API-Key": GATEWAY_API_KEY})
    assert resp.status_code == 202, resp.text
    return resp.json()


async def alerts(api, token, **params) -> dict:
    resp = await api.get(ALERTS_URL, params=params, headers=bearer(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def rows(db) -> list:
    cursor = await db.execute("SELECT * FROM alerts ORDER BY id")
    return [dict(row) for row in await cursor.fetchall()]


@pytest.fixture
def fast_clear(monkeypatch):
    monkeypatch.setattr(settings, "alert_clear_seconds", 60)


# ── Criterio 1: se crea con sensor, valor, umbral y hora ───────────────

@pytest.mark.asyncio
async def test_limit_code_creates_alert_with_sensor_value_threshold_ts(api, tokens):
    await ingest(api, reading(0, "TEMP_ALTA", temp_c=31.2))
    data = await alerts(api, tokens["producer"])
    assert data["active_count"] == 1
    alert = data["alerts"][0]
    assert alert["metric"] == "temp_c"
    assert alert["label"] == "Temperatura del aire" and alert["unit"] == "°C"
    assert alert["device_id"] == GATEWAY_ID
    assert alert["code"] == "TEMP_ALTA"
    assert alert["value"] == 31.2
    assert (alert["threshold"], alert["threshold_name"], alert["comparison"]) == (
        30.0, "TEMP_LIMITE_MAX", ">")
    assert alert["direction"] == "high"
    assert datetime.fromisoformat(alert["reading_ts"]) == T0     # hora de la medición
    assert alert["status"] == "active"
    assert alert["area"] == settings.default_area


@pytest.mark.asyncio
async def test_alert_exists_when_ingest_returns(api, db):
    """Dentro de un ciclo: la alerta ya está en la BD cuando el gateway recibe el 202."""
    await ingest(api, reading(0, "SUELO_SECO"))
    saved = await rows(db)
    assert len(saved) == 1
    assert (saved[0]["metric"], saved[0]["value"], saved[0]["threshold"]) == ("suelo_pct", 31, 50)
    assert saved[0]["reading_ts"] == stored(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", sorted(OUTSIDE))
async def test_each_limit_code_maps_to_its_sensor(api, db, code):
    await ingest(api, reading(0, code))
    (saved,) = await rows(db)
    metric, value = OUTSIDE[code]
    limit = LIMIT_ALERTS[code]
    assert (saved["metric"], saved["code"], saved["value"]) == (metric, code, value)
    assert (saved["direction"], saved["threshold"], saved["threshold_name"]) == (
        limit.direction, limit.threshold, limit.constant)


@pytest.mark.asyncio
async def test_independent_sensors_get_their_own_alert(api, db):
    await ingest(api, reading(0, "TEMP_ALTA", "SUELO_SECO", "TANQUE_UN_CUARTO"))
    assert sorted(row["metric"] for row in await rows(db)) == ["nivel_pct", "suelo_pct", "temp_c"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [
    "TEMP_ALTA_LEVE", "HUM_AIRE_BAJA_LEVE", "SUELO_SECO_LEVE", "SUELO_HUMEDO_LEVE",
    "TANQUE_MITAD", "SOMBRA_PROLONGADA", "SOL_INSUFICIENTE",
])
async def test_avisos_do_not_create_alerts(api, db, code):
    """D1: los avisos pintan la tarjeta, pero no son alertas."""
    await ingest(api, reading(0, code))
    assert await rows(db) == []


@pytest.mark.asyncio
async def test_sensor_failures_do_not_create_alerts(api, db):
    await ingest(api, reading(0, "FALLA_DHT", "FALLA_SUELO", temp_c=None, hum_aire_pct=None,
                              suelo_pct=None))
    assert await rows(db) == []


# ── Criterio 3: sin duplicados mientras la condición siga ──────────────

@pytest.mark.asyncio
async def test_persisting_code_no_duplicate(api, db):
    await ingest(api, *[reading(2 * i, "TEMP_ALTA", temp_c=30.5 + i / 10) for i in range(5)])
    await ingest(api, *[reading(2 * i, "TEMP_ALTA", temp_c=31.0 - i / 10) for i in range(5, 10)])
    (saved,) = await rows(db)
    assert saved["status"] == "active"
    assert saved["value"] == 30.5                       # el valor que la disparó
    assert saved["last_value"] == pytest.approx(30.1)   # el último que seguía fuera
    assert saved["worst_value"] == pytest.approx(30.9)
    assert saved["last_ts"] == stored(18)


@pytest.mark.asyncio
async def test_flapping_inside_window_is_the_same_alert(api, db, fast_clear):
    await ingest(api, reading(0, "TEMP_ALTA"), reading(2), reading(10), reading(30),
                 reading(40, "TEMP_ALTA"), reading(42))
    (saved,) = await rows(db)
    assert saved["status"] == "active"
    assert saved["clear_since"] == stored(42)


@pytest.mark.asyncio
async def test_clears_after_seconds_without_code(api, db, fast_clear):
    await ingest(api, reading(0, "TEMP_ALTA"))
    await ingest(api, *[reading(s, temp_c=27.0) for s in (2, 20, 40, 60)])
    assert (await rows(db))[0]["status"] == "active"          # 58 s sin el código
    await ingest(api, reading(62, temp_c=26.5))
    (saved,) = await rows(db)
    assert saved["status"] == "cleared"
    assert (saved["cleared_ts"], saved["cleared_value"]) == (stored(62), 26.5)
    assert saved["cleared_at"] is not None


@pytest.mark.asyncio
async def test_new_alert_after_clear(api, db, fast_clear):
    await ingest(api, reading(0, "SUELO_SECO"), reading(2), reading(70))
    await ingest(api, reading(80, "SUELO_SECO"))
    saved = await rows(db)
    assert [row["status"] for row in saved] == ["cleared", "active"]
    assert saved[1]["reading_ts"] == stored(80)


@pytest.mark.asyncio
async def test_leve_counts_as_back_inside_the_limit(api, db, fast_clear):
    await ingest(api, reading(0, "TEMP_ALTA"),
                 *[reading(s, "TEMP_ALTA_LEVE", temp_c=27.0) for s in (2, 30, 64)])
    assert (await rows(db))[0]["status"] == "cleared"


@pytest.mark.asyncio
async def test_falla_or_null_does_not_clear(api, db, fast_clear):
    await ingest(api, reading(0, "TEMP_ALTA"),
                 *[reading(s, "FALLA_DHT", temp_c=None, hum_aire_pct=None) for s in (2, 40, 80)])
    (saved,) = await rows(db)
    assert saved["status"] == "active" and saved["clear_since"] is None


@pytest.mark.asyncio
async def test_reading_without_alertas_changes_nothing(api, db, fast_clear):
    await ingest(api, reading(0, "TEMP_ALTA"))
    quiet = [reading(s, temp_c=22.0) for s in (2, 70)]
    for item in quiet:
        del item["alertas"]
    await ingest(api, *quiet)
    assert (await rows(db))[0]["status"] == "active"


@pytest.mark.asyncio
async def test_direction_change_closes_and_opens(api, db):
    await ingest(api, reading(0, "SUELO_SECO"), reading(2, "SUELO_ENCHARCADO"))
    saved = await rows(db)
    assert [(row["code"], row["status"]) for row in saved] == [
        ("SUELO_SECO", "cleared"), ("SUELO_ENCHARCADO", "active")]


@pytest.mark.asyncio
async def test_out_of_order_reading_is_ignored(api, db, fast_clear):
    await ingest(api, reading(100, "TEMP_ALTA"))
    await ingest(api, reading(10), reading(20, "TEMP_ALTA", temp_c=35.0))   # atrasadas
    (saved,) = await rows(db)
    assert (saved["value"], saved["last_value"], saved["clear_since"]) == (31.2, 31.2, None)


@pytest.mark.asyncio
async def test_old_reading_after_clear_does_not_reopen(api, db, fast_clear):
    await ingest(api, reading(0, "TEMP_ALTA"), reading(2), reading(70))
    await ingest(api, reading(50, "TEMP_ALTA"))   # llega tarde y es de antes del cierre
    assert [row["status"] for row in await rows(db)] == ["cleared"]


@pytest.mark.asyncio
async def test_duplicate_readings_are_not_evaluated_twice(api, db):
    first = reading(0, "TEMP_ALTA")
    await ingest(api, first)
    data = await ingest(api, first)
    assert data["duplicates"] == 1
    assert len(await rows(db)) == 1


@pytest.mark.asyncio
async def test_clear_time_is_configurable(api, db, monkeypatch):
    monkeypatch.setattr(settings, "alert_clear_seconds", 5)
    await ingest(api, reading(0, "TEMP_ALTA"), reading(2), reading(8))
    assert (await rows(db))[0]["status"] == "cleared"


# ── API ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_active_first_then_most_recent(api, tokens, fast_clear):
    await ingest(api, reading(0, "SUELO_SECO"), reading(2), reading(70),   # cerrada
                 reading(100, "TEMP_ALTA"), reading(110, "TANQUE_UN_CUARTO"))
    data = await alerts(api, tokens["producer"])
    assert [(a["code"], a["status"]) for a in data["alerts"]] == [
        ("TANQUE_UN_CUARTO", "active"), ("TEMP_ALTA", "active"), ("SUELO_SECO", "cleared")]
    assert data["active_count"] == 2
    assert data["clear_after_seconds"] == 60
    cleared = data["alerts"][2]
    assert cleared["duration_seconds"] == 70
    assert cleared["cleared_value"] == 62


@pytest.mark.asyncio
async def test_status_filter_and_limit(api, tokens, fast_clear):
    await ingest(api, reading(0, "SUELO_SECO"), reading(2), reading(70), reading(100, "TEMP_ALTA"))
    assert [a["code"] for a in (await alerts(api, tokens["producer"], status="active"))["alerts"]] == [
        "TEMP_ALTA"]
    assert [a["code"] for a in (await alerts(api, tokens["producer"], status="cleared"))["alerts"]] == [
        "SUELO_SECO"]
    assert len((await alerts(api, tokens["producer"], limit=1))["alerts"]) == 1
    bad = await api.get(ALERTS_URL, params={"limit": 0}, headers=bearer(tokens["producer"]))
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_no_alerts_yet(api, tokens):
    data = await alerts(api, tokens["producer"])
    assert (data["active_count"], data["alerts"]) == (0, [])


@pytest.mark.asyncio
async def test_producer_cannot_see_other_greenhouse(api, tokens):
    resp = await api.get(f"/api/v1/greenhouses/{OTHER_GREENHOUSE_ID}/alerts",
                         headers=bearer(tokens["producer"]))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_sees_any_greenhouse(api, tokens):
    await ingest(api, reading(0, "TEMP_ALTA"))
    assert (await alerts(api, tokens["admin"]))["active_count"] == 1


@pytest.mark.asyncio
async def test_requires_session(api):
    assert (await api.get(ALERTS_URL)).status_code == 401
