"""
Tests de AI-04 — task "Extend the recommendation engine to weigh multiple
variables".

Criterios de aceptación del item:
  1. Cuando corre la lógica de recomendación, consume la temperatura, la
     humedad de la tierra y el tiempo desde el último riego.
  2. Si cambia cualquiera de esas variables y la lógica vuelve a correr, el
     resultado puede cambiar.

Hay dos niveles: las funciones puras de app/recommendation.py (sin BD) y el
endpoint que les junta las entradas.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.recommendation import (
    BASE_THRESHOLD,
    LONG_WITHOUT_IRRIGATION_POINTS,
    RECENT_IRRIGATION_POINTS,
    THRESHOLD_MAX,
    THRESHOLD_MIN,
    WATCH_MARGIN,
    Inputs,
    Outcome,
    irrigation_points,
    recommend,
    temperature_points,
)
from tests.conftest import (
    GATEWAY_ID,
    GREENHOUSE_ID,
    OTHER_GREENHOUSE_ID,
    bearer,
    insert_readings,
    now_iso,
    ts_text,
)

URL = f"/api/v1/greenhouses/{GREENHOUSE_ID}/irrigation/recommendation"

# Un día templado, la tierra un punto arriba del mínimo ideal y el último
# riego de hace 6 h: el umbral queda en la base y se recomienda regar pronto.
BASE = dict(soil_pct=61.0, temp_c=22.0, hours_since_irrigation=6.0)


def run(**changes):
    return recommend(Inputs(**{**BASE, **changes}))


# ── Criterio 1: las tres variables entran ──────────────────────────────

def test_the_base_case_uses_the_ideal_minimum_as_threshold():
    result = run()
    assert result.threshold == BASE_THRESHOLD == 60.0
    assert result.watch_until == BASE_THRESHOLD + WATCH_MARGIN
    assert result.outcome == Outcome.irrigate_soon


def test_heat_raises_the_threshold_and_cold_lowers_it():
    assert temperature_points(22.0) == 0.0
    assert temperature_points(27.0) == pytest.approx(2.0)
    assert temperature_points(16.0) == pytest.approx(-4.0)
    assert run(temp_c=27.0).temperature_points == 2.0
    assert run(temp_c=27.0).threshold == 62.0


def test_a_recent_irrigation_lowers_the_threshold_and_a_long_wait_raises_it():
    assert irrigation_points(6.0) == 0.0
    assert irrigation_points(0.5) == RECENT_IRRIGATION_POINTS
    assert irrigation_points(30.0) == LONG_WITHOUT_IRRIGATION_POINTS
    assert run(hours_since_irrigation=0.5).threshold == 60.0 + RECENT_IRRIGATION_POINTS
    assert run(hours_since_irrigation=30.0).threshold == 60.0 + LONG_WITHOUT_IRRIGATION_POINTS


def test_all_three_move_the_same_threshold_together():
    result = run(temp_c=26.0, hours_since_irrigation=30.0)
    assert result.temperature_points == 1.0
    assert result.irrigation_points == LONG_WITHOUT_IRRIGATION_POINTS
    assert result.threshold == 60.0 + 1.0 + LONG_WITHOUT_IRRIGATION_POINTS


# ── Criterio 2: cambiar cualquiera puede cambiar el resultado ─────────

@pytest.mark.parametrize("change, outcome", [
    ({"soil_pct": 58.0}, Outcome.irrigate),              # la tierra se secó
    ({"soil_pct": 70.0}, Outcome.no_irrigation),         # la tierra está bien
    ({"temp_c": 29.0}, Outcome.irrigate),                # calor: regar antes
    ({"temp_c": 15.0}, Outcome.no_irrigation),           # frío: puede esperar
    ({"hours_since_irrigation": 1.0}, Outcome.no_irrigation),   # se acaba de regar
    ({"hours_since_irrigation": 30.0}, Outcome.irrigate),       # mucho sin regar
])
def test_changing_one_variable_changes_the_outcome(change, outcome):
    assert run().outcome == Outcome.irrigate_soon
    assert run(**change).outcome == outcome


def test_the_same_inputs_give_the_same_recommendation():
    assert run(temp_c=27.3, hours_since_irrigation=3.1) == run(temp_c=27.3, hours_since_irrigation=3.1)


# ── Las orillas ────────────────────────────────────────────────────────

def test_soil_below_the_radish_limit_is_urgent_even_right_after_irrigating():
    """Si recién se regó y la tierra sigue abajo del límite, algo anda mal."""
    result = run(soil_pct=45.0, hours_since_irrigation=0.2)
    assert result.outcome == Outcome.irrigate
    assert result.urgent is True


def test_irrigating_below_the_threshold_is_not_urgent_above_the_limit():
    result = run(soil_pct=55.0)
    assert result.outcome == Outcome.irrigate and result.urgent is False


def test_waterlogged_soil_is_never_irrigated_even_with_heat():
    assert run(soil_pct=85.0, temp_c=35.0, hours_since_irrigation=48.0).outcome == Outcome.too_wet


def test_while_the_pump_runs_the_recommendation_says_so():
    assert run(soil_pct=40.0, irrigating=True).outcome == Outcome.irrigating


def test_without_soil_data_nothing_is_recommended():
    result = run(soil_pct=None)
    assert result.outcome == Outcome.insufficient_data
    assert result.urgent is False and result.tank_blocks is False


def test_missing_temperature_or_history_counts_as_no_adjustment():
    result = run(temp_c=None, hours_since_irrigation=None)
    assert result.temperature_points == 0.0 and result.irrigation_points == 0.0
    assert result.threshold == BASE_THRESHOLD


@pytest.mark.parametrize("change, threshold", [
    ({"temp_c": 40.0, "hours_since_irrigation": 30.0}, THRESHOLD_MAX),
    ({"temp_c": 5.0, "hours_since_irrigation": 0.5}, THRESHOLD_MIN),
])
def test_the_threshold_stays_between_the_radish_limit_and_its_ideal(change, threshold):
    assert run(**change).threshold == threshold


def test_a_critical_tank_is_flagged_when_irrigation_is_recommended():
    assert run(soil_pct=55.0, tank_pct=20.0).tank_blocks is True
    assert run(soil_pct=55.0, tank_pct=60.0).tank_blocks is False
    assert run(soil_pct=70.0, tank_pct=20.0).tank_blocks is False     # no se iba a regar


# ── El endpoint ────────────────────────────────────────────────────────

def recent_readings(minutes: int = 4, **values):
    """Una lectura cada 20 s en los últimos `minutes`, con estos valores."""
    now = datetime.now(timezone.utc)
    base = {"temp_c": 22.0, "suelo_pct": 61, "nivel_pct": 70, **values}
    return [{"ts": now - timedelta(seconds=20 * i), **base} for i in range(minutes * 3)]


async def add_pump(db, *events):
    await db.execute(
        """
        INSERT INTO actuators (actuator_id, name, type, greenhouse_id, area, control_channel,
                               gateway_device_id, status, registered_at)
        VALUES ('pump-1', 'Bomba de riego', 'pump', ?, 'general', 'L298N-B/D5', ?, 'active', ?)
        """,
        (GREENHOUSE_ID, GATEWAY_ID, now_iso()),
    )
    for moment, event in events:
        await db.execute(
            """
            INSERT INTO actuator_events (actuator_id, greenhouse_id, device_id, event, origin,
                                         reason, reading_ts, created_at)
            VALUES ('pump-1', ?, ?, ?, 'auto', 'prueba', ?, ?)
            """,
            (GREENHOUSE_ID, GATEWAY_ID, event, ts_text(moment), now_iso()),
        )
    await db.commit()


def hours_ago(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


@pytest.mark.asyncio
async def test_the_endpoint_consumes_soil_temperature_and_last_irrigation(api, db, tokens):
    """Criterio 1, de punta a punta: las tres entradas salen como se usaron."""
    await insert_readings(db, recent_readings(suelo_pct=55, temp_c=27.0))
    await add_pump(db, (hours_ago(6), "on"), (hours_ago(5.99), "off"))

    resp = await api.get(URL, headers=bearer(tokens["producer"]))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["outcome"] == "irrigate"
    assert data["inputs"]["soil"]["value"] == 55.0
    assert data["inputs"]["soil"]["unit"] == "%"
    assert data["inputs"]["soil"]["samples"] == 12
    assert data["inputs"]["temperature"]["value"] == 27.0
    assert data["inputs"]["last_irrigation"]["hours_since"] == pytest.approx(6, abs=0.01)
    assert data["inputs"]["last_irrigation"]["irrigating"] is False
    assert data["inputs"]["tank"]["value"] == 70.0
    assert data["breakdown"] == {
        "base": 60.0, "temperature": 2.0, "last_irrigation": 0.0, "min": 50.0, "max": 65.0,
    }
    assert data["threshold"] == 62.0
    assert data["missing"] == []


@pytest.mark.asyncio
async def test_a_recent_irrigation_changes_the_endpoint_answer(api, db, tokens):
    """Criterio 2, de punta a punta: solo cambia el último riego."""
    await insert_readings(db, recent_readings(suelo_pct=59))
    await add_pump(db, (hours_ago(6), "on"), (hours_ago(5.99), "off"))
    before = (await api.get(URL, headers=bearer(tokens["producer"]))).json()

    await db.execute(
        """
        INSERT INTO actuator_events (actuator_id, greenhouse_id, device_id, event, origin,
                                     reason, reading_ts, created_at)
        VALUES ('pump-1', ?, ?, ?, 'manual', 'prueba', ?, ?)
        """,
        (GREENHOUSE_ID, GATEWAY_ID, "on", ts_text(hours_ago(0.5)), now_iso()),
    )
    await db.execute(
        """
        INSERT INTO actuator_events (actuator_id, greenhouse_id, device_id, event, origin,
                                     reason, reading_ts, created_at)
        VALUES ('pump-1', ?, ?, ?, 'manual', 'prueba', ?, ?)
        """,
        (GREENHOUSE_ID, GATEWAY_ID, "off", ts_text(hours_ago(0.49)), now_iso()),
    )
    await db.commit()
    after = (await api.get(URL, headers=bearer(tokens["producer"]))).json()

    assert before["outcome"] == "irrigate"
    assert after["outcome"] == "no_irrigation"
    assert after["breakdown"]["last_irrigation"] == RECENT_IRRIGATION_POINTS


@pytest.mark.asyncio
async def test_a_single_odd_reading_does_not_move_the_recommendation(api, db, tokens):
    rows = recent_readings(suelo_pct=62)
    rows[0]["suelo_pct"] = 30                      # la más reciente, un parpadeo
    await insert_readings(db, rows)

    data = (await api.get(URL, headers=bearer(tokens["producer"]))).json()
    assert data["inputs"]["soil"]["value"] == 62.0
    assert data["outcome"] == "irrigate_soon"


@pytest.mark.asyncio
async def test_old_readings_are_not_used(api, db, tokens):
    """Una lectura más vieja que el límite de "lectura vieja" no cuenta."""
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    await insert_readings(db, [{"ts": old, "suelo_pct": 40, "temp_c": 30.0, "nivel_pct": 70}])

    data = (await api.get(URL, headers=bearer(tokens["producer"]))).json()
    assert data["outcome"] == "insufficient_data"
    assert set(data["missing"]) == {"soil", "temperature", "last_irrigation", "tank"}
    assert data["inputs"]["soil"]["value"] is None and data["inputs"]["soil"]["samples"] == 0


@pytest.mark.asyncio
async def test_without_irrigation_history_it_still_recommends(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=58))

    data = (await api.get(URL, headers=bearer(tokens["producer"]))).json()
    assert data["outcome"] == "irrigate"
    assert data["missing"] == ["last_irrigation"]
    assert data["inputs"]["last_irrigation"] == {
        "last_started_at": None, "hours_since": None, "irrigating": False,
    }


@pytest.mark.asyncio
async def test_a_pump_still_on_is_reported_as_irrigating(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=45))
    await add_pump(db, (hours_ago(0.01), "on"))

    data = (await api.get(URL, headers=bearer(tokens["producer"]))).json()
    assert data["outcome"] == "irrigating"
    assert data["inputs"]["last_irrigation"]["irrigating"] is True


@pytest.mark.asyncio
async def test_a_critical_tank_is_reported(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=45, nivel_pct=20))

    data = (await api.get(URL, headers=bearer(tokens["producer"]))).json()
    assert data["outcome"] == "irrigate" and data["urgent"] is True
    assert data["tank_blocks"] is True


@pytest.mark.asyncio
async def test_other_greenhouses_readings_do_not_count(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=40), greenhouse_id=OTHER_GREENHOUSE_ID)

    data = (await api.get(URL, headers=bearer(tokens["admin"]))).json()
    assert data["outcome"] == "insufficient_data"


@pytest.mark.asyncio
async def test_a_producer_cannot_see_another_greenhouse(api, tokens):
    resp = await api.get(f"/api/v1/greenhouses/{OTHER_GREENHOUSE_ID}/irrigation/recommendation",
                         headers=bearer(tokens["producer"]))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_the_recommendation_needs_a_session(api):
    assert (await api.get(URL)).status_code == 401
