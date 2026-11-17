"""
Recomendación de riego del vivero (AI-04), su explicación (AI-05) y la
decisión de una persona sobre ella (AI-07).

    GET  /greenhouses/{greenhouse_id}/irrigation/recommendation
    GET  /greenhouses/{greenhouse_id}/irrigation/recommendations
    POST /greenhouses/{greenhouse_id}/irrigation/recommendations/{id}/decision

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

Decisión (AI-07): una persona acepta o rechaza la recomendación guardada
que tiene en pantalla, una sola vez, y queda con su nombre y la hora (también
en la bitácora). Solo se decide la más reciente del vivero: una vieja ya no
describe lo que pasa ahora. Aceptar NO riega: el riego lo dispara la persona
con la orden manual de ACT-02, desde la misma tarjeta. Así la decisión final
sigue en sus manos y se respetan las reglas de la bomba (modo manual, tanque).

Permisos (AUTH-02): todos los roles; un productor solo su vivero.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import List, Optional, Union

import aiosqlite
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query

from app import audit, history
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
    DecisionEnum,
    ErrorResponse,
    IrrigationInput,
    IrrigationRecommendation,
    MeasuredInput,
    RecommendationDecision,
    RecommendationDecisionCreate,
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

# Sin dato de la tierra o con la bomba regando no hay nada que decidir.
NOT_DECIDABLE = (
    recommendation.Outcome.insufficient_data, recommendation.Outcome.irrigating,
)


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
    try:
        response = await evaluate(db, greenhouse_id, utcnow())
    except RuntimeError:
        raise HTTPException(503, "La recomendación no tiene explicación: no se muestra.")
    assert response is not None  # sin on_ingest siempre se calcula
    if response.id is not None:
        response.decision = (await _decisions(db, [response.id])).get(response.id)
    return response


async def evaluate(
    db: aiosqlite.Connection,
    greenhouse_id: str,
    now: datetime,
    *,
    occurred_at: Optional[Union[datetime, str]] = None,
    on_ingest: bool = False,
) -> Optional[IrrigationRecommendation]:
    """Calcula la recomendación del vivero, la valida y la guarda si cambió.

    La usan la consulta (GET) y la ingesta (SYS-04, `on_ingest=True`). Desde
    la ingesta, `occurred_at` es la hora de la lectura más nueva del lote: si
    ya está fuera de la ventana de lecturas frescas (un backfill viejo), no se
    evalúa y se devuelve None. Una recomendación sin explicación levanta
    RuntimeError y no se guarda (criterio 4 de AI-05).
    """
    cutoff = now - timedelta(minutes=settings.reading_stale_minutes)
    if on_ingest and occurred_at is not None:
        latest = (
            datetime.fromisoformat(occurred_at)
            if isinstance(occurred_at, str) else occurred_at
        )
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        if latest < cutoff:
            return None

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
        raise RuntimeError("La recomendación no tiene explicación: no se guarda.")

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
    response.id = await _store_if_changed(
        db, response, occurred_at=occurred_at, skip_initial_insufficient=on_ingest,
    )
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
        SELECT id, payload FROM irrigation_recommendations
        WHERE greenhouse_id = ?
        ORDER BY id DESC LIMIT ?
        """,
        (greenhouse_id, limit),
    )
    rows = await cursor.fetchall()
    decisions = await _decisions(db, [row["id"] for row in rows])
    items = []
    for row in rows:
        item = IrrigationRecommendation.parse_raw(row["payload"])
        item.id = row["id"]
        item.decision = decisions.get(row["id"])
        items.append(item)
    return RecommendationHistory(greenhouse_id=greenhouse_id, items=items)


def _decision_error(status: int, error: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": error, "message": message})


@router.post(
    "/{greenhouse_id}/irrigation/recommendations/{recommendation_id}/decision",
    response_model=RecommendationDecision,
    status_code=201,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422)},
    summary="Aceptar o rechazar una recomendación de riego",
    description=(
        "Guarda la decisión de la persona con su nombre y la hora, y la deja en "
        "la bitácora. Una sola vez por recomendación y solo sobre la más "
        "reciente del vivero. Aceptar no riega: el riego se dispara con la orden "
        "manual de la bomba (POST /actuators/{actuator_id}/commands)."
    ),
)
async def decide_recommendation(
    body: RecommendationDecisionCreate = Body(...),
    greenhouse_id: str = Path(..., min_length=1, max_length=64),
    recommendation_id: int = Path(..., ge=1),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> RecommendationDecision:
    ensure_greenhouse_access(user, greenhouse_id)
    cursor = await db.execute(
        """
        SELECT id, outcome, payload FROM irrigation_recommendations
        WHERE id = ? AND greenhouse_id = ?
        """,
        (recommendation_id, greenhouse_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise _decision_error(404, "not_found", "Esa recomendación no existe en este vivero.")
    if recommendation.Outcome(row["outcome"]) in NOT_DECIDABLE:
        raise _decision_error(
            422, "not_decidable", "Esta recomendación no pide ninguna decisión.")
    cursor = await db.execute(
        "SELECT MAX(id) AS newest FROM irrigation_recommendations WHERE greenhouse_id = ?",
        (greenhouse_id,),
    )
    if (await cursor.fetchone())["newest"] != recommendation_id:
        raise _decision_error(
            409, "superseded", "Ya hay una recomendación más nueva: decide sobre esa.")

    now = utcnow()
    decided_at = history.ts_text(now)   # lo que se guarda (milisegundos)
    actor = audit.user_actor(user)
    decision = RecommendationDecision(
        decision=body.decision, user_id=user["id"], user_name=actor["actor_name"],
        decided_at=datetime.fromisoformat(decided_at),
    )
    try:
        await db.execute(
            """
            INSERT INTO recommendation_decisions
                (recommendation_id, greenhouse_id, decision, user_id, user_name, decided_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (recommendation_id, greenhouse_id, decision.decision.value, user["id"],
             decision.user_name, decided_at),
        )
    except sqlite3.IntegrityError:
        raise _decision_error(
            409, "already_decided", "Alguien ya decidió sobre esta recomendación.")

    summary = IrrigationRecommendation.parse_raw(row["payload"]).explanation.summary
    verb = "Aceptó" if decision.decision == DecisionEnum.accepted else "Rechazó"
    # Bitácora (AUTH-05), en la misma transacción que la decisión.
    await audit.record(
        db, action=f"recommendation_{decision.decision.value}", actor=actor,
        greenhouse_id=greenhouse_id, target_type="recommendation",
        target_id=recommendation_id, summary=f"{verb} la recomendación: {summary}",
        detail={"outcome": row["outcome"]}, occurred_at=now, now=now,
    )
    await db.commit()
    return decision


async def _decisions(db: aiosqlite.Connection, ids: List[int]) -> dict:
    """{id de recomendación: su decisión} de las que tienen una."""
    if not ids:
        return {}
    cursor = await db.execute(
        f"""
        SELECT recommendation_id, decision, user_id, user_name, decided_at
        FROM recommendation_decisions
        WHERE recommendation_id IN ({','.join('?' * len(ids))})
        """,
        ids,
    )
    return {
        row["recommendation_id"]: RecommendationDecision(
            decision=row["decision"], user_id=row["user_id"], user_name=row["user_name"],
            decided_at=datetime.fromisoformat(row["decided_at"]),
        )
        for row in await cursor.fetchall()
    }


async def _store_if_changed(
    db: aiosqlite.Connection,
    current: IrrigationRecommendation,
    *,
    occurred_at: Optional[Union[datetime, str]] = None,
    skip_initial_insufficient: bool = False,
) -> Optional[int]:
    """Guarda la recomendación si dice algo distinto de la última guardada.

    Devuelve el id de la guardada que la representa (la nueva o la última,
    si dice lo mismo); None si no se guardó.

    Con `skip_initial_insufficient` (desde la ingesta), un vivero sin
    historial no estrena la tabla con un \"faltan datos\": una lectura sin
    tierra no es una recomendación que valga la pena guardar.
    """
    cursor = await db.execute(
        """
        SELECT id, outcome, urgent, tank_blocks FROM irrigation_recommendations
        WHERE greenhouse_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (current.greenhouse_id,),
    )
    last = await cursor.fetchone()
    if (
        skip_initial_insufficient
        and last is None
        and current.outcome == recommendation.Outcome.insufficient_data
    ):
        return None
    key = (current.outcome.value, int(current.urgent), int(current.tank_blocks))
    if last is not None and (last["outcome"], last["urgent"], last["tank_blocks"]) == key:
        return last["id"]
    cursor = await db.execute(
        """
        INSERT INTO irrigation_recommendations
            (greenhouse_id, outcome, urgent, tank_blocks, payload, generated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (current.greenhouse_id, *key, current.json(), history.ts_text(current.generated_at)),
    )
    # Bitácora (AUTH-05): la recomendación la produce la plataforma.
    await audit.record(
        db, action="recommendation_generated", actor=audit.SYSTEM_ACTOR,
        greenhouse_id=current.greenhouse_id, target_type="recommendation",
        target_id=cursor.lastrowid, summary=current.explanation.summary,
        detail={"outcome": current.outcome.value, "urgent": current.urgent,
                "tank_blocks": current.tank_blocks, "threshold": current.threshold,
                "missing": current.missing},
        occurred_at=occurred_at or current.generated_at, now=current.generated_at,
    )
    await db.commit()
    return cursor.lastrowid


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
