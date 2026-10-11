"""
Alertas de un vivero (ALERT-01).

GET /greenhouses/{id}/alerts?status=active|cleared|all&limit=50

Las crea el motor (app/alerts.py) con cada lote de lecturas; aquí solo se
consultan. Primero las activas y luego las más recientes. El dashboard las
pide cada 15 s para mostrarlas sin recargar.

Permisos (AUTH-02): todos los roles; un productor solo su vivero.
"""

from enum import Enum

import aiosqlite
from fastapi import APIRouter, Depends, Path, Query

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
from app.firmware import LIMIT_ALERTS
from app.models import AlertList, AlertOut, ErrorResponse
from app.routers.readings import METRICS

router = APIRouter(
    prefix="/greenhouses",
    tags=["alerts"],
    dependencies=[Depends(get_current_user)],
)

_LABELS = {column: (label, unit) for column, label, unit, _ in METRICS}


class AlertFilter(str, Enum):
    active = "active"
    cleared = "cleared"
    all = "all"


@router.get(
    "/{greenhouse_id}/alerts",
    response_model=AlertList,
    responses={code: {"model": ErrorResponse} for code in (401, 403)},
    summary="Alertas del vivero",
    description=(
        "Alertas creadas cuando el Arduino reporta que una medición cruzó su "
        "límite. Cada una guarda sensor, valor, umbral y hora. Primero las "
        "activas, luego las más recientes."
    ),
)
async def list_alerts(
    greenhouse_id: str = Path(..., min_length=1, max_length=64),
    status: AlertFilter = Query(AlertFilter.all),
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> AlertList:
    ensure_greenhouse_access(user, greenhouse_id)
    now = utcnow()

    where, params = "greenhouse_id = ?", [greenhouse_id]
    if status != AlertFilter.all:
        where += " AND status = ?"
        params.append(status.value)
    cursor = await db.execute(
        f"""
        SELECT * FROM alerts WHERE {where}
        ORDER BY (status = 'active') DESC, reading_ts DESC, id DESC
        LIMIT ?
        """,
        (*params, limit),
    )
    rows = await cursor.fetchall()

    cursor = await db.execute(
        "SELECT COUNT(*) AS n FROM alerts WHERE greenhouse_id = ? AND status = 'active'",
        (greenhouse_id,),
    )
    active_count = (await cursor.fetchone())["n"]

    alerts = []
    for row in rows:
        started = parse_utc(row["reading_ts"])
        ended = parse_utc(row["cleared_ts"]) if row["cleared_ts"] else now
        label, unit = _LABELS.get(row["metric"], (row["metric"], ""))
        limit_alert = LIMIT_ALERTS.get(row["code"])
        alerts.append(AlertOut(
            **{key: row[key] for key in (
                "id", "greenhouse_id", "area", "metric", "device_id", "code", "direction",
                "threshold", "threshold_name", "value", "last_value", "worst_value",
                "status", "cleared_value",
            )},
            label=label,
            unit=unit,
            comparison=limit_alert.comparison if limit_alert else "",
            reading_ts=started,
            created_at=parse_utc(row["created_at"]),
            last_ts=parse_utc(row["last_ts"]),
            cleared_at=parse_utc(row["cleared_at"]) if row["cleared_at"] else None,
            cleared_ts=parse_utc(row["cleared_ts"]) if row["cleared_ts"] else None,
            duration_seconds=max(0, int((ended - started).total_seconds())),
        ))

    return AlertList(
        greenhouse_id=greenhouse_id,
        generated_at=now,
        active_count=active_count,
        clear_after_seconds=settings.alert_clear_seconds,
        alerts=alerts,
    )
