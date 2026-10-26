"""
AI-04 — Recomendación de riego que pesa varias variables a la vez.

Hasta ahora el único "motor" era el del Arduino: riega cuando la tierra baja
de SUELO_LIMITE_MIN, una sola variable contra un solo umbral. AI-04 pide que
la recomendación considere juntas la HUMEDAD DE LA TIERRA, la TEMPERATURA y
el TIEMPO DESDE EL ÚLTIMO RIEGO (criterio 1), y que cambiar cualquiera de las
tres pueda cambiar el resultado (criterio 2).

Cómo se combinan: las tres hablan del mismo número, el UMBRAL — la humedad de
la tierra abajo de la cual conviene regar.

1. El umbral parte del mínimo ideal del rábano (SUELO_IDEAL_MIN, 60 %).
2. TEMPERATURA. Con calor la tierra se seca más rápido y conviene regar
   antes: arriba de TEMP_IDEAL_MAX el umbral sube `HEAT_POINTS_PER_DEGREE`
   por grado (hasta `HEAT_MAX_POINTS`). Con frío se seca más lento: abajo de
   TEMP_IDEAL_MIN baja `COOL_POINTS_PER_DEGREE` por grado (hasta
   `COOL_MAX_POINTS`).
3. ÚLTIMO RIEGO. Si se regó hace menos de `RECENT_IRRIGATION_HOURS`, el agua
   todavía está bajando y el sensor tarda en verla: el umbral baja
   `RECENT_IRRIGATION_POINTS` para no regar dos veces seguidas. Si pasaron
   más de `LONG_WITHOUT_IRRIGATION_HOURS`, sube `LONG_WITHOUT_IRRIGATION_POINTS`.
4. El umbral nunca sale de [SUELO_LIMITE_MIN, SUELO_IDEAL_MAX]: ni se
   recomienda dejar secar la tierra más allá del límite del rábano, ni regar
   con la tierra ya en su ideal.

Con el umbral, la HUMEDAD DE LA TIERRA decide (en este orden):

    sin dato de la tierra               → insufficient_data
    la bomba está regando               → irrigating
    tierra arriba de SUELO_LIMITE_MAX   → too_wet (no regar)
    tierra abajo del umbral             → irrigate (urgente si además está
                                          abajo de SUELO_LIMITE_MIN)
    tierra a menos de WATCH_MARGIN      → irrigate_soon
    lo demás                            → no_irrigation

Sin temperatura o sin historial de riego no se inventa nada: ese ajuste vale
0 y la respuesta dice que faltó el dato. El tanque no cambia la
recomendación, pero si está en NIVEL_PCT_CRITICO se avisa que el Arduino no
dejaría regar (`tank_blocks`).

Es una RECOMENDACIÓN, no una orden: el riego lo sigue decidiendo el Arduino
(automático) o una persona (manual, ACT-02). Aquí no se redacta ninguna
explicación en lenguaje natural: eso es AI-05.

Funciones puras, como app/analysis.py: sin FastAPI, sin BD, sin reloj. Las
mismas entradas dan la misma recomendación.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from app.firmware import CONSTANTS

BASE_THRESHOLD = float(CONSTANTS["SUELO_IDEAL_MIN"])
THRESHOLD_MIN = float(CONSTANTS["SUELO_LIMITE_MIN"])
THRESHOLD_MAX = float(CONSTANTS["SUELO_IDEAL_MAX"])
SOIL_TOO_WET = float(CONSTANTS["SUELO_LIMITE_MAX"])
TANK_CRITICAL = float(CONSTANTS["NIVEL_PCT_CRITICO"])
TEMP_IDEAL_MIN = float(CONSTANTS["TEMP_IDEAL_MIN"])
TEMP_IDEAL_MAX = float(CONSTANTS["TEMP_IDEAL_MAX"])

# Temperatura: puntos de humedad que se suman (calor) o restan (frío) al
# umbral por cada grado fuera del ideal. Con 30 °C (TEMP_LIMITE_MAX) el umbral
# llega a 65 %, el máximo ideal.
HEAT_POINTS_PER_DEGREE = 1.0
HEAT_MAX_POINTS = 5.0
COOL_POINTS_PER_DEGREE = 1.0
COOL_MAX_POINTS = 5.0

# Último riego.
RECENT_IRRIGATION_HOURS = 2.0
RECENT_IRRIGATION_POINTS = -8.0
LONG_WITHOUT_IRRIGATION_HOURS = 24.0
LONG_WITHOUT_IRRIGATION_POINTS = 3.0

# Cuánto arriba del umbral se dice "regar pronto" en vez de "no hace falta".
WATCH_MARGIN = 3.0


class Outcome(str, Enum):
    """Qué se recomienda hacer con el riego."""

    irrigate = "irrigate"                    # regar ahora
    irrigate_soon = "irrigate_soon"          # está cerca: regar pronto
    no_irrigation = "no_irrigation"          # no hace falta
    too_wet = "too_wet"                      # no regar: la tierra está encharcada
    irrigating = "irrigating"                # la bomba ya está regando
    insufficient_data = "insufficient_data"  # sin dato de la tierra


@dataclass(frozen=True)
class Inputs:
    """Lo que consume la recomendación (criterio 1). `None` = no hay dato."""

    soil_pct: Optional[float]
    temp_c: Optional[float]
    hours_since_irrigation: Optional[float]
    irrigating: bool = False
    tank_pct: Optional[float] = None


@dataclass(frozen=True)
class Recommendation:
    outcome: Outcome
    urgent: bool                 # la tierra ya está abajo del límite del rábano
    tank_blocks: bool            # el Arduino no dejaría regar: tanque crítico
    threshold: float             # regar con la tierra abajo de esto
    watch_until: float           # hasta aquí se dice "regar pronto"
    base_threshold: float
    temperature_points: float    # lo que movió el umbral la temperatura
    irrigation_points: float     # lo que lo movió el último riego


def temperature_points(temp_c: Optional[float]) -> float:
    """Cuánto sube (calor) o baja (frío) el umbral por la temperatura."""
    if temp_c is None:
        return 0.0
    if temp_c > TEMP_IDEAL_MAX:
        return min(HEAT_POINTS_PER_DEGREE * (temp_c - TEMP_IDEAL_MAX), HEAT_MAX_POINTS)
    if temp_c < TEMP_IDEAL_MIN:
        return -min(COOL_POINTS_PER_DEGREE * (TEMP_IDEAL_MIN - temp_c), COOL_MAX_POINTS)
    return 0.0


def irrigation_points(hours_since: Optional[float]) -> float:
    """Cuánto mueve el umbral el tiempo desde el último riego."""
    if hours_since is None:
        return 0.0
    if hours_since < RECENT_IRRIGATION_HOURS:
        return RECENT_IRRIGATION_POINTS
    if hours_since > LONG_WITHOUT_IRRIGATION_HOURS:
        return LONG_WITHOUT_IRRIGATION_POINTS
    return 0.0


def recommend(inputs: Inputs) -> Recommendation:
    """La recomendación de riego para estas entradas."""
    heat = round(temperature_points(inputs.temp_c), 1)
    recent = irrigation_points(inputs.hours_since_irrigation)
    threshold = round(
        min(THRESHOLD_MAX, max(THRESHOLD_MIN, BASE_THRESHOLD + heat + recent)), 1
    )
    soil = inputs.soil_pct

    urgent = False
    if soil is None:
        outcome = Outcome.insufficient_data
    elif inputs.irrigating:
        outcome = Outcome.irrigating
    elif soil > SOIL_TOO_WET:
        outcome = Outcome.too_wet
    elif soil < threshold:
        outcome = Outcome.irrigate
        urgent = soil < THRESHOLD_MIN
    elif soil < threshold + WATCH_MARGIN:
        outcome = Outcome.irrigate_soon
    else:
        outcome = Outcome.no_irrigation

    tank_blocks = (
        outcome in (Outcome.irrigate, Outcome.irrigate_soon)
        and inputs.tank_pct is not None
        and inputs.tank_pct <= TANK_CRITICAL
    )
    return Recommendation(
        outcome=outcome,
        urgent=urgent,
        tank_blocks=tank_blocks,
        threshold=threshold,
        watch_until=round(threshold + WATCH_MARGIN, 1),
        base_threshold=BASE_THRESHOLD,
        temperature_points=heat,
        irrigation_points=recent,
    )
