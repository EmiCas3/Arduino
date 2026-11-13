"""
Tests de AUTH-05 — bitácora de auditoría.

Tasks: "Design unified event schema (user activity, alerts, actuator events)"
e "Implement audit-trail write layer + query API".

Criterios de aceptación:
  1. Inicios de sesión, alertas, eventos de actuadores y recomendaciones de
     la IA: cada uno queda en una bitácora central con actor, acción y hora.
  2. Una consulta por fecha y tipo de evento devuelve los que coinciden.
  3. Desde la aplicación, lo ya escrito no se puede modificar ni borrar.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.routing import APIRoute

from app import audit
from app.database import close_db, init_db
from tests import test_alerts, test_irrigation
from tests.conftest import (
    GATEWAY_ID,
    GREENHOUSE_ID,
    OTHER_GREENHOUSE_ID,
    PASSWORDS,
    USERS,
    bearer,
    insert_readings,
    login,
)
from tests.test_irrigation import PUMP, equipment, reset_clock  # noqa: F401  (fixtures)

AUDIT_URL = "/api/v1/audit/events"
LOGIN_URL = "/api/v1/auth/login"
NOW = datetime.now(timezone.utc)


async def events(api, token, **params) -> dict:
    resp = await api.get(AUDIT_URL, params=params, headers=bearer(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def rows(db, action=None) -> list:
    """Eventos directo de la BD, del más viejo al más nuevo."""
    query = "SELECT * FROM audit_log"
    cursor = await db.execute(
        query + (" WHERE action = ?" if action else "") + " ORDER BY id",
        (action,) if action else (),
    )
    return [dict(row) for row in await cursor.fetchall()]


def assert_who_what_when(event: dict) -> None:
    """Criterio 1: todo evento dice quién, qué y cuándo."""
    assert event["actor_type"] in audit.ACTOR_TYPES
    assert event["action"] in audit.ACTIONS[event["event_type"]]
    assert event["summary"]
    assert datetime.fromisoformat(event["occurred_at"]).tzinfo is not None
    assert datetime.fromisoformat(event["recorded_at"]).tzinfo is not None


# ── Criterio 1: cada cosa que pasa queda registrada ────────────────────

@pytest.mark.asyncio
async def test_login_is_recorded_with_the_user_as_actor(api, db):
    await login(api, "producer")
    [event] = await rows(db, "login")
    assert_who_what_when(event)
    assert event["event_type"] == "auth"
    assert (event["actor_type"], event["actor_name"]) == ("user", USERS["producer"]["full_name"])
    assert event["greenhouse_id"] == GREENHOUSE_ID
    assert event["target_type"] == "session" and event["target_id"]


@pytest.mark.asyncio
async def test_failed_login_is_recorded_without_what_was_typed(api, db):
    """El motivo queda en la bitácora; lo que se tecleó, no (NFR-03)."""
    secret = "Esto-Era-Mi-Clave-99"
    await api.post(LOGIN_URL, json={"login": "productor", "password": secret})
    await api.post(LOGIN_URL, json={"login": secret, "password": "x"})

    wrong, unknown = await rows(db, "login_failed")
    assert wrong["actor_type"] == "user" and '"wrong_password"' in wrong["detail"]
    assert unknown["actor_type"] == "anonymous" and unknown["actor_id"] is None
    assert '"unknown_user"' in unknown["detail"]
    for event in (wrong, unknown):
        assert_who_what_when(event)
        assert secret not in str(event)


@pytest.mark.asyncio
async def test_inactive_user_login_is_recorded(api, db):
    await db.execute("UPDATE users SET status = 'inactive' WHERE username = 'productor'")
    await db.commit()
    resp = await api.post(
        LOGIN_URL, json={"login": "productor", "password": PASSWORDS["producer"]})
    assert resp.status_code == 401
    [event] = await rows(db, "login_failed")
    assert '"inactive_user"' in event["detail"]


@pytest.mark.asyncio
async def test_logout_and_idle_expiry_are_recorded(api, db, tokens):
    await api.post("/api/v1/auth/logout", headers=bearer(tokens["producer"]))
    [logout] = await rows(db, "logout")
    assert logout["actor_name"] == USERS["producer"]["full_name"]

    long_ago = (NOW - timedelta(hours=3)).isoformat()
    await db.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE user_id = "
        "(SELECT id FROM users WHERE username = 'admin')", (long_ago,))
    await db.commit()
    resp = await api.get("/api/v1/auth/me", headers=bearer(tokens["admin"]))
    assert resp.status_code == 401
    [expired] = await rows(db, "session_expired")
    assert_who_what_when(expired)
    assert expired["actor_name"] == USERS["admin"]["full_name"]
    assert '"idle"' in expired["detail"]


@pytest.mark.asyncio
async def test_alert_raised_and_cleared_are_recorded(api, db):
    """La alerta queda a nombre del Arduino y con la hora de la LECTURA."""
    await test_alerts.ingest(api, test_alerts.reading(0, "TEMP_ALTA"))
    await test_alerts.ingest(api, test_alerts.reading(2, "TEMP_ALTA"))   # sigue: no duplica
    [raised] = await rows(db, "alert_raised")
    assert_who_what_when(raised)
    assert raised["event_type"] == "alert"
    assert (raised["actor_type"], raised["actor_id"]) == ("device", GATEWAY_ID)
    assert raised["occurred_at"] == test_alerts.stored(0)
    assert raised["greenhouse_id"] == GREENHOUSE_ID and raised["target_type"] == "alert"
    assert '"TEMP_ALTA"' in raised["detail"] and "31.2" in raised["detail"]

    await test_alerts.ingest(api, test_alerts.reading(4), test_alerts.reading(70))
    [cleared] = await rows(db, "alert_cleared")
    assert cleared["target_id"] == raised["target_id"]
    assert cleared["occurred_at"] == test_alerts.stored(70)


@pytest.mark.asyncio
async def test_automatic_actuator_events_belong_to_the_arduino(api, db, equipment):
    ingest, reading = test_irrigation.ingest, test_irrigation.reading
    await ingest(api, reading())
    await ingest(api, reading(bomba_activa=True, suelo_pct=31))
    await ingest(api, reading(bomba_activa=False, bomba_paro="OBJETIVO", suelo_pct=40))

    [on] = await rows(db, "actuator_on")
    [off] = await rows(db, "actuator_off")
    for event in (on, off):
        assert_who_what_when(event)
        assert event["event_type"] == "actuator"
        assert (event["actor_type"], event["actor_id"]) == ("device", GATEWAY_ID)
        assert (event["target_type"], event["target_id"]) == ("actuator", PUMP)
        assert '"origin": "auto"' in event["detail"]
    assert on["occurred_at"] < off["occurred_at"]


@pytest.mark.asyncio
async def test_manual_actions_belong_to_the_person(api, db, tokens, equipment):
    """Cambio de modo, orden y encendido manual: a nombre de quien los pidió."""
    ingest, reading = test_irrigation.ingest, test_irrigation.reading
    await test_irrigation.manual_mode(api, tokens)
    resp = await api.post(test_irrigation.COMMANDS_URL, json={"action": "on"},
                          headers=bearer(tokens["producer"]))
    assert resp.status_code == 202, resp.text
    command_id = resp.json()["id"]
    await ingest(api, reading(modo="MANUAL", cmd_id=command_id - 1))
    await ingest(api, reading(modo="MANUAL", cmd_id=command_id, bomba_activa=True))

    producer = USERS["producer"]["full_name"]
    [mode] = await rows(db, "mode_changed")
    [command] = await rows(db, "command_requested")
    [on] = await rows(db, "actuator_on")
    for event in (mode, command, on):
        assert_who_what_when(event)
        assert (event["actor_type"], event["actor_name"]) == ("user", producer)
    assert mode["target_type"] == "irrigation_mode" and '"MANUAL"' in mode["detail"]
    assert command["target_id"] == PUMP and f'"command_id": {command_id}' in command["detail"]
    assert '"origin": "manual"' in on["detail"]


@pytest.mark.asyncio
async def test_actuator_registry_changes_are_recorded(api, db, tokens):
    admin = bearer(tokens["admin"])
    body = {"actuator_id": "luz-test-01", "name": "Lámpara", "type": "light",
            "greenhouse_id": GREENHOUSE_ID, "area": "general", "control_channel": "D7"}
    assert (await api.post("/api/v1/actuators", json=body, headers=admin)).status_code == 201
    url = "/api/v1/actuators/luz-test-01"
    assert (await api.patch(url, json={"name": "Lámpara LED"}, headers=admin)).status_code == 200
    assert (await api.patch(url, json={"status": "inactive"}, headers=admin)).status_code == 200
    assert (await api.patch(url, json={"status": "active"}, headers=admin)).status_code == 200

    recorded = [e for e in await rows(db) if e["event_type"] == "actuator"]
    assert [e["action"] for e in recorded] == [
        "actuator_registered", "actuator_updated", "actuator_deactivated", "actuator_reactivated"]
    for event in recorded:
        assert_who_what_when(event)
        assert event["actor_name"] == USERS["admin"]["full_name"]
        assert event["target_id"] == "luz-test-01"
    assert '"to": "Lámpara LED"' in recorded[1]["detail"]


@pytest.mark.asyncio
async def test_a_rejected_action_leaves_no_event(api, db, tokens):
    """Lo que no se ejecutó (403) no aparece como hecho."""
    body = {"actuator_id": "luz-test-02", "name": "Lámpara", "type": "light",
            "greenhouse_id": GREENHOUSE_ID, "area": "general", "control_channel": "D8"}
    resp = await api.post("/api/v1/actuators", json=body, headers=bearer(tokens["producer"]))
    assert resp.status_code == 403
    assert await rows(db, "actuator_registered") == []


@pytest.mark.asyncio
async def test_ai_recommendation_is_recorded_once_per_change(api, db, tokens):
    url = f"/api/v1/greenhouses/{GREENHOUSE_ID}/irrigation/recommendation"
    await insert_readings(db, [{"ts": NOW - timedelta(seconds=30), "suelo_pct": 20,
                                "temp_c": 24.0, "nivel_pct": 80}])
    first = await api.get(url, headers=bearer(tokens["producer"]))
    assert first.status_code == 200, first.text
    await api.get(url, headers=bearer(tokens["producer"]))   # igual: no se repite

    [event] = await rows(db, "recommendation_generated")
    assert_who_what_when(event)
    assert event["event_type"] == "recommendation" and event["actor_type"] == "system"
    assert event["greenhouse_id"] == GREENHOUSE_ID and event["target_type"] == "recommendation"
    assert event["summary"] == first.json()["explanation"]["summary"]
    assert f'"outcome": "{first.json()["outcome"]}"' in event["detail"]


# ── Criterio 2: consulta por fecha y tipo de evento ────────────────────

async def seed_events(db) -> None:
    """Tres eventos en horas conocidas: hace 3 h, hace 2 h y hace 1 h."""
    actor = audit.device_actor(GATEWAY_ID)
    for hours, action, greenhouse in (
        (3, "alert_raised", GREENHOUSE_ID),
        (2, "actuator_on", GREENHOUSE_ID),
        (1, "alert_cleared", OTHER_GREENHOUSE_ID),
    ):
        await audit.record(db, action=action, actor=actor, summary=action,
                           greenhouse_id=greenhouse, detail={"hours": hours},
                           occurred_at=NOW - timedelta(hours=hours))
    await db.commit()


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


@pytest.mark.asyncio
async def test_query_by_event_type(api, db, tokens):
    await seed_events(db)
    data = await events(api, tokens["admin"], event_type="alert")
    assert [e["action"] for e in data["items"]] == ["alert_cleared", "alert_raised"]
    assert data["total"] == 2

    both = await events(api, tokens["admin"], event_type=["alert", "actuator"])
    assert {e["event_type"] for e in both["items"]} == {"alert", "actuator"}

    logins = await events(api, tokens["admin"], event_type="auth")
    assert {e["action"] for e in logins["items"]} == {"login"}   # los 3 del fixture


@pytest.mark.asyncio
async def test_query_by_date_range(api, db, tokens):
    await seed_events(db)
    data = await events(api, tokens["admin"], **{"from": iso(2.5), "to": iso(0.5)})
    assert [e["action"] for e in data["items"]] == ["alert_cleared", "actuator_on"]

    # Los límites se incluyen, y un desfase distinto de UTC significa lo mismo.
    local = (NOW - timedelta(hours=3)).astimezone(timezone(timedelta(hours=-6))).isoformat()
    edge = await events(api, tokens["admin"], **{"from": local, "to": iso(2.5)})
    assert [e["action"] for e in edge["items"]] == ["alert_raised"]

    nothing = await events(api, tokens["admin"], **{"from": iso(10), "to": iso(5)})
    assert (nothing["total"], nothing["items"]) == (0, [])


@pytest.mark.asyncio
async def test_query_by_date_and_type_together(api, db, tokens):
    await seed_events(db)
    data = await events(api, tokens["admin"], event_type="alert",
                        **{"from": iso(2.5), "to": iso(0)})
    assert [e["action"] for e in data["items"]] == ["alert_cleared"]
    event = data["items"][0]
    assert event["detail"] == {"hours": 1}
    assert event["actor_id"] == GATEWAY_ID and event["greenhouse_id"] == OTHER_GREENHOUSE_ID


@pytest.mark.asyncio
async def test_default_range_is_the_last_24_hours(api, db, tokens):
    await audit.record(db, action="alert_raised", actor=audit.SYSTEM_ACTOR, summary="vieja",
                       occurred_at=NOW - timedelta(hours=30))
    await seed_events(db)
    data = await events(api, tokens["admin"], event_type="alert")
    assert "vieja" not in [e["summary"] for e in data["items"]]
    span = datetime.fromisoformat(data["to"].replace("Z", "+00:00")) - \
        datetime.fromisoformat(data["from"].replace("Z", "+00:00"))
    assert span == timedelta(hours=24)


@pytest.mark.asyncio
async def test_other_filters_and_pagination(api, db, tokens):
    await seed_events(db)
    admin = tokens["admin"]
    assert (await events(api, admin, greenhouse_id=OTHER_GREENHOUSE_ID))["total"] == 1
    assert (await events(api, admin, action="actuator_on"))["total"] == 1
    assert (await events(api, admin, actor_type="device"))["total"] == 3
    assert (await events(api, admin, actor_type="device", actor_id="otro"))["total"] == 0

    page1 = await events(api, admin, actor_type="device", limit=2)
    page2 = await events(api, admin, actor_type="device", limit=2, offset=2)
    assert page1["total"] == page2["total"] == 3
    assert [e["action"] for e in page1["items"] + page2["items"]] == [
        "alert_cleared", "actuator_on", "alert_raised"]


@pytest.mark.asyncio
@pytest.mark.parametrize("params,error", [
    ({"from": "2026-11-01T00:00:00"}, "invalid_range"),            # sin zona horaria
    ({"from": "2026-11-02T00:00:00Z", "to": "2026-11-01T00:00:00Z"}, "invalid_range"),
    ({"action": "no_existe"}, "unknown_action"),
])
async def test_invalid_queries_are_rejected(api, tokens, params, error):
    resp = await api.get(AUDIT_URL, params=params, headers=bearer(tokens["admin"]))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == error


@pytest.mark.asyncio
async def test_unknown_event_type_is_rejected(api, tokens):
    resp = await api.get(AUDIT_URL, params={"event_type": "otro"}, headers=bearer(tokens["admin"]))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_only_admins_can_read_the_log(api, tokens):
    assert (await api.get(AUDIT_URL)).status_code == 401
    assert (await api.get(AUDIT_URL, headers=bearer(tokens["producer"]))).status_code == 403
    for role in ("admin", "super_admin"):
        assert (await api.get(AUDIT_URL, headers=bearer(tokens[role]))).status_code == 200


# ── Criterio 3: lo escrito no se modifica ni se borra ──────────────────

@pytest.mark.asyncio
async def test_the_database_refuses_updates_and_deletes(db):
    await seed_events(db)
    before = await rows(db)
    for statement in (
        "UPDATE audit_log SET summary = 'cambiado'",
        "UPDATE audit_log SET occurred_at = '2020-01-01T00:00:00.000+00:00' WHERE id = 1",
        "DELETE FROM audit_log",
        "DELETE FROM audit_log WHERE id = 1",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="solo agregar"):
            await db.execute(statement)
        await db.rollback()
    assert await rows(db) == before


def test_the_api_has_no_route_that_changes_the_log():
    from app.main import app

    audit_routes = [r for r in app.routes if isinstance(r, APIRoute) and "/audit" in r.path]
    assert audit_routes, "falta la ruta de la bitácora"
    assert {m for route in audit_routes for m in route.methods} == {"GET"}


@pytest.mark.asyncio
async def test_writing_methods_are_not_allowed(api, tokens):
    admin = bearer(tokens["super_admin"])
    for method in ("post", "put", "patch", "delete"):
        resp = await getattr(api, method)(AUDIT_URL, headers=admin)
        assert resp.status_code == 405, method


@pytest.mark.asyncio
async def test_the_log_survives_a_restart(tmp_path):
    """Los triggers también protegen una BD que ya existía (no solo la nueva)."""
    path = str(tmp_path / "bitacora.db")
    conn = await init_db(path)
    await audit.record(conn, action="login", actor=audit.SYSTEM_ACTOR, summary="uno")
    await conn.commit()
    await close_db()

    conn = await init_db(path)
    try:
        assert [e["summary"] for e in await rows(conn)] == ["uno"]
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute("DELETE FROM audit_log")
    finally:
        await close_db()


# ── Capa de escritura ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_rejects_what_is_not_in_the_catalog(db):
    with pytest.raises(ValueError):
        await audit.record(db, action="inventada", actor=audit.SYSTEM_ACTOR, summary="x")
    with pytest.raises(ValueError):
        await audit.record(db, action="login", actor={"actor_type": "robot"}, summary="x")
    assert await rows(db) == []


@pytest.mark.asyncio
async def test_record_does_not_commit_on_its_own(db):
    """El evento se guarda (o se descarta) junto con la acción que registra."""
    await audit.record(db, action="login", actor=audit.SYSTEM_ACTOR, summary="x")
    await db.rollback()
    assert await rows(db) == []


def test_every_action_belongs_to_one_event_type():
    actions = [a for group in audit.ACTIONS.values() for a in group]
    assert len(actions) == len(set(actions))
    assert set(audit.ACTIONS) == set(audit.EVENT_TYPES)
