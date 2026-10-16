"""
Lecturas de un vivero: las últimas (DASH-01) y el histórico (DASH-02).

Devuelve el último valor conocido de cada medición con su unidad, cuándo se
tomó, cuánto hace de eso y si ya está vieja (más antigua que
SMARTGREENAI_READING_STALE_MINUTES). Es lo que consumirán los widgets del
dashboard, que son la otra task de DASH-01.

Convención del spec: null = sensor caído. Por eso cada medición busca su
último valor NO nulo, y `sensor_down` avisa cuando la lectura más reciente del
vivero llegó con ese sensor en null.

También lista los viveros que el usuario puede consultar (GET /greenhouses),
para que un Administrador, que no tiene vivero asignado, elija cuál ver.

DASH-02 agrega el histórico, con dos formas de leer lo mismo:

- `GET /{id}/readings` — las lecturas guardadas de un rango, tal cual. Es el
  contrato que se publicó en el spec desde SYS-01: sirve para revisar o
  exportar los datos crudos.
- `GET /{id}/readings/series` — UNA medición lista para dibujar. Si en el
  rango hay más lecturas que `max_points`, se devuelven promediadas por
  bucket, con el mínimo y el máximo de cada bucket, para que la gráfica siga
  siendo legible (criterio 3 de DASH-02). Un rango válido sin lecturas
  responde `has_data=false` con la lista vacía, y la página muestra el aviso
  en vez de una gráfica en blanco (criterio 2).

Permisos (AUTH-02): todos los roles; un productor solo consulta su vivero.
"""

import json
from datetime import datetime, timedelta
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app import history
from app import metrics as metric_catalog
from app.config import settings
from app.database import get_db
from app.dependencies import (
    ALL_ROLES,
    ensure_greenhouse_access,
    get_current_user,
    parse_utc,
    require_roles,
    utcnow,
    visible_greenhouse,
)
from app.models import (
    ErrorResponse,
    GreenhouseSummary,
    LatestMetric,
    LatestReadings,
    LatestReadingSummary,
    MetricKey,
    ReadingSeries,
    SeriesPoint,
    StoredReading,
)

router = APIRouter(
    prefix="/greenhouses",
    tags=["readings"],
    dependencies=[Depends(get_current_user)],
)

# (columna, etiqueta, unidad, columna raw) desde el catálogo de app/metrics.py,
# que es la única fuente de estos nombres. Los nombres de columna que entran a
# una consulta SQL salen SOLO de ahí, nunca de texto del cliente.
METRICS = tuple(
    (metric.key, metric.label, metric.unit, metric.raw_column)
    for metric in metric_catalog.METRICS
)

# Campos de `readings` que el contrato devuelve como booleanos.
_BOOL_COLUMNS = ("es_dia", "riego_sugerido", "bomba_habilitada")


def _as_bool(value) -> Optional[bool]:
    return None if value is None else bool(value)


def _invalid_range(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": "invalid_range", "message": str(error)},
    )


@router.get(
    "",
    response_model=List[GreenhouseSummary],
    responses={401: {"model": ErrorResponse}},
    summary="Viveros que el usuario puede consultar",
    description=(
        "Un productor recibe solo su vivero asignado; admin y super admin, "
        "todos los que tienen al menos un dispositivo registrado."
    ),
)
async def list_greenhouses(
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> List[GreenhouseSummary]:
    only = visible_greenhouse(user)   # None = todos (admins)
    where, params = ("", ()) if only is None else ("WHERE greenhouse_id = ?", (only,))
    cursor = await db.execute(
        f"""
        SELECT greenhouse_id, COUNT(*) AS devices, MAX(last_seen_at) AS last_seen_at
        FROM devices
        {where}
        GROUP BY greenhouse_id
        ORDER BY greenhouse_id
        """,
        params,
    )
    return [
        GreenhouseSummary(
            greenhouse_id=row["greenhouse_id"],
            devices=row["devices"],
            last_seen_at=parse_utc(row["last_seen_at"]) if row["last_seen_at"] else None,
        )
        for row in await cursor.fetchall()
    ]


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
                area=settings.default_area,
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
            area=settings.default_area,
        ))

    return LatestReadings(
        greenhouse_id=greenhouse_id,
        generated_at=now,
        stale_after_minutes=settings.reading_stale_minutes,
        has_data=last is not None,
        last_reading=summary,
        metrics=metrics,
    )


# ── Histórico (DASH-02) ────────────────────────────────────────────────

@router.get(
    "/{greenhouse_id}/readings",
    response_model=List[StoredReading],
    responses={code: {"model": ErrorResponse} for code in (401, 403, 422)},
    summary="Consultar mediciones de un vivero",
    description=(
        "Lecturas guardadas del rango, de la más reciente a la más antigua. "
        "Sin `from` ni `to` se devuelven las últimas 24 horas. Es el dato "
        "crudo: para dibujar una gráfica usa `/readings/series`, que lo "
        "agrega cuando hay demasiados puntos."
    ),
)
async def list_readings(
    greenhouse_id: str = Path(..., min_length=1, max_length=64),
    from_: Optional[datetime] = Query(
        None, alias="from", description="Inicio del rango (ISO 8601 con zona). Por defecto, 24 h atrás"
    ),
    to: Optional[datetime] = Query(
        None, description="Fin del rango (ISO 8601 con zona). Por defecto, ahora"
    ),
    limit: int = Query(200, ge=1, le=1000, description="Lecturas como máximo"),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> List[StoredReading]:
    ensure_greenhouse_access(user, greenhouse_id)
    try:
        window = history.parse_window(from_, to, utcnow())
    except history.RangeError as error:
        raise _invalid_range(error)

    cursor = await db.execute(
        """
        SELECT * FROM readings
        WHERE greenhouse_id = ? AND ts >= ? AND ts < ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (greenhouse_id, history.ts_text(window.start), history.ts_text(window.end), limit),
    )
    return [_stored_reading(row) for row in await cursor.fetchall()]


def _stored_reading(row: aiosqlite.Row) -> StoredReading:
    """Una fila de `readings` como la devuelve el contrato.

    Los campos que el firmware manda pero que no viven en esta tabla
    (`bomba_activa`, `modo`, `cmd_id`…) quedan en null: se guardan en
    actuator_states y controller_status, cada uno con su endpoint.
    """
    data = {key: row[key] for key in row.keys()}
    data["alertas"] = json.loads(data["alertas"]) if data.get("alertas") else None
    for column in _BOOL_COLUMNS:
        data[column] = _as_bool(data.get(column))
    return StoredReading(**data)


@router.get(
    "/{greenhouse_id}/readings/series",
    response_model=ReadingSeries,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 422)},
    summary="Serie histórica de una medición",
    description=(
        "Lo que dibuja la gráfica de la página de historial. Devuelve una "
        "medición en el rango pedido: las lecturas tal cual si caben en "
        "`max_points`, o promedios por bucket (con el mínimo y el máximo de "
        "cada uno) si no caben. `bucket_seconds` dice de cuánto quedó cada "
        "paso y `aggregated` si hubo que promediar. Un rango sin lecturas "
        "responde 200 con `has_data=false`."
    ),
)
async def readings_series(
    greenhouse_id: str = Path(..., min_length=1, max_length=64),
    metric: MetricKey = Query(MetricKey.temp_c, description="Medición a graficar"),
    from_: Optional[datetime] = Query(
        None, alias="from", description="Inicio del rango (ISO 8601 con zona). Por defecto, 24 h atrás"
    ),
    to: Optional[datetime] = Query(
        None, description="Fin del rango (ISO 8601 con zona). Por defecto, ahora"
    ),
    max_points: int = Query(
        history.DEFAULT_MAX_POINTS,
        ge=history.MIN_MAX_POINTS,
        le=history.MAX_MAX_POINTS,
        description="Puntos que se devuelven como máximo",
    ),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> ReadingSeries:
    ensure_greenhouse_access(user, greenhouse_id)
    now = utcnow()
    try:
        window = history.parse_window(from_, to, now)
    except history.RangeError as error:
        raise _invalid_range(error)

    definition = metric_catalog.BY_KEY[metric.value]
    column = definition.key                     # sale del catálogo, no del cliente
    since, until = history.ts_text(window.start), history.ts_text(window.end)
    bounds = (greenhouse_id, since, until)

    cursor = await db.execute(
        f"""
        SELECT COUNT({column}) AS readings
        FROM readings
        WHERE greenhouse_id = ? AND ts >= ? AND ts < ?
        """,
        bounds,
    )
    reading_count = (await cursor.fetchone())["readings"]

    # Se agrega según las lecturas que HAY, no según las que podría haber: un
    # rango de un mes con cuatro lecturas se dibuja con sus cuatro puntos.
    bucket_seconds = 0 if reading_count <= max_points else history.choose_bucket(
        window.span_seconds, max_points
    )

    if reading_count == 0:
        points: List[SeriesPoint] = []
    elif bucket_seconds == 0:
        points = await _raw_points(db, column, bounds)
    else:
        points = await _bucket_points(db, definition, bucket_seconds, bounds)

    lows = [point.min if point.min is not None else point.value for point in points]
    highs = [point.max if point.max is not None else point.value for point in points]

    return ReadingSeries(
        greenhouse_id=greenhouse_id,
        metric=metric,
        label=definition.label,
        unit=definition.unit,
        from_=window.start,
        to=window.end,
        generated_at=now,
        bucket_seconds=bucket_seconds,
        aggregated=bucket_seconds > 0,
        max_points=max_points,
        reading_count=reading_count,
        point_count=len(points),
        has_data=bool(points),
        value_min=min(lows) if lows else None,
        value_max=max(highs) if highs else None,
        points=points,
    )


async def _raw_points(
    db: aiosqlite.Connection, column: str, bounds: tuple
) -> List[SeriesPoint]:
    """Las lecturas del rango, sin agregar (caben en `max_points`)."""
    cursor = await db.execute(
        f"""
        SELECT ts, {column} AS value
        FROM readings
        WHERE greenhouse_id = ? AND ts >= ? AND ts < ? AND {column} IS NOT NULL
        ORDER BY ts
        """,
        bounds,
    )
    return [
        SeriesPoint(ts=parse_utc(row["ts"]), value=row["value"], count=1)
        for row in await cursor.fetchall()
    ]


async def _bucket_points(
    db: aiosqlite.Connection,
    definition: metric_catalog.Metric,
    bucket_seconds: int,
    bounds: tuple,
) -> List[SeriesPoint]:
    """Promedio, mínimo y máximo por bucket.

    Los buckets se alinean con la época Unix (no con el inicio del rango), así
    que dos consultas del mismo periodo caen en los mismos buckets.
    """
    cursor = await db.execute(
        f"""
        SELECT CAST(strftime('%s', ts) / ? AS INTEGER) * ? AS bucket,
               AVG({definition.key}) AS average,
               MIN({definition.key}) AS low,
               MAX({definition.key}) AS high,
               COUNT({definition.key}) AS readings
        FROM readings
        WHERE greenhouse_id = ? AND ts >= ? AND ts < ? AND {definition.key} IS NOT NULL
        GROUP BY bucket
        ORDER BY bucket
        """,
        (bucket_seconds, bucket_seconds, *bounds),
    )
    return [
        SeriesPoint(
            ts=history.from_epoch(row["bucket"]),
            value=round(row["average"], definition.average_decimals),
            min=row["low"],
            max=row["high"],
            count=row["readings"],
        )
        for row in await cursor.fetchall()
    ]
