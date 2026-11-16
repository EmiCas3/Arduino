"""
Tests de SYS-04 — "Cablear el flujo completo: sensor → nube → alerta/IA → actuador".

Criterios de aceptación:
  1. Sin intervención humana: una lectura que llega por la ingesta se guarda,
     se compara contra los umbrales (alertas), pasa por la IA (recomendación
     explicada y guardada) y actualiza el estado de los actuadores, todo en
     el mismo POST. Nadie tiene que abrir el dashboard para que pase.
  2. Cada paso se puede verificar en el dashboard (los endpoints que lee) y en
     la bitácora de auditoría.
  3. Si un eslabón falla, se ve un error explícito (respuesta de la ingesta y
     log del backend) y no falla en silencio; los demás pasos siguen.

Todo entra por POST /devices/{id}/readings, como lo manda la Raspberry Pi.
En el criterio 1 NUNCA se llama a GET /irrigation/recommendation: si la
recomendación aparece guardada es porque la ingesta la generó sola.
"""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import actuator_state, alerts, irrigation
from app.config import settings
from app.routers import recommendation as recommendation_router
from tests.conftest import GATEWAY_API_KEY, GATEWAY_ID, GREENHOUSE_ID, bearer
from tests.test_irrigation import FAN, PUMP, equipment  # noqa: F401  (fixture)

INGEST_URL = f"/api/v1/devices/{GATEWAY_ID}/readings"
BASE = f"/api/v1/greenhouses/{GREENHOUSE_ID}"
AUDIT_URL = "/api/v1/audit/events"
INGEST_LOGGER = "smartgreenai.ingest"

# Todas las lecturas caen dentro de la ventana de lecturas frescas (15 min):
# la ingesta solo evalúa la recomendación con lecturas recientes.
T0 = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(microsecond=0)


def at(seconds: float) -> str:
    """Hora T0 + n segundos, como la manda la Pi."""
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stored(seconds: float) -> str:
    """La misma hora como la guarda el backend."""
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def reading(seconds: float, *codes: str, **overrides) -> dict:
    base = {
        "ts": at(seconds), "ms": 1000, "temp_c": 23.0, "hum_aire_pct": 70.0,
        "suelo_raw": 380, "suelo_pct": 70, "nivel_raw": 120, "nivel_pct": 80,
        "luz_raw": 306, "luz_nivel": 717, "es_dia": True, "sol_min_hoy": 42,
        "sol_min_prev": None, "riego_sugerido": False, "bomba_habilitada": True,
        "bomba_activa": False, "vent": "BASE", "vent_activo": False,
        "modo": "AUTO", "cmd_id": 0, "cmd_err": None, "bomba_paro": None,
        "estado": "ALERTA" if codes else "OK", "alertas": list(codes),
    }
    base.update(overrides)
    return base


async def ingest(api, *readings) -> dict:
    resp = await api.post(INGEST_URL, json={"readings": list(readings)},
                          headers={"X-API-Key": GATEWAY_API_KEY})
    assert resp.status_code == 202, resp.text
    return resp.json()


async def get(api, token, path) -> dict:
    resp = await api.get(BASE + path, headers=bearer(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def latest_metric(api, token, key) -> dict:
    data = await get(api, token, "/readings/latest")
    return next(m for m in data["metrics"] if m["key"] == key)


async def stored_recommendations(api, token) -> list:
    """Recomendaciones guardadas, de la más vieja a la más nueva."""
    data = await get(api, token, "/irrigation/recommendations?limit=50")
    return list(reversed(data["items"]))


async def actuator_events(api, token) -> list:
    data = await get(api, token, "/actuators/events")
    return list(reversed(data["events"]))


async def audit_events(api, token, **params) -> list:
    resp = await api.get(AUDIT_URL, params={"limit": 200, **params}, headers=bearer(token))
    assert resp.status_code == 200, resp.text
    return list(reversed(resp.json()["items"]))


def ingest_records(caplog, level) -> list:
    return [r for r in caplog.records if r.name == INGEST_LOGGER and r.levelno == level]


@pytest.fixture
def per_reading(monkeypatch):
    """Que cada lectura decida sola la recomendación.

    La recomendación usa la mediana de los últimos 5 min; aquí las lecturas van
    a segundos de distancia y se quiere ver cómo cambia con cada una.
    """
    monkeypatch.setattr(recommendation_router, "SMOOTHING_MINUTES", 0)


@pytest.fixture
def fast_clear(monkeypatch):
    monkeypatch.setattr(settings, "alert_clear_seconds", 60)


# ── Criterio 1: sensor → nube → umbral/IA → alerta/recomendación → actuador ─

@pytest.mark.asyncio
async def test_one_reading_runs_the_whole_chain_without_anyone(api, tokens, equipment):
    """Un solo POST de la Pi: lectura, alerta, recomendación y actuadores."""
    token = tokens["producer"]
    result = await ingest(api, reading(0, "SUELO_SECO", suelo_pct=42, riego_sugerido=True))
    assert result["accepted"] == 1 and result["rejected"] == []
    assert result["errors"] == [] and result["commands"] == []

    # La lectura está guardada (dashboard: tarjetas).
    soil = await latest_metric(api, token, "suelo_pct")
    assert soil["value"] == 42 and soil["sensor_down"] is False

    # El umbral se evaluó: la alerta ya existe cuando la Pi recibe el 202.
    data = await get(api, token, "/alerts")
    assert data["active_count"] == 1
    assert (data["alerts"][0]["code"], data["alerts"][0]["value"]) == ("SUELO_SECO", 42)

    # La IA corrió y guardó la recomendación con su explicación y entradas.
    [rec] = await stored_recommendations(api, token)
    assert (rec["outcome"], rec["urgent"]) == ("irrigate", True)
    assert rec["explanation"]["summary"].strip()
    assert any(reason.strip() for reason in rec["explanation"]["reasons"])
    assert rec["inputs"]["soil"]["value"] == 42
    assert rec["inputs"]["temperature"]["value"] == 23.0
    assert rec["inputs"]["tank"]["value"] == 80

    # El estado de los actuadores se actualizó con lo que reportó el Arduino.
    status = await get(api, token, "/actuators/status")
    pump = next(a for a in status["actuators"] if a["actuator_id"] == PUMP)
    assert (pump["state"], pump["reason"]) == ("off", "reported")


@pytest.mark.asyncio
async def test_each_change_of_outcome_is_stored_by_the_ingestion(api, tokens, equipment, per_reading):
    token = tokens["producer"]
    sequence = [
        reading(0, suelo_pct=70),                                    # no hace falta
        reading(10, suelo_pct=70),                                   # igual: no se repite
        reading(20, suelo_pct=55),                                   # regar
        reading(30, "SUELO_SECO", suelo_pct=42, riego_sugerido=True),   # regar, urgente
        reading(40, "SUELO_SECO", suelo_pct=42, riego_sugerido=True, bomba_activa=True),  # regando
        reading(50, "SUELO_SECO", suelo_pct=45, riego_sugerido=True, bomba_activa=True),  # sigue
    ]
    for item in sequence:
        assert (await ingest(api, item))["errors"] == []

    recs = await stored_recommendations(api, token)
    assert [(r["outcome"], r["urgent"]) for r in recs] == [
        ("no_irrigation", False), ("irrigate", False), ("irrigate", True), ("irrigating", False),
    ]
    assert [r["inputs"]["soil"]["value"] for r in recs] == [70, 55, 42, 42]
    for rec in recs:
        assert rec["explanation"]["summary"].strip()

    # El actuador: la bomba arrancó sola y quedó registrado.
    log = await actuator_events(api, token)
    assert [(e["actuator_id"], e["event"], e["origin"]) for e in log] == [(PUMP, "on", "auto")]
    status = await get(api, token, "/actuators/status")
    pump = next(a for a in status["actuators"] if a["actuator_id"] == PUMP)
    assert pump["state"] == "on"


@pytest.mark.asyncio
async def test_alerts_open_and_clear_from_the_ingestion_alone(api, tokens, fast_clear):
    token = tokens["producer"]
    await ingest(api, reading(0, "TEMP_ALTA", temp_c=31.2))
    await ingest(api, reading(2, "TEMP_ALTA", temp_c=31.5))      # sigue: no duplica
    data = await get(api, token, "/alerts")
    assert data["active_count"] == 1 and len(data["alerts"]) == 1

    await ingest(api, reading(4), reading(70))                    # normal más de 60 s
    assert (await get(api, token, "/alerts"))["active_count"] == 0


@pytest.mark.asyncio
async def test_pending_commands_reach_the_gateway_in_the_same_response(api, tokens, equipment):
    """El último eslabón: lo pendiente para el Arduino va en `commands` del 202."""
    await ingest(api, reading(0))
    resp = await api.put(f"{BASE}/irrigation/mode", json={"mode": "manual"},
                         headers=bearer(tokens["producer"]))
    assert resp.status_code == 200, resp.text
    result = await ingest(api, reading(2))
    assert [(c["kind"], c["value"]) for c in result["commands"]] == [("mode", "MANUAL")]
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_old_backfill_does_not_invent_recommendations(api, tokens):
    """Lecturas viejas (la Pi vaciando su buffer) se guardan, pero no se recomienda con ellas."""
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="milliseconds")
    result = await ingest(api, reading(0, ts=old, suelo_pct=42))
    assert result["accepted"] == 1 and result["errors"] == []
    assert await stored_recommendations(api, tokens["producer"]) == []


# ── Criterio 2: cada paso se ve en el dashboard y en la bitácora ───────

@pytest.mark.asyncio
async def test_every_step_is_visible_in_dashboard_and_audit_log(api, tokens, equipment, per_reading):
    producer, admin = tokens["producer"], tokens["admin"]
    await ingest(api, reading(0, suelo_pct=62))
    await ingest(api, reading(10, "TEMP_ALTA", "SUELO_SECO", temp_c=31.2, suelo_pct=42,
                              riego_sugerido=True))
    await ingest(api, reading(20, "TEMP_ALTA", "SUELO_SECO", temp_c=31.2, suelo_pct=42,
                              riego_sugerido=True, bomba_activa=True))
    await ingest(api, reading(30, "TEMP_ALTA", temp_c=31.2, suelo_pct=51,
                              bomba_activa=False, bomba_paro="OBJETIVO"))

    # Dashboard.
    assert (await latest_metric(api, producer, "temp_c"))["value"] == 31.2
    assert (await latest_metric(api, producer, "suelo_pct"))["value"] == 51
    active = (await get(api, producer, "/alerts"))["alerts"]
    assert {a["code"] for a in active} >= {"TEMP_ALTA"}
    recs = await stored_recommendations(api, producer)
    outcomes = [r["outcome"] for r in recs]
    assert "irrigate" in outcomes and "irrigating" in outcomes
    log = await actuator_events(api, producer)
    assert [(e["actuator_id"], e["event"]) for e in log] == [(PUMP, "on"), (PUMP, "off")]
    status = await get(api, producer, "/actuators/status")
    pump = next(a for a in status["actuators"] if a["actuator_id"] == PUMP)
    assert pump["state"] == "off"

    # Bitácora: quién, qué y cuándo.
    events = await audit_events(api, admin, greenhouse_id=GREENHOUSE_ID)
    by_action = {}
    for event in events:
        by_action.setdefault(event["action"], []).append(event)
    for action in ("alert_raised", "recommendation_generated", "actuator_on", "actuator_off"):
        assert by_action.get(action), f"falta {action} en la bitácora"
        for event in by_action[action]:
            assert event["summary"]
            occurred = datetime.fromisoformat(event["occurred_at"])
            assert occurred.tzinfo is not None
            assert T0 - timedelta(seconds=1) <= occurred <= datetime.now(timezone.utc)
            assert datetime.fromisoformat(event["recorded_at"]).tzinfo is not None

    raised_codes = {e["detail"]["code"] for e in by_action["alert_raised"] if e["detail"]}
    assert {"TEMP_ALTA", "SUELO_SECO"} <= raised_codes
    for event in by_action["alert_raised"] + by_action["actuator_on"] + by_action["actuator_off"]:
        assert (event["actor_type"], event["actor_id"]) == ("device", GATEWAY_ID)
    assert by_action["alert_raised"][0]["occurred_at"].startswith(stored(10)[:19])
    assert by_action["actuator_on"][0]["occurred_at"].startswith(stored(20)[:19])
    assert by_action["actuator_off"][0]["occurred_at"].startswith(stored(30)[:19])

    # Una entrada de bitácora por cada recomendación guardada, a nombre del sistema.
    generated = by_action["recommendation_generated"]
    assert len(generated) == len(recs)
    for event, rec in zip(generated, recs):
        assert event["actor_type"] == "system"
        assert event["summary"] == rec["explanation"]["summary"]
        assert event["detail"]["outcome"] == rec["outcome"]


# ── Criterio 3: si un eslabón falla, se ve ─────────────────────────────

@pytest.mark.asyncio
async def test_broken_air_sensor_is_visible(api, tokens):
    token = tokens["producer"]
    result = await ingest(api, reading(0, "FALLA_DHT", temp_c=None, hum_aire_pct=None,
                                       suelo_pct=55))
    assert result["accepted"] == 1 and result["errors"] == []
    assert (await latest_metric(api, token, "temp_c"))["sensor_down"] is True
    [rec] = await stored_recommendations(api, token)
    assert "temperature" in rec["missing"]
    assert rec["inputs"]["temperature"]["value"] is None


@pytest.mark.asyncio
async def test_broken_soil_sensor_is_visible(api, tokens):
    token = tokens["producer"]
    result = await ingest(api, reading(0, "FALLA_SUELO", suelo_pct=None, suelo_raw=None))
    assert result["accepted"] == 1 and result["errors"] == []
    assert (await latest_metric(api, token, "suelo_pct"))["sensor_down"] is True
    # Sin tierra no se inventa una recomendación guardada…
    assert await stored_recommendations(api, token) == []
    # …y el dashboard dice que falta el dato.
    rec = await get(api, token, "/irrigation/recommendation")
    assert rec["outcome"] == "insufficient_data"
    assert "soil" in rec["missing"]


@pytest.mark.asyncio
async def test_invalid_reading_is_rejected_and_logged(api, caplog):
    caplog.set_level(logging.WARNING, logger=INGEST_LOGGER)
    result = await ingest(api, reading(0), reading(2, suelo_pct=150))
    assert result["accepted"] == 1
    assert [r["index"] for r in result["rejected"]] == [1]
    assert "suelo_pct" in result["rejected"][0]["reason"]
    warnings = ingest_records(caplog, logging.WARNING)
    assert warnings, "la lectura rechazada no quedó en el log"
    assert any(GATEWAY_ID in r.getMessage() and "suelo_pct" in r.getMessage() for r in warnings)


@pytest.mark.asyncio
async def test_unexplained_recommendation_is_an_explicit_error(api, tokens, monkeypatch, caplog):
    """Eslabón IA roto: la lectura se guarda, nada sin explicación se guarda y se avisa."""
    token = tokens["producer"]
    monkeypatch.setattr(recommendation_router, "explain",
                        lambda *args, **kwargs: SimpleNamespace(summary=" ", reasons=[], notes=[]))
    caplog.set_level(logging.ERROR, logger=INGEST_LOGGER)

    result = await ingest(api, reading(0, "SUELO_SECO", suelo_pct=42))
    assert result["accepted"] == 1
    assert [e["step"] for e in result["errors"]] == ["recommendation"]
    assert "explicación" in result["errors"][0]["message"]

    assert (await latest_metric(api, token, "suelo_pct"))["value"] == 42      # guardada
    assert (await get(api, token, "/alerts"))["active_count"] == 1           # los demás siguen
    assert await stored_recommendations(api, token) == []                     # nada sin explicar
    events = await audit_events(api, tokens["admin"], event_type="recommendation")
    assert events == []
    assert ingest_records(caplog, logging.ERROR), "el fallo no quedó en el log"


async def _boom(*args, **kwargs):
    raise RuntimeError("fallo simulado")


@pytest.mark.asyncio
@pytest.mark.parametrize("module, function, step", [
    (actuator_state, "apply_readings", "actuator_state"),
    (irrigation, "process", "irrigation"),
    (alerts, "evaluate", "alerts"),
    (irrigation, "deliver", "commands"),
])
async def test_a_broken_step_is_reported_and_the_rest_still_runs(
    api, tokens, equipment, monkeypatch, caplog, module, function, step,
):
    token = tokens["producer"]
    monkeypatch.setattr(module, function, _boom)
    caplog.set_level(logging.ERROR, logger=INGEST_LOGGER)

    result = await ingest(api, reading(0, "SUELO_SECO", suelo_pct=42, riego_sugerido=True))
    assert result["accepted"] == 1
    assert result["errors"] == [{"step": step, "message": "fallo simulado"}]
    assert ingest_records(caplog, logging.ERROR), "el fallo no quedó en el log"

    # La lectura quedó guardada.
    assert (await latest_metric(api, token, "suelo_pct"))["value"] == 42
    # Los demás pasos corrieron.
    active = (await get(api, token, "/alerts"))["active_count"]
    assert active == (0 if step == "alerts" else 1)
    [rec] = await stored_recommendations(api, token)
    assert rec["outcome"] == "irrigate"
    if step == "commands":
        assert result["commands"] == []


@pytest.mark.asyncio
async def test_the_chain_recovers_on_the_next_batch(api, tokens, monkeypatch):
    """Un fallo pasajero no deja el error pegado: el siguiente lote sale limpio."""
    monkeypatch.setattr(alerts, "evaluate", _boom)
    first = await ingest(api, reading(0, "SUELO_SECO", suelo_pct=42))
    assert [e["step"] for e in first["errors"]] == ["alerts"]
    monkeypatch.undo()
    second = await ingest(api, reading(2, "SUELO_SECO", suelo_pct=42))
    assert second["errors"] == []
    assert (await get(api, tokens["producer"], "/alerts"))["active_count"] == 1
