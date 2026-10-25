"""
Tests de AI-02 — task "Implement anomaly-detection rules/model".

Criterios de aceptación del item:
  1. Cuando una medición se aparta del patrón reciente de su sensor, se marca
     como anomalía con sensor, valor, rango esperado y hora.
  2. El dashboard la muestra distinta de una alerta de umbral (eso se ve en
     el frontend; aquí se prueba que la API las separa: otra ruta, otro
     formato, sin límite del firmware).
  3. Un valor dentro del patrón esperado no genera anomalía.

Hay dos niveles: las funciones puras de app/anomalies.py (sin BD) y el
endpoint que las usa.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import metrics as metric_catalog
from app.analysis import Prepared, Sample
from app.anomalies import (
    BASELINE_BUCKETS,
    IRRIGATION_SETTLE_SECONDS,
    MIN_BASELINE_SAMPLES,
    Anomaly,
    AnomalyKind,
    Expected,
    detect,
    drop_irrigation_effects,
    expected_range,
    irrigation_windows,
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

ANOMALIES_URL = f"/api/v1/greenhouses/{GREENHOUSE_ID}/anomalies"
BUCKET = 60
TEMP_FLOOR = metric_catalog.BY_KEY["temp_c"].anomaly_floor


def noise(i: int) -> float:
    """Ruido fijo de ±0.2 (sin azar: la prueba tiene que repetirse igual)."""
    return ((i * 7) % 5 - 2) * 0.1


def series_of(values, bucket: int = BUCKET, start: int = 1_760_000_000) -> Prepared:
    """Una serie ya preprocesada: un bucket con dato por valor, `None` = hueco."""
    samples = tuple(
        Sample(epoch=start + i * bucket, value=value, count=1)
        for i, value in enumerate(values) if value is not None
    )
    return Prepared(
        bucket_seconds=bucket, samples=samples, gaps=(), expected_samples=len(values),
        readings_used=len(samples), readings_missing=0,
    )


def flat(count: int, level: float = 22.0):
    return [round(level + noise(i), 2) for i in range(count)]


# ── El rango esperado ──────────────────────────────────────────────────

def test_the_expected_range_is_centered_on_the_median():
    expected = expected_range([20.0, 20.0, 20.0, 21.0, 22.0], floor=0.0)
    # Mediana 20, MAD 0 → sin floor el rango se cierra sobre la mediana.
    assert expected == Expected(center=20.0, low=20.0, high=20.0)


def test_one_odd_value_barely_moves_the_expected_range():
    """Por eso mediana y MAD: una lectura rara dentro del patrón no lo arrastra."""
    calm = expected_range(flat(12), floor=0.0)
    with_spike = expected_range(flat(11) + [40.0], floor=0.0)
    assert abs(with_spike.center - calm.center) < 0.11
    assert with_spike.high - with_spike.low < 2 * (calm.high - calm.low)


def test_the_floor_keeps_a_quiet_sensor_from_flagging_everything():
    expected = expected_range([22.0] * 12, floor=TEMP_FLOOR)
    assert (expected.low, expected.high) == (22.0 - TEMP_FLOOR, 22.0 + TEMP_FLOOR)


# ── Criterio 1: lo que se sale del patrón se marca ────────────────────

def test_a_spike_is_flagged_with_value_expected_range_and_time():
    values = flat(30) + [26.0] + flat(10)
    prepared = series_of(values)
    result = detect(prepared, TEMP_FLOOR, evaluate_from=0)

    assert len(result.anomalies) == 1
    anomaly = result.anomalies[0]
    spike = prepared.samples[30]
    assert anomaly.kind == AnomalyKind.high
    assert anomaly.value == 26.0
    assert anomaly.peak_epoch == anomaly.start_epoch == spike.epoch
    assert anomaly.end_epoch == spike.epoch + BUCKET
    assert anomaly.buckets == 1
    assert anomaly.expected.low < 22.0 < anomaly.expected.high < 26.0


def test_a_drop_is_flagged_as_low():
    result = detect(series_of(flat(30) + [17.5] + flat(5)), TEMP_FLOOR, evaluate_from=0)
    assert [anomaly.kind for anomaly in result.anomalies] == [AnomalyKind.low]
    assert result.anomalies[0].value == 17.5


def test_consecutive_buckets_out_of_range_are_one_anomaly_with_its_peak():
    values = flat(30) + [25.0, 27.5, 26.0] + flat(10)
    result = detect(series_of(values), TEMP_FLOOR, evaluate_from=0)

    assert len(result.anomalies) == 1
    anomaly = result.anomalies[0]
    assert anomaly.buckets == 3
    assert anomaly.value == 27.5                       # el más alejado
    assert anomaly.peak_epoch == anomaly.start_epoch + BUCKET
    assert anomaly.end_epoch == anomaly.start_epoch + 3 * BUCKET


def test_a_high_and_then_a_low_are_two_anomalies():
    values = flat(30) + [26.0, 18.0] + flat(10)
    result = detect(series_of(values), TEMP_FLOOR, evaluate_from=0)
    assert [anomaly.kind for anomaly in result.anomalies] == [AnomalyKind.high, AnomalyKind.low]


def test_a_new_level_is_flagged_once_and_then_becomes_the_pattern():
    """Un cambio sostenido sale como anomalía mientras el patrón no lo incluye."""
    values = flat(30) + flat(40, level=26.0)
    result = detect(series_of(values), TEMP_FLOOR, evaluate_from=0)

    assert len(result.anomalies) == 1
    anomaly = result.anomalies[0]
    # La mediana cambia de nivel cuando más de la mitad del patrón ya es el nuevo.
    assert anomaly.buckets <= BASELINE_BUCKETS // 2 + 1
    assert anomaly.start_epoch == series_of(values).samples[30].epoch


# ── Criterio 3: lo que está dentro del patrón no se marca ─────────────

def test_ordinary_noise_is_not_an_anomaly():
    result = detect(series_of(flat(200)), TEMP_FLOOR, evaluate_from=0)
    assert result.anomalies == ()
    assert result.evaluated == 200 - MIN_BASELINE_SAMPLES


def test_a_gradual_rise_is_not_an_anomaly():
    """El día que se calienta (2 °C por hora en buckets de 1 min) es un patrón."""
    values = [round(18.0 + i * 2 / 60 + noise(i), 2) for i in range(240)]
    assert detect(series_of(values), TEMP_FLOOR, evaluate_from=0).anomalies == ()


def test_a_change_smaller_than_the_floor_is_not_an_anomaly():
    quiet = [22.0] * 30
    assert detect(series_of(quiet + [22.0 + TEMP_FLOOR * 0.9]), TEMP_FLOOR, 0).anomalies == ()
    assert len(detect(series_of(quiet + [22.0 + TEMP_FLOOR * 1.1]), TEMP_FLOOR, 0).anomalies) == 1


# ── Sin patrón no se juzga ─────────────────────────────────────────────

def test_without_enough_history_nothing_is_judged():
    values = flat(MIN_BASELINE_SAMPLES - 1) + [40.0]
    result = detect(series_of(values), TEMP_FLOOR, evaluate_from=0)
    assert result.anomalies == ()
    assert result.evaluated == 0


def test_after_a_long_gap_the_old_pattern_does_not_count():
    """Tras un hueco más largo que la ventana no hay patrón con qué comparar."""
    values = flat(30) + [None] * BASELINE_BUCKETS + [40.0]
    result = detect(series_of(values), TEMP_FLOOR, evaluate_from=0)
    assert result.anomalies == ()
    assert result.evaluated == 30 - MIN_BASELINE_SAMPLES


def test_buckets_before_the_range_are_pattern_but_are_not_judged():
    values = flat(10) + [30.0] + flat(20) + [26.0]
    prepared = series_of(values)
    since = prepared.samples[20].epoch
    result = detect(prepared, TEMP_FLOOR, evaluate_from=since)

    assert [anomaly.value for anomaly in result.anomalies] == [26.0]
    assert result.evaluated == len(values) - 20


def test_the_same_input_gives_the_same_anomalies():
    values = flat(30) + [26.0, 27.0] + flat(30) + [17.0] + flat(10)
    assert detect(series_of(values), TEMP_FLOOR, 0) == detect(series_of(values), TEMP_FLOOR, 0)


# ── El riego explica lo que mueve ──────────────────────────────────────

def test_irrigation_windows_pair_each_start_with_its_stop():
    events = [(100, True), (160, False), (300, True), (310, True), (400, False), (500, True)]
    assert irrigation_windows(events, until=900) == ((100, 160), (300, 400), (500, 900))


def test_a_stop_without_a_start_is_not_a_window():
    assert irrigation_windows([(100, False), (200, False)], until=900) == ()


def anomaly_at(start: int, kind: AnomalyKind) -> Anomaly:
    return Anomaly(kind=kind, start_epoch=start, end_epoch=start + BUCKET, buckets=1,
                   peak_epoch=start, value=0.0, expected=Expected(0.0, 0.0, 0.0))


@pytest.mark.parametrize("metric, kind, start, dropped", [
    ("suelo_pct", AnomalyKind.high, 1_000, True),     # la tierra se moja al regar
    ("nivel_pct", AnomalyKind.low, 1_060, True),      # el tanque baja al regar
    ("suelo_pct", AnomalyKind.high, 1_120 + IRRIGATION_SETTLE_SECONDS - BUCKET, True),
    ("suelo_pct", AnomalyKind.high, 1_120 + IRRIGATION_SETTLE_SECONDS, False),
    ("suelo_pct", AnomalyKind.low, 1_000, False),     # que se seque NO lo explica el riego
    ("nivel_pct", AnomalyKind.high, 1_000, False),
    ("temp_c", AnomalyKind.high, 1_000, False),
    ("suelo_pct", AnomalyKind.high, 880, False),      # empezó antes de regar
])
def test_only_what_irrigation_moves_is_explained_by_it(metric, kind, start, dropped):
    windows = ((1_000, 1_120),)
    kept, explained = drop_irrigation_effects([anomaly_at(start, kind)], metric, windows, BUCKET)
    assert explained == int(dropped)
    assert len(kept) == int(not dropped)


# ── El endpoint ────────────────────────────────────────────────────────

def on_the_minute() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


def readings(start: datetime, count: int, **overrides):
    """Una lectura por minuto, todas las mediciones en su patrón con ruido.

    `overrides` = {columna: {índice: valor}} para sacar alguna de su patrón.
    """
    rows = []
    for i in range(count):
        row = {
            "ts": start + timedelta(minutes=i),
            "temp_c": round(22.0 + noise(i), 1), "hum_aire_pct": 70.0 + (i % 3),
            "suelo_pct": 61 + (i * 7) % 3, "nivel_pct": 75, "luz_nivel": 700 + (i % 2) * 300,
            "co2_ppm": 450 + (i % 3) * 5,
        }
        for column, changes in overrides.items():
            if i in changes:
                row[column] = changes[i]
        rows.append(row)
    return rows


async def get_anomalies(api, token, start, end, **params):
    return await api.get(ANOMALIES_URL, params={
        "from": ts_text(start), "to": ts_text(end), "bucket_seconds": BUCKET, **params,
    }, headers=bearer(token))


def check(data: dict, key: str) -> dict:
    return next(item for item in data["variables"] if item["key"] == key)


@pytest.mark.asyncio
async def test_the_endpoint_flags_the_sensor_value_range_and_time(api, db, tokens):
    """Criterio 1, de punta a punta: la tierra cae de ~62 % a 48 % por 3 min."""
    end = on_the_minute()
    start = end - timedelta(hours=2)
    await insert_readings(db, readings(start, 120, suelo_pct={60: 50, 61: 48, 62: 52}))

    resp = await get_anomalies(api, tokens["producer"], start, end)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert len(data["anomalies"]) == 1
    anomaly = data["anomalies"][0]
    assert anomaly["metric"] == "suelo_pct"
    assert anomaly["label"] == "Humedad de la tierra" and anomaly["unit"] == "%"
    assert anomaly["kind"] == "low"
    assert anomaly["value"] == 48.0
    assert anomaly["expected_min"] < anomaly["expected"] < anomaly["expected_max"]
    assert anomaly["expected_min"] > 48.0
    assert datetime.fromisoformat(anomaly["ts"]) == start + timedelta(minutes=61)
    assert datetime.fromisoformat(anomaly["from"]) == start + timedelta(minutes=60)
    assert datetime.fromisoformat(anomaly["to"]) == start + timedelta(minutes=63)
    assert anomaly["buckets"] == 3
    assert anomaly["ongoing"] is False
    # Una anomalía no es una alerta: no trae límite ni código del firmware.
    assert "threshold" not in anomaly and "code" not in anomaly

    assert check(data, "suelo_pct")["anomalies"] == 1
    assert check(data, "temp_c")["anomalies"] == 0
    assert data["period"]["bucket_seconds"] == BUCKET
    assert data["baseline_seconds"] == BASELINE_BUCKETS * BUCKET


@pytest.mark.asyncio
async def test_the_endpoint_finds_nothing_when_everything_follows_its_pattern(api, db, tokens):
    """Criterio 3, de punta a punta: horas de lecturas normales, cero anomalías."""
    end = on_the_minute()
    start = end - timedelta(hours=3)
    await insert_readings(db, readings(start, 180))

    data = (await get_anomalies(api, tokens["producer"], start, end)).json()
    assert data["has_data"] is True
    assert data["anomalies"] == []
    temp = check(data, "temp_c")
    assert temp["checked"] is True and temp["evaluated"] > 150
    assert temp["floor"] == TEMP_FLOOR


@pytest.mark.asyncio
async def test_light_is_not_checked(api, db, tokens):
    """La luz brinca sola (sol, nubes): no se revisa, y la respuesta lo dice."""
    end = on_the_minute()
    start = end - timedelta(hours=1)
    await insert_readings(db, readings(start, 60, luz_nivel={40: 0}))

    data = (await get_anomalies(api, tokens["producer"], start, end)).json()
    assert data["anomalies"] == []
    light = check(data, "luz_nivel")
    assert light["checked"] is False and light["evaluated"] == 0 and light["floor"] is None


@pytest.mark.asyncio
async def test_the_first_minutes_of_the_range_use_the_readings_before_it(api, db, tokens):
    """El patrón de los primeros buckets sale de antes del rango pedido."""
    end = on_the_minute()
    start = end - timedelta(hours=1)
    earlier = start - timedelta(minutes=30)
    await insert_readings(db, readings(earlier, 90, temp_c={32: 28.0}))

    data = (await get_anomalies(api, tokens["producer"], start, end)).json()
    assert [a["metric"] for a in data["anomalies"]] == ["temp_c"]
    assert datetime.fromisoformat(data["anomalies"][0]["ts"]) == earlier + timedelta(minutes=32)


@pytest.mark.asyncio
async def test_an_anomaly_that_reaches_the_newest_readings_is_ongoing(api, db, tokens):
    end = on_the_minute()
    start = end - timedelta(hours=1)
    await insert_readings(db, readings(start, 60, co2_ppm={57: 900, 58: 950, 59: 980}))

    data = (await get_anomalies(api, tokens["producer"], start, end)).json()
    assert len(data["anomalies"]) == 1
    anomaly = data["anomalies"][0]
    assert anomaly["metric"] == "co2_ppm" and anomaly["kind"] == "high"
    assert anomaly["ongoing"] is True
    assert anomaly["id"] == f"co2_ppm-{int((start + timedelta(minutes=57)).timestamp())}"


@pytest.mark.asyncio
async def test_the_newest_anomaly_comes_first(api, db, tokens):
    end = on_the_minute()
    start = end - timedelta(hours=2)
    await insert_readings(db, readings(start, 120, temp_c={40: 28.0}, nivel_pct={90: 40}))

    data = (await get_anomalies(api, tokens["producer"], start, end)).json()
    assert [a["metric"] for a in data["anomalies"]] == ["nivel_pct", "temp_c"]


async def add_pump_with_events(db, *events):
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


@pytest.mark.asyncio
async def test_soil_getting_wet_while_irrigating_is_not_an_anomaly(api, db, tokens):
    """El riego moja la tierra y vacía el tanque: es lo esperado, no algo raro."""
    end = on_the_minute()
    start = end - timedelta(hours=2)
    wet = {i: 80 for i in range(60, 120)}
    tank = {i: 60 for i in range(60, 120)}
    await insert_readings(db, readings(start, 120, suelo_pct=wet, nivel_pct=tank))
    await add_pump_with_events(
        db,
        (start + timedelta(minutes=59, seconds=30), "on"),
        (start + timedelta(minutes=62), "off"),
    )

    data = (await get_anomalies(api, tokens["producer"], start, end)).json()
    assert data["anomalies"] == []
    assert check(data, "suelo_pct")["explained_by_irrigation"] == 1
    assert check(data, "nivel_pct")["explained_by_irrigation"] == 1


@pytest.mark.asyncio
async def test_a_pump_already_on_before_the_range_still_explains_it(api, db, tokens):
    end = on_the_minute()
    start = end - timedelta(hours=1)
    await insert_readings(db, readings(start - timedelta(minutes=30), 90,
                                       suelo_pct={i: 85 for i in range(31, 90)}))
    await add_pump_with_events(db, (start - timedelta(hours=3), "on"))

    data = (await get_anomalies(api, tokens["producer"], start, end)).json()
    assert data["anomalies"] == []
    assert check(data, "suelo_pct")["explained_by_irrigation"] == 1


@pytest.mark.asyncio
async def test_soil_drying_while_irrigating_is_still_an_anomaly(api, db, tokens):
    """Si regando la tierra SE SECA, algo anda mal (fuga, bomba sin agua)."""
    end = on_the_minute()
    start = end - timedelta(hours=2)
    await insert_readings(db, readings(start, 120, suelo_pct={60: 45, 61: 44}))
    await add_pump_with_events(
        db, (start + timedelta(minutes=59), "on"), (start + timedelta(minutes=63), "off"),
    )

    data = (await get_anomalies(api, tokens["producer"], start, end)).json()
    assert [(a["metric"], a["kind"]) for a in data["anomalies"]] == [("suelo_pct", "low")]


@pytest.mark.asyncio
async def test_a_downed_sensor_is_not_an_anomaly(api, db, tokens):
    """`null` = sensor caído: eso lo dice FALLA_* en las alertas, no AI-02."""
    end = on_the_minute()
    start = end - timedelta(hours=1)
    await insert_readings(db, readings(start, 60, temp_c={i: None for i in range(30, 40)}))

    data = (await get_anomalies(api, tokens["producer"], start, end)).json()
    assert data["anomalies"] == []


@pytest.mark.asyncio
async def test_the_same_request_twice_gives_the_same_anomalies(api, db, tokens):
    end = on_the_minute()
    start = end - timedelta(hours=2)
    await insert_readings(db, readings(start, 120, temp_c={50: 27.0}, co2_ppm={80: 300}))

    first = (await get_anomalies(api, tokens["producer"], start, end)).json()
    second = (await get_anomalies(api, tokens["producer"], start, end)).json()
    assert first["anomalies"] == second["anomalies"] and len(first["anomalies"]) == 2
    assert first["variables"] == second["variables"]


@pytest.mark.asyncio
async def test_without_readings_nothing_is_invented(api, tokens):
    end = on_the_minute()
    data = (await get_anomalies(api, tokens["producer"], end - timedelta(hours=6), end)).json()
    assert data["has_data"] is False
    assert data["anomalies"] == []
    assert all(item["evaluated"] == 0 for item in data["variables"])


@pytest.mark.asyncio
async def test_the_default_range_is_the_last_24_hours(api, tokens):
    data = (await api.get(ANOMALIES_URL, headers=bearer(tokens["admin"]))).json()
    assert data["period"]["hours"] == pytest.approx(24, abs=0.01)


@pytest.mark.asyncio
async def test_a_bucket_too_small_for_the_range_is_rejected(api, tokens):
    end = on_the_minute()
    resp = await get_anomalies(api, tokens["producer"], end - timedelta(days=7), end)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_range"


@pytest.mark.asyncio
async def test_a_producer_cannot_see_another_greenhouse(api, tokens):
    resp = await api.get(f"/api/v1/greenhouses/{OTHER_GREENHOUSE_ID}/anomalies",
                         headers=bearer(tokens["producer"]))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_anomalies_need_a_session(api):
    assert (await api.get(ANOMALIES_URL)).status_code == 401
