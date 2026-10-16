"""
Análisis de la información histórica de un vivero (AI-01).

    GET /greenhouses/{greenhouse_id}/analysis?from=&to=&bucket_seconds=

Devuelve, por cada medición: un indicador de tendencia (`rising`, `stable`,
`falling` o `insufficient_data`), la pendiente por hora, el cambio estimado en
el periodo, el primer y el último valor, promedio, mínimo, máximo, cuántos
datos se usaron y los HUECOS del rango. El periodo y el tamaño de bucket van
en la respuesta porque son parte del resultado (criterio 1) y porque son lo
que hace falta para repetir el análisis (criterio 2).

El cálculo vive en app/analysis.py y es puro. Aquí solo se traducen las
lecturas guardadas a esa entrada, con dos consultas:

1. Cuántas lecturas hay en el rango y cuántas trajeron cada medición. La
   diferencia son las que el sensor no dio (`null` = sensor caído): se
   reportan como `readings_missing`, no se esconden (criterio 3).
2. Promedio y cuenta por bucket de las cinco mediciones a la vez. Agrupar en
   SQLite es parte del preprocesado: traer un mes de lecturas crudas a Python
   serían cientos de miles de filas para dibujar 144 puntos.

Permisos (AUTH-02): todos los roles; un productor solo analiza su vivero.
"""

from datetime import datetime
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app import analysis, history
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
    ErrorResponse,
    GreenhouseAnalysis,
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

    bounds = (greenhouse_id, history.ts_text(window.start), history.ts_text(window.end))
    counts = await _count_readings(db, bounds)
    buckets = await _bucket_rows(db, bucket, bounds)

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


async def _count_readings(db: aiosqlite.Connection, bounds: tuple) -> dict:
    """Lecturas del rango y cuántas trajeron cada medición (no null).

    Los nombres de columna salen del catálogo (app/metrics.py), nunca del
    cliente.
    """
    columns = ", ".join(f"COUNT({metric.key}) AS {metric.key}" for metric in metric_catalog.METRICS)
    cursor = await db.execute(
        f"""
        SELECT COUNT(*) AS total, {columns}
        FROM readings
        WHERE greenhouse_id = ? AND ts >= ? AND ts < ?
        """,
        bounds,
    )
    row = await cursor.fetchone()
    return {key: row[key] for key in row.keys()}


async def _bucket_rows(
    db: aiosqlite.Connection, bucket: int, bounds: tuple
) -> List[aiosqlite.Row]:
    """Promedio y cuenta por bucket de todas las mediciones, en una consulta."""
    columns = ", ".join(
        f"AVG({metric.key}) AS {metric.key}_avg, COUNT({metric.key}) AS {metric.key}_n"
        for metric in metric_catalog.METRICS
    )
    cursor = await db.execute(
        f"""
        SELECT CAST(strftime('%s', ts) / ? AS INTEGER) * ? AS bucket, {columns}
        FROM readings
        WHERE greenhouse_id = ? AND ts >= ? AND ts < ?
        GROUP BY bucket
        ORDER BY bucket
        """,
        (bucket, bucket, *bounds),
    )
    return list(await cursor.fetchall())
