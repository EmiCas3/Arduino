"""
Tests de AI-07 — task "Build accept/reject UI + store the producer's decision".

Criterios de aceptación del item:
  1. Cuando el productor acepta o rechaza una recomendación mostrada, la
     decisión se guarda con el usuario y la hora.
  2. Una recomendación de riego aceptada permite disparar el riego desde la
     misma pantalla (la orden manual de ACT-02).
  3. Una recomendación rechazada sigue visible en el historial con lo que
     recomendaba.

Además la decisión queda en la bitácora de AUTH-05.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import GREENHOUSE_ID, OTHER_GREENHOUSE_ID, bearer, insert_readings
from tests.test_irrigation import PUMP, equipment, manual_mode  # noqa: F401  (fixture)
from tests.test_recommendation import URL, recent_readings

HISTORY_URL = f"{URL}s"


def decision_url(recommendation_id, greenhouse=GREENHOUSE_ID) -> str:
    return f"/api/v1/greenhouses/{greenhouse}/irrigation/recommendations/{recommendation_id}/decision"


async def shown(api, tokens, role="producer") -> dict:
    resp = await api.get(URL, headers=bearer(tokens[role]))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def decide(api, tokens, recommendation_id, decision, role="producer"):
    return await api.post(
        decision_url(recommendation_id), json={"decision": decision},
        headers=bearer(tokens[role]),
    )


async def set_soil(db, soil):
    await db.execute("DELETE FROM readings")
    await db.commit()
    await insert_readings(db, recent_readings(suelo_pct=soil))


# ── La recomendación mostrada se puede identificar ────────────────────

@pytest.mark.asyncio
async def test_the_shown_recommendation_is_the_stored_one(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=55))
    first = await shown(api, tokens)
    again = await shown(api, tokens)

    items = (await api.get(HISTORY_URL, headers=bearer(tokens["producer"]))).json()["items"]
    assert first["id"] is not None
    assert first["id"] == again["id"] == items[0]["id"]
    assert first["decision"] is None


# ── Criterio 1: se guarda con usuario y hora ──────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["accepted", "rejected"])
async def test_the_decision_is_stored_with_user_and_time(api, db, tokens, decision):
    await insert_readings(db, recent_readings(suelo_pct=55))
    recommendation = await shown(api, tokens)
    before = datetime.now(timezone.utc)

    resp = await decide(api, tokens, recommendation["id"], decision)
    assert resp.status_code == 201, resp.text
    saved = resp.json()
    assert saved["decision"] == decision
    assert saved["user_name"] == "Productor de prueba"
    decided_at = datetime.fromisoformat(saved["decided_at"].replace("Z", "+00:00"))
    assert before - timedelta(seconds=1) <= decided_at <= datetime.now(timezone.utc)

    # Sale en la recomendación actual y en el historial.
    assert (await shown(api, tokens))["decision"] == saved
    items = (await api.get(HISTORY_URL, headers=bearer(tokens["producer"]))).json()["items"]
    assert items[0]["decision"] == saved

    cursor = await db.execute("SELECT user_id FROM recommendation_decisions")
    assert (await cursor.fetchone())["user_id"] is not None


@pytest.mark.asyncio
async def test_the_decision_goes_to_the_audit_log(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=55))
    recommendation = await shown(api, tokens)
    await decide(api, tokens, recommendation["id"], "rejected")

    cursor = await db.execute(
        "SELECT * FROM audit_log WHERE event_type = 'recommendation' AND actor_type = 'user'"
    )
    events = [dict(row) for row in await cursor.fetchall()]
    assert [event["action"] for event in events] == ["recommendation_rejected"]
    assert events[0]["actor_name"] == "Productor de prueba"
    assert events[0]["target_id"] == str(recommendation["id"])
    assert events[0]["greenhouse_id"] == GREENHOUSE_ID
    assert "Conviene regar ahora." in events[0]["summary"]


@pytest.mark.asyncio
async def test_a_recommendation_is_decided_only_once(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=55))
    recommendation = await shown(api, tokens)
    assert (await decide(api, tokens, recommendation["id"], "accepted")).status_code == 201

    resp = await decide(api, tokens, recommendation["id"], "rejected", role="admin")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "already_decided"


@pytest.mark.asyncio
async def test_an_older_recommendation_can_no_longer_be_decided(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=55))
    old = await shown(api, tokens)
    await set_soil(db, 90)
    await shown(api, tokens)

    resp = await decide(api, tokens, old["id"], "accepted")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "superseded"


@pytest.mark.asyncio
async def test_without_soil_data_there_is_nothing_to_decide(api, db, tokens):
    recommendation = await shown(api, tokens)
    assert recommendation["outcome"] == "insufficient_data"

    resp = await decide(api, tokens, recommendation["id"], "accepted")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "not_decidable"


@pytest.mark.asyncio
async def test_only_accepted_or_rejected(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=55))
    recommendation = await shown(api, tokens)
    assert (await decide(api, tokens, recommendation["id"], "maybe")).status_code == 422


@pytest.mark.asyncio
async def test_unknown_recommendation_and_other_greenhouses(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=55))
    recommendation = await shown(api, tokens)

    assert (await decide(api, tokens, 999, "accepted")).status_code == 404
    other = await api.post(
        decision_url(recommendation["id"], OTHER_GREENHOUSE_ID),
        json={"decision": "accepted"}, headers=bearer(tokens["admin"]),
    )
    assert other.status_code == 404
    forbidden = await api.post(
        decision_url(recommendation["id"], OTHER_GREENHOUSE_ID),
        json={"decision": "accepted"}, headers=bearer(tokens["producer"]),
    )
    assert forbidden.status_code == 403
    anonymous = await api.post(decision_url(recommendation["id"]), json={"decision": "accepted"})
    assert anonymous.status_code == 401


# ── Criterio 2: aceptar y regar desde la misma pantalla ───────────────

@pytest.mark.asyncio
async def test_after_accepting_the_pump_can_be_ordered(api, db, tokens, equipment):
    await manual_mode(api, tokens)
    await set_soil(db, 55)
    recommendation = await shown(api, tokens)
    assert recommendation["outcome"] == "irrigate"
    assert (await decide(api, tokens, recommendation["id"], "accepted")).status_code == 201

    # Es la orden manual de ACT-02: la tarjeta la manda con este mismo POST.
    resp = await api.post(
        f"/api/v1/actuators/{PUMP}/commands", json={"action": "on"},
        headers=bearer(tokens["producer"]),
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["value"] == "ON"


# ── Criterio 3: la rechazada sigue en el historial ────────────────────

@pytest.mark.asyncio
async def test_a_rejected_recommendation_stays_in_the_history(api, db, tokens):
    await insert_readings(db, recent_readings(suelo_pct=55))
    rejected = await shown(api, tokens)
    await decide(api, tokens, rejected["id"], "rejected")

    await set_soil(db, 90)
    await shown(api, tokens)

    items = (await api.get(HISTORY_URL, headers=bearer(tokens["producer"]))).json()["items"]
    assert [item["outcome"] for item in items] == ["too_wet", "irrigate"]
    assert items[0]["decision"] is None
    assert items[1]["id"] == rejected["id"]
    assert items[1]["decision"]["decision"] == "rejected"
    assert items[1]["explanation"] == rejected["explanation"]
