"""
Consulta de la bitácora de auditoría (AUTH-05).

    GET /audit/events

Filtra por fecha y por tipo de evento (criterio 2), y además por acción,
actor y vivero. Solo hay GET: lo que ya se escribió no se modifica ni se
borra desde la aplicación (criterio 3; la BD también lo impide con triggers).

Permisos (AUTH-02): Administrador y Super Administrador. La bitácora tiene
actividad de todos los usuarios y de todos los viveros, así que un productor
no la ve.

La escritura está en app/audit.py.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from app import audit
from app.database import get_db
from app.dependencies import ADMIN_ROLES, get_current_user, require_roles, utcnow
from app.models import (
    AuditActorType,
    AuditEvent,
    AuditEventList,
    AuditEventType,
    ErrorResponse,
)

router = APIRouter(
    prefix="/audit",
    tags=["audit"],
    dependencies=[Depends(get_current_user)],
)

DEFAULT_RANGE_HOURS = 24


def _utc(value: datetime, name: str) -> datetime:
    """Un límite sin zona horaria es ambiguo: se rechaza, igual que en el histórico."""
    if value.tzinfo is None:
        raise HTTPException(422, detail={
            "error": "invalid_range",
            "message": f"`{name}` necesita zona horaria: agrega Z (UTC) o un desfase como -06:00.",
        })
    return value.astimezone(timezone.utc)


@router.get(
    "/events",
    response_model=AuditEventList,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 422)},
    summary="Bitácora de auditoría por fecha y tipo de evento",
    description=(
        "Inicios de sesión, alertas, acciones sobre los equipos y "
        "recomendaciones de la IA, cada uno con quién lo hizo, qué pasó y "
        "cuándo. Del más nuevo al más viejo. Por defecto, las últimas 24 h. "
        "Solo Administradores. La bitácora es de solo lectura: no existe "
        "ningún endpoint para modificar o borrar eventos."
    ),
)
async def list_events(
    from_: Optional[datetime] = Query(
        None, alias="from", description="Inicio del rango (ISO 8601 con zona). Por defecto, 24 h atrás"
    ),
    to: Optional[datetime] = Query(
        None, description="Fin del rango (ISO 8601 con zona). Por defecto, ahora"
    ),
    event_type: Optional[List[AuditEventType]] = Query(
        None, description="Tipo de evento. Se puede repetir para pedir varios"
    ),
    action: Optional[List[str]] = Query(
        None, description="Acción exacta (login, alert_raised…). Se puede repetir"
    ),
    actor_type: Optional[AuditActorType] = Query(None, description="Quién: persona, Arduino o plataforma"),
    actor_id: Optional[str] = Query(None, max_length=64, description="Id del usuario o del dispositivo"),
    greenhouse_id: Optional[str] = Query(None, min_length=1, max_length=64),
    limit: int = Query(50, ge=1, le=200, description="Eventos por página"),
    offset: int = Query(0, ge=0, description="Eventos a saltar (paginación)"),
    user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> AuditEventList:
    until = _utc(to, "to") if to is not None else utcnow()
    since = _utc(from_, "from") if from_ is not None else until - timedelta(hours=DEFAULT_RANGE_HOURS)
    if since > until:
        raise HTTPException(422, detail={
            "error": "invalid_range", "message": "`from` debe ser anterior a `to`.",
        })
    unknown = sorted(set(action or []) - {a for actions in audit.ACTIONS.values() for a in actions})
    if unknown:
        raise HTTPException(422, detail={
            "error": "unknown_action",
            "message": f"Acción desconocida: {', '.join(unknown)}.",
        })

    total, rows = await audit.query(
        db, since=since, until=until,
        event_types=[item.value for item in event_type] if event_type else None,
        actions=action, actor_type=actor_type.value if actor_type else None,
        actor_id=actor_id, greenhouse_id=greenhouse_id, limit=limit, offset=offset,
    )
    return AuditEventList(
        period_from=since, period_to=until, total=total, limit=limit, offset=offset,
        items=[AuditEvent(**row) for row in rows],
    )
