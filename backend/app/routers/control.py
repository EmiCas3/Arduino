"""
Estado y control de los equipos de un vivero.

ACT-04 · GET /greenhouses/{id}/actuators/status
    Cada actuador ACTIVO del vivero con su estado: on, off, unavailable o
    unknown (ver app/actuator_state.py). El dashboard lo pide cada 15 s.

ACT-02 · GET /greenhouses/{id}/irrigation
         PUT /greenhouses/{id}/irrigation/mode
         GET /greenhouses/{id}/actuators/events
    Modo de riego (automático o manual), si el Arduino acepta órdenes, y
    los últimos encendidos/apagados con su origen. Las órdenes manuales a la
    bomba están en POST /actuators/{id}/commands (routers/actuators.py).
    Ver app/irrigation.py.

Permisos (AUTH-02): todos los roles; un productor solo su vivero. Cambiar
el modo también lo puede hacer el productor (decisión D6 del plan).
"""

from datetime import timedelta

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app import irrigation
from app.actuator_state import DETAILS, power_state
from app.config import settings
from app.database import get_db
from app.dependencies import (
    ALL_ROLES,
    ensure_greenhouse_access,
    get_current_user,
    parse_utc,
    require_roles,
    utcnow,
)
from app.models import (
    ActuatorEventList,
    ActuatorEventOut,
    ActuatorLive,
    ActuatorLiveList,
    ErrorResponse,
    FirmwareIrrigation,
    IrrigationMode,
    IrrigationStatus,
    ModeChange,
)

router = APIRouter(
    prefix="/greenhouses",
    tags=["control"],
    dependencies=[Depends(get_current_user)],
)

_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403)}
_MODE_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 409, 422)}


def _error(status: int, error: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": error, "message": message})


@router.get(
    "/{greenhouse_id}/actuators/status",
    response_model=ActuatorLiveList,
    responses=_ERRORS,
    summary="Estado actual de cada actuador",
    description=(
        "Actuadores activos del vivero con su estado: `on`, `off`, "
        "`unavailable` (bomba bloqueada por tanque bajo o gateway desactivado) "
        "o `unknown` (sin confirmación del Arduino o confirmación vieja). "
        "Nunca se reporta `off` sin confirmación."
    ),
)
async def actuator_status(
    greenhouse_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> ActuatorLiveList:
    ensure_greenhouse_access(user, greenhouse_id)
    now = utcnow()
    stale_after = timedelta(minutes=settings.reading_stale_minutes)

    await irrigation.expire_stale(db, now)
    await db.commit()
    cursor = await db.execute(
        """
        SELECT a.*, d.status AS gateway_status,
               s.is_on, s.enabled, s.reading_ts
        FROM actuators a
        LEFT JOIN devices d ON d.device_id = a.gateway_device_id
        LEFT JOIN actuator_states s ON s.actuator_id = a.actuator_id
        WHERE a.greenhouse_id = ? AND a.status = 'active'
        ORDER BY a.area, a.name
        """,
        (greenhouse_id,),
    )
    items = []
    for row in await cursor.fetchall():
        actuator = dict(row)
        state_row = None if row["reading_ts"] is None else {
            "is_on": row["is_on"], "enabled": row["enabled"], "reading_ts": row["reading_ts"],
        }
        state, reason, reported_at, age = power_state(
            actuator, state_row, row["gateway_status"], now, stale_after, parse_utc,
        )
        last_command = None
        if row["type"] == "pump":
            last_command = await irrigation.recent_command(
                db, actuator_id=row["actuator_id"], kind="pump", now=now)
        items.append(ActuatorLive(
            actuator_id=row["actuator_id"],
            name=row["name"],
            type=row["type"],
            area=row["area"],
            control_channel=row["control_channel"],
            gateway_device_id=row["gateway_device_id"],
            state=state,
            reason=reason,
            detail=DETAILS[reason],
            reported_at=reported_at,
            age_seconds=age,
            last_command=irrigation.command_out(last_command),
        ))

    return ActuatorLiveList(
        greenhouse_id=greenhouse_id,
        generated_at=now,
        stale_after_minutes=settings.reading_stale_minutes,
        actuators=items,
    )


# ── Riego (ACT-02) ──────────────────────────────────────────────────────

async def _irrigation_status(db: aiosqlite.Connection, greenhouse_id: str) -> IrrigationStatus:
    now = utcnow()
    await irrigation.expire_stale(db, now)
    await db.commit()
    controller = await irrigation.controller_for(db, greenhouse_id)
    problem, detail = irrigation.control_problem(controller)
    chosen = await irrigation.chosen_mode(db, greenhouse_id)
    last_mode = None
    if controller is not None:
        last_mode = await irrigation.recent_command(
            db, device_id=controller["device_id"], kind="mode", now=now)
    reported = controller["mode"] if controller else None
    return IrrigationStatus(
        greenhouse_id=greenhouse_id,
        mode=chosen["mode"],
        reported_mode=reported.lower() if reported else None,
        controllable=problem is None,
        detail=detail,
        controller_device_id=controller["device_id"] if controller else None,
        reported_at=irrigation.utc(controller["reading_ts"]) if controller else None,
        mode_changed_at=irrigation.utc(chosen["changed_at"]),
        mode_changed_by=chosen["changed_by"],
        last_mode_command=irrigation.command_out(last_mode),
        firmware=FirmwareIrrigation(**irrigation.firmware_params()),
    )


@router.get(
    "/{greenhouse_id}/irrigation",
    response_model=IrrigationStatus,
    responses=_ERRORS,
    summary="Modo de riego y si el Arduino acepta órdenes",
    description=(
        "`mode` es el que eligió una persona (auto por defecto, igual que el "
        "Arduino al arrancar); `reported_mode` el que reporta el Arduino "
        "(null si su firmware no acepta órdenes). `firmware` trae el objetivo, "
        "el tiempo máximo y el descanso que usa el Arduino."
    ),
)
async def irrigation_status(
    greenhouse_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> IrrigationStatus:
    ensure_greenhouse_access(user, greenhouse_id)
    return await _irrigation_status(db, greenhouse_id)


@router.put(
    "/{greenhouse_id}/irrigation/mode",
    response_model=IrrigationStatus,
    responses=_MODE_ERRORS,
    summary="Cambiar a riego automático o manual",
    description=(
        "Guarda el modo elegido (con quién y cuándo) y le manda la orden al "
        "Arduino por el gateway. En manual el Arduino no riega solo y, si "
        "estaba regando, apaga la bomba. 409 si al Arduino no se le pueden "
        "mandar órdenes. Todos los roles; un productor solo su vivero."
    ),
)
async def change_mode(
    body: ModeChange,
    greenhouse_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> IrrigationStatus:
    ensure_greenhouse_access(user, greenhouse_id)
    controller = await irrigation.controller_for(db, greenhouse_id)
    problem, message = irrigation.control_problem(controller)
    if problem is not None:
        raise _error(409, problem, message)

    now = utcnow()
    await db.execute(
        """
        INSERT INTO irrigation_mode (greenhouse_id, mode, changed_at, changed_by)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(greenhouse_id) DO UPDATE SET
            mode = excluded.mode, changed_at = excluded.changed_at, changed_by = excluded.changed_by
        """,
        (greenhouse_id, body.mode.value, now.isoformat(), user["id"]),
    )
    label = "automático" if body.mode == IrrigationMode.auto else "manual"
    await irrigation.queue_command(
        db, greenhouse_id=greenhouse_id, device_id=controller["device_id"], kind="mode",
        value=body.mode.value.upper(), trigger="manual", user_id=user["id"], now=now,
        reason=f"Cambio a riego {label}",
    )
    await db.commit()
    return await _irrigation_status(db, greenhouse_id)


@router.get(
    "/{greenhouse_id}/actuators/events",
    response_model=ActuatorEventList,
    responses=_ERRORS,
    summary="Últimos encendidos y apagados",
    description=(
        "Cada encendido o apagado que reportó el Arduino, con su origen: "
        "`auto` (lo decidió el firmware) o `manual` (una persona, con su "
        "nombre), y el motivo. Los más recientes primero."
    ),
)
async def actuator_events(
    greenhouse_id: str = Path(..., min_length=1, max_length=64),
    limit: int = Query(20, ge=1, le=200),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> ActuatorEventList:
    ensure_greenhouse_access(user, greenhouse_id)
    cursor = await db.execute(
        """
        SELECT e.*, a.name AS actuator_name, a.type AS actuator_type,
               u.full_name, u.username
        FROM actuator_events e
        JOIN actuators a ON a.actuator_id = e.actuator_id
        LEFT JOIN users u ON u.id = e.user_id
        WHERE e.greenhouse_id = ?
        ORDER BY e.reading_ts DESC, e.id DESC
        LIMIT ?
        """,
        (greenhouse_id, limit),
    )
    events = [
        ActuatorEventOut(
            id=row["id"],
            actuator_id=row["actuator_id"],
            actuator_name=row["actuator_name"],
            actuator_type=row["actuator_type"],
            event=row["event"],
            origin=row["origin"],
            reason=row["reason"],
            user_name=row["full_name"] or row["username"],
            reading_ts=parse_utc(row["reading_ts"]),
        )
        for row in await cursor.fetchall()
    ]
    return ActuatorEventList(greenhouse_id=greenhouse_id, events=events)
