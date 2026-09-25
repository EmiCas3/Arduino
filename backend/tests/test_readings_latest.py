"""
Tests de DASH-01 — task "Build latest readings API".

La API alimenta el dashboard de mediciones actuales:
  - Último valor por medición con unidad y tiempo desde la lectura.
  - Una lectura más vieja que el intervalo configurado se marca como `stale`.
  - null = sensor caído: se muestra el último valor válido y se avisa.
  - Un productor solo consulta su vivero asignado (AUTH-02).
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
)

INGEST_URL = f"/api/v1/devices/{GATEWAY_ID}/readings"
LATEST_URL = f"/api/v1/greenhouses/{GREENHOUSE_ID}/readings/latest"


def ts_ago(**delta) -> str:
    moment = datetime.now(timezone.utc) - timedelta(**delta)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reading(ts: str, **overrides) -> dict:
    base = {
        "ts": ts, "ms": 1000, "temp_c": 24.2, "hum_aire_pct": 72.0,
        "suelo_raw": 380, "suelo_pct": 62, "nivel_raw": 150, "nivel_pct": 75,
        "luz_raw": 306, "luz_nivel": 717, "es_dia": True, "sol_min_hoy": 42,
        "sol_min_prev": None, "riego_sugerido": False, "bomba_habilitada": True,
        "vent": "BASE", "estado": "OK", "alertas": [],
    }
    base.update(overrides)
    return base


async def ingest(api, *readings):
    resp = await api.post(INGEST_URL, json={"readings": list(readings)},
                          headers={"X-API-Key": GATEWAY_API_KEY})
    assert resp.status_code == 202, resp.text


def metric(data: dict, key: str) -> dict:
    return next(m for m in data["metrics"] if m["key"] == key)


@pytest.mark.asyncio
async def test_latest_values_with_units(api, tokens):
    """Caso 1: último valor por medición, con unidad, antigüedad y sin stale."""
    await ingest(api, reading(ts_ago(minutes=5), temp_c=21.0),
                 reading(ts_ago(seconds=30), temp_c=24.2))
    resp = await api.get(LATEST_URL, headers=bearer(tokens["producer"]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_data"] is True
    assert data["stale_after_minutes"] == settings.reading_stale_minutes

    temp = metric(data, "temp_c")
    assert temp["value"] == 24.2
    assert temp["unit"] == "°C"
    assert temp["label"] == "Temperatura del aire"
    assert 0 <= temp["age_seconds"] < 120
    assert temp["stale"] is False
    assert temp["sensor_down"] is False
    assert temp["device_location"] == "Mesa de pruebas"

    assert {m["key"] for m in data["metrics"]} == {
        "temp_c", "hum_aire_pct", "suelo_pct", "nivel_pct", "luz_nivel"}


@pytest.mark.asyncio
async def test_integer_values_keep_type_and_raw(api, tokens):
    """Caso 2: los porcentajes siguen siendo enteros y traen su raw."""
    await ingest(api, reading(ts_ago(seconds=10)))
    data = (await api.get(LATEST_URL, headers=bearer(tokens["admin"]))).json()
    suelo = metric(data, "suelo_pct")
    assert suelo["value"] == 62 and isinstance(suelo["value"], int)
    assert suelo["raw"] == 380
    assert metric(data, "luz_nivel")["raw"] == 306


@pytest.mark.asyncio
async def test_old_reading_flagged_stale(api, tokens):
    """Caso 3: una lectura más vieja que el intervalo configurado → stale."""
    await ingest(api, reading(ts_ago(minutes=settings.reading_stale_minutes + 10)))
    data = (await api.get(LATEST_URL, headers=bearer(tokens["producer"]))).json()
    assert metric(data, "temp_c")["stale"] is True
    assert data["last_reading"]["stale"] is True


@pytest.mark.asyncio
async def test_stale_threshold_is_configurable(api, tokens, monkeypatch):
    """Caso 4: el umbral sale de la configuración, sin redeploy de código."""
    monkeypatch.setattr(settings, "reading_stale_minutes", 1)
    await ingest(api, reading(ts_ago(minutes=2)))
    data = (await api.get(LATEST_URL, headers=bearer(tokens["producer"]))).json()
    assert data["stale_after_minutes"] == 1
    assert metric(data, "temp_c")["stale"] is True


@pytest.mark.asyncio
async def test_sensor_down_shows_last_valid_value(api, tokens):
    """Caso 5: null = sensor caído → último valor válido + sensor_down=true."""
    old_ts = ts_ago(minutes=3)
    await ingest(api, reading(old_ts, temp_c=22.5),
                 reading(ts_ago(seconds=20), temp_c=None, hum_aire_pct=None,
                         estado="ALERTA", alertas=["FALLA_DHT"]))
    data = (await api.get(LATEST_URL, headers=bearer(tokens["producer"]))).json()
    temp = metric(data, "temp_c")
    assert temp["value"] == 22.5
    assert temp["sensor_down"] is True
    assert temp["ts"].startswith(old_ts[:19])
    assert metric(data, "suelo_pct")["sensor_down"] is False
    assert data["last_reading"]["estado"] == "ALERTA"
    assert data["last_reading"]["alertas"] == ["FALLA_DHT"]


@pytest.mark.asyncio
async def test_last_reading_summary(api, tokens):
    """Caso 6: el resumen trae estado, decisiones del Arduino y alertas."""
    await ingest(api, reading(ts_ago(seconds=5), riego_sugerido=True,
                              bomba_habilitada=False, vent="ALTA", estado="AVISO",
                              alertas=["SUELO_SECO_LEVE"]))
    summary = (await api.get(LATEST_URL, headers=bearer(tokens["admin"]))).json()["last_reading"]
    assert summary["device_id"] == GATEWAY_ID
    assert summary["riego_sugerido"] is True
    assert summary["bomba_habilitada"] is False
    assert summary["vent"] == "ALTA"
    assert summary["estado"] == "AVISO"


@pytest.mark.asyncio
async def test_no_readings_yet(api, tokens):
    """Caso 7: vivero sin lecturas → has_data=false y valores null, sin error."""
    resp = await api.get(LATEST_URL, headers=bearer(tokens["producer"]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_data"] is False
    assert data["last_reading"] is None
    assert all(m["value"] is None and m["stale"] for m in data["metrics"])


@pytest.mark.asyncio
async def test_producer_only_own_greenhouse(api, tokens):
    """Caso 8: un productor no puede consultar otro vivero (403)."""
    resp = await api.get(
        f"/api/v1/greenhouses/{OTHER_GREENHOUSE_ID}/readings/latest",
        headers=bearer(tokens["producer"]),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "super_admin"])
async def test_admins_can_query_any_greenhouse(api, tokens, role):
    """Caso 9: Admin y Super Admin consultan cualquier vivero."""
    resp = await api.get(
        f"/api/v1/greenhouses/{OTHER_GREENHOUSE_ID}/readings/latest",
        headers=bearer(tokens[role]),
    )
    assert resp.status_code == 200
    assert resp.json()["has_data"] is False


@pytest.mark.asyncio
async def test_producer_without_greenhouse_forbidden(api, db, tokens):
    """Caso 10: un productor sin vivero asignado no ve datos de ninguno."""
    await db.execute("UPDATE users SET greenhouse_id = NULL WHERE role = 'producer'")
    await db.commit()
    resp = await api.get(LATEST_URL, headers=bearer(tokens["producer"]))
    assert resp.status_code == 403
