"""
Recomendación de riego del vivero (AI-04) y su explicación (AI-05).

    GET /greenhouses/{greenhouse_id}/irrigation/recommendation
    GET /greenhouses/{greenhouse_id}/irrigation/recommendations

Junta lo que consume la recomendación — humedad de la tierra, temperatura,
último riego y, como aviso, el tanque — y le pasa todo a app/recommendation.py,
que es puro. Aquí solo se leen las entradas de la BD.

- Mediciones: la MEDIANA de las lecturas con valor de los últimos
  `SMOOTHING_MINUTES` antes de la más reciente. Con lecturas cada 2 s, el
  ruido de ±1 % del sensor de tierra haría saltar la recomendación entre
  "regar" y "regar pronto" en cada refresco; la mediana no se mueve por una
  lectura rara. Si la lectura más reciente es más vieja que
  SMARTGREENAI_READING_STALE_MINUTES, esa medición no tiene dato: no se
  recomienda con lo que medía el sensor hace una hora.
- Último riego: el último encendido de la bomba en `actuator_events` (ACT-02),
  sea automático o manual. Si el último evento es un encendido, la bomba
  sigue regando.

La respuesta trae las entradas tal como se usaron y de dónde sale el umbral,
para que la misma recomendación se pueda revisar después.

Guardado (AI-05): la recomendación se calcula al consultarla, y si es distinta
de la última guardada del vivero se guarda tal cual, con sus entradas y su
explicación. Con el refresco del dashboard, guardar cada consulta llenaría la
tabla con copias. Una recomendación sin explicación no se guarda ni se devuelve
(criterio 4 de AI-05).

Permisos (AUTH-02): todos los roles; un productor solo su vivero.
"""

from datetime import datetime, timedelta
from statistics import median
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app import history
from app import metrics as metric_catalog
from app import recommendation
from app.config import settings
from app.database import get_db
from app.dependencies import (
    ALL_ROLES,
    ensure_greenhouse_access,
    get_current_user,
    require_roles,
    utcnow,
)
from app.explanation import explain, is_explained
from app.models import (
    ErrorResponse,
    IrrigationInput,
    IrrigationRecommendation,
    MeasuredInput,
    RecommendationExplanation,
    RecommendationHistory,
    RecommendationInputs,
    ThresholdBreakdown,
)

router = APIRouter(
    prefix="/greenhouses",
    tags=["analysis"],
    dependencies=[Depends(get_current_user)],
)

# Minutos de lecturas que entran a la mediana de cada medición.
SMOOTHING_MINUTES = 5


@router.get(
    "/{greenhouse_id}/irrigation/recommendation",
    response_model=IrrigationRecommendation,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 503)},
    summary="Recomendación de riego con temperatura, tierra y último riego",
    description=(
        "Recomienda regar ahora, pronto o no, pesando juntas la humedad de la "
        "tierra, la temperatura y el tiempo desde el último riego. Las tres "
        "mueven el umbral de humedad abajo del cual conviene regar; la "
        "respuesta trae la explicación en lenguaje claro, las entradas que se "
        "usaron y de dónde sale el umbral. "
        "Es una recomendación: el riego lo sigue decidiendo el Arduino "
        "(automático) o una persona (manual)."
    ),
)
async def irrigation_recommendation(
    greenhouse_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> IrrigationRecommendation:
    ensure_greenhouse_access(user, greenhouse_id)
    now = utcnow()
    cutoff = now - timedelta(minutes=settings.reading_stale_minutes)

    soil = await _measured(db, greenhouse_id, "suelo_pct", cutoff)
    temperature = await _measured(db, greenhouse_id, "temp_c", cutoff)
    tank = await _measured(db, greenhouse_id, "nivel_pct", cutoff)
    irrigation = await _last_irrigation(db, greenhouse_id, now)

    engine_inputs = recommendation.Inputs(
        soil_pct=soil.value,
        temp_c=temperature.value,
        hours_since_irrigation=irrigation.hours_since,
        irrigating=irrigation.irrigating,
        tank_pct=tank.value,
    )
    result = recommendation.recommend(engine_inputs)
    explanation = explain(engine_inputs, result)
    if not is_explained(explanation):
        raise HTTPException(503, "La recomendación no tiene explicación: no se muestra.")

    missing: List[str] = [
        name for name, absent in (
            ("soil", soil.value is None),
            ("temperature", temperature.value is None),
            ("last_irrigation", irrigation.last_started_at is None),
            ("tank", tank.value is None),
        ) if absent
    ]
    response = IrrigationRecommendation(
        greenhouse_id=greenhouse_id,
        generated_at=now,
        outcome=result.outcome,
        urgent=result.urgent,
        tank_blocks=result.tank_blocks,
        explanation=RecommendationExplanation(
            summary=explanation.summary,
            reasons=explanation.reasons,
            notes=explanation.notes,
        ),
        threshold=result.threshold,
        watch_until=result.watch_until,
        breakdown=ThresholdBreakdown(
            base=result.base_threshold,
            temperature=result.temperature_points,
            last_irrigation=result.irrigation_points,
            min=recommendation.THRESHOLD_MIN,
            max=recommendation.THRESHOLD_MAX,
        ),
        inputs=RecommendationInputs(
            soil=soil, temperature=temperature, last_irrigation=irrigation, tank=tank,
        ),
        missing=missing,
    )
    await _store_if_changed(db, response)
    return response


@router.get(
    "/{greenhouse_id}/irrigation/recommendations",
    response_model=RecommendationHistory,
    responses={code: {"model": ErrorResponse} for code in (401, 403)},
    summary="Recomendaciones guardadas, con sus entradas exactas",
    description=(
        "Las recomendaciones que se guardaron, de la más reciente a la más "
        "vieja, tal como se mostraron: su explicación y las entradas que usaron."
    ),
)
async def stored_recommendations(
    greenhouse_id: str = Path(..., min_length=1, max_length=64),
    limit: int = Query(10, ge=1, le=50),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> RecommendationHistory:
    ensure_greenhouse_access(user, greenhouse_id)
    cursor = await db.execute(
        """
        SELECT payload FROM irrigation_recommendations
        WHERE greenhouse_id = ?
        ORDER BY id DESC LIMIT ?
        """,
        (greenhouse_id, limit),
    )
    return RecommendationHistory(
        greenhouse_id=greenhouse_id,
        items=[
            IrrigationRecommendation.parse_raw(row["payload"])
            for row in await cursor.fetchall()
        ],
    )


async def _store_if_changed(
    db: aiosqlite.Connection, current: IrrigationRecommendation
) -> None:
    """Guarda la recomendación si dice algo distinto de la última guardada."""
    cursor = await db.execute(
        """
        SELECT outcome, urgent, tank_blocks FROM irrigation_recommendations
        WHERE greenhouse_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (current.greenhouse_id,),
    )
    last = await cursor.fetchone()
    key = (current.outcome.value, int(current.urgent), int(current.tank_blocks))
    if last is not None and tuple(last) == key:
        return
    await db.execute(
        """
        INSERT INTO irrigation_recommendations
            (greenhouse_id, outcome, urgent, tank_blocks, payload, generated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (current.greenhouse_id, *key, current.json(), history.ts_text(current.generated_at)),
    )
    await db.commit()


async def _measured(
    db: aiosqlite.Connection, greenhouse_id: str, column: str, cutoff: datetime
) -> MeasuredInput:
    """Mediana de una medición en sus últimos minutos con dato.

    `column` sale siempre del catálogo (app/metrics.py), nunca del cliente.
    """
    metric = metric_catalog.BY_KEY[column]
    cursor = await db.execute(
        f"""
        SELECT ts FROM readings
        WHERE greenhouse_id = ? AND ts >= ? AND {metric.key} IS NOT NULL
        ORDER BY ts DESC LIMIT 1
        """,
        (greenhouse_id, history.ts_text(cutoff)),
    )
    latest = await cursor.fetchone()
    if latest is None:
        return MeasuredInput(value=None, unit=metric.unit, ts=None, samples=0)

    newest = datetime.fromisoformat(latest["ts"])
    since = newest - timedelta(minutes=SMOOTHING_MINUTES)
    cursor = await db.execute(
        f"""
        SELECT {metric.key} AS value FROM readings
        WHERE greenhouse_id = ? AND ts >= ? AND ts <= ? AND {metric.key} IS NOT NULL
        """,
        (greenhouse_id, history.ts_text(max(since, cutoff)), latest["ts"]),
    )
    values = [row["value"] for row in await cursor.fetchall()]
    return MeasuredInput(
        value=round(median(values), metric.average_decimals),
        unit=metric.unit,
        ts=newest,
        samples=len(values),
    )


async def _last_irrigation(
    db: aiosqlite.Connection, greenhouse_id: str, now: datetime
) -> IrrigationInput:
    """Último encendido de la bomba y si sigue encendida."""
    cursor = await db.execute(
        """
        SELECT e.reading_ts, e.event
        FROM actuator_events e
        JOIN actuators a ON a.actuator_id = e.actuator_id
        WHERE e.greenhouse_id = ? AND a.type = 'pump'
        ORDER BY e.reading_ts DESC, e.id DESC
        LIMIT 1
        """,
        (greenhouse_id,),
    )
    last = await cursor.fetchone()
    cursor = await db.execute(
        """
        SELECT MAX(e.reading_ts) AS started
        FROM actuator_events e
        JOIN actuators a ON a.actuator_id = e.actuator_id
        WHERE e.greenhouse_id = ? AND a.type = 'pump' AND e.event = 'on'
        """,
        (greenhouse_id,),
    )
    started_text: Optional[str] = (await cursor.fetchone())["started"]
    if started_text is None:
        return IrrigationInput(last_started_at=None, hours_since=None, irrigating=False)

    started = datetime.fromisoformat(started_text)
    hours = max(0.0, (now - started).total_seconds() / 3600)
    return IrrigationInput(
        last_started_at=started,
        hours_since=round(hours, 2),
        irrigating=last is not None and last["event"] == "on",
    )
