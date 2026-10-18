"""
Tests de NF-01 — sensor de CO₂ (Sensirion SCD40/SCD41 por I²C).

Criterios (planeación del Sprint 3; el item aún no tiene historia en el PDF):
  1. El sensor se integra: `co2_ppm` llega en la lectura del Arduino, se
     guarda y se ve en el dashboard (últimas lecturas, histórico, análisis).
  2. Los umbrales del firmware (CO2_LIMITE_MIN/MAX) generan alertas con
     sensor, valor, umbral y hora, igual que los demás sensores (ALERT-01).
  3. Lectura periódica y guardado correcto: `null` = sensor caído, nunca 0,
     y la BD de la Pi (creada antes del CO₂) gana la columna sin perder nada.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import database
from app.config import settings
from app.firmware import CONSTANTS, LIMIT_ALERTS
from tests.conftest import (
    GATEWAY_API_KEY,
    GATEWAY_ID,
    GREENHOUSE_ID,
    bearer,
    insert_readings,
    ts_text,
)

INGEST_URL = f"/api/v1/devices/{GATEWAY_ID}/readings"
BASE = f"/api/v1/greenhouses/{GREENHOUSE_ID}"
T0 = (datetime.now(timezone.utc) - timedelta(minutes=20)).replace(microsecond=0)


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def reading(seconds: float, co2_ppm=450, *codes: str, **overrides) -> dict:
    """Una línea del firmware nuevo, como la manda la Pi."""
    base = {
        "ts": at(seconds), "ms": 1000, "temp_c": 23.0, "hum_aire_pct": 70.0,
        "suelo_raw": 380, "suelo_pct": 62, "nivel_raw": 120, "nivel_pct": 80,
        "luz_raw": 306, "luz_nivel": 717, "co2_ppm": co2_ppm, "es_dia": True,
        "sol_min_hoy": 42, "sol_min_prev": None, "riego_sugerido": False,
        "bomba_habilitada": True, "vent": "BASE",
        "estado": "ALERTA" if codes else "OK", "alertas": list(codes),
    }
    base.update(overrides)
    return base


async def ingest(api, *readings) -> dict:
    resp = await api.post(INGEST_URL, json={"readings": list(readings)},
                          headers={"X-API-Key": GATEWAY_API_KEY})
    assert resp.status_code == 202, resp.text
    return resp.json()


async def alert_rows(db) -> list:
    cursor = await db.execute("SELECT * FROM alerts ORDER BY id")
    return [dict(row) for row in await cursor.fetchall()]


# ── Criterio 1: la lectura con CO₂ se acepta y se guarda ───────────────

@pytest.mark.asyncio
async def test_a_reading_with_co2_is_stored(api, db):
    data = await ingest(api, reading(0, 452))
    assert data["accepted"] == 1 and data["rejected"] == []
    cursor = await db.execute("SELECT co2_ppm FROM readings")
    assert [row["co2_ppm"] for row in await cursor.fetchall()] == [452]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["CO2_ALTO", "CO2_BAJO", "CO2_ALTO_LEVE", "CO2_BAJO_LEVE", "FALLA_CO2"])
async def test_every_co2_code_of_the_firmware_is_accepted(api, code):
    """Un código que el backend no conoce tumbaría la lectura COMPLETA."""
    co2 = None if code == "FALLA_CO2" else 450
    data = await ingest(api, reading(0, co2, code))
    assert data["accepted"] == 1 and data["rejected"] == [], data


@pytest.mark.asyncio
async def test_co2_null_is_a_downed_sensor_not_a_zero(api, db):
    await ingest(api, reading(0, None, "FALLA_CO2"))
    cursor = await db.execute("SELECT co2_ppm FROM readings")
    assert (await cursor.fetchone())["co2_ppm"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [-1, 40001, "mucho"])
async def test_an_impossible_co2_value_rejects_only_that_reading(api, db, value):
    data = await ingest(api, reading(0, value), reading(2, 460))
    assert data["accepted"] == 1
    assert [item["index"] for item in data["rejected"]] == [0]
    assert "co2_ppm" in data["rejected"][0]["reason"]


# ── Criterio 2: umbrales del firmware → alertas ────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("code, value, threshold, name, comparison, direction", [
    ("CO2_ALTO", 1650, 1500, "CO2_LIMITE_MAX", ">", "high"),
    ("CO2_BAJO", 250, 300, "CO2_LIMITE_MIN", "<", "low"),
])
async def test_a_co2_limit_creates_an_alert_with_value_and_threshold(
        api, tokens, code, value, threshold, name, comparison, direction):
    await ingest(api, reading(0, value, code))
    data = (await api.get(f"{BASE}/alerts", headers=bearer(tokens["producer"]))).json()
    assert data["active_count"] == 1
    alert = data["alerts"][0]
    assert (alert["metric"], alert["code"], alert["value"]) == ("co2_ppm", code, value)
    assert (alert["threshold"], alert["threshold_name"], alert["comparison"]) == (
        threshold, name, comparison)
    assert alert["direction"] == direction
    assert (alert["label"], alert["unit"]) == ("CO₂", "ppm")
    assert datetime.fromisoformat(alert["reading_ts"]) == T0


def test_the_co2_thresholds_are_the_firmware_constants():
    """El número que guarda la alerta es el del .ino (test_firmware_mirror
    compara CONSTANTS contra el firmware)."""
    assert LIMIT_ALERTS["CO2_ALTO"].threshold == CONSTANTS["CO2_LIMITE_MAX"]
    assert LIMIT_ALERTS["CO2_BAJO"].threshold == CONSTANTS["CO2_LIMITE_MIN"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code, value", [("CO2_ALTO_LEVE", 1200), ("CO2_BAJO_LEVE", 350)])
async def test_a_co2_warning_is_not_an_alert(api, db, code, value):
    await ingest(api, reading(0, value, code))
    assert await alert_rows(db) == []


@pytest.mark.asyncio
async def test_a_failing_co2_sensor_neither_opens_nor_closes_an_alert(api, db):
    await ingest(api, reading(0, None, "FALLA_CO2"))
    assert await alert_rows(db) == []

    await ingest(api, reading(10, 1700, "CO2_ALTO"),
                 *[reading(s, None, "FALLA_CO2") for s in (12, 60, 120, 200)])
    (saved,) = await alert_rows(db)
    assert saved["status"] == "active" and saved["clear_since"] is None


@pytest.mark.asyncio
async def test_co2_back_inside_the_limit_clears_the_alert(api, db, monkeypatch):
    monkeypatch.setattr(settings, "alert_clear_seconds", 60)
    await ingest(api, reading(0, 1700, "CO2_ALTO"),
                 *[reading(s, 900) for s in (2, 40, 80)])
    (saved,) = await alert_rows(db)
    assert saved["status"] == "cleared"


# ── Criterio 1: se ve en el dashboard ──────────────────────────────────

@pytest.mark.asyncio
async def test_latest_readings_include_co2_in_ppm(api, tokens):
    now = datetime.now(timezone.utc)
    await ingest(api, reading(0, 470, ts=ts_text(now - timedelta(seconds=30))))
    data = (await api.get(f"{BASE}/readings/latest", headers=bearer(tokens["producer"]))).json()
    co2 = next(item for item in data["metrics"] if item["key"] == "co2_ppm")
    assert (co2["value"], co2["unit"], co2["label"]) == (470, "ppm", "CO₂")
    assert co2["sensor_down"] is False and co2["raw"] is None


@pytest.mark.asyncio
async def test_latest_readings_flag_a_downed_co2_sensor(api, tokens):
    now = datetime.now(timezone.utc)
    await ingest(api, reading(0, 470, ts=ts_text(now - timedelta(seconds=40))),
                 reading(0, None, "FALLA_CO2", ts=ts_text(now - timedelta(seconds=20))))
    data = (await api.get(f"{BASE}/readings/latest", headers=bearer(tokens["producer"]))).json()
    co2 = next(item for item in data["metrics"] if item["key"] == "co2_ppm")
    assert co2["sensor_down"] is True
    assert co2["value"] == 470                      # el último válido, nunca 0


@pytest.mark.asyncio
async def test_stored_readings_return_co2(api, db, tokens):
    end = datetime.now(timezone.utc) - timedelta(minutes=1)
    await insert_readings(db, [{"ts": end, "temp_c": 23.0, "co2_ppm": 515, "estado": "OK"}])
    data = (await api.get(f"{BASE}/readings", headers=bearer(tokens["producer"]))).json()
    assert data[0]["co2_ppm"] == 515


@pytest.mark.asyncio
async def test_co2_can_be_graphed(api, db, tokens):
    end = datetime.now(timezone.utc) - timedelta(minutes=1)
    await insert_readings(db, [
        {"ts": end - timedelta(seconds=20 * i), "co2_ppm": 450 + i, "estado": "OK"}
        for i in range(30)
    ] + [{"ts": end - timedelta(seconds=5), "co2_ppm": None, "estado": "ALERTA"}])
    data = (await api.get(f"{BASE}/readings/series", params={"metric": "co2_ppm"},
                          headers=bearer(tokens["producer"]))).json()
    assert (data["metric"], data["unit"], data["label"]) == ("co2_ppm", "ppm", "CO₂")
    assert data["reading_count"] == 30                 # el null no se grafica
    assert data["value_min"] == 450 and data["value_max"] == 479


@pytest.mark.asyncio
async def test_co2_has_a_trend_and_reports_its_missing_readings(api, db, tokens):
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=1)
    start = end - timedelta(hours=2)
    rows = [{"ts": start + timedelta(minutes=i), "temp_c": 23.0, "hum_aire_pct": 70.0,
             "suelo_pct": 62, "nivel_pct": 80, "luz_nivel": 700,
             "co2_ppm": 450 + 2 * i, "estado": "OK"} for i in range(120)]
    rows[-1]["co2_ppm"] = None
    await insert_readings(db, rows)
    data = (await api.get(f"{BASE}/analysis", params={"from": ts_text(start), "to": ts_text(end)},
                          headers=bearer(tokens["producer"]))).json()
    co2 = next(item for item in data["variables"] if item["key"] == "co2_ppm")
    assert co2["trend"] == "rising"                     # +2 ppm por minuto
    assert co2["unit"] == "ppm"
    assert co2["readings_missing"] == 1


# ── Criterio 3: la BD de la Pi gana la columna sin perder nada ─────────

OLD_READINGS_TABLE = """
CREATE TABLE readings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id        TEXT    NOT NULL,
    greenhouse_id    TEXT    NOT NULL,
    received_at      TEXT    NOT NULL,
    ts               TEXT    NOT NULL,
    ms               INTEGER,
    temp_c           REAL,
    hum_aire_pct     REAL,
    suelo_raw        INTEGER,
    suelo_pct        INTEGER,
    nivel_raw        INTEGER,
    nivel_pct        INTEGER,
    luz_raw          INTEGER,
    luz_nivel        INTEGER,
    es_dia           INTEGER,
    sol_min_hoy      INTEGER,
    sol_min_prev     INTEGER,
    riego_sugerido   INTEGER,
    bomba_habilitada INTEGER,
    vent             TEXT,
    estado           TEXT,
    alertas          TEXT,
    UNIQUE(device_id, ts)
);
"""


@pytest.mark.asyncio
async def test_a_database_from_before_co2_gains_the_column_and_keeps_its_readings(tmp_path):
    path = tmp_path / "pi-antes-del-co2.db"
    with sqlite3.connect(path) as old:
        old.executescript(OLD_READINGS_TABLE)
        old.execute(
            "INSERT INTO readings (device_id, greenhouse_id, received_at, ts, temp_c) "
            "VALUES ('pi-vivero-01', 'vivero-rabano-01', '2026-10-01T00:00:00.000+00:00', "
            "'2026-10-01T00:00:00.000+00:00', 22.5)")

    for _ in range(2):                                   # arrancar dos veces no truena
        conn = await database.init_db(str(path))
        try:
            cursor = await conn.execute("PRAGMA table_info(readings)")
            columns = [row["name"] for row in await cursor.fetchall()]
            cursor = await conn.execute("SELECT * FROM readings")
            rows = [dict(row) for row in await cursor.fetchall()]
        finally:
            await database.close_db()                    # si algo falla, que no se quede colgado
        assert columns.count("co2_ppm") == 1
        assert [(row["temp_c"], row["co2_ppm"]) for row in rows] == [(22.5, None)]
