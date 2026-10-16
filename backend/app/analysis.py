"""
AI-01 — Análisis de la información histórica del vivero.

Las dos tasks del item, una función cada una:

1. `prepare()` — PREPROCESADO. De lecturas crudas a una serie regular y
   comparable: se descartan las que no son dato (`null` = sensor caído, la
   convención del contrato), se promedia por bucket de tiempo y los buckets
   SIN lectura **no se rellenan**: se devuelven como huecos. Rellenar un hueco
   con el valor anterior inventaría una tendencia que nadie midió, y el
   criterio 3 del item pide justo lo contrario: que los huecos se reporten.

2. `trend()` — TENDENCIA. Sobre esa serie, mínimos cuadrados (regresión
   lineal simple) → pendiente por hora, cambio estimado en el periodo y un
   indicador por variable: `rising`, `stable` o `falling` (criterio 1).
   `stable` no es "pendiente exactamente cero": es "el cambio en el periodo no
   pasa de la banda estable de esa medición" (app/metrics.py), porque todos
   estos sensores tienen ruido y una pendiente diminuta no es una tendencia.

Determinismo (criterio 2 del item): el resultado sale SOLO de las lecturas
guardadas, del rango pedido y del tamaño de bucket. No hay azar, no se mira el
reloj y no se depende del orden de ningún diccionario. Con la misma entrada,
la misma salida — también los redondeos. `generated_at` es metadato de la
respuesta HTTP, no entra en el cálculo.

Por qué mínimos cuadrados y no "último menos primero": una sola lectura rara
al final del rango cambiaría el resultado por completo. La recta usa todos los
buckets, y `r2` dice qué tan recta es de verdad la serie (0 = la recta no
explica nada, 1 = los puntos están sobre la recta). AI-04 (varias variables) y
AI-05 (explicación en lenguaje natural) se construyen sobre esto; aquí NO se
redacta ninguna explicación ni se recomienda nada.

Sin dependencias de FastAPI ni de la BD: todo son funciones puras sobre
tuplas, probables con pytest sin levantar la app.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, List, Optional, Sequence, Tuple

from app.history import Window, bucket_count


class TrendDirection(str, Enum):
    """Indicador de tendencia de una variable en el periodo analizado."""

    rising = "rising"                        # sube más que su banda estable
    falling = "falling"                      # baja más que su banda estable
    stable = "stable"                        # se mueve dentro de la banda
    insufficient_data = "insufficient_data"  # no hay con qué sostener nada


# Buckets con dato que hacen falta para hablar de tendencia. Con uno solo no
# hay pendiente; con dos, cualquier parpadeo del sensor parece una tendencia.
MIN_SAMPLES_FOR_TREND = 3


@dataclass(frozen=True)
class Sample:
    """Un bucket con dato: su inicio (época Unix) y el promedio de sus lecturas."""

    epoch: int
    value: float
    count: int


@dataclass(frozen=True)
class Gap:
    """Tramo seguido del rango sin ninguna lectura de esa medición."""

    start_epoch: int
    end_epoch: int
    buckets: int

    @property
    def seconds(self) -> int:
        return self.end_epoch - self.start_epoch


@dataclass(frozen=True)
class Prepared:
    """Serie lista para analizar, con lo que se descartó y lo que falta.

    `samples` va ordenada por tiempo y sin huecos rellenados: los huecos están
    en `gaps`. `expected_samples` es cuántos buckets cubren el rango pedido,
    así `coverage_pct` dice qué parte del periodo se midió de verdad.
    """

    bucket_seconds: int
    samples: Tuple[Sample, ...]
    gaps: Tuple[Gap, ...]
    expected_samples: int
    readings_used: int      # lecturas con valor que entraron al promedio
    readings_missing: int   # lecturas en el rango con esta medición en null

    @property
    def sample_count(self) -> int:
        return len(self.samples)

    @property
    def coverage_pct(self) -> float:
        """Porcentaje de buckets del rango que sí traen dato."""
        if self.expected_samples <= 0:
            return 0.0
        return round(100 * len(self.samples) / self.expected_samples, 1)

    @property
    def gap_seconds(self) -> int:
        return sum(gap.seconds for gap in self.gaps)

    @property
    def has_data(self) -> bool:
        return bool(self.samples)


@dataclass(frozen=True)
class Trend:
    """Resultado del análisis de una variable."""

    direction: TrendDirection
    slope_per_hour: Optional[float]  # unidades de la medición por hora
    change: Optional[float]          # cambio que estima la recta en el periodo
    observed_hours: Optional[float]  # horas entre el primer y el último bucket
    first: Optional[float]
    last: Optional[float]
    mean: Optional[float]
    minimum: Optional[float]
    maximum: Optional[float]
    fit_r2: Optional[float]          # 0 a 1: qué tan recta es la serie


def prepare(
    buckets: Iterable[Tuple[int, float, int]],
    window: Window,
    bucket_seconds: int,
    readings_total: int = 0,
    readings_valid: int = 0,
) -> Prepared:
    """Preprocesa los buckets de UNA medición (task 1 de AI-01).

    `buckets` son tripletas (inicio del bucket en época Unix, promedio,
    lecturas que entraron al promedio), tal como las agrupa SQLite con los
    `null` ya fuera. Aquí se ordena, se alinea a la rejilla del rango, se
    tiran los buckets vacíos o fuera de rango y se calculan los huecos.

    `readings_total` y `readings_valid` son el total de lecturas del rango y
    cuántas traían esta medición: su diferencia son las que el sensor no dio y
    se reportan como `readings_missing` (criterio 3). Si no se pasan, se
    deducen de los buckets.
    """
    if bucket_seconds <= 0:
        raise ValueError("bucket_seconds debe ser mayor que 0")

    grid_start = (window.start_epoch // bucket_seconds) * bucket_seconds
    expected = bucket_count(window.span_seconds, bucket_seconds)
    grid_end = grid_start + expected * bucket_seconds

    samples: List[Sample] = []
    for epoch, value, count in buckets:
        if count <= 0 or value is None:
            continue                      # bucket sin dato de esta medición
        if not (grid_start <= epoch < grid_end):
            continue                      # fuera de la rejilla del rango
        samples.append(Sample(epoch=int(epoch), value=float(value), count=int(count)))
    samples.sort(key=lambda sample: sample.epoch)

    used = sum(sample.count for sample in samples)
    valid = readings_valid or used
    missing = max(0, readings_total - valid) if readings_total else 0

    return Prepared(
        bucket_seconds=bucket_seconds,
        samples=tuple(samples),
        gaps=_find_gaps(samples, grid_start, grid_end, bucket_seconds),
        expected_samples=expected,
        readings_used=used,
        readings_missing=missing,
    )


def _find_gaps(
    samples: Sequence[Sample], grid_start: int, grid_end: int, bucket: int
) -> Tuple[Gap, ...]:
    """Tramos seguidos de buckets vacíos dentro del rango.

    Cuentan también el tramo del principio y el del final: "no hay datos de
    las primeras tres horas de lo que pediste" es información, no un detalle.
    Sin ninguna muestra, el hueco es todo el rango.
    """
    if grid_end <= grid_start:
        return ()
    if not samples:
        return (Gap(grid_start, grid_end, (grid_end - grid_start) // bucket),)

    gaps: List[Gap] = []
    cursor = grid_start
    for sample in samples:
        if sample.epoch > cursor:
            gaps.append(Gap(cursor, sample.epoch, (sample.epoch - cursor) // bucket))
        cursor = sample.epoch + bucket
    if grid_end > cursor:
        gaps.append(Gap(cursor, grid_end, (grid_end - cursor) // bucket))
    return tuple(gaps)


def trend(prepared: Prepared, stable_band: float, decimals: int = 1) -> Trend:
    """Tendencia de una variable ya preprocesada (task 2 de AI-01).

    `stable_band` es cuánto puede cambiar en el periodo sin que se llame
    subida ni bajada (app/metrics.py). `decimals` son los de la medición: los
    valores se redondean con ellos y la pendiente con dos más, porque es un
    cambio por hora y suele ser pequeño.
    """
    samples = prepared.samples
    if not samples:
        return Trend(
            direction=TrendDirection.insufficient_data,
            slope_per_hour=None, change=None, observed_hours=None,
            first=None, last=None, mean=None, minimum=None, maximum=None, fit_r2=None,
        )

    values = [sample.value for sample in samples]
    first, last = values[0], values[-1]
    observed_hours = (samples[-1].epoch - samples[0].epoch) / 3600

    base = dict(
        first=round(first, decimals),
        last=round(last, decimals),
        mean=round(sum(values) / len(values), decimals + 1),
        minimum=round(min(values), decimals),
        maximum=round(max(values), decimals),
        observed_hours=round(observed_hours, 3),
    )

    fit = _least_squares([(sample.epoch - samples[0].epoch) / 3600 for sample in samples], values)
    if len(samples) < MIN_SAMPLES_FOR_TREND or fit is None:
        # Hay valores (sirven para el mínimo, el máximo y el promedio), pero no
        # para sostener una dirección: se dice, no se adivina.
        return Trend(
            direction=TrendDirection.insufficient_data,
            slope_per_hour=None, change=None, fit_r2=None, **base,
        )

    slope, r2 = fit
    change = slope * observed_hours
    if change > stable_band:
        direction = TrendDirection.rising
    elif change < -stable_band:
        direction = TrendDirection.falling
    else:
        direction = TrendDirection.stable

    return Trend(
        direction=direction,
        slope_per_hour=round(slope, decimals + 2),
        change=round(change, decimals + 1),
        fit_r2=round(r2, 3),
        **base,
    )


def _least_squares(
    hours: Sequence[float], values: Sequence[float]
) -> Optional[Tuple[float, float]]:
    """Pendiente por hora y r² de la recta que mejor ajusta los puntos.

    None si todos los puntos caen en el mismo instante (no hay pendiente que
    calcular). Si la serie es plana, r² se reporta como 1: la recta —
    horizontal — explica los datos perfectamente.
    """
    n = len(hours)
    if n < 2:
        return None
    mean_x = sum(hours) / n
    mean_y = sum(values) / n
    sxx = sum((x - mean_x) ** 2 for x in hours)
    if sxx == 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(hours, values))
    slope = sxy / sxx
    syy = sum((y - mean_y) ** 2 for y in values)
    r2 = 1.0 if syy == 0 else max(0.0, min(1.0, (sxy * sxy) / (sxx * syy)))
    return slope, r2
