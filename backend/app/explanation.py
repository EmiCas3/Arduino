"""
AI-05 — Explicación en lenguaje claro de la recomendación de riego.

Convierte el resultado de app/recommendation.py en frases para una persona que
no conoce el sistema: qué se recomienda, qué mediciones lo causaron (criterio
2) y qué dato faltó. Sin identificadores, fórmulas ni códigos (criterio 3):
tampoco dice "umbral" ni "mediana", sino "la humedad en la que conviene regar".

Solo redacta: la decisión ya la tomó recommend(), y los números salen de sus
entradas y su resultado, así que el texto no se puede contradecir con ellos.

`is_explained()` es la regla del criterio 4: lo que no pasa no se muestra ni se
guarda (lo aplica app/routers/recommendation.py).

Funciones puras, como app/recommendation.py: sin FastAPI, sin BD, sin reloj.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import List, Optional

from app.recommendation import SOIL_TOO_WET, THRESHOLD_MIN, Inputs, Outcome, Recommendation

SUMMARIES = {
    Outcome.irrigate: "Conviene regar ahora.",
    Outcome.irrigate_soon: "Conviene regar pronto.",
    Outcome.no_irrigation: "Por ahora no hace falta regar.",
    Outcome.too_wet: "No conviene regar: la tierra tiene demasiada agua.",
    Outcome.irrigating: "Ya se está regando.",
    Outcome.insufficient_data: "Por ahora no se puede dar una recomendación.",
}
URGENT_SUMMARY = "Conviene regar cuanto antes."


@dataclass(frozen=True)
class Explanation:
    summary: str         # qué se recomienda
    reasons: List[str]   # las mediciones que lo causaron
    notes: List[str]     # lo que faltó medir y lo que impide regar


def is_explained(explanation: Optional[Explanation]) -> bool:
    """Criterio 4: sin qué se recomienda y por qué, no hay explicación."""
    return (
        explanation is not None
        and bool(explanation.summary.strip())
        and any(reason.strip() for reason in explanation.reasons)
    )


def _number(value: float) -> str:
    """58.0 → "58"; 61.5 → "61.5"."""
    return f"{value:g}"


def _elapsed(hours: float) -> str:
    """"hace 30 minutos", "hace 6 horas", "hace 3 días"."""
    minutes = floor(hours * 60)
    if minutes < 1:
        return "hace menos de 1 minuto"
    if minutes < 60:
        return f"hace {minutes} minuto{'' if minutes == 1 else 's'}"
    if hours < 48:
        whole = floor(hours)
        return f"hace {whole} hora{'' if whole == 1 else 's'}"
    return f"hace {floor(hours / 24)} días"


def _soil_reason(soil: float, result: Recommendation) -> str:
    moisture = f"La tierra está en {_number(soil)} % de humedad"
    point = f"{_number(result.threshold)} %"
    if result.outcome == Outcome.too_wet:
        return f"{moisture}, más de lo que le conviene al rábano ({_number(SOIL_TOO_WET)} %)."
    if result.urgent:
        return f"{moisture}, menos del mínimo que aguanta el rábano ({_number(THRESHOLD_MIN)} %)."
    if result.outcome == Outcome.irrigate:
        return f"{moisture}, y en este momento conviene regar con menos de {point}."
    if result.outcome == Outcome.irrigate_soon:
        return f"{moisture}, muy cerca de {point}, la humedad en la que conviene regar."
    return f"{moisture}, por encima de {point}, la humedad en la que conviene regar."


def _temperature_reason(temp_c: float, result: Recommendation) -> str:
    temperature = f"La temperatura está en {_number(temp_c)} °C"
    if result.temperature_points > 0:
        return f"{temperature}: con calor la tierra se seca más rápido y conviene regar un poco antes."
    if result.temperature_points < 0:
        return f"{temperature}: con frío la tierra se seca más lento y se puede esperar un poco más."
    return f"{temperature}, dentro de lo ideal, así que no cambia el momento de regar."


def _irrigation_reason(hours: float, result: Recommendation) -> str:
    moment = f"El último riego fue {_elapsed(hours)}"
    if result.irrigation_points < 0:
        return f"{moment}: el agua todavía está bajando y conviene esperar antes de volver a regar."
    if result.irrigation_points > 0:
        return f"{moment}: lleva mucho sin agua y conviene regar un poco antes."
    return f"{moment}, así que no cambia el momento de regar."


def explain(inputs: Inputs, result: Recommendation) -> Explanation:
    """La explicación de `result`, que recommend() sacó de `inputs`."""
    summary = URGENT_SUMMARY if result.urgent else SUMMARIES[result.outcome]

    if inputs.soil_pct is None:
        # La tierra es lo que decide: sin ella, lo demás no causó nada.
        return Explanation(
            summary=summary,
            reasons=["No hay una lectura reciente de la humedad de la tierra, que es el dato principal."],
            notes=[],
        )

    reasons = [_soil_reason(inputs.soil_pct, result)]
    notes: List[str] = []
    if result.outcome == Outcome.irrigating:
        reasons.insert(0, "La bomba de riego está encendida.")
    elif result.outcome != Outcome.too_wet:
        if inputs.temp_c is None:
            notes.append("No hay una lectura reciente de la temperatura, así que no se tomó en cuenta.")
        else:
            reasons.append(_temperature_reason(inputs.temp_c, result))
        if inputs.hours_since_irrigation is None:
            notes.append("No hay registro de riegos anteriores, así que no se tomaron en cuenta.")
        else:
            reasons.append(_irrigation_reason(inputs.hours_since_irrigation, result))

    if result.tank_blocks:
        notes.append(
            f"El tanque está en {_number(inputs.tank_pct)} %: con tan poca agua el sistema "
            "no dejaría regar. Hay que llenarlo primero."
        )
    return Explanation(summary=summary, reasons=reasons, notes=notes)
