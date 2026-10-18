"""
Tests de DASH-02 — task "Build historical-readings query API".

Criterios de aceptación del item:
  1. Con una medición y un rango, se devuelve la serie de esa medición con su
     etiqueta, su unidad y su eje de tiempo.
  2. Un rango SIN datos no es un error: se dice que no hay datos, para que la
     página muestre el aviso en vez de una gráfica vacía.
  3. Un rango con muchísimos puntos se agrega o se muestrea para que la
     gráfica siga siendo legible.

Y lo que el proyecto ya exigía: `null` = sensor caído (no se grafica), un
productor solo consulta su vivero (AUTH-02) y sin sesión no se consulta nada.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import history
from app import metrics as metric_catalog
from app.models import MetricKey
from app.routers.readings import METRICS
from tests.conftest import (
    GREENHOUSE_ID,
    OTHER_GREENHOUSE_ID,
    bearer,
    insert_readings,
    ts_text,
)

SERIES_URL = f"/api/v1/greenhouses/{GREENHOUSE_ID}/readings/series"
LIST_URL = f"/api/v1/greenhouses/{GREENHOUSE_ID}/readings"


def minutes_ago(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def ramp(count: int, *, step_seconds: int, end: datetime, first: float, rise: float):
    """`count` lecturas cada `step_seconds`, terminando en `end`.

    La temperatura sube `rise` por lectura; el resto de las mediciones van
    fijas, así cada prueba mira solo la que le interesa.
    """
    start = end - timedelta(seconds=step_seconds * count)
    return [
        {
            "ts": start + timedelta(seconds=step_seconds * i),
            "temp_c": round(first + rise * i, 2),
            "hum_aire_pct": 70.0,
            "suelo_pct": 62,
            "suelo_raw": 380,
            "nivel_pct": 75,
            "nivel_raw": 150,
            "luz_nivel": 700,
            "luz_raw": 323,
            "co2_ppm": 450,
            "estado": "OK",
        }
        for i in range(count)
    ]


# ── Criterio 1: la serie de una medición en un rango ───────────────────

@pytest.mark.asyncio
async def test_series_has_metric_label_unit_and_time_axis(api, db, tokens):
    """Caso 1: la serie trae etiqueta, unidad y los puntos en orden de tiempo."""
    end = minutes_ago(1)
    await insert_readings(db, ramp(30, step_seconds=2, end=end, first=20.0, rise=0.1))

    resp = await api.get(SERIES_URL, params={"metric": "temp_c"},
                         headers=bearer(tokens["producer"]))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["metric"] == "temp_c"
    assert data["label"] == "Temperatura del aire"
    assert data["unit"] == "°C"
    assert data["has_data"] is True
    assert data["point_count"] == len(data["points"]) == 30
    assert data["reading_count"] == 30

    stamps = [point["ts"] for point in data["points"]]
    assert stamps == sorted(stamps), "el eje de tiempo va de más viejo a más nuevo"
    assert data["value_min"] == 20.0 and data["value_max"] == 22.9


@pytest.mark.asyncio
async def test_series_defaults_to_the_last_24_hours(api, db, tokens):
    """Caso 2: sin `from` ni `to` se consultan las últimas 24 h."""
    await insert_readings(db, [
        {"ts": minutes_ago(10), "temp_c": 24.0},
        {"ts": minutes_ago(60 * 30), "temp_c": 9.0},    # hace 30 h: fuera
    ])

    data = (await api.get(SERIES_URL, params={"metric": "temp_c"},
                          headers=bearer(tokens["producer"]))).json()
    span = datetime.fromisoformat(data["to"]) - datetime.fromisoformat(data["from"])
    assert span == timedelta(hours=history.DEFAULT_SPAN_HOURS)
    assert [point["value"] for point in data["points"]] == [24.0]


@pytest.mark.asyncio
async def test_series_honours_an_explicit_range(api, db, tokens):
    """Caso 3: con `from` y `to` solo entran las lecturas de ese rango."""
    end = minutes_ago(5)
    await insert_readings(db, ramp(20, step_seconds=60, end=end, first=10.0, rise=1.0))

    since = end - timedelta(minutes=5)
    data = (await api.get(SERIES_URL, params={
        "metric": "temp_c", "from": ts_text(since), "to": ts_text(end),
    }, headers=bearer(tokens["producer"]))).json()

    assert data["from"].startswith(since.isoformat()[:16])
    values = [point["value"] for point in data["points"]]
    assert values == [25.0, 26.0, 27.0, 28.0, 29.0]


@pytest.mark.asyncio
async def test_every_metric_of_the_catalog_can_be_graphed(api, db, tokens):
    """Caso 4: las seis mediciones del catálogo (con el CO₂ de NF-01) responden con su unidad."""
    await insert_readings(db, ramp(5, step_seconds=2, end=minutes_ago(1),
                                   first=21.0, rise=0.1))
    for metric in metric_catalog.METRICS:
        data = (await api.get(SERIES_URL, params={"metric": metric.key},
                              headers=bearer(tokens["producer"]))).json()
        assert data["unit"] == metric.unit, metric.key
        assert data["point_count"] == 5, metric.key


# ── Criterio 2: un rango sin datos se dice, no se finge ────────────────

@pytest.mark.asyncio
async def test_a_range_without_data_is_not_an_error(api, db, tokens):
    """Caso 5: rango válido sin lecturas → 200 con has_data=false y sin puntos."""
    await insert_readings(db, [{"ts": minutes_ago(5), "temp_c": 24.0}])
    hole_end = minutes_ago(60)

    resp = await api.get(SERIES_URL, params={
        "metric": "temp_c",
        "from": ts_text(hole_end - timedelta(hours=1)),
        "to": ts_text(hole_end),
    }, headers=bearer(tokens["producer"]))

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_data"] is False
    assert data["points"] == []
    assert data["reading_count"] == 0
    assert data["value_min"] is None and data["value_max"] is None


@pytest.mark.asyncio
async def test_a_greenhouse_without_readings_has_no_data(api, tokens):
    """Caso 6: un vivero al que nunca llegó nada responde igual, sin reventar."""
    data = (await api.get(SERIES_URL, params={"metric": "nivel_pct"},
                          headers=bearer(tokens["producer"]))).json()
    assert data["has_data"] is False and data["points"] == []


@pytest.mark.asyncio
async def test_null_readings_are_not_plotted(api, db, tokens):
    """Caso 7: null = sensor caído: no es un punto en la gráfica."""
    end = minutes_ago(1)
    await insert_readings(db, [
        {"ts": end - timedelta(seconds=6), "temp_c": 24.0},
        {"ts": end - timedelta(seconds=4), "temp_c": None},   # el DHT no respondió
        {"ts": end - timedelta(seconds=2), "temp_c": 24.4},
    ])

    data = (await api.get(SERIES_URL, params={"metric": "temp_c"},
                          headers=bearer(tokens["producer"]))).json()
    assert [point["value"] for point in data["points"]] == [24.0, 24.4]
    assert data["reading_count"] == 2


# ── Criterio 3: muchos puntos se agregan ───────────────────────────────

@pytest.mark.asyncio
async def test_many_points_are_aggregated_into_buckets(api, db, tokens):
    """Caso 8: 600 lecturas con tope de 100 puntos → promedios por bucket."""
    end = minutes_ago(1)
    await insert_readings(db, ramp(600, step_seconds=2, end=end, first=20.0, rise=0.01))

    data = (await api.get(SERIES_URL, params={
        "metric": "temp_c", "from": ts_text(end - timedelta(seconds=1200)),
        "to": ts_text(end), "max_points": 100,
    }, headers=bearer(tokens["producer"]))).json()

    assert data["aggregated"] is True
    assert data["bucket_seconds"] in history.BUCKET_CHOICES
    assert data["reading_count"] == 600
    assert 0 < data["point_count"] <= 100

    # Ningún punto se pierde y los puntos representan varias lecturas cada uno
    # (el primero y el último pueden quedar partidos por el borde del bucket).
    assert sum(point["count"] for point in data["points"]) == 600
    assert max(point["count"] for point in data["points"]) > 1
    assert all(point["min"] <= point["value"] <= point["max"] for point in data["points"])
    # El promedio del bucket no esconde el pico: el máximo de la serie es el
    # máximo real de las lecturas, no el del último promedio.
    assert data["value_max"] == 25.99


@pytest.mark.asyncio
async def test_max_points_is_respected_at_its_limits(api, db, tokens):
    """Caso 9: con el tope más chico se sigue devolviendo una serie usable."""
    end = minutes_ago(1)
    await insert_readings(db, ramp(400, step_seconds=3, end=end, first=20.0, rise=0.01))

    data = (await api.get(SERIES_URL, params={
        "metric": "temp_c", "max_points": history.MIN_MAX_POINTS,
    }, headers=bearer(tokens["producer"]))).json()

    assert data["aggregated"] is True
    assert 0 < data["point_count"] <= history.MIN_MAX_POINTS


@pytest.mark.asyncio
async def test_few_readings_are_returned_raw(api, db, tokens):
    """Caso 10: si caben, se devuelven las lecturas tal cual (sin promediar)."""
    await insert_readings(db, ramp(12, step_seconds=2, end=minutes_ago(1),
                                   first=20.0, rise=0.5))

    data = (await api.get(SERIES_URL, params={"metric": "temp_c"},
                          headers=bearer(tokens["producer"]))).json()
    assert data["aggregated"] is False
    assert data["bucket_seconds"] == 0
    assert all(point["count"] == 1 for point in data["points"])
    assert all(point["min"] is None and point["max"] is None for point in data["points"])


@pytest.mark.asyncio
async def test_buckets_are_aligned_to_the_clock(api, db, tokens):
    """Caso 11: los buckets son múltiplos exactos de su tamaño.

    Así dos consultas del mismo periodo con rangos ligeramente distintos caen
    en los mismos buckets (es lo que hace reproducible el análisis de AI-01).
    """
    end = minutes_ago(1)
    await insert_readings(db, ramp(600, step_seconds=2, end=end, first=20.0, rise=0.01))

    data = (await api.get(SERIES_URL, params={
        "metric": "temp_c", "max_points": 20,
    }, headers=bearer(tokens["producer"]))).json()

    bucket = data["bucket_seconds"]
    assert bucket > 0
    for point in data["points"]:
        epoch = datetime.fromisoformat(point["ts"]).timestamp()
        assert epoch % bucket == 0, point["ts"]


# ── Rango inválido, medición inexistente y permisos ────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("params, reason", [
    ({"from": "2026-10-10T00:00:00Z", "to": "2026-10-09T00:00:00Z"}, "rango invertido"),
    ({"from": "2026-01-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"}, "más de 90 días"),
])
async def test_an_unusable_range_is_rejected(api, tokens, params, reason):
    """Caso 12: un rango que no se puede consultar se explica, no se silencia."""
    resp = await api.get(SERIES_URL, params={"metric": "temp_c", **params},
                         headers=bearer(tokens["producer"]))
    assert resp.status_code == 422, reason
    assert resp.json()["detail"]["error"] == "invalid_range"
    assert resp.json()["detail"]["message"]


ANALYSIS_URL = f"/api/v1/greenhouses/{GREENHOUSE_ID}/analysis"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [SERIES_URL, LIST_URL, ANALYSIS_URL])
@pytest.mark.parametrize("params", [
    {"from": "2026-10-15T00:00:00"},
    {"from": "2026-10-15T00:00:00Z", "to": "2026-10-15T06:00:00"},
])
async def test_a_date_without_time_zone_is_rejected(api, tokens, url, params):
    """Caso 12b: igual que la ingesta, `from`/`to` llevan zona horaria.

    Sin zona, FastAPI la acepta y antes se tomaba como UTC: alguien que
    escribía su hora de México veía el rango corrido 6 h.
    """
    resp = await api.get(url, params=params, headers=bearer(tokens["producer"]))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_range"
    assert "zona horaria" in resp.json()["detail"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [SERIES_URL, LIST_URL, ANALYSIS_URL])
@pytest.mark.parametrize("params", [
    {"to": "0001-01-01T00:00:00Z"},                 # 24 h antes del año 1
    {"to": "9999-12-31T23:59:59-01:00"},            # en UTC ya es el año 10000
])
async def test_a_date_at_the_ends_of_the_calendar_is_not_a_crash(api, tokens, url, params):
    """Caso 12c: una fecha imposible de restar es un rango inválido, no un 500."""
    resp = await api.get(url, params=params, headers=bearer(tokens["producer"]))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_range"


@pytest.mark.parametrize("hours, max_points", [
    (24, 240), (24 * 7, 168), (24 * 7, 240), (24 * 30, 240), (24 * 90, 10), (6, 240),
])
def test_the_chosen_bucket_fits_even_when_the_range_is_not_aligned(hours, max_points):
    """Caso 9b (criterio 3): con buckets alineados a la época, un rango de n
    buckets puede tocar n + 1. El bucket elegido tiene que caber con ese de más."""
    start = datetime(2026, 10, 15, 12, 0, 30, tzinfo=timezone.utc)
    window = history.Window(start=start, end=start + timedelta(hours=hours))
    bucket = history.choose_bucket(window.span_seconds, max_points)
    if bucket:
        assert history.grid(window, bucket).count <= max_points


@pytest.mark.asyncio
async def test_seven_days_with_a_tight_cap_stay_under_it(api, db, tokens):
    """Caso 9c (criterio 3): 7 días con `max_points=168` daban buckets de 1 h
    y, con el rango corrido respecto a la hora, 169 puntos."""
    # A la media hora y 17 s: el primer y el último bucket de 1 h quedan a
    # medias y los dos traen lecturas.
    on_the_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = on_the_hour - timedelta(minutes=30) + timedelta(seconds=17)
    await insert_readings(db, ramp(2016, step_seconds=300, end=end, first=20.0, rise=0.001))

    data = (await api.get(SERIES_URL, params={
        "metric": "temp_c", "from": ts_text(end - timedelta(days=7)), "to": ts_text(end),
        "max_points": 168,
    }, headers=bearer(tokens["producer"]))).json()

    assert data["aggregated"] is True
    assert sum(point["count"] for point in data["points"]) == data["reading_count"]
    assert 0 < data["point_count"] <= 168


@pytest.mark.asyncio
async def test_thirty_days_with_the_default_cap_stay_under_it(api, db, tokens):
    """Caso 9d (criterio 3): el rango de 30 días de la página, con el tope de
    siempre, cabe y no pierde lecturas."""
    end = minutes_ago(1)
    await insert_readings(db, ramp(1440, step_seconds=1800, end=end, first=20.0, rise=0.001))

    data = (await api.get(SERIES_URL, params={
        "metric": "temp_c", "from": ts_text(end - timedelta(days=30)),
    }, headers=bearer(tokens["producer"]))).json()

    assert data["aggregated"] is True
    assert data["reading_count"] == 1440
    assert sum(point["count"] for point in data["points"]) == 1440
    assert 0 < data["point_count"] <= data["max_points"] == 240


@pytest.mark.asyncio
async def test_readings_faster_than_usual_are_still_capped(api, db, tokens):
    """Caso 9e (criterio 3): una lectura por segundo (simulador en time-lapse)
    en un rango corto. Antes se devolvían crudas aunque no cupieran."""
    end = minutes_ago(1)
    await insert_readings(db, ramp(400, step_seconds=1, end=end, first=20.0, rise=0.01))

    data = (await api.get(SERIES_URL, params={
        "metric": "temp_c", "from": ts_text(end - timedelta(seconds=410)),
        "to": ts_text(end), "max_points": 240,
    }, headers=bearer(tokens["producer"]))).json()

    assert data["aggregated"] is True
    assert data["reading_count"] == 400
    assert 0 < data["point_count"] <= 240


@pytest.mark.asyncio
async def test_a_long_range_with_few_readings_is_still_raw(api, db, tokens):
    """Caso 10b: un mes con cuatro lecturas se dibuja con sus cuatro puntos."""
    end = minutes_ago(1)
    await insert_readings(db, ramp(4, step_seconds=86400 * 5, end=end, first=20.0, rise=1.0))

    data = (await api.get(SERIES_URL, params={
        "metric": "temp_c", "from": ts_text(end - timedelta(days=30)),
    }, headers=bearer(tokens["producer"]))).json()

    assert data["aggregated"] is False and data["bucket_seconds"] == 0
    assert data["reading_count"] == 4 and data["point_count"] == 4


@pytest.mark.asyncio
async def test_an_unknown_metric_is_rejected(api, tokens):
    """Caso 13: solo se grafican las mediciones del catálogo."""
    resp = await api.get(SERIES_URL, params={"metric": "ph_suelo"},
                         headers=bearer(tokens["producer"]))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_a_producer_cannot_graph_another_greenhouse(api, tokens):
    """Caso 14: AUTO-02 también en el histórico: el vivero ajeno da 403."""
    resp = await api.get(
        f"/api/v1/greenhouses/{OTHER_GREENHOUSE_ID}/readings/series",
        params={"metric": "temp_c"}, headers=bearer(tokens["producer"]))
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [SERIES_URL, LIST_URL])
async def test_the_history_needs_a_session(api, url):
    """Caso 15: sin sesión no se consulta el histórico."""
    assert (await api.get(url)).status_code == 401


@pytest.mark.asyncio
async def test_an_admin_can_graph_any_greenhouse(api, db, tokens):
    """Caso 16: un administrador no está limitado a un vivero."""
    await insert_readings(db, [{"ts": minutes_ago(2), "temp_c": 23.0}])
    resp = await api.get(SERIES_URL, params={"metric": "temp_c"},
                         headers=bearer(tokens["admin"]))
    assert resp.status_code == 200 and resp.json()["has_data"] is True


# ── Lecturas crudas del rango (el contrato publicado) ──────────────────

@pytest.mark.asyncio
async def test_raw_readings_come_newest_first(api, db, tokens):
    """Caso 17: /readings devuelve lo guardado, de lo más nuevo a lo más viejo."""
    end = minutes_ago(1)
    await insert_readings(db, ramp(5, step_seconds=60, end=end, first=20.0, rise=1.0))

    rows = (await api.get(LIST_URL, headers=bearer(tokens["producer"]))).json()
    assert [row["temp_c"] for row in rows] == [24.0, 23.0, 22.0, 21.0, 20.0]

    row = rows[0]
    assert row["greenhouse_id"] == GREENHOUSE_ID
    assert row["alertas"] is None or isinstance(row["alertas"], list)
    assert row["ts"] and row["received_at"] and row["id"] > 0


@pytest.mark.asyncio
async def test_raw_readings_respect_the_limit(api, db, tokens):
    """Caso 18: `limit` corta la lista por lo más reciente."""
    await insert_readings(db, ramp(40, step_seconds=10, end=minutes_ago(1),
                                   first=20.0, rise=0.1))
    rows = (await api.get(LIST_URL, params={"limit": 7},
                          headers=bearer(tokens["producer"]))).json()
    assert len(rows) == 7


@pytest.mark.asyncio
async def test_raw_readings_parse_their_json_fields(api, db, tokens):
    """Caso 19: `alertas` sale como lista y los enteros 0/1 como booleanos."""
    await insert_readings(db, [{
        "ts": minutes_ago(2), "temp_c": 31.5, "es_dia": 1, "riego_sugerido": 0,
        "bomba_habilitada": 1, "estado": "ALERTA", "alertas": '["TEMP_ALTA"]',
    }])
    row = (await api.get(LIST_URL, headers=bearer(tokens["producer"]))).json()[0]
    assert row["alertas"] == ["TEMP_ALTA"]
    assert row["es_dia"] is True
    assert row["riego_sugerido"] is False
    assert row["bomba_habilitada"] is True
    assert row["estado"] == "ALERTA"


# ── El catálogo es la única fuente de mediciones ───────────────────────

def test_the_graphable_metrics_are_the_catalog():
    """El enum del contrato, el catálogo y lo que usa DASH-01 no se separan."""
    assert {item.value for item in MetricKey} == set(metric_catalog.KEYS)
    assert tuple(column for column, _, _, _ in METRICS) == metric_catalog.KEYS
    assert all(metric.stable_band > 0 for metric in metric_catalog.METRICS)
