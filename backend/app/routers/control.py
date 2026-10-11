"""
Estado y control de los equipos de un vivero.

ACT-04 · GET /greenhouses/{id}/actuators/status
    Cada actuador ACTIVO del vivero con su estado: on, off, unavailable o
    unknown (ver app/actuator_state.py). El dashboard lo pide cada 15 s.

Permisos (AUTH-02): todos los roles; un productor solo su vivero.
"""

from datetime import timedelta

import aiosqlite
from fastapi import APIRouter, Depends, Path

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
from app.models import ActuatorLive, ActuatorLiveList, ErrorResponse

router = APIRouter(
    prefix="/greenhouses",
    tags=["control"],
    dependencies=[Depends(get_current_user)],
)

_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403)}


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
        ))

    return ActuatorLiveList(
        greenhouse_id=greenhouse_id,
        generated_at=now,
        stale_after_minutes=settings.reading_stale_minutes,
        actuators=items,
    )
