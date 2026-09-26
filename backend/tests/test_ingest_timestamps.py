"""
Tests del ajuste de MON-04 (defecto, horas de reserva del Sprint 2).

- `ts` se guarda en UTC con milisegundos, venga en la zona que venga.
- `ts` sin zona horaria, en formato no ISO o numérico se rechaza.
- Horas imposibles se rechazan: antes de SMARTGREENAI_MIN_READING_TS o más de
  SMARTGREENAI_MAX_CLOCK_SKEW_SECONDS en el futuro.
- Validación lectura por lectura: las inválidas van a `rejected` con su índice
  y las válidas del mismo lote se guardan.

Los 9 tests originales de test_ingest.py siguen sin cambios.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import config
from app.config import settings
from tests.conftest import GATEWAY_API_KEY, GATEWAY_ID, GREENHOUSE_ID, bearer

INGEST_URL = f"/api/v1/devices/{GATEWAY_ID}/readings"
LATEST_URL = f"/api/v1/greenhouses/{GREENHOUSE_ID}/readings/latest"
MX = timezone(timedelta(hours=-6))


def reading(ts, **overrides) -> dict:
    base = {
        "ts": ts, "ms": 1000, "temp_c": 24.2, "hum_aire_pct": 72.0,
        "suelo_raw": 380, "suelo_pct": 62, "nivel_raw": 150, "nivel_pct": 75,
        "luz_raw": 306, "luz_nivel": 717, "es_dia": True, "sol_min_hoy": 42,
        "sol_min_prev": None, "riego_sugerido": False, "bomba_habilitada": True,
        "vent": "BASE", "estado": "OK", "alertas": [],
    }
    base.update(overrides)
    return base


def hours_ago(hours: float) -> datetime:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0)


async def post(api, *readings) -> dict:
    resp = await api.post(INGEST_URL, json={"readings": list(readings)},
                          headers={"X-API-Key": GATEWAY_API_KEY})
    assert resp.status_code == 202, resp.text
    return resp.json()


async def stored_ts(db) -> list:
    cursor = await db.execute("SELECT ts FROM readings ORDER BY id")
    return [row["ts"] for row in await cursor.fetchall()]


def rejected_by_index(data: dict) -> dict:
    return {item["index"]: item["reason"] for item in data["rejected"]}


# ── UTC con milisegundos ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ts_with_offset_is_stored_in_utc_with_ms(api, db):
    """Hora de México (-06:00) → se guarda en UTC con milisegundos."""
    data = await post(api, reading("2026-09-20T12:00:00-06:00"))
    assert data["accepted"] == 1
    assert await stored_ts(db) == ["2026-09-20T18:00:00.000+00:00"]


@pytest.mark.asyncio
async def test_z_suffix_is_stored_with_ms(api, db):
    await post(api, reading("2026-09-20T18:00:00Z"))
    assert await stored_ts(db) == ["2026-09-20T18:00:00.000+00:00"]


@pytest.mark.asyncio
async def test_microseconds_are_truncated_to_ms(api, db):
    await post(api, reading("2026-09-20T18:00:00.123456Z"))
    assert await stored_ts(db) == ["2026-09-20T18:00:00.123+00:00"]


@pytest.mark.asyncio
async def test_same_instant_in_two_formats_is_duplicate(api, db):
    """Idempotencia real: el mismo instante escrito distinto no se duplica."""
    first = await post(api, reading("2026-09-20T18:00:00Z"))
    second = await post(api, reading("2026-09-20T12:00:00.000-06:00"))
    assert first["accepted"] == 1
    assert second == {"accepted": 0, "duplicates": 1, "rejected": []}
    assert len(await stored_ts(db)) == 1


@pytest.mark.asyncio
async def test_latest_uses_real_time_order_across_offsets(api, db, tokens):
    """Antes se comparaba el texto: '13:30-06:00' quedaba "antes" de '18:00Z'."""
    older = hours_ago(3)                    # en UTC
    newer = hours_ago(2).astimezone(MX)     # una hora después, escrita en -06:00
    await post(
        api,
        reading(older.isoformat().replace("+00:00", "Z"), temp_c=20.0),
        reading(newer.isoformat(), temp_c=25.0),
    )
    resp = await api.get(LATEST_URL, headers=bearer(tokens["producer"]))
    assert resp.status_code == 200
    temp = next(m for m in resp.json()["metrics"] if m["key"] == "temp_c")
    assert temp["value"] == 25.0


# ── Formato de ts ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_naive_ts_is_rejected(api, db):
    data = await post(api, reading("2026-09-20T18:00:00"))
    assert data["accepted"] == 0
    assert "zona horaria" in rejected_by_index(data)[0]
    assert await stored_ts(db) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ts", [1758900000, "1758900000", "ayer", "", None])
async def test_non_iso_ts_is_rejected(api, db, bad_ts):
    data = await post(api, reading(bad_ts))
    assert data["accepted"] == 0
    assert rejected_by_index(data)[0].startswith("ts:")


@pytest.mark.asyncio
async def test_missing_ts_is_rejected(api):
    item = reading("2026-09-20T18:00:00Z")
    del item["ts"]
    data = await post(api, item)
    assert rejected_by_index(data)[0].startswith("ts:")


# ── Rango de horas ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unsynced_clock_1970_is_rejected(api, db):
    """Una Pi sin NTP cree que es 1970: la lectura no se guarda."""
    data = await post(api, reading("1970-01-01T00:00:05Z"))
    assert data["accepted"] == 0
    assert "no está sincronizado" in rejected_by_index(data)[0]
    assert await stored_ts(db) == []


@pytest.mark.asyncio
async def test_before_min_reading_ts_is_rejected(api):
    data = await post(api, reading("2025-12-31T23:59:59Z"))
    assert 0 in rejected_by_index(data)


@pytest.mark.asyncio
async def test_future_beyond_skew_is_rejected(api, db):
    future = datetime.now(timezone.utc) + timedelta(seconds=settings.max_clock_skew_seconds + 60)
    data = await post(api, reading(future.isoformat()))
    assert data["accepted"] == 0
    assert "futuro" in rejected_by_index(data)[0]


@pytest.mark.asyncio
async def test_small_future_skew_is_accepted(api):
    """Unos segundos de diferencia entre relojes no deben perder lecturas."""
    near = datetime.now(timezone.utc) + timedelta(seconds=60)
    data = await post(api, reading(near.isoformat()))
    assert data["accepted"] == 1


@pytest.mark.asyncio
async def test_range_comes_from_settings(api, monkeypatch):
    monkeypatch.setattr(settings, "max_clock_skew_seconds", 10)
    monkeypatch.setattr(
        settings, "min_reading_ts", datetime(2026, 9, 1, tzinfo=timezone.utc)
    )
    near = datetime.now(timezone.utc) + timedelta(seconds=60)
    data = await post(api, reading(near.isoformat()), reading("2026-08-31T23:00:00Z"))
    assert data["accepted"] == 0
    assert set(rejected_by_index(data)) == {0, 1}


# ── Validación lectura por lectura ─────────────────────────────────────

@pytest.mark.asyncio
async def test_bad_readings_do_not_sink_the_batch(api, db):
    """Una lectura mala ya no tumba el lote con 422: las buenas se guardan."""
    batch = [
        reading("2026-09-20T18:00:00Z"),                 # 0 ok
        reading("2026-09-20T18:00:02Z", suelo_pct=150),  # 1 fuera de rango
        reading("2026-09-20T18:00:04"),                  # 2 sin zona
        "no soy una lectura",                            # 3 no es objeto
        reading("2026-09-20T18:00:06Z", vent="TURBO"),   # 4 enum inválido
        reading("2026-09-20T18:00:08Z", temp_c=None),    # 5 ok (sensor caído)
    ]
    data = await post(api, *batch)
    assert data["accepted"] == 2
    assert data["duplicates"] == 0
    reasons = rejected_by_index(data)
    assert set(reasons) == {1, 2, 3, 4}
    assert reasons[1].startswith("suelo_pct:")
    assert "zona horaria" in reasons[2]
    assert "objeto" in reasons[3]
    assert reasons[4].startswith("vent:")
    assert await stored_ts(db) == [
        "2026-09-20T18:00:00.000+00:00",
        "2026-09-20T18:00:08.000+00:00",
    ]


@pytest.mark.asyncio
async def test_all_rejected_still_answers_202(api):
    data = await post(api, reading("1970-01-01T00:00:00Z"), reading("2026-09-20T18:00:00"))
    assert data["accepted"] == 0
    assert len(data["rejected"]) == 2


@pytest.mark.asyncio
async def test_rejected_reading_does_not_update_last_seen(api, db):
    await post(api, reading("1970-01-01T00:00:00Z"))
    cursor = await db.execute("SELECT last_seen_at FROM devices WHERE device_id = ?", (GATEWAY_ID,))
    assert (await cursor.fetchone())["last_seen_at"] is None


@pytest.mark.asyncio
async def test_firmware_extra_fields_are_ignored(api, db):
    """bomba_activa y vent_activo aún no están en el contrato (ACT-04)."""
    data = await post(api, reading("2026-09-20T18:00:00Z", bomba_activa=True, vent_activo=False))
    assert data["accepted"] == 1


# ── Configuración ──────────────────────────────────────────────────────

def test_min_reading_ts_env_requires_timezone(monkeypatch):
    monkeypatch.setenv("SMARTGREENAI_MIN_READING_TS", "2026-01-01T00:00:00")
    with pytest.raises(RuntimeError, match="zona horaria"):
        config.Settings()


def test_min_reading_ts_env_rejects_garbage(monkeypatch):
    monkeypatch.setenv("SMARTGREENAI_MIN_READING_TS", "enero")
    with pytest.raises(RuntimeError, match="ISO 8601"):
        config.Settings()


def test_timestamp_settings_defaults(monkeypatch):
    monkeypatch.delenv("SMARTGREENAI_MIN_READING_TS", raising=False)
    monkeypatch.delenv("SMARTGREENAI_MAX_CLOCK_SKEW_SECONDS", raising=False)
    fresh = config.Settings()
    assert fresh.min_reading_ts == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert fresh.max_clock_skew_seconds == 300
