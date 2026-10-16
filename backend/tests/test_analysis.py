"""
Tests de AI-01 — tasks "data-preprocessing pipeline" y "trend-analysis logic".

Criterios de aceptación del item:
  1. Con las mediciones guardadas de un periodo, el análisis devuelve un
     indicador de tendencia por variable — subiendo, estable o bajando —
     junto con el periodo que usó.
  2. Con la misma entrada, el resultado es reproducible.
  3. Los huecos de datos se reportan en el resultado, no se ignoran en
     silencio.

Hay dos niveles: las funciones puras de app/analysis.py (preprocesado y
tendencia, sin BD) y el endpoint que las usa.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import history
from app import metrics as metric_catalog
from app.analysis import (
    MIN_SAMPLES_FOR_TREND,
    Prepared,
    TrendDirection,
    prepare,
    trend,
)
from app.history import Window
from tests.conftest import (
    GREENHOUSE_ID,
    OTHER_GREENHOUSE_ID,
    bearer,
    insert_readings,
    ts_text,
)

ANALYSIS_URL = f"/api/v1/greenhouses/{GREENHOUSE_ID}/analysis"


def on_the_minute(minutes_ago: int = 0) -> datetime:
    """Una hora redonda (sin segundos), para que los buckets no se corran."""
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.replace(second=0, microsecond=0)


def window_of(hours: int, end: datetime) -> Window:
    return Window(start=end - timedelta(hours=hours), end=end)


def series(start: datetime, count: int, step_seconds: int, value_at, **fixed):
    """Lecturas regulares; `value_at(i)` da la temperatura de cada una."""
    return [
        {"ts": start + timedelta(seconds=step_seconds * i),
         "temp_c": value_at(i), "hum_aire_pct": 70.0, "suelo_pct": 62,
         "nivel_pct": 75, "luz_nivel": 700, **fixed}
        for i in range(count)
    ]


def variable(data: dict, key: str) -> dict:
    return next(item for item in data["variables"] if item["key"] == key)


# ── Preprocesado (task 1), en funciones puras ──────────────────────────

def test_preprocessing_averages_each_bucket_and_orders_them():
    """Caso 1: cada bucket queda con el promedio de sus lecturas, en orden."""
    end = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    window = window_of(1, end)
    base = int(window.start.timestamp())
    # A propósito desordenados: el preprocesado los ordena.
    buckets = [(base + 600, 21.0, 10), (base, 20.0, 10), (base + 1200, 22.0, 5)]

    result = prepare(buckets, window, 600)
    assert [sample.value for sample in result.samples] == [20.0, 21.0, 22.0]
    assert [sample.epoch for sample in result.samples] == [base, base + 600, base + 1200]
    assert result.readings_used == 25
    assert result.expected_samples == 6          # una hora en buckets de 10 min
    assert result.coverage_pct == 50.0


def test_preprocessing_drops_buckets_without_data_and_out_of_range():
    """Caso 2: un bucket vacío o fuera del rango no se convierte en un dato."""
    end = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    window = window_of(1, end)
    base = int(window.start.timestamp())

    result = prepare([
        (base, 20.0, 3),
        (base + 600, None, 0),            # el sensor no dio nada en ese bucket
        (base - 600, 99.0, 5),            # antes del rango
        (base + 7200, 99.0, 5),           # después del rango
    ], window, 600)

    assert [sample.value for sample in result.samples] == [20.0]
    assert result.readings_used == 3


def test_preprocessing_reports_null_readings_as_missing():
    """Caso 3 (criterio 3): las lecturas con la medición en null se reportan."""
    end = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    window = window_of(1, end)
    base = int(window.start.timestamp())

    result = prepare([(base, 20.0, 8)], window, 600,
                     readings_total=10, readings_valid=8)
    assert result.readings_missing == 2


@pytest.mark.parametrize("filled, expected_gaps", [
    ([0], 1),                 # solo el primero: falta todo lo demás
    ([5], 1),                 # solo el último (el 5 cierra la hora): falta antes
    ([4], 2),                 # uno en medio: falta antes y después
    ([0, 1, 2, 3, 4, 5], 0),  # completo
    ([0, 3, 5], 2),           # dos huecos en medio
])
def test_preprocessing_finds_every_gap(filled, expected_gaps):
    """Caso 4 (criterio 3): los huecos se encuentran, también en los bordes."""
    end = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    window = window_of(1, end)
    base = int(window.start.timestamp())

    result = prepare([(base + 600 * i, 20.0, 1) for i in filled], window, 600)
    assert len(result.gaps) == expected_gaps
    assert all(gap.seconds > 0 and gap.buckets > 0 for gap in result.gaps)


def test_preprocessing_without_data_is_one_big_gap():
    """Caso 5: sin ninguna lectura, el hueco es todo el periodo."""
    end = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    window = window_of(1, end)

    result = prepare([], window, 600)
    assert result.has_data is False
    assert len(result.gaps) == 1
    assert result.gaps[0].seconds == 3600
    assert result.coverage_pct == 0.0
    assert result.gap_seconds == 3600


# ── Tendencia (task 2), en funciones puras ─────────────────────────────

def line(count: int, first: float, rise: float, bucket: int = 600) -> Prepared:
    base = 1_792_000_000
    window = Window(
        start=history.from_epoch(base),
        end=history.from_epoch(base + bucket * count),
    )
    return prepare([(base + bucket * i, first + rise * i, 1) for i in range(count)],
                   window, bucket)


@pytest.mark.parametrize("rise, direction", [
    (0.5, TrendDirection.rising),
    (-0.5, TrendDirection.falling),
    (0.0, TrendDirection.stable),
])
def test_the_trend_indicator_says_rising_stable_or_falling(rise, direction):
    """Caso 6 (criterio 1): el indicador es subida, estable o bajada."""
    result = trend(line(12, 20.0, rise), stable_band=0.5, decimals=1)
    assert result.direction is direction


def test_a_change_inside_the_stable_band_is_stable():
    """Caso 7: un cambio más chico que la banda es ruido, no tendencia.

    La banda existe porque estos sensores tienen ruido: sin ella, una décima
    de grado en seis horas se reportaría como "la temperatura está subiendo".
    """
    slow = line(12, 20.0, 0.01)                       # +0.11 °C en el periodo
    assert trend(slow, stable_band=0.5, decimals=1).direction is TrendDirection.stable
    assert trend(slow, stable_band=0.05, decimals=1).direction is TrendDirection.rising


def test_the_trend_reports_the_numbers_behind_it():
    """Caso 8: pendiente por hora, cambio y estadísticas del periodo."""
    result = trend(line(7, 20.0, 1.0), stable_band=0.5, decimals=1)
    assert result.slope_per_hour == 6.0               # +1.0 por cada 10 min
    assert result.change == 6.0                       # una hora de datos
    assert result.observed_hours == 1.0
    assert (result.first, result.last) == (20.0, 26.0)
    assert (result.minimum, result.maximum) == (20.0, 26.0)
    assert result.mean == 23.0
    assert result.fit_r2 == 1.0                       # es exactamente una recta


def test_a_flat_series_fits_perfectly():
    """Caso 9: una serie plana la explica una recta horizontal (r² = 1)."""
    result = trend(line(10, 22.0, 0.0), stable_band=0.5, decimals=1)
    assert result.fit_r2 == 1.0
    assert result.slope_per_hour == 0.0


def test_noise_lowers_the_fit():
    """Caso 10: r² baja cuando la serie no es una recta."""
    base = 1_792_000_000
    window = Window(start=history.from_epoch(base), end=history.from_epoch(base + 6000))
    zigzag = prepare(
        [(base + 600 * i, 20.0 + (3.0 if i % 2 else -3.0), 1) for i in range(10)],
        window, 600,
    )
    assert trend(zigzag, stable_band=0.5, decimals=1).fit_r2 < 0.2


def test_too_few_samples_is_not_a_trend():
    """Caso 11: con uno o dos buckets no se inventa una dirección."""
    for count in range(MIN_SAMPLES_FOR_TREND):
        result = trend(line(count, 20.0, 1.0), stable_band=0.5, decimals=1)
        assert result.direction is TrendDirection.insufficient_data, count
        assert result.slope_per_hour is None
    assert trend(line(MIN_SAMPLES_FOR_TREND, 20.0, 1.0), stable_band=0.5,
                 decimals=1).direction is TrendDirection.rising


def test_values_survive_even_without_a_trend():
    """Caso 12: con dos buckets no hay tendencia, pero sí mínimo y máximo."""
    result = trend(line(2, 20.0, 4.0), stable_band=0.5, decimals=1)
    assert result.direction is TrendDirection.insufficient_data
    assert (result.first, result.last) == (20.0, 24.0)
    assert (result.minimum, result.maximum) == (20.0, 24.0)


def test_the_same_input_gives_the_same_result():
    """Caso 13 (criterio 2): las funciones puras son reproducibles."""
    first = trend(line(20, 20.0, 0.3), stable_band=0.5, decimals=1)
    second = trend(line(20, 20.0, 0.3), stable_band=0.5, decimals=1)
    assert first == second


# ── El endpoint ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_analysis_returns_a_trend_per_variable_and_the_period(api, db, tokens):
    """Caso 14 (criterio 1): una tendencia por variable, con el periodo usado."""
    end = on_the_minute()
    start = end - timedelta(hours=6)
    await insert_readings(db, series(start, 360, 60, lambda i: round(18.0 + i * 0.02, 2)))

    resp = await api.get(ANALYSIS_URL, params={"from": ts_text(start), "to": ts_text(end)},
                         headers=bearer(tokens["producer"]))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["has_data"] is True
    assert data["readings"] == 360
    assert data["period"]["hours"] == 6.0
    assert data["period"]["bucket_seconds"] in history.BUCKET_CHOICES
    assert data["period"]["from"].startswith(start.isoformat()[:16])
    assert {item["key"] for item in data["variables"]} == set(metric_catalog.KEYS)

    temp = variable(data, "temp_c")
    assert temp["trend"] == "rising"
    assert temp["slope_per_hour"] > 0
    assert temp["change"] == pytest.approx(7.2, abs=0.3)   # 0.02 °C × 359
    assert temp["first"] == 18.0 and temp["last"] > 25.0
    assert temp["unit"] == "°C" and temp["label"] == "Temperatura del aire"
    assert temp["coverage_pct"] == 100.0
    assert temp["gaps"] == []

    # Las que no se movieron salen estables, con su banda a la vista.
    humidity = variable(data, "hum_aire_pct")
    assert humidity["trend"] == "stable"
    assert humidity["stable_band"] == metric_catalog.BY_KEY["hum_aire_pct"].stable_band


@pytest.mark.asyncio
async def test_a_falling_variable_is_reported_as_falling(api, db, tokens):
    """Caso 15: el tanque que se vacía se reporta bajando."""
    end = on_the_minute()
    start = end - timedelta(hours=4)
    rows = series(start, 240, 60, lambda i: 22.0)
    for i, row in enumerate(rows):
        row["nivel_pct"] = max(0, 90 - i // 4)        # baja ~15 % por hora
    await insert_readings(db, rows)

    data = (await api.get(ANALYSIS_URL, params={"from": ts_text(start), "to": ts_text(end)},
                          headers=bearer(tokens["producer"]))).json()
    tank = variable(data, "nivel_pct")
    assert tank["trend"] == "falling"
    assert tank["slope_per_hour"] < 0
    assert tank["change"] < -metric_catalog.BY_KEY["nivel_pct"].stable_band


@pytest.mark.asyncio
async def test_the_same_request_twice_gives_the_same_analysis(api, db, tokens):
    """Caso 16 (criterio 2): el mismo rango sobre los mismos datos, igual.

    `generated_at` cambia porque es metadato de la respuesta; el análisis no.
    """
    end = on_the_minute()
    start = end - timedelta(hours=3)
    await insert_readings(db, series(start, 180, 60, lambda i: round(20.0 + i * 0.01, 2)))

    params = {"from": ts_text(start), "to": ts_text(end)}
    first = (await api.get(ANALYSIS_URL, params=params,
                           headers=bearer(tokens["producer"]))).json()
    second = (await api.get(ANALYSIS_URL, params=params,
                            headers=bearer(tokens["producer"]))).json()

    assert first["variables"] == second["variables"]
    assert first["period"] == second["period"]
    assert first["generated_at"] != second["generated_at"] or True   # puede coincidir


@pytest.mark.asyncio
async def test_gaps_are_reported_not_filled(api, db, tokens):
    """Caso 17 (criterio 3): media hora sin lecturas aparece como hueco."""
    end = on_the_minute()
    start = end - timedelta(hours=2)
    rows = [row for i, row in enumerate(series(start, 120, 60, lambda i: 22.0))
            if not 45 <= i < 75]                       # 30 min sin nada
    await insert_readings(db, rows)

    data = (await api.get(ANALYSIS_URL, params={
        "from": ts_text(start), "to": ts_text(end), "bucket_seconds": 60,
    }, headers=bearer(tokens["producer"]))).json()

    temp = variable(data, "temp_c")
    assert temp["samples"] == 90
    assert temp["expected_samples"] == 120
    assert temp["coverage_pct"] == 75.0
    assert len(temp["gaps"]) == 1
    gap = temp["gaps"][0]
    assert gap["seconds"] == 1800 and gap["buckets"] == 30
    assert gap["from"] < gap["to"]


@pytest.mark.asyncio
async def test_a_downed_sensor_is_reported_as_missing_readings(api, db, tokens):
    """Caso 18 (criterio 3): un sensor en null no desaparece del resultado."""
    end = on_the_minute()
    start = end - timedelta(hours=1)
    rows = series(start, 60, 60, lambda i: 22.0)
    for row in rows[20:40]:
        row["temp_c"] = None                           # el DHT se cayó 20 min
    await insert_readings(db, rows)

    data = (await api.get(ANALYSIS_URL, params={
        "from": ts_text(start), "to": ts_text(end), "bucket_seconds": 60,
    }, headers=bearer(tokens["producer"]))).json()

    temp = variable(data, "temp_c")
    assert temp["readings_used"] == 40
    assert temp["readings_missing"] == 20
    assert temp["coverage_pct"] < 100.0
    # La humedad sí llegó completa en esas mismas lecturas.
    assert variable(data, "hum_aire_pct")["readings_missing"] == 0


@pytest.mark.asyncio
async def test_without_readings_nothing_is_invented(api, tokens):
    """Caso 19: sin datos, cada variable dice que no alcanza para una tendencia."""
    resp = await api.get(ANALYSIS_URL, headers=bearer(tokens["producer"]))
    assert resp.status_code == 200
    data = resp.json()

    assert data["has_data"] is False and data["readings"] == 0
    for item in data["variables"]:
        assert item["trend"] == "insufficient_data"
        assert item["slope_per_hour"] is None and item["change"] is None
        assert item["samples"] == 0 and item["coverage_pct"] == 0.0
        assert len(item["gaps"]) == 1                  # todo el periodo es hueco


@pytest.mark.asyncio
async def test_the_analysis_defaults_to_the_last_24_hours(api, tokens):
    """Caso 20: sin rango, las últimas 24 h."""
    data = (await api.get(ANALYSIS_URL, headers=bearer(tokens["producer"]))).json()
    assert data["period"]["hours"] == float(history.DEFAULT_SPAN_HOURS)


@pytest.mark.asyncio
async def test_a_bucket_too_small_for_the_range_is_rejected(api, tokens):
    """Caso 21: un bucket de 10 s en 90 días partiría el rango en 777 600."""
    end = on_the_minute()
    resp = await api.get(ANALYSIS_URL, params={
        "from": ts_text(end - timedelta(days=80)), "to": ts_text(end),
        "bucket_seconds": 10,
    }, headers=bearer(tokens["producer"]))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_range"


@pytest.mark.asyncio
async def test_an_inverted_range_is_rejected(api, tokens):
    """Caso 22: `from` después de `to` es un error del cliente."""
    resp = await api.get(ANALYSIS_URL, params={
        "from": "2026-10-10T00:00:00Z", "to": "2026-10-09T00:00:00Z",
    }, headers=bearer(tokens["producer"]))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_a_producer_cannot_analyse_another_greenhouse(api, tokens):
    """Caso 23: AUTH-02 también aquí."""
    resp = await api.get(f"/api/v1/greenhouses/{OTHER_GREENHOUSE_ID}/analysis",
                         headers=bearer(tokens["producer"]))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_the_analysis_needs_a_session(api):
    """Caso 24: sin sesión no se analiza nada."""
    assert (await api.get(ANALYSIS_URL)).status_code == 401
