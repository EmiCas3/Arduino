"""
AI-02 — Detección de condiciones inusuales (anomalías).

Una ALERTA (ALERT-01) dice "la medición cruzó el límite del rábano", y ese
límite es fijo y vive en el firmware. Una ANOMALÍA dice "la medición se salió
de lo que este sensor venía haciendo", aunque siga dentro de los límites. Es
el aviso temprano: la tierra llevaba toda la mañana en 62 % y de pronto marca
48 %, todavía lejos de SUELO_SECO, pero algo pasó (una fuga, un sensor suelto,
una bomba que no arrancó).

La regla, por medición y sobre la serie que ya deja lista el preprocesado de
AI-01 (`analysis.prepare`: promedio por bucket, `null` fuera, huecos sin
rellenar):

1. PATRÓN RECIENTE. Para cada bucket se toman los buckets con dato de las
   `BASELINE_BUCKETS` posiciones anteriores (una ventana de tiempo, no "los
   últimos N con dato": tras un hueco largo no se compara contra algo viejo).
   Con menos de `MIN_BASELINE_SAMPLES` no hay patrón y ese bucket no se juzga.
2. RANGO ESPERADO. Centro = mediana del patrón; ancho = `ROBUST_Z` veces la
   dispersión robusta (MAD × 1.4826, que equivale a una desviación estándar
   cuando el ruido es normal). Mediana y MAD en vez de promedio y desviación
   estándar porque una sola lectura rara dentro del patrón casi no los mueve.
   El ancho nunca baja de `anomaly_floor` de la medición (app/metrics.py): un
   sensor muy quieto tendría un rango de ±0 y cualquier parpadeo saldría.
3. ANOMALÍA. Un bucket fuera de su rango esperado (criterio 1); dentro de él,
   nada (criterio 3). Los buckets seguidos fuera de rango, en el mismo
   sentido, son UNA anomalía: se reporta el más alejado (valor, rango
   esperado y hora) y de cuándo a cuándo duró.

Una subida o bajada gradual (el día que se calienta) no sale: la mediana va
unos buckets atrás, pero la dispersión de ese mismo tramo es mayor que ese
retraso. Un cambio de nivel sostenido sale una vez y, cuando el patrón ya lo
incluye, deja de salir: es "lo nuevo normal".

El riego es la excepción conocida: con la bomba encendida la tierra se moja y
el tanque baja, que es justo lo esperado. Esas anomalías (tierra hacia arriba,
tanque hacia abajo, empezando durante un riego o poco después) se descartan
con `drop_irrigation_effects` y se cuentan aparte.

Igual que app/analysis.py: funciones puras, sin FastAPI ni BD, sin azar ni
reloj. Con las mismas lecturas y el mismo rango, las mismas anomalías.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from app.analysis import Prepared, Sample

# Tamaño del patrón reciente, en buckets. Con los buckets que usa la gráfica
# son 24 min en el rango de 6 h (buckets de 2 min) y 2 h en el de 24 h.
BASELINE_BUCKETS = 12

# Buckets con dato que hacen falta dentro de esa ventana para hablar de
# "patrón". Con menos, una sola lectura rara sería todo el patrón.
MIN_BASELINE_SAMPLES = 6

# Cuántas dispersiones robustas de distancia a la mediana se toleran. 3.5 es
# el corte usual del "z-score modificado" (Iglewicz y Hoaglin).
ROBUST_Z = 3.5

# MAD → desviación estándar equivalente, para ruido normal.
MAD_TO_SIGMA = 1.4826

# Tras apagar la bomba, la tierra y el tanque se siguen acomodando un rato.
IRRIGATION_SETTLE_SECONDS = 600


class AnomalyKind(str, Enum):
    """Hacia dónde se salió la medición de su patrón reciente."""

    high = "high"   # más alto de lo esperado
    low = "low"     # más bajo de lo esperado


# Lo que el riego mueve, y hacia dónde. Solo esas anomalías se le atribuyen.
IRRIGATION_EFFECTS: Dict[str, AnomalyKind] = {
    "suelo_pct": AnomalyKind.high,    # la tierra se moja
    "nivel_pct": AnomalyKind.low,     # el tanque se vacía
}


@dataclass(frozen=True)
class Expected:
    """Rango esperado de un bucket según su patrón reciente."""

    center: float
    low: float
    high: float


@dataclass(frozen=True)
class Anomaly:
    """Uno o varios buckets seguidos fuera de su rango, en el mismo sentido.

    `peak_*`, `value` y `expected` son del bucket más alejado del episodio:
    es el que mejor dice "cuánto" se salió.
    """

    kind: AnomalyKind
    start_epoch: int        # inicio del primer bucket fuera de rango
    end_epoch: int          # fin del último bucket fuera de rango
    buckets: int
    peak_epoch: int
    value: float
    expected: Expected


@dataclass(frozen=True)
class Detection:
    """Resultado de revisar UNA medición."""

    anomalies: Tuple[Anomaly, ...]
    evaluated: int          # buckets que tuvieron patrón con qué compararse


def expected_range(baseline: Sequence[float], floor: float) -> Expected:
    """Rango esperado a partir del patrón reciente (paso 2 de la regla)."""
    if not baseline:
        raise ValueError("el patrón necesita al menos un valor")
    center = _median(baseline)
    spread = MAD_TO_SIGMA * _median([abs(value - center) for value in baseline])
    half = max(ROBUST_Z * spread, floor)
    return Expected(center=center, low=center - half, high=center + half)


def detect(
    prepared: Prepared, floor: float, evaluate_from: int, decimals: int = 1
) -> Detection:
    """Anomalías de una medición ya preprocesada.

    `prepared` puede empezar antes del rango pedido: así los primeros buckets
    del rango ya tienen patrón. Solo se juzgan los buckets que empiezan en
    `evaluate_from` o después. `decimals` son los de la medición; el valor y
    el rango se redondean con uno más, como los promedios de la gráfica.
    """
    if floor < 0:
        raise ValueError("floor no puede ser negativo")
    bucket = prepared.bucket_seconds
    lookback = BASELINE_BUCKETS * bucket
    samples = prepared.samples

    flagged: List[Tuple[Sample, AnomalyKind, Expected]] = []
    evaluated = 0
    first = 0                     # primer bucket dentro de la ventana del patrón
    for index, sample in enumerate(samples):
        while samples[first].epoch < sample.epoch - lookback:
            first += 1
        if sample.epoch < evaluate_from:
            continue
        baseline = [previous.value for previous in samples[first:index]]
        if len(baseline) < MIN_BASELINE_SAMPLES:
            continue
        evaluated += 1
        expected = expected_range(baseline, floor)
        if sample.value > expected.high:
            flagged.append((sample, AnomalyKind.high, expected))
        elif sample.value < expected.low:
            flagged.append((sample, AnomalyKind.low, expected))

    return Detection(
        anomalies=tuple(_episodes(flagged, bucket, decimals + 1)),
        evaluated=evaluated,
    )


def _episodes(
    flagged: Sequence[Tuple[Sample, AnomalyKind, Expected]], bucket: int, digits: int
) -> Iterable[Anomaly]:
    """Junta los buckets seguidos y del mismo sentido en una sola anomalía."""
    run: List[Tuple[Sample, AnomalyKind, Expected]] = []
    for item in flagged:
        if run and (item[1] != run[-1][1] or item[0].epoch != run[-1][0].epoch + bucket):
            yield _anomaly(run, bucket, digits)
            run = []
        run.append(item)
    if run:
        yield _anomaly(run, bucket, digits)


def _anomaly(
    run: Sequence[Tuple[Sample, AnomalyKind, Expected]], bucket: int, digits: int
) -> Anomaly:
    kind = run[0][1]
    # El más alejado de su propio centro; en empate, el primero.
    peak, _, expected = max(run, key=lambda item: abs(item[0].value - item[2].center))
    return Anomaly(
        kind=kind,
        start_epoch=run[0][0].epoch,
        end_epoch=run[-1][0].epoch + bucket,
        buckets=len(run),
        peak_epoch=peak.epoch,
        value=round(peak.value, digits),
        expected=Expected(
            center=round(expected.center, digits),
            low=round(expected.low, digits),
            high=round(expected.high, digits),
        ),
    )


def irrigation_windows(
    events: Iterable[Tuple[int, bool]], until: int
) -> Tuple[Tuple[int, int], ...]:
    """Tramos (encendido, apagado) de la bomba a partir de sus eventos.

    `events` son (época Unix, encendida) en orden. Un riego que no se ha
    apagado llega hasta `until`. Eventos repetidos en el mismo estado no
    abren ni cierran nada.
    """
    windows: List[Tuple[int, int]] = []
    started: Optional[int] = None
    for epoch, is_on in events:
        if is_on and started is None:
            started = epoch
        elif not is_on and started is not None:
            windows.append((started, epoch))
            started = None
    if started is not None:
        windows.append((started, max(started, until)))
    return tuple(windows)


def drop_irrigation_effects(
    anomalies: Sequence[Anomaly],
    metric_key: str,
    windows: Sequence[Tuple[int, int]],
    bucket_seconds: int,
) -> Tuple[Tuple[Anomaly, ...], int]:
    """Quita las anomalías que explica un riego; devuelve (las que quedan, cuántas se quitaron).

    Solo cuentan las del sentido que mueve el riego (IRRIGATION_EFFECTS) cuyo
    PRIMER bucket se cruza con un riego o con los IRRIGATION_SETTLE_SECONDS
    siguientes. Si la anomalía empezó antes del riego, no la explica el riego.
    """
    effect = IRRIGATION_EFFECTS.get(metric_key)
    if effect is None or not windows:
        return tuple(anomalies), 0

    def explained(anomaly: Anomaly) -> bool:
        start, end = anomaly.start_epoch, anomaly.start_epoch + bucket_seconds
        return anomaly.kind == effect and any(
            start < off + IRRIGATION_SETTLE_SECONDS and on < end for on, off in windows
        )

    kept = tuple(anomaly for anomaly in anomalies if not explained(anomaly))
    return kept, len(anomalies) - len(kept)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
