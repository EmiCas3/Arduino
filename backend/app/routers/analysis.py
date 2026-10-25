"""
Análisis de la información histórica de un vivero (AI-01) y sus condiciones
inusuales (AI-02).

    GET /greenhouses/{greenhouse_id}/analysis?from=&to=&bucket_seconds=
    GET /greenhouses/{greenhouse_id}/anomalies?from=&to=&bucket_seconds=

Las anomalías usan el mismo rango, los mismos buckets y el mismo preprocesado
que las tendencias; la regla está en app/anomalies.py. Lo de abajo describe
`/analysis`.

Devuelve, por cada medición: un indicador de tendencia (`rising`, `stable`,
`falling` o `insufficient_data`), la pendiente por hora, el cambio estimado en
el periodo, el primer y el último valor, promedio, mínimo, máximo, cuántos
datos se usaron y los HUECOS del rango. El periodo y el tamaño de bucket van
en la respuesta porque son parte del resultado (criterio 1) y porque son lo
que hace falta para repetir el análisis (criterio 2).

El cálculo vive en app/analysis.py y es puro. Aquí solo se traducen las
lecturas guardadas a esa entrada, con UNA consulta: por bucket, cuántas
lecturas hay y el promedio y la cuenta de cada medición. Agrupar en SQLite es
parte del preprocesado: traer un mes de lecturas crudas a Python serían
cientos de miles de filas para dibujar 240 puntos.

Sumando los buckets salen cuántas lecturas hay en el rango y cuántas trajeron
cada medición. La diferencia son las que el sensor no dio (`null` = sensor
caído): se reportan como `readings_missing`, no se esconden (criterio 3).
(Antes era una consulta aparte; con 30 días eran ~0.4 s más por refresco con
la única conexión a la BD.)

Permisos (AUTH-02): todos los roles; un productor solo analiza su vivero.
"""

from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app import analysis, anomalies, history
from app import metrics as metric_catalog
from app.database import get_db
from app.dependencies import (
    ALL_ROLES,
    ensure_greenhouse_access,
    get_current_user,
    require_roles,
    utcnow,
)
from app.models import (
    AnalysisGap,
    AnalysisPeriod,
    Anomaly,
    AnomalyCheck,
    ErrorResponse,
    GreenhouseAnalysis,
    GreenhouseAnomalies,
    VariableAnalysis,
)

router = APIRouter(
    prefix="/greenhouses",
    tags=["analysis"],
    dependencies=[Depends(get_current_user)],
)

# Buckets en los que se parte el rango cuando nadie pide un tamaño: los
# mismos que usa la gráfica, para que lo que se ve y lo que se analiza
# coincidan.
DEFAULT_ANALYSIS_POINTS = history.DEFAULT_MAX_POINTS


def _invalid_range(message: str) -> HTTPException:
    return HTTPException(
        status_code=422, detail={"error": "invalid_range", "message": message}
    )


@router.get(
    "/{greenhouse_id}/analysis",
    response_model=GreenhouseAnalysis,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 422)},
    summary="Tendencias del vivero en un periodo",
    description=(
        "Analiza las lecturas guardadas del rango y devuelve, por medición, "
        "si va subiendo, estable o bajando, con la pendiente, el cambio "
        "estimado y la calidad del dato (cobertura y huecos). Sin `from` ni "
        "`to` analiza las últimas 24 horas. El mismo rango sobre los mismos "
        "datos da siempre el mismo resultado."
    ),
)
async def analyze_greenhouse(
    greenhouse_id: str = Path(..., min_length=1, max_length=64),
    from_: Optional[datetime] = Query(
        None, alias="from", description="Inicio del periodo (ISO 8601 con zona)"
    ),
    to: Optional[datetime] = Query(
        None, description="Fin del periodo (ISO 8601 con zona). Por defecto, ahora"
    ),
    bucket_seconds: Optional[int] = Query(
        None,
        ge=history.MIN_BUCKET_SECONDS,
        le=history.MAX_BUCKET_SECONDS,
        description="Tamaño del bucket en segundos. Por defecto se elige según el rango",
    ),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> GreenhouseAnalysis:
    ensure_greenhouse_access(user, greenhouse_id)
    now = utcnow()
    window, bucket, expected = _resolve_period(from_, to, bucket_seconds, now)

    bounds = (greenhouse_id, history.ts_text(window.start), history.ts_text(window.end))
    buckets = await _bucket_rows(db, bucket, bounds)
    counts = _totals(buckets)

    variables: List[VariableAnalysis] = []
    for metric in metric_catalog.METRICS:
        prepared = analysis.prepare(
            ((row["bucket"], row[f"{metric.key}_avg"], row[f"{metric.key}_n"]) for row in buckets),
            window,
            bucket,
            readings_total=counts["total"],
            readings_valid=counts[metric.key],
        )
        result = analysis.trend(prepared, metric.stable_band, metric.decimals)
        variables.append(VariableAnalysis(
            key=metric.key,
            label=metric.label,
            unit=metric.unit,
            trend=result.direction,
            slope_per_hour=result.slope_per_hour,
            change=result.change,
            stable_band=metric.stable_band,
            observed_hours=result.observed_hours,
            first=result.first,
            last=result.last,
            mean=result.mean,
            min=result.minimum,
            max=result.maximum,
            fit_r2=result.fit_r2,
            samples=prepared.sample_count,
            expected_samples=prepared.expected_samples,
            coverage_pct=prepared.coverage_pct,
            readings_used=prepared.readings_used,
            readings_missing=prepared.readings_missing,
            gaps=[
                AnalysisGap(
                    from_=history.from_epoch(gap.start_epoch),
                    to=history.from_epoch(gap.end_epoch),
                    seconds=gap.seconds,
                    buckets=gap.buckets,
                )
                for gap in prepared.gaps
            ],
        ))

    return GreenhouseAnalysis(
        greenhouse_id=greenhouse_id,
        generated_at=now,
        period=AnalysisPeriod(
            from_=window.start,
            to=window.end,
            hours=round(window.span_hours, 3),
            bucket_seconds=bucket,
            expected_samples=expected,
        ),
        has_data=counts["total"] > 0,
        readings=counts["total"],
        variables=variables,
    )


@router.get(
    "/{greenhouse_id}/anomalies",
    response_model=GreenhouseAnomalies,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 422)},
    summary="Condiciones inusuales del vivero en un periodo",
    description=(
        "Compara cada tramo del periodo con el patrón reciente de su sensor y "
        "devuelve los que se salieron de él: sensor, valor, rango esperado y "
        "hora. No son alertas: una anomalía puede estar dentro de los límites "
        "del rábano. Los cambios que explica un riego (tierra que se moja, "
        "tanque que baja) no cuentan. Sin `from` ni `to` revisa las últimas "
        "24 horas. El mismo rango sobre los mismos datos da siempre el mismo "
        "resultado."
    ),
)
async def greenhouse_anomalies(
    greenhouse_id: str = Path(..., min_length=1, max_length=64),
    from_: Optional[datetime] = Query(
        None, alias="from", description="Inicio del periodo (ISO 8601 con zona)"
    ),
    to: Optional[datetime] = Query(
        None, description="Fin del periodo (ISO 8601 con zona). Por defecto, ahora"
    ),
    bucket_seconds: Optional[int] = Query(
        None,
        ge=history.MIN_BUCKET_SECONDS,
        le=history.MAX_BUCKET_SECONDS,
        description="Tamaño del bucket en segundos. Por defecto se elige según el rango",
    ),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> GreenhouseAnomalies:
    ensure_greenhouse_access(user, greenhouse_id)
    now = utcnow()
    window, bucket, expected = _resolve_period(from_, to, bucket_seconds, now)

    # Se leen también los buckets ANTERIORES al rango que forman el patrón de
    # los primeros: si no, la primera media hora del periodo nunca se juzgaría.
    lookback = anomalies.BASELINE_BUCKETS * bucket
    extended = history.Window(start=window.start - timedelta(seconds=lookback), end=window.end)
    buckets = await _bucket_rows(
        db, bucket,
        (greenhouse_id, history.ts_text(extended.start), history.ts_text(window.end)),
    )
    cells = history.grid(window, bucket)
    in_range = [row for row in buckets if row["bucket"] >= cells.start_epoch]
    irrigation = anomalies.irrigation_windows(
        await _pump_events(db, greenhouse_id, extended),
        until=cells.end_epoch,
    )

    found: List[Anomaly] = []
    checks: List[AnomalyCheck] = []
    for metric in metric_catalog.METRICS:
        if metric.anomaly_floor is None:
            checks.append(AnomalyCheck(
                key=metric.key, label=metric.label, unit=metric.unit,
                checked=False, evaluated=0, anomalies=0, explained_by_irrigation=0,
            ))
            continue
        prepared = analysis.prepare(
            ((row["bucket"], row[f"{metric.key}_avg"], row[f"{metric.key}_n"]) for row in buckets),
            extended,
            bucket,
        )
        detection = anomalies.detect(
            prepared, metric.anomaly_floor, cells.start_epoch, metric.decimals
        )
        kept, explained = anomalies.drop_irrigation_effects(
            detection.anomalies, metric.key, irrigation, bucket
        )
        found.extend(_anomaly_out(metric, item, cells.end_epoch - bucket) for item in kept)
        checks.append(AnomalyCheck(
            key=metric.key, label=metric.label, unit=metric.unit,
            checked=True, evaluated=detection.evaluated, anomalies=len(kept),
            explained_by_irrigation=explained, floor=metric.anomaly_floor,
        ))

    # Las más recientes primero; en empate, en el orden del catálogo (sorted
    # es estable), para que la misma consulta dé la misma lista.
    found.sort(key=lambda item: item.to, reverse=True)
    return GreenhouseAnomalies(
        greenhouse_id=greenhouse_id,
        generated_at=now,
        period=AnalysisPeriod(
            from_=window.start,
            to=window.end,
            hours=round(window.span_hours, 3),
            bucket_seconds=bucket,
            expected_samples=expected,
        ),
        baseline_seconds=lookback,
        has_data=any(row["total"] for row in in_range),
        anomalies=found,
        variables=checks,
    )


def _anomaly_out(metric, item: anomalies.Anomaly, last_bucket: int) -> Anomaly:
    """Una anomalía del cálculo puro, con los datos de su medición.

    `ongoing`: llega al último bucket del periodo o al anterior (el último
    casi siempre va a medias y puede no traer lecturas todavía).
    """
    return Anomaly(
        id=f"{metric.key}-{item.start_epoch}",
        metric=metric.key,
        label=metric.label,
        unit=metric.unit,
        kind=item.kind,
        ts=history.from_epoch(item.peak_epoch),
        value=item.value,
        expected=item.expected.center,
        expected_min=item.expected.low,
        expected_max=item.expected.high,
        from_=history.from_epoch(item.start_epoch),
        to=history.from_epoch(item.end_epoch),
        buckets=item.buckets,
        ongoing=item.end_epoch >= last_bucket,
    )


async def _pump_events(
    db: aiosqlite.Connection, greenhouse_id: str, window: history.Window
) -> List[tuple]:
    """Encendidos y apagados de la bomba (ACT-02) que tocan la ventana.

    Incluye el último evento ANTERIOR a la ventana: si la bomba ya venía
    encendida, el riego empezó antes y sigue contando.
    """
    start, end = history.ts_text(window.start), history.ts_text(window.end)
    query = """
        SELECT e.reading_ts, e.event
        FROM actuator_events e
        JOIN actuators a ON a.actuator_id = e.actuator_id
        WHERE e.greenhouse_id = ? AND a.type = 'pump' AND {condition}
        ORDER BY e.reading_ts {order}, e.id {order}
        {limit}
    """
    cursor = await db.execute(
        query.format(condition="e.reading_ts < ?", order="DESC", limit="LIMIT 1"),
        (greenhouse_id, start),
    )
    rows = list(await cursor.fetchall())
    cursor = await db.execute(
        query.format(condition="e.reading_ts >= ? AND e.reading_ts < ?", order="ASC", limit=""),
        (greenhouse_id, start, end),
    )
    rows.extend(await cursor.fetchall())
    return [
        (int(datetime.fromisoformat(row["reading_ts"]).timestamp()), row["event"] == "on")
        for row in rows
    ]


def _resolve_period(
    from_: Optional[datetime], to: Optional[datetime],
    bucket_seconds: Optional[int], now: datetime,
) -> Tuple[history.Window, int, int]:
    """Rango, tamaño de bucket y buckets esperados; 422 si no son usables."""
    try:
        window = history.parse_window(from_, to, now)
    except history.RangeError as error:
        raise _invalid_range(str(error))

    bucket = bucket_seconds or (
        history.choose_bucket(window.span_seconds, DEFAULT_ANALYSIS_POINTS)
        or history.MIN_BUCKET_SECONDS
    )
    expected = history.grid(window, bucket).count
    if expected > history.MAX_MAX_POINTS:
        raise _invalid_range(
            f"Un bucket de {bucket} s parte ese rango en {expected} tramos; "
            f"el máximo es {history.MAX_MAX_POINTS}. Usa un bucket más grande "
            "o un rango más corto."
        )
    return window, bucket, expected


def _totals(buckets: List[aiosqlite.Row]) -> dict:
    """Lecturas del rango y cuántas trajeron cada medición (no null)."""
    totals = {"total": sum(row["total"] for row in buckets)}
    for metric in metric_catalog.METRICS:
        totals[metric.key] = sum(row[f"{metric.key}_n"] for row in buckets)
    return totals


async def _bucket_rows(
    db: aiosqlite.Connection, bucket: int, bounds: tuple
) -> List[aiosqlite.Row]:
    """Por bucket: lecturas, y promedio y cuenta de cada medición.

    Los nombres de columna salen del catálogo (app/metrics.py), nunca del
    cliente.
    """
    columns = ", ".join(
        f"AVG({metric.key}) AS {metric.key}_avg, COUNT({metric.key}) AS {metric.key}_n"
        for metric in metric_catalog.METRICS
    )
    cursor = await db.execute(
        f"""
        SELECT CAST(strftime('%s', ts) / ? AS INTEGER) * ? AS bucket,
               COUNT(*) AS total, {columns}
        FROM readings
        WHERE greenhouse_id = ? AND ts >= ? AND ts < ?
        GROUP BY bucket
        ORDER BY bucket
        """,
        (bucket, bucket, *bounds),
    )
    return list(await cursor.fetchall())
