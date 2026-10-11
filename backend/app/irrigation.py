"""
Riego automático y manual (ACT-02).

Quién decide qué (decisión D4 del plan):

  - El ARDUINO decide el riego automático con sus constantes: arranca con la
    tierra abajo de SUELO_LIMITE_MIN, para al llegar a ese objetivo o al
    cumplir BOMBA_MAX_MS, y espera BOMBA_DESCANSO_MS entre riegos.
  - La PLATAFORMA guarda el modo que eligió una persona (automático o
    manual), manda las órdenes manuales y registra cada encendido y apagado
    diciendo si fue automático o manual.

Cómo viaja una orden:

  1. Se guarda en `actuator_commands` como `pending`.
  2. Sale en la respuesta 202 del siguiente POST de lecturas de ese gateway
     (`commands`) y pasa a `sent`. El gateway la escribe por Serial.
  3. El Arduino la aplica y en su siguiente lectura manda `cmd_id` (la última
     orden aplicada) y `cmd_err` si no pudo. Eso la deja en `confirmed` o
     `rejected`. Sin confirmación en SMARTGREENAI_COMMAND_ACK_SECONDS → `expired`.

Si el firmware no manda `modo`, no acepta órdenes: no se genera ninguna y la
UI lo explica. Lo que sí funciona con cualquier firmware es el registro de
encendidos y apagados (todos automáticos, porque los decide el Arduino).
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import aiosqlite

from app.actuator_state import Transition
from app.config import settings
from app.firmware import CONSTANTS
from app.models import CommandOut, ModoEnum, Reading

DEFAULT_MODE = "auto"   # igual que el Arduino al arrancar (decisión D6)
RECENT_COMMAND_MINUTES = 10
PENDING_MAX_MINUTES = 5  # una orden que el gateway nunca recogió

TARGET_PCT = CONSTANTS["SUELO_LIMITE_MIN"]
MAX_RUN_SECONDS = int(CONSTANTS["BOMBA_MAX_MS"] // 1000)
REST_SECONDS = int(CONSTANTS["BOMBA_DESCANSO_MS"] // 1000)

STOP_REASONS = {
    "OBJETIVO": "Objetivo alcanzado",
    "TIEMPO": f"Tiempo máximo ({MAX_RUN_SECONDS} s)",
    "TANQUE": "Tanque bajo",
    "SENSOR": "Falla del sensor de tierra",
    "MODO": "Cambio de modo",
    "ORDEN": "Orden manual",
}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


# ── Consultas ───────────────────────────────────────────────────────────

async def controller_for(db: aiosqlite.Connection, greenhouse_id: str) -> Optional[dict]:
    """El Arduino que controla la bomba del vivero (vía su gateway), o None.

    Devuelve device_id, estado del gateway y lo último que reportó el
    firmware (mode NULL = no acepta órdenes; sin fila = aún no reporta).
    """
    cursor = await db.execute(
        """
        SELECT a.actuator_id, a.gateway_device_id AS device_id, d.status AS device_status,
               c.mode, c.last_cmd_id, c.last_cmd_error, c.reading_ts
        FROM actuators a
        JOIN devices d ON d.device_id = a.gateway_device_id
        LEFT JOIN controller_status c ON c.device_id = a.gateway_device_id
        WHERE a.greenhouse_id = ? AND a.type = 'pump' AND a.status = 'active'
        ORDER BY a.actuator_id
        LIMIT 1
        """,
        (greenhouse_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def chosen_mode(db: aiosqlite.Connection, greenhouse_id: str) -> dict:
    cursor = await db.execute(
        """
        SELECT m.mode, m.changed_at, u.full_name, u.username
        FROM irrigation_mode m LEFT JOIN users u ON u.id = m.changed_by
        WHERE m.greenhouse_id = ?
        """,
        (greenhouse_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return {"mode": DEFAULT_MODE, "changed_at": None, "changed_by": None}
    return {
        "mode": row["mode"],
        "changed_at": row["changed_at"],
        "changed_by": row["full_name"] or row["username"],
    }


async def expire_stale(db: aiosqlite.Connection, now: datetime) -> None:
    """Órdenes que nadie confirmó a tiempo. No hace commit."""
    await db.execute(
        "UPDATE actuator_commands SET status = 'expired' WHERE status = 'sent' AND sent_at < ?",
        (_iso(now - timedelta(seconds=settings.command_ack_seconds)),),
    )
    await db.execute(
        "UPDATE actuator_commands SET status = 'expired' WHERE status = 'pending' AND created_at < ?",
        (_iso(now - timedelta(minutes=PENDING_MAX_MINUTES)),),
    )


COMMAND_SELECT = """
    SELECT c.*, u.full_name, u.username
    FROM actuator_commands c LEFT JOIN users u ON u.id = c.user_id
"""


def command_row(row) -> dict:
    data = dict(row)
    data["user_name"] = data.pop("full_name", None) or data.pop("username", None)
    data.pop("username", None)
    return data


async def recent_command(
    db: aiosqlite.Connection, *, actuator_id: Optional[str] = None,
    device_id: Optional[str] = None, kind: str, now: datetime,
) -> Optional[dict]:
    """La orden más reciente de ese tipo en los últimos minutos (o None)."""
    where = ["c.kind = ?", "c.created_at >= ?"]
    params: list = [kind, _iso(now - timedelta(minutes=RECENT_COMMAND_MINUTES))]
    if actuator_id is not None:
        where.append("c.actuator_id = ?")
        params.append(actuator_id)
    if device_id is not None:
        where.append("c.device_id = ?")
        params.append(device_id)
    cursor = await db.execute(
        f"{COMMAND_SELECT} WHERE {' AND '.join(where)} ORDER BY c.id DESC LIMIT 1", params
    )
    row = await cursor.fetchone()
    return command_row(row) if row else None


def control_problem(controller) -> tuple:
    """(error, mensaje) si al Arduino no se le pueden mandar órdenes; (None, texto) si sí."""
    if controller is None:
        return ("no_pump", "Este vivero no tiene una bomba activa conectada a un gateway.")
    if controller["device_status"] != "active":
        return ("gateway_inactive", "El gateway de la bomba está desactivado.")
    if controller["reading_ts"] is None:
        return ("no_report", "El Arduino aún no ha enviado lecturas.")
    if controller["mode"] is None:
        return ("firmware_without_control",
                "El Arduino todavía no acepta órdenes: falta grabarle el firmware con el "
                "protocolo de riego (ACT-02). El estado de los equipos sí se muestra.")
    return (None, "El Arduino acepta órdenes de la plataforma.")


async def controller_of_device(db: aiosqlite.Connection, device_id: str) -> Optional[dict]:
    """Lo mismo que controller_for(), para el gateway de un actuador concreto."""
    cursor = await db.execute(
        """
        SELECT d.device_id, d.status AS device_status, c.mode, c.last_cmd_id,
               c.last_cmd_error, c.reading_ts
        FROM devices d LEFT JOIN controller_status c ON c.device_id = d.device_id
        WHERE d.device_id = ?
        """,
        (device_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


# ── Órdenes ─────────────────────────────────────────────────────────────

async def queue_command(
    db: aiosqlite.Connection,
    *,
    greenhouse_id: str,
    device_id: str,
    kind: str,
    value: str,
    trigger: str,
    reason: str,
    now: datetime,
    user_id: Optional[int] = None,
    actuator_id: Optional[str] = None,
) -> int:
    """Guarda una orden `pending`. Una orden del mismo tipo que aún no salió
    queda reemplazada (`superseded`): solo cuenta la última. No hace commit."""
    await db.execute(
        """
        UPDATE actuator_commands SET status = 'superseded'
        WHERE device_id = ? AND kind = ? AND status = 'pending'
          AND (actuator_id IS ? OR kind = 'mode')
        """,
        (device_id, kind, actuator_id),
    )
    cursor = await db.execute(
        """
        INSERT INTO actuator_commands (greenhouse_id, device_id, actuator_id, kind, value,
                                       trigger, reason, user_id, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (greenhouse_id, device_id, actuator_id, kind, value, trigger, reason, user_id, _iso(now)),
    )
    return cursor.lastrowid


async def deliver(db: aiosqlite.Connection, device_id: str, now: datetime) -> List[dict]:
    """Órdenes pendientes de este gateway, en orden; pasan a `sent`. No hace commit."""
    await expire_stale(db, now)
    cursor = await db.execute(
        """
        SELECT id, kind, value, actuator_id FROM actuator_commands
        WHERE device_id = ? AND status = 'pending' ORDER BY id
        """,
        (device_id,),
    )
    commands = [dict(row) for row in await cursor.fetchall()]
    if commands:
        ids = [c["id"] for c in commands]
        await db.execute(
            f"UPDATE actuator_commands SET status = 'sent', sent_at = ? "
            f"WHERE id IN ({','.join('?' * len(ids))})",
            (_iso(now), *ids),
        )
    return commands


# ── Lo que llega con las lecturas ───────────────────────────────────────

def _on_reason(transition: Transition, origin: str) -> str:
    reading = transition.reading
    if transition.actuator["type"] == "fan":
        return ("Temperatura o humedad arriba del ideal" if transition.is_on
                else "Temperatura y humedad dentro del ideal")
    if origin == "manual":
        return "Orden manual"
    if reading.suelo_pct is not None:
        return f"Tierra al {reading.suelo_pct} %, abajo del objetivo de {TARGET_PCT:g} %"
    return "Tierra seca"


def _off_reason(transition: Transition) -> str:
    reading = transition.reading
    if transition.actuator["type"] == "fan":
        return _on_reason(transition, "auto")
    known = STOP_REASONS.get(reading.bomba_paro or "")
    if reading.bomba_paro == "OBJETIVO" and reading.suelo_pct is not None:
        return f"{known} (tierra al {reading.suelo_pct} %)"
    if known:
        return known
    # Firmware sin `bomba_paro`: se deduce de la lectura.
    if reading.suelo_pct is not None and reading.suelo_pct >= TARGET_PCT:
        return f"Objetivo alcanzado (tierra al {reading.suelo_pct} %)"
    if reading.bomba_habilitada is False:
        return "Tanque bajo"
    return "La apagó el Arduino"


async def _command_behind(
    db: aiosqlite.Connection, transition: Transition, device_id: str
) -> Optional[dict]:
    """La orden manual que explica un cambio de la bomba en modo MANUAL."""
    reading = transition.reading
    if transition.is_on or reading.bomba_paro == "ORDEN":
        kind, value = "pump", "ON" if transition.is_on else "OFF"
    elif reading.bomba_paro == "MODO":
        kind, value = "mode", None
    else:
        return None
    query = (
        "SELECT id, user_id, trigger, value FROM actuator_commands WHERE device_id = ? "
        "AND kind = ? AND status IN ('sent', 'confirmed') AND id <= ?"
    )
    params: list = [device_id, kind, reading.cmd_id or 0]
    if value is not None:
        query += " AND value = ?"
        params.append(value)
    cursor = await db.execute(query + " ORDER BY id DESC LIMIT 1", params)
    row = await cursor.fetchone()
    return dict(row) if row else None


async def _record_events(
    db: aiosqlite.Connection,
    device_id: str,
    greenhouse_id: str,
    transitions: Sequence[Transition],
    now_iso: str,
) -> None:
    for transition in transitions:
        reading = transition.reading
        pump = transition.actuator["type"] == "pump"
        pump_off = pump and not transition.is_on
        command = None
        if pump_off and reading.modo is not None and reading.bomba_paro is None:
            # Firmware con protocolo que nunca ha apagado la bomba desde que
            # arrancó: se reinició (el pin arranca apagado). No lo decidió nadie.
            origin, reason = "auto", "El Arduino se reinició (la bomba arranca apagada)"
        elif pump_off and reading.bomba_paro == "MODO":
            # La apagó un cambio de modo: de una persona, o del backend al
            # re-sincronizar el modo elegido después de un reinicio.
            command = await _command_behind(db, transition, device_id)
            if command is not None and command["trigger"] == "system":
                origin, reason = "auto", f"Se restableció el modo elegido ({command['value']})"
                command = None
            else:
                origin, reason = "manual", STOP_REASONS["MODO"]
        else:
            # El ventilador siempre lo decide el firmware. La bomba: el origen
            # es el modo en que estaba el Arduino (en AUTO no acepta órdenes de
            # bomba y en MANUAL no riega solo). Firmware sin `modo` = automático.
            origin = "manual" if pump and reading.modo == ModoEnum.MANUAL else "auto"
            if origin == "manual":
                command = await _command_behind(db, transition, device_id)
            reason = _on_reason(transition, origin) if transition.is_on else _off_reason(transition)
        await db.execute(
            """
            INSERT INTO actuator_events (actuator_id, greenhouse_id, device_id, event, origin,
                                         reason, user_id, command_id, reading_ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transition.actuator["actuator_id"], greenhouse_id, device_id,
                "on" if transition.is_on else "off", origin, reason,
                command["user_id"] if command else None,
                command["id"] if command else None,
                transition.reading_ts, now_iso,
            ),
        )


async def _confirm(db: aiosqlite.Connection, device_id: str, reading: Reading, now_iso: str) -> None:
    """`cmd_id` = última orden aplicada: esa y las anteriores en camino ya
    llegaron (el gateway las escribe en orden). Una orden que ya se había dado
    por perdida solo se corrige si es exactamente esa.

    Excepción: el cambio de modo se puede comprobar. Si la orden de modo más
    nueva que "ya llegó" no coincide con el `modo` que reporta el Arduino, se
    perdió (por ejemplo en un reinicio): queda `expired` y no `confirmed`, y
    process() manda otra enseguida.
    """
    if reading.cmd_id is None or reading.cmd_id <= 0:
        return
    if reading.modo is not None:
        cursor = await db.execute(
            """
            SELECT id, value FROM actuator_commands
            WHERE device_id = ? AND kind = 'mode' AND status = 'sent' AND id <= ?
            ORDER BY id DESC LIMIT 1
            """,
            (device_id, reading.cmd_id),
        )
        newest_mode = await cursor.fetchone()
        if newest_mode is not None and newest_mode["value"] != reading.modo.value:
            await db.execute(
                "UPDATE actuator_commands SET status = 'expired' WHERE id = ?", (newest_mode["id"],))
    await db.execute(
        """
        UPDATE actuator_commands
        SET status = CASE WHEN id = :cmd AND :err IS NOT NULL THEN 'rejected' ELSE 'confirmed' END,
            error = CASE WHEN id = :cmd THEN :err ELSE NULL END,
            confirmed_at = :now
        WHERE device_id = :device
          AND ((status = 'sent' AND id <= :cmd) OR (status = 'expired' AND id = :cmd))
        """,
        {"cmd": reading.cmd_id, "err": reading.cmd_err, "now": now_iso, "device": device_id},
    )


async def process(
    db: aiosqlite.Connection,
    device_id: str,
    greenhouse_id: str,
    readings: Sequence[Tuple[str, Reading]],
    transitions: Sequence[Transition],
    now: datetime,
) -> None:
    """Todo lo de ACT-02 que se deriva de un lote de lecturas NUEVAS
    (ordenadas por hora). No hace commit."""
    now_iso = _iso(now)
    latest_ts, latest = readings[-1]

    await db.execute(
        """
        INSERT INTO controller_status (device_id, mode, last_cmd_id, last_cmd_error,
                                       reading_ts, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            mode = excluded.mode, last_cmd_id = excluded.last_cmd_id,
            last_cmd_error = excluded.last_cmd_error, reading_ts = excluded.reading_ts,
            updated_at = excluded.updated_at
        WHERE excluded.reading_ts > controller_status.reading_ts
        """,
        (device_id, latest.modo.value if latest.modo else None, latest.cmd_id,
         latest.cmd_err, latest_ts, now_iso),
    )
    await _confirm(db, device_id, latest, now_iso)
    await _record_events(db, device_id, greenhouse_id, transitions, now_iso)

    # Re-sincronizar el modo: el Arduino arranca en AUTO; si la persona
    # eligió MANUAL (o una orden se perdió), se le vuelve a mandar.
    if latest.modo is None:
        return
    controller = await controller_for(db, greenhouse_id)
    if controller is None or controller["device_id"] != device_id:
        return
    wanted = (await chosen_mode(db, greenhouse_id))["mode"]
    if latest.modo.value.lower() == wanted:
        return
    await expire_stale(db, now)
    cursor = await db.execute(
        "SELECT 1 FROM actuator_commands WHERE device_id = ? AND kind = 'mode' "
        "AND status IN ('pending', 'sent')",
        (device_id,),
    )
    if await cursor.fetchone() is None:
        await queue_command(
            db, greenhouse_id=greenhouse_id, device_id=device_id, kind="mode",
            value=wanted.upper(), trigger="system", now=now,
            reason=f"El Arduino reporta {latest.modo.value}; se le regresa al modo elegido",
        )


def utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def firmware_params() -> Dict[str, float]:
    return {"target_pct": TARGET_PCT, "max_run_seconds": MAX_RUN_SECONDS, "rest_seconds": REST_SECONDS}


def command_out(command: Optional[dict]) -> Optional[CommandOut]:
    if command is None:
        return None
    return CommandOut(
        **{key: command[key] for key in (
            "id", "actuator_id", "kind", "value", "trigger", "reason", "status", "error",
        )},
        user_name=command.get("user_name"),
        created_at=utc(command["created_at"]),
        sent_at=utc(command.get("sent_at")),
        confirmed_at=utc(command.get("confirmed_at")),
    )
