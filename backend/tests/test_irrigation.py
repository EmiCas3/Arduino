"""
Tests de ACT-02 — task "Build rule engine for automatic trigger".

El ARDUINO decide el riego automático con sus constantes (objetivo
SUELO_LIMITE_MIN, tiempo máximo BOMBA_MAX_MS). La plataforma guarda el modo,
manda las órdenes manuales por el gateway y registra cada acción.

Criterios de aceptación:
  1. En automático y con la tierra abajo del objetivo, el riego arranca solo.
  2. Riego automático en curso: para al llegar al objetivo o al tiempo máximo.
  3. En manual no se automatiza: solo actúan las órdenes manuales.
  4. Una acción automática se distingue de una manual al registrarse.

Aquí se prueba la parte de la plataforma: lo que el Arduino reporta en cada
lectura (`bomba_activa`, `modo`, `cmd_id`, `cmd_err`, `bomba_paro`) y lo que
la plataforma le manda (`commands` en la respuesta de la ingesta). La lógica
del firmware se prueba en vivero_sensores/pruebas_pc y con el simulador del
gateway.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from tests.conftest import (
    GATEWAY_API_KEY,
    GATEWAY_ID,
    GREENHOUSE_ID,
    OTHER_GREENHOUSE_ID,
    USERS,
    bearer,
    now_iso,
)

INGEST_URL = f"/api/v1/devices/{GATEWAY_ID}/readings"
BASE = f"/api/v1/greenhouses/{GREENHOUSE_ID}"
PUMP, FAN = "bomba-test-01", "vent-test-01"
COMMANDS_URL = f"/api/v1/actuators/{PUMP}/commands"
T0 = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(microsecond=0)
_clock = {"s": 0}


def next_ts() -> str:
    """Cada lectura nueva, 2 s después de la anterior (como el Arduino)."""
    _clock["s"] += 2
    moment = T0 + timedelta(seconds=_clock["s"])
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def reading(**overrides) -> dict:
    base = {
        "ts": next_ts(), "ms": 1000, "temp_c": 23.0, "hum_aire_pct": 70.0,
        "suelo_raw": 380, "suelo_pct": 62, "nivel_raw": 120, "nivel_pct": 80,
        "luz_raw": 306, "luz_nivel": 717, "es_dia": True, "sol_min_hoy": 42,
        "sol_min_prev": None, "riego_sugerido": False, "bomba_habilitada": True,
        "bomba_activa": False, "vent": "BASE", "vent_activo": False,
        "modo": "AUTO", "cmd_id": 0, "cmd_err": None, "bomba_paro": None,
        "estado": "OK", "alertas": [],
    }
    base.update(overrides)
    return base


def old_firmware(**overrides) -> dict:
    """Lectura del firmware de antes de ACT-02: sin modo ni órdenes."""
    data = reading(**overrides)
    for field in ("modo", "cmd_id", "cmd_err", "bomba_paro"):
        if field not in overrides:
            data.pop(field)
    return data


async def ingest(api, *readings, api_key=GATEWAY_API_KEY, url=INGEST_URL) -> dict:
    resp = await api.post(url, json={"readings": list(readings)}, headers={"X-API-Key": api_key})
    assert resp.status_code == 202, resp.text
    return resp.json()


async def get(api, token, path) -> dict:
    resp = await api.get(BASE + path, headers=bearer(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def events(api, token) -> list:
    data = await get(api, token, "/actuators/events")
    return list(reversed(data["events"]))   # en orden de hora


async def set_mode(api, token, mode, greenhouse=GREENHOUSE_ID):
    return await api.put(f"/api/v1/greenhouses/{greenhouse}/irrigation/mode",
                         json={"mode": mode}, headers=bearer(token))


async def command_status(db, command_id) -> dict:
    cursor = await db.execute("SELECT * FROM actuator_commands WHERE id = ?", (command_id,))
    return dict(await cursor.fetchone())


@pytest.fixture(autouse=True)
def reset_clock():
    _clock["s"] = 0


@pytest.fixture
async def equipment(db):
    for actuator_id, name, kind, channel in (
        (PUMP, "Bomba de riego", "pump", "L298N-B/D5"),
        (FAN, "Ventilador", "fan", "L298N-A/D3"),
    ):
        await db.execute(
            """
            INSERT INTO actuators (actuator_id, name, type, greenhouse_id, area, control_channel,
                                   gateway_device_id, status, registered_at)
            VALUES (?, ?, ?, ?, 'general', ?, ?, 'active', ?)
            """,
            (actuator_id, name, kind, GREENHOUSE_ID, channel, GATEWAY_ID, now_iso()),
        )
    await db.commit()


async def manual_mode(api, tokens) -> int:
    """Pasa a manual y deja la orden confirmada por el Arduino. Devuelve su id."""
    await ingest(api, reading())
    resp = await set_mode(api, tokens["producer"], "manual")
    assert resp.status_code == 200, resp.text
    sent = (await ingest(api, reading()))["commands"]
    assert [(c["kind"], c["value"]) for c in sent] == [("mode", "MANUAL")]
    await ingest(api, reading(modo="MANUAL", cmd_id=sent[0]["id"]))
    return sent[0]["id"]


# ── Criterios 1 y 2: el Arduino riega solo y se registra ───────────────

@pytest.mark.asyncio
async def test_auto_irrigation_is_recorded_with_reason(api, tokens, equipment):
    await ingest(api, reading(suelo_pct=55))
    await ingest(api, reading(suelo_pct=42, riego_sugerido=True, bomba_activa=True))
    await ingest(api, reading(suelo_pct=51, bomba_activa=False, bomba_paro="OBJETIVO"))
    log = await events(api, tokens["producer"])
    assert [(e["event"], e["origin"]) for e in log] == [("on", "auto"), ("off", "auto")]
    assert log[0]["reason"] == "Tierra al 42 %, abajo del objetivo de 50 %"
    assert log[1]["reason"] == "Objetivo alcanzado (tierra al 51 %)"
    assert log[0]["user_name"] is None and log[0]["actuator_name"] == "Bomba de riego"


@pytest.mark.asyncio
async def test_stop_at_max_run_time_is_recorded(api, tokens, equipment):
    await ingest(api, reading(suelo_pct=40), reading(suelo_pct=40, bomba_activa=True))
    await ingest(api, reading(suelo_pct=44, bomba_activa=False, bomba_paro="TIEMPO"))
    log = await events(api, tokens["producer"])
    assert log[-1]["reason"] == "Tiempo máximo (60 s)"


@pytest.mark.asyncio
@pytest.mark.parametrize("paro,reason", [
    ("TANQUE", "Tanque bajo"), ("SENSOR", "Falla del sensor de tierra"),
])
async def test_other_stop_reasons(api, tokens, equipment, paro, reason):
    await ingest(api, reading(suelo_pct=40), reading(suelo_pct=40, bomba_activa=True),
                 reading(suelo_pct=40, bomba_activa=False, bomba_paro=paro))
    assert (await events(api, tokens["producer"]))[-1]["reason"] == reason


@pytest.mark.asyncio
async def test_old_firmware_actions_are_automatic(api, tokens, equipment):
    """Con el firmware de antes (sin `modo`) todo lo decide el Arduino."""
    await ingest(api, old_firmware(suelo_pct=55),
                 old_firmware(suelo_pct=45, bomba_activa=True),
                 old_firmware(suelo_pct=50, bomba_activa=False))
    log = await events(api, tokens["producer"])
    assert [(e["event"], e["origin"]) for e in log] == [("on", "auto"), ("off", "auto")]
    assert log[1]["reason"] == "Objetivo alcanzado (tierra al 50 %)"


@pytest.mark.asyncio
async def test_first_report_is_not_an_event(api, tokens, equipment):
    await ingest(api, reading(bomba_activa=True))
    assert await events(api, tokens["producer"]) == []


@pytest.mark.asyncio
async def test_fan_actions_are_always_automatic(api, tokens, equipment):
    await ingest(api, reading(), reading(vent="ALTA", vent_activo=True, temp_c=27.0))
    log = await events(api, tokens["producer"])
    assert [(e["actuator_id"], e["event"], e["origin"]) for e in log] == [(FAN, "on", "auto")]
    assert log[0]["reason"] == "Temperatura o humedad arriba del ideal"


# ── Modo automático / manual ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_default_mode_is_auto_with_firmware_params(api, tokens, equipment):
    data = await get(api, tokens["producer"], "/irrigation")
    assert (data["mode"], data["controllable"], data["reported_mode"]) == ("auto", False, None)
    assert data["firmware"] == {"target_pct": 50, "max_run_seconds": 60, "rest_seconds": 300}
    await ingest(api, reading())
    data = await get(api, tokens["producer"], "/irrigation")
    assert (data["mode"], data["controllable"], data["reported_mode"]) == ("auto", True, "auto")
    assert data["controller_device_id"] == GATEWAY_ID


@pytest.mark.asyncio
async def test_old_firmware_cannot_be_controlled(api, tokens, equipment):
    await ingest(api, old_firmware())
    data = await get(api, tokens["producer"], "/irrigation")
    assert data["controllable"] is False
    assert "firmware" in data["detail"]
    resp = await set_mode(api, tokens["producer"], "manual")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "firmware_without_control"


@pytest.mark.asyncio
async def test_mode_change_is_delivered_and_confirmed(api, db, tokens, equipment):
    await ingest(api, reading())
    resp = await set_mode(api, tokens["producer"], "manual")
    data = resp.json()
    assert data["mode"] == "manual"
    assert data["mode_changed_by"] == USERS["producer"]["full_name"]
    assert data["last_mode_command"]["status"] == "pending"
    assert data["last_mode_command"]["trigger"] == "manual"

    delivered = (await ingest(api, reading()))["commands"]
    assert delivered == [{"id": data["last_mode_command"]["id"], "kind": "mode",
                          "value": "MANUAL", "actuator_id": None}]
    assert (await command_status(db, delivered[0]["id"]))["status"] == "sent"
    assert (await ingest(api, reading()))["commands"] == []          # no se repite

    await ingest(api, reading(modo="MANUAL", cmd_id=delivered[0]["id"]))
    assert (await command_status(db, delivered[0]["id"]))["status"] == "confirmed"
    data = await get(api, tokens["producer"], "/irrigation")
    assert (data["mode"], data["reported_mode"]) == ("manual", "manual")


@pytest.mark.asyncio
async def test_resync_mode_after_arduino_reset(api, db, tokens, equipment):
    """El Arduino se reinicia (vuelve a AUTO, cmd_id 0): se le regresa a MANUAL."""
    await manual_mode(api, tokens)
    resp = await ingest(api, reading(modo="AUTO", cmd_id=0))
    assert [(c["kind"], c["value"]) for c in resp["commands"]] == [("mode", "MANUAL")]
    command = await command_status(db, resp["commands"][0]["id"])
    assert (command["trigger"], command["user_id"]) == ("system", None)
    # Mientras esa orden va en camino no se manda otra igual.
    assert (await ingest(api, reading(modo="AUTO", cmd_id=0)))["commands"] == []


@pytest.mark.asyncio
async def test_no_resync_when_modes_match(api, tokens, equipment):
    await ingest(api, reading())
    assert (await ingest(api, reading()))["commands"] == []


@pytest.mark.asyncio
async def test_switch_to_manual_stops_auto_irrigation(api, tokens, equipment):
    """ACT-02 #3: el Arduino apaga el riego automático al pasar a manual."""
    await ingest(api, reading(suelo_pct=40), reading(suelo_pct=40, bomba_activa=True))
    resp = await set_mode(api, tokens["producer"], "manual")
    command_id = resp.json()["last_mode_command"]["id"]
    await ingest(api, reading(suelo_pct=40, bomba_activa=True))   # entrega la orden
    await ingest(api, reading(suelo_pct=40, bomba_activa=False, modo="MANUAL",
                              cmd_id=command_id, bomba_paro="MODO"))
    log = await events(api, tokens["producer"])
    assert (log[-1]["event"], log[-1]["origin"], log[-1]["reason"]) == ("off", "manual", "Cambio de modo")
    assert log[-1]["user_name"] == USERS["producer"]["full_name"]


@pytest.mark.asyncio
async def test_rejected_command_keeps_the_arduino_reason(api, db, tokens, equipment):
    await manual_mode(api, tokens)
    sent = await api.post(COMMANDS_URL, json={"action": "on"}, headers=bearer(tokens["producer"]))
    command_id = sent.json()["id"]
    await ingest(api, reading(modo="MANUAL"))
    await ingest(api, reading(modo="MANUAL", cmd_id=command_id, cmd_err="TANQUE"))
    command = await command_status(db, command_id)
    assert (command["status"], command["error"]) == ("rejected", "TANQUE")


@pytest.mark.asyncio
async def test_unconfirmed_command_expires_and_late_ack_confirms(api, db, tokens, equipment):
    await ingest(api, reading())
    command_id = (await set_mode(api, tokens["producer"], "manual")).json()["last_mode_command"]["id"]
    await ingest(api, reading())                                   # sale hacia el gateway
    old = (datetime.now(timezone.utc) - timedelta(seconds=settings.command_ack_seconds + 5)).isoformat()
    await db.execute("UPDATE actuator_commands SET sent_at = ? WHERE id = ?", (old, command_id))
    await db.commit()
    data = await get(api, tokens["producer"], "/irrigation")
    assert data["last_mode_command"]["status"] == "expired"
    await ingest(api, reading(modo="MANUAL", cmd_id=command_id))   # llegó tarde, pero llegó
    assert (await command_status(db, command_id))["status"] == "confirmed"


# ── Criterio 3: en manual solo actúan las órdenes manuales ─────────────

@pytest.mark.asyncio
async def test_manual_command_rejected_in_auto_mode(api, tokens, equipment):
    await ingest(api, reading())
    resp = await api.post(COMMANDS_URL, json={"action": "on"}, headers=bearer(tokens["producer"]))
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "auto_mode"


@pytest.mark.asyncio
async def test_manual_on_and_off_are_recorded_as_manual(api, tokens, equipment):
    await manual_mode(api, tokens)
    resp = await api.post(COMMANDS_URL, json={"action": "on"}, headers=bearer(tokens["producer"]))
    assert resp.status_code == 202
    on = resp.json()
    assert (on["kind"], on["value"], on["status"], on["user_name"]) == (
        "pump", "ON", "pending", USERS["producer"]["full_name"])

    delivered = (await ingest(api, reading(modo="MANUAL")))["commands"]
    assert delivered == [{"id": on["id"], "kind": "pump", "value": "ON", "actuator_id": PUMP}]
    await ingest(api, reading(modo="MANUAL", cmd_id=on["id"], bomba_activa=True))

    off = (await api.post(COMMANDS_URL, json={"action": "off"}, headers=bearer(tokens["admin"]))).json()
    await ingest(api, reading(modo="MANUAL", cmd_id=on["id"], bomba_activa=True))
    await ingest(api, reading(modo="MANUAL", cmd_id=off["id"], bomba_activa=False, bomba_paro="ORDEN"))

    log = await events(api, tokens["producer"])
    assert [(e["event"], e["origin"], e["user_name"], e["reason"]) for e in log] == [
        ("on", "manual", USERS["producer"]["full_name"], "Orden manual"),
        ("off", "manual", USERS["admin"]["full_name"], "Orden manual"),
    ]


@pytest.mark.asyncio
async def test_manual_mode_never_records_auto_pump_actions(api, tokens, equipment):
    """ACT-02 #3/#4: en MANUAL un cambio de la bomba es manual, aunque se apague por tiempo."""
    await manual_mode(api, tokens)
    await ingest(api, reading(modo="MANUAL", bomba_activa=True),
                 reading(modo="MANUAL", bomba_activa=False, bomba_paro="TIEMPO"))
    assert {e["origin"] for e in await events(api, tokens["producer"])} == {"manual"}


@pytest.mark.asyncio
async def test_new_command_supersedes_the_pending_one(api, db, tokens, equipment):
    await manual_mode(api, tokens)
    first = (await api.post(COMMANDS_URL, json={"action": "on"}, headers=bearer(tokens["producer"]))).json()
    second = (await api.post(COMMANDS_URL, json={"action": "off"}, headers=bearer(tokens["producer"]))).json()
    delivered = (await ingest(api, reading(modo="MANUAL")))["commands"]
    assert [c["id"] for c in delivered] == [second["id"]]
    assert (await command_status(db, first["id"]))["status"] == "superseded"


@pytest.mark.asyncio
async def test_manual_on_refused_with_low_tank(api, tokens, equipment):
    await manual_mode(api, tokens)
    await ingest(api, reading(modo="MANUAL", bomba_habilitada=False, nivel_pct=10))
    resp = await api.post(COMMANDS_URL, json={"action": "on"}, headers=bearer(tokens["producer"]))
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "tank_low"
    off = await api.post(COMMANDS_URL, json={"action": "off"}, headers=bearer(tokens["producer"]))
    assert off.status_code == 202    # apagar siempre se puede


@pytest.mark.asyncio
async def test_only_the_pump_accepts_commands(api, tokens, equipment):
    await manual_mode(api, tokens)
    resp = await api.post(f"/api/v1/actuators/{FAN}/commands", json={"action": "on"},
                          headers=bearer(tokens["producer"]))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "not_controllable"


@pytest.mark.asyncio
async def test_command_body_is_strict(api, tokens, equipment):
    await manual_mode(api, tokens)
    for body in ({"action": "prende"}, {"action": "on", "max_s": 600}, {}):
        resp = await api.post(COMMANDS_URL, json=body, headers=bearer(tokens["producer"]))
        assert resp.status_code == 422, body


@pytest.mark.asyncio
async def test_status_shows_the_last_command(api, tokens, equipment):
    await manual_mode(api, tokens)
    await api.post(COMMANDS_URL, json={"action": "on"}, headers=bearer(tokens["producer"]))
    status = await get(api, tokens["producer"], "/actuators/status")
    pump = next(a for a in status["actuators"] if a["actuator_id"] == PUMP)
    fan = next(a for a in status["actuators"] if a["actuator_id"] == FAN)
    assert (pump["last_command"]["value"], pump["last_command"]["status"]) == ("ON", "pending")
    assert fan["last_command"] is None


# ── Entrega por gateway ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_commands_go_only_to_their_gateway(api, db, tokens, equipment):
    other_key = "otra-llave-de-prueba-123"
    await db.execute(
        """
        INSERT INTO devices (device_id, greenhouse_id, type, status, api_key_hash, registered_at)
        VALUES ('pi-otro-01', ?, 'gateway', 'active', ?, ?)
        """,
        (OTHER_GREENHOUSE_ID, hashlib.sha256(other_key.encode()).hexdigest(), now_iso()),
    )
    await db.commit()
    await ingest(api, reading())
    await set_mode(api, tokens["producer"], "manual")
    other = await ingest(api, reading(), api_key=other_key, url="/api/v1/devices/pi-otro-01/readings")
    assert other["commands"] == []
    assert len((await ingest(api, reading()))["commands"]) == 1


@pytest.mark.asyncio
async def test_duplicate_batch_still_delivers_commands(api, tokens, equipment):
    first = reading()
    await ingest(api, first)
    await set_mode(api, tokens["producer"], "manual")
    data = await ingest(api, first)          # el gateway reintenta el mismo lote
    assert data["duplicates"] == 1
    assert [(c["kind"], c["value"]) for c in data["commands"]] == [("mode", "MANUAL")]


# ── Permisos (AUTH-02) ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_producer_cannot_touch_other_greenhouse(api, tokens, equipment):
    assert (await set_mode(api, tokens["producer"], "manual", OTHER_GREENHOUSE_ID)).status_code == 403
    resp = await api.get(f"/api/v1/greenhouses/{OTHER_GREENHOUSE_ID}/irrigation",
                         headers=bearer(tokens["producer"]))
    assert resp.status_code == 403
    resp = await api.get(f"/api/v1/greenhouses/{OTHER_GREENHOUSE_ID}/actuators/events",
                         headers=bearer(tokens["producer"]))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_change_mode_of_any_greenhouse(api, tokens, equipment):
    await ingest(api, reading())
    resp = await set_mode(api, tokens["admin"], "manual")
    assert resp.status_code == 200
    assert resp.json()["mode_changed_by"] == USERS["admin"]["full_name"]


@pytest.mark.asyncio
async def test_greenhouse_without_pump(api, tokens):
    data = await get(api, tokens["producer"], "/irrigation")
    assert data["controllable"] is False
    assert (await set_mode(api, tokens["producer"], "manual")).json()["detail"]["error"] == "no_pump"


# ── Reinicios del Arduino ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lost_mode_command_is_not_confirmed_and_is_resent(api, db, tokens, equipment):
    """El resync se pierde en un reinicio y luego el Arduino rechaza una orden
    de bomba por estar en AUTO: el cambio de modo NO cuenta como confirmado."""
    await manual_mode(api, tokens)
    resync = (await ingest(api, reading(modo="AUTO", cmd_id=0)))["commands"][0]   # se reinició
    pump = (await api.post(COMMANDS_URL, json={"action": "on"}, headers=bearer(tokens["producer"]))).json()
    await ingest(api, reading(modo="AUTO", cmd_id=0))                          # sale la orden de bomba
    resp = await ingest(api, reading(modo="AUTO", cmd_id=pump["id"], cmd_err="MODO"))
    assert (await command_status(db, resync["id"]))["status"] == "expired"
    assert (await command_status(db, pump["id"]))["status"] == "rejected"
    assert [(c["kind"], c["value"]) for c in resp["commands"]] == [("mode", "MANUAL")]   # otra vez, ya


@pytest.mark.asyncio
async def test_resync_turning_the_pump_off_is_not_a_person(api, tokens, equipment):
    await manual_mode(api, tokens)
    resync = (await ingest(api, reading(modo="AUTO", cmd_id=0, suelo_pct=40)))["commands"][0]
    await ingest(api, reading(modo="AUTO", cmd_id=0, suelo_pct=40, bomba_activa=True))   # riega solo
    await ingest(api, reading(modo="MANUAL", cmd_id=resync["id"], suelo_pct=40,
                              bomba_activa=False, bomba_paro="MODO"))
    last = (await events(api, tokens["producer"]))[-1]
    assert (last["event"], last["origin"], last["user_name"]) == ("off", "auto", None)
    assert last["reason"] == "Se restableció el modo elegido (MANUAL)"


@pytest.mark.asyncio
async def test_pump_off_by_arduino_reset(api, tokens, equipment):
    await manual_mode(api, tokens)
    on = (await api.post(COMMANDS_URL, json={"action": "on"}, headers=bearer(tokens["producer"]))).json()
    await ingest(api, reading(modo="MANUAL"))
    await ingest(api, reading(modo="MANUAL", cmd_id=on["id"], bomba_activa=True, suelo_pct=55))
    await ingest(api, reading(modo="AUTO", cmd_id=0, bomba_activa=False, suelo_pct=55))   # se reinició
    last = (await events(api, tokens["producer"]))[-1]
    assert (last["event"], last["origin"]) == ("off", "auto")
    assert last["reason"] == "El Arduino se reinició (la bomba arranca apagada)"
