"""
Estado de los actuadores (ACT-04).

El Arduino manda en cada lectura qué hizo con cada equipo: `bomba_activa`
para la bomba y `vent_activo` para el ventilador. La ingesta pasa aquí las
lecturas NUEVAS en orden de hora y se guarda el último estado de cada
actuador activo de ese gateway en `actuator_states`.

Cuando el dashboard pregunta, el estado se calcula con estas reglas:

    on / off       el Arduino lo confirmó hace menos de
                   SMARTGREENAI_READING_STALE_MINUTES
    unavailable    la bomba está bloqueada por el firmware (tanque crítico,
                   `bomba_habilitada: false`) o su gateway está desactivado
    unknown        nunca lo reportó, la confirmación es vieja, no tiene
                   gateway, o es un tipo que el firmware no reporta

Un equipo sin confirmación NUNCA se muestra como apagado.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import aiosqlite

from app.models import PowerReason, PowerState, Reading

# Tipo de actuador → campo de la lectura que dice si quedó encendido.
REPORTED_FIELDS: Dict[str, str] = {
    "pump": "bomba_activa",
    "fan": "vent_activo",
}

DETAILS = {
    PowerReason.reported: "Confirmado por el Arduino.",
    PowerReason.tank_low: "Tanque bajo: el Arduino bloquea la bomba hasta que haya agua.",
    PowerReason.gateway_inactive: "Su gateway está desactivado: no puede recibir ni reportar nada.",
    PowerReason.no_gateway: "No tiene un gateway asignado: nadie reporta su estado.",
    PowerReason.not_reported: "El Arduino no reporta el estado de este tipo de equipo.",
    PowerReason.never_reported: "El Arduino aún no ha confirmado su estado.",
    PowerReason.stale: "Sin confirmación reciente del Arduino.",
}


@dataclass
class Transition:
    """Un actuador que cambió de apagado a encendido o al revés."""

    actuator: dict
    is_on: bool
    reading: Reading
    reading_ts: str


async def active_actuators_of(db: aiosqlite.Connection, device_id: str) -> List[dict]:
    """Actuadores ACTIVOS que reporta este gateway y cuyo tipo tiene campo."""
    cursor = await db.execute(
        "SELECT * FROM actuators WHERE gateway_device_id = ? AND status = 'active'",
        (device_id,),
    )
    return [dict(row) for row in await cursor.fetchall() if row["type"] in REPORTED_FIELDS]


async def apply_readings(
    db: aiosqlite.Connection,
    device_id: str,
    readings: Sequence[Tuple[str, Reading]],
    now_iso: str,
) -> List[Transition]:
    """Guarda el estado reportado por cada lectura nueva.

    `readings` son pares (ts UTC con ms, lectura) ya ordenados por hora. Las
    lecturas más viejas que el último estado guardado se ignoran (pueden
    llegar fuera de orden tras una caída). Devuelve los cambios on ↔ off; el
    primer reporte de un equipo no cuenta como cambio. No hace commit.
    """
    actuators = await active_actuators_of(db, device_id)
    if not actuators:
        return []

    ids = [a["actuator_id"] for a in actuators]
    cursor = await db.execute(
        f"SELECT * FROM actuator_states WHERE actuator_id IN ({','.join('?' * len(ids))})",
        ids,
    )
    current: Dict[str, dict] = {row["actuator_id"]: dict(row) for row in await cursor.fetchall()}

    transitions: List[Transition] = []
    for ts, reading in readings:
        for actuator in actuators:
            value = getattr(reading, REPORTED_FIELDS[actuator["type"]])
            if value is None:
                continue
            previous = current.get(actuator["actuator_id"])
            if previous is not None and ts <= previous["reading_ts"]:
                continue
            enabled = reading.bomba_habilitada if actuator["type"] == "pump" else None
            row = {
                "actuator_id": actuator["actuator_id"],
                "is_on": int(value),
                "enabled": None if enabled is None else int(enabled),
                "reading_ts": ts,
                "device_id": device_id,
                "updated_at": now_iso,
            }
            if previous is not None and previous["is_on"] != row["is_on"]:
                transitions.append(Transition(actuator, bool(value), reading, ts))
            current[actuator["actuator_id"]] = row
            await db.execute(
                """
                INSERT INTO actuator_states
                    (actuator_id, is_on, enabled, reading_ts, device_id, updated_at)
                VALUES (:actuator_id, :is_on, :enabled, :reading_ts, :device_id, :updated_at)
                ON CONFLICT(actuator_id) DO UPDATE SET
                    is_on = excluded.is_on, enabled = excluded.enabled,
                    reading_ts = excluded.reading_ts, device_id = excluded.device_id,
                    updated_at = excluded.updated_at
                """,
                row,
            )
    return transitions


def power_state(
    actuator: dict,
    state: Optional[dict],
    gateway_status: Optional[str],
    now: datetime,
    stale_after: timedelta,
    parse_ts,
) -> Tuple[PowerState, PowerReason, Optional[datetime], Optional[int]]:
    """Estado a mostrar: (estado, motivo, hora del reporte, edad en segundos)."""
    if not actuator.get("gateway_device_id"):
        return PowerState.unknown, PowerReason.no_gateway, None, None
    if gateway_status is not None and gateway_status != "active":
        return PowerState.unavailable, PowerReason.gateway_inactive, None, None
    if actuator["type"] not in REPORTED_FIELDS:
        return PowerState.unknown, PowerReason.not_reported, None, None
    if state is None:
        return PowerState.unknown, PowerReason.never_reported, None, None

    reported_at = parse_ts(state["reading_ts"])
    age = max(0, int((now - reported_at).total_seconds()))
    if now - reported_at > stale_after:
        return PowerState.unknown, PowerReason.stale, reported_at, age
    if state["is_on"]:
        return PowerState.on, PowerReason.reported, reported_at, age
    if actuator["type"] == "pump" and state["enabled"] == 0:
        return PowerState.unavailable, PowerReason.tank_low, reported_at, age
    return PowerState.off, PowerReason.reported, reported_at, age
