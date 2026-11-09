"""
Tests de AI-05 — task "Generate the natural-language explanation text".

Criterios de aceptación del item:
  1. Una recomendación guardada, al consultarla, muestra las entradas exactas.
  2. Toda recomendación mostrada trae una explicación en lenguaje claro que
     nombra las mediciones que la causaron.
  3. La explicación no contiene identificadores internos, fórmulas ni códigos.
  4. Una recomendación sin explicación no se muestra.

Hay dos niveles: el texto puro de app/explanation.py (sin BD) y los endpoints.
"""

import itertools
import re

import pytest

from app.explanation import explain, is_explained
from app.recommendation import Inputs, Outcome, recommend
from tests.conftest import GREENHOUSE_ID, OTHER_GREENHOUSE_ID, bearer, insert_readings
from tests.test_recommendation import URL, add_pump, hours_ago, recent_readings

HISTORY_URL = f"{URL}s"

# Todas las combinaciones de entradas que cambian el texto.
ALL_INPUTS = [
    Inputs(soil_pct=soil, temp_c=temp, hours_since_irrigation=hours,
           irrigating=irrigating, tank_pct=tank)
    for soil, temp, hours, irrigating, tank in itertools.product(
        (None, 40.0, 58.0, 61.5, 70.0, 90.0),
        (None, 15.0, 22.0, 27.0),
        (None, 0.0, 0.5, 1.0, 6.0, 30.0, 72.0),
        (False, True),
        (None, 5.0, 70.0),
    )
]


def text_of(explanation) -> str:
    return " ".join([explanation.summary, *explanation.reasons, *explanation.notes])


def explain_inputs(**values):
    inputs = Inputs(**{
        "soil_pct": 61.0, "temp_c": 22.0, "hours_since_irrigation": 6.0, **values,
    })
    return explain(inputs, recommend(inputs))


# ── Criterio 2: nombra las mediciones que la causaron ─────────────────

def test_it_names_the_three_measurements_with_their_values():
    text = text_of(explain_inputs(soil_pct=58.0, temp_c=27.0, hours_since_irrigation=6.0))
    assert "tierra" in text and "58 %" in text
    assert "temperatura" in text and "27 °C" in text
    assert "último riego" in text and "6 horas" in text


@pytest.mark.parametrize("values, summary", [
    ({"soil_pct": 58.0}, "Conviene regar ahora."),
    ({"soil_pct": 40.0}, "Conviene regar cuanto antes."),
    ({"soil_pct": 61.0}, "Conviene regar pronto."),
    ({"soil_pct": 70.0}, "Por ahora no hace falta regar."),
    ({"soil_pct": 90.0}, "No conviene regar: la tierra tiene demasiada agua."),
    ({"soil_pct": 61.0, "irrigating": True}, "Ya se está regando."),
    ({"soil_pct": None}, "Por ahora no se puede dar una recomendación."),
])
def test_the_summary_says_what_to_do(values, summary):
    assert explain_inputs(**values).summary == summary


def test_it_gives_the_moisture_at_which_to_irrigate_after_the_adjustments():
    """Con 27 °C el punto de riego sube de 60 a 62: el texto dice 62, no 60."""
    assert "62 %" in text_of(explain_inputs(soil_pct=58.0, temp_c=27.0))


@pytest.mark.parametrize("temp, words", [
    (27.0, "se seca más rápido"),
    (15.0, "se seca más lento"),
    (22.0, "no cambia"),
])
def test_it_says_how_the_temperature_counted(temp, words):
    assert words in text_of(explain_inputs(temp_c=temp))


@pytest.mark.parametrize("hours, words", [
    (0.5, "hace 30 minutos"),
    (1.0, "hace 1 hora"),
    (6.0, "hace 6 horas"),
    (30.0, "hace 30 horas"),
    (72.0, "hace 3 días"),
])
def test_it_says_when_the_last_irrigation_was(hours, words):
    assert words in text_of(explain_inputs(hours_since_irrigation=hours))


def test_a_very_recent_irrigation_does_not_say_zero_minutes():
    assert "hace menos de 1 minuto" in text_of(explain_inputs(hours_since_irrigation=0.0))


def test_it_admits_what_it_could_not_measure():
    notes = " ".join(explain_inputs(temp_c=None, hours_since_irrigation=None).notes)
    assert "temperatura" in notes
    assert "riegos anteriores" in notes


def test_without_soil_data_it_says_why_and_mentions_no_other_measurement():
    explanation = explain_inputs(soil_pct=None)
    assert len(explanation.reasons) == 1
    assert "humedad de la tierra" in explanation.reasons[0]


def test_a_critical_tank_is_explained():
    notes = " ".join(explain_inputs(soil_pct=58.0, tank_pct=5.0).notes)
    assert "tanque" in notes and "5 %" in notes


def test_a_tank_with_water_is_not_mentioned():
    assert "tanque" not in text_of(explain_inputs(soil_pct=58.0, tank_pct=70.0))


# ── Criterio 3: sin identificadores, fórmulas ni códigos ──────────────

FORBIDDEN = re.compile(
    r"_"                               # suelo_pct, SUELO_IDEAL_MIN
    r"|[=+*/<>]"                       # fórmulas
    r"|\b[A-Z]{2,}\b"                  # códigos: RIEGO, FALLA, AI-04
    r"|(?i:\b(irrigate|outcome|threshold|soil|null|none|true|false)\b)"
    r"|(?i:\b(umbral|mediana|puntos)\b)"
)


def test_no_explanation_has_identifiers_formulas_or_codes():
    for inputs in ALL_INPUTS:
        text = text_of(explain(inputs, recommend(inputs)))
        assert not FORBIDDEN.search(text), text


def test_the_forbidden_pattern_really_catches_them():
    for bad in ("suelo_pct", "60 + 2", "RIEGO", "irrigate", "el umbral"):
        assert FORBIDDEN.search(bad), bad


# ── Criterio 4: sin explicación no se muestra ─────────────────────────

def test_every_combination_gets_an_explanation():
    for inputs in ALL_INPUTS:
        assert is_explained(explain(inputs, recommend(inputs))), inputs


@pytest.mark.parametrize("summary, reasons", [("", ["algo"]), ("Regar.", []), ("  ", ["algo"]), ("Regar.", [" "])])
def test_an_empty_explanation_does_not_count(summary, reasons):
    explanation = explain_inputs()
    empty = type(explanation)(summary=summary, reasons=reasons, notes=[])
    assert not is_explained(empty)


@pytest.mark.asyncio
async def test_a_recommendation_without_explanation_is_not_shown(api, db, tokens, monkeypatch):
    from app.routers import recommendation as router

    await insert_readings(db, recent_readings(suelo_pct=55))
    monkeypatch.setattr(
        router, "explain", lambda *_: type(explain_inputs())(summary="", reasons=[], notes=[])
    )
    headers = bearer(tokens["producer"])

    resp = await api.get(URL, headers=headers)
    assert resp.status_code == 503
    assert "explicación" in resp.json()["detail"]

    history = await api.get(HISTORY_URL, headers=headers)
    assert history.json()["items"] == []


@pytest.mark.asyncio
async def test_the_endpoint_returns_the_explanation(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=55, temp_c=27.0))
    await add_pump(db, (hours_ago(6), "on"), (hours_ago(5.99), "off"))

    data = (await api.get(URL, headers=bearer(tokens["producer"]))).json()

    assert data["explanation"]["summary"] == "Conviene regar ahora."
    reasons = " ".join(data["explanation"]["reasons"])
    assert "55 %" in reasons and "27 °C" in reasons and "6 horas" in reasons


# ── Criterio 1: la guardada conserva sus entradas exactas ─────────────

@pytest.mark.asyncio
async def test_a_stored_recommendation_keeps_its_exact_inputs(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=55, temp_c=27.0))
    await add_pump(db, (hours_ago(6), "on"), (hours_ago(5.99), "off"))
    headers = bearer(tokens["producer"])

    shown = (await api.get(URL, headers=headers)).json()
    # Llegan lecturas nuevas; la guardada no debe cambiar.
    await db.execute("DELETE FROM readings")
    await db.commit()
    await insert_readings(db, recent_readings(suelo_pct=90, temp_c=15.0))

    await api.get(URL, headers=headers)
    items = (await api.get(HISTORY_URL, headers=headers)).json()["items"]

    assert [item["outcome"] for item in items] == ["too_wet", "irrigate"]
    assert items[1] == shown
    assert items[1]["inputs"]["soil"]["value"] == 55.0
    assert items[1]["inputs"]["temperature"]["value"] == 27.0
    assert items[1]["threshold"] == 62.0


@pytest.mark.asyncio
async def test_it_only_stores_when_the_recommendation_changes(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=55))
    headers = bearer(tokens["producer"])

    for _ in range(3):
        await api.get(URL, headers=headers)

    assert len((await api.get(HISTORY_URL, headers=headers)).json()["items"]) == 1


@pytest.mark.asyncio
async def test_an_urgent_recommendation_is_a_new_one(api, db, tokens):
    headers = bearer(tokens["producer"])
    await insert_readings(db, recent_readings(suelo_pct=55))
    await api.get(URL, headers=headers)

    await db.execute("DELETE FROM readings")
    await db.commit()
    await insert_readings(db, recent_readings(suelo_pct=40))
    await api.get(URL, headers=headers)

    items = (await api.get(HISTORY_URL, headers=headers)).json()["items"]
    assert [(item["outcome"], item["urgent"]) for item in items] == [
        ("irrigate", True), ("irrigate", False),
    ]


@pytest.mark.asyncio
async def test_the_history_respects_the_limit(api, db, tokens):
    headers = bearer(tokens["producer"])
    for soil in (55, 90, 55):
        await db.execute("DELETE FROM readings")
        await db.commit()
        await insert_readings(db, recent_readings(suelo_pct=soil))
        await api.get(URL, headers=headers)

    resp = await api.get(HISTORY_URL, params={"limit": 2}, headers=headers)
    assert [item["outcome"] for item in resp.json()["items"]] == ["irrigate", "too_wet"]
    assert (await api.get(HISTORY_URL, params={"limit": 0}, headers=headers)).status_code == 422


@pytest.mark.asyncio
async def test_each_greenhouse_has_its_own_history(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=55))
    await api.get(URL, headers=bearer(tokens["producer"]))

    other = f"/api/v1/greenhouses/{OTHER_GREENHOUSE_ID}/irrigation/recommendations"
    assert (await api.get(other, headers=bearer(tokens["admin"]))).json()["items"] == []
    assert (await api.get(other, headers=bearer(tokens["producer"]))).status_code == 403
    assert (await api.get(HISTORY_URL)).status_code == 401
    assert GREENHOUSE_ID in HISTORY_URL
