"""
Últimas lecturas de un vivero (DASH-01, task "Build latest readings API").

Devuelve el último valor conocido de cada medición con su unidad, cuándo se
tomó, cuánto hace de eso y si ya está vieja (más antigua que
SMARTGREENAI_READING_STALE_MINUTES). Es lo que consumirán los widgets del
dashboard, que son la otra task de DASH-01.

Convención del spec: null = sensor caído. Por eso cada medición busca su
último valor NO nulo, y `sensor_down` avisa cuando la lectura más reciente del
vivero llegó con ese sensor en null.

Permisos (AUTH-02): todos los roles; un productor solo consulta su vivero.
"""

import json
from datetime import timedelta
from typing import Optional

import aiosqlite
from fastapi import APIRouter, Depends, Path

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
from app.models import ErrorResponse, LatestMetric, LatestReadings, LatestReadingSummary

router = APIRouter(
    prefix="/greenhouses",
    tags=["readings"],
    dependencies=[Depends(get_current_user)],
)

# (columna, etiqueta, unidad, columna raw). Los nombres de columna de las
# consultas salen SOLO de esta tupla fija, nunca de texto del cliente.
METRICS = (
    ("temp_c", "Temperatura del aire", "°C", None),
    ("hum_aire_pct", "Humedad del aire", "%", None),
    ("suelo_pct", "Humedad de la tierra", "%", "suelo_raw"),
    ("nivel_pct", "Nivel del tanque", "%", "nivel_raw"),
    ("luz_nivel", "Luz", "nivel 0-1023", "luz_raw"),
)


def _as_bool(value) -> Optional[bool]:
    return None if value is None else bool(value)


@router.get(
    "/{greenhouse_id}/readings/latest",
    response_model=LatestReadings,
    responses={code: {"model": ErrorResponse} for code in (401, 403)},
    summary="Últimas lecturas de un vivero",
    description=(
        "Último valor por medición con unidad, antigüedad y bandera `stale`. "
        "Si el vivero aún no tiene lecturas responde `has_data=false`."
    ),
)
async def latest_readings(
    greenhouse_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> LatestReadings:
    ensure_greenhouse_access(user, greenhouse_id)

    now = utcnow()
    stale_after = timedelta(minutes=settings.reading_stale_minutes)

    cursor = await db.execute(
        "SELECT * FROM readings WHERE greenhouse_id = ? ORDER BY ts DESC LIMIT 1",
        (greenhouse_id,),
    )
    last = await cursor.fetchone()

    summary = None
    if last is not None:
        last_ts = parse_utc(last["ts"])
        summary = LatestReadingSummary(
            ts=last_ts,
            received_at=parse_utc(last["received_at"]),
            age_seconds=max(0, int((now - last_ts).total_seconds())),
            stale=(now - last_ts) > stale_after,
            device_id=last["device_id"],
            estado=last["estado"],
            alertas=json.loads(last["alertas"]) if last["alertas"] else [],
            riego_sugerido=_as_bool(last["riego_sugerido"]),
            bomba_habilitada=_as_bool(last["bomba_habilitada"]),
            vent=last["vent"],
            es_dia=_as_bool(last["es_dia"]),
        )

    metrics = []
    for column, label, unit, raw_column in METRICS:
        raw_select = f"r.{raw_column}" if raw_column else "NULL"
        cursor = await db.execute(
            f"""
            SELECT r.ts, r.device_id, r.{column} AS value, {raw_select} AS raw,
                   d.location AS device_location
            FROM readings r
            LEFT JOIN devices d ON d.device_id = r.device_id
            WHERE r.greenhouse_id = ? AND r.{column} IS NOT NULL
            ORDER BY r.ts DESC
            LIMIT 1
            """,
            (greenhouse_id,),
        )
        row = await cursor.fetchone()
        sensor_down = last is not None and last[column] is None

        if row is None:
            metrics.append(LatestMetric(
                key=column, label=label, unit=unit,
                stale=True, sensor_down=sensor_down,
            ))
            continue

        ts = parse_utc(row["ts"])
        metrics.append(LatestMetric(
            key=column,
            label=label,
            unit=unit,
            value=row["value"],
            raw=row["raw"],
            ts=ts,
            age_seconds=max(0, int((now - ts).total_seconds())),
            stale=(now - ts) > stale_after,
            sensor_down=sensor_down,
            device_id=row["device_id"],
            device_location=row["device_location"],
        ))

    return LatestReadings(
        greenhouse_id=greenhouse_id,
        generated_at=now,
        stale_after_minutes=settings.reading_stale_minutes,
        has_data=last is not None,
        last_reading=summary,
        metrics=metrics,
    )
