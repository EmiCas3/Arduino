"""
Rango y resolución de una consulta histórica (DASH-02 y AI-01).

Dos decisiones viven aquí, iguales para la gráfica y para el análisis:

1. QUÉ VENTANA se consulta. `from` y `to` son opcionales: sin ellos se usan
   las últimas DEFAULT_SPAN_HOURS. Se normaliza todo a UTC y se rechaza un
   rango invertido o más largo que MAX_SPAN_DAYS (una consulta de años sobre
   SQLite no termina a tiempo y no es lo que el dashboard necesita).

2. CON QUÉ RESOLUCIÓN se devuelve. El Arduino imprime una lectura cada 2 s
   (INTERVALO_MS del firmware) y la Pi las manda en lotes cada 10 s
   (SG_SEND_INTERVAL), así que un día son ~43 000 lecturas: dibujarlas todas
   deja una mancha, no una gráfica (criterio 3 de DASH-02). `choose_bucket`
   elige el bucket más fino cuya cuenta de puntos quepa en `max_points`.

Los buckets se alinean con la época Unix (múltiplos exactos del tamaño), no
con el inicio del rango: así dos consultas del mismo periodo con rangos
ligeramente distintos caen en los MISMOS buckets y el análisis de AI-01 es
reproducible (su criterio 2).

Sin dependencias de FastAPI ni de la BD: se puede probar con pytest sin
levantar la app.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

# Ventana por defecto y máxima de una consulta histórica.
DEFAULT_SPAN_HOURS = 24
MAX_SPAN_DAYS = 90

# Puntos que se devuelven como máximo. 240 es lo que se distingue a simple
# vista en una gráfica del ancho de una tarjeta (~4 px por punto en escritorio).
DEFAULT_MAX_POINTS = 240
MIN_MAX_POINTS = 10
MAX_MAX_POINTS = 1000

# Intervalo nativo entre lecturas, según el firmware (INTERVALO_MS = 2000).
READING_INTERVAL_SECONDS = 2

# Tamaños de bucket que se ofrecen, en segundos. Son divisores "redondos" de
# minuto, hora y día para que el eje de tiempo caiga en horas en punto.
BUCKET_CHOICES: Tuple[int, ...] = (
    10, 20, 30, 60, 120, 300, 600, 900, 1800,
    3600, 7200, 14400, 21600, 43200, 86400,
)
MIN_BUCKET_SECONDS = BUCKET_CHOICES[0]
MAX_BUCKET_SECONDS = BUCKET_CHOICES[-1]


class RangeError(ValueError):
    """El rango pedido no es usable. El router lo convierte en 422."""


@dataclass(frozen=True)
class Window:
    """Ventana de tiempo ya normalizada: `start` incluido, `end` excluido."""

    start: datetime
    end: datetime

    @property
    def span_seconds(self) -> int:
        return int((self.end - self.start).total_seconds())

    @property
    def span_hours(self) -> float:
        return self.span_seconds / 3600

    @property
    def start_epoch(self) -> int:
        return int(self.start.timestamp())

    @property
    def end_epoch(self) -> int:
        return int(self.end.timestamp())


def _utc(value: datetime) -> datetime:
    """Un datetime llevado a UTC.

    Los que manda el cliente ya llegan con zona (parse_window rechaza los que
    no la traen). Uno sin zona solo puede venir de código nuestro; se asume
    UTC antes que adivinar la zona del servidor.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _has_zone(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def parse_window(
    start: Optional[datetime],
    end: Optional[datetime],
    now: datetime,
    span_hours: int = DEFAULT_SPAN_HOURS,
) -> Window:
    """Ventana a consultar a partir de lo que pidió el cliente.

    - Sin `to`: hasta ahora. Sin `from`: `span_hours` antes de `to`.
    - `from` y `to` deben traer zona horaria, igual que el `ts` de la
      ingesta: FastAPI acepta "2026-10-15T00:00:00" sin zona, y tomarla como
      UTC corre 6 h el rango de alguien que escribió su hora de México.
    - `from` igual o posterior a `to` es un error, no un rango vacío: el
      cliente se equivocó y hay que decírselo (criterio 2 de DASH-02 es para
      un rango válido SIN datos, que es otra cosa).
    - Una fecha en los extremos del calendario (año 1 o 9999) no se puede
      restar ni pasar a UTC: también es un rango inválido, no un error 500.
    """
    for name, value in (("from", start), ("to", end)):
        if value is not None and not _has_zone(value):
            raise RangeError(
                f"`{name}` debe llevar zona horaria (por ejemplo "
                "2026-10-15T00:00:00Z o 2026-10-15T00:00:00-06:00)."
            )
    try:
        to = _utc(end) if end is not None else _utc(now)
        since = _utc(start) if start is not None else to - timedelta(hours=span_hours)
    except (OverflowError, ValueError):
        raise RangeError("La fecha está fuera del rango que se puede consultar.") from None

    if since >= to:
        raise RangeError("`from` debe ser anterior a `to`.")
    if (to - since) > timedelta(days=MAX_SPAN_DAYS):
        raise RangeError(f"El rango no puede pasar de {MAX_SPAN_DAYS} días.")
    return Window(start=since, end=to)


def choose_bucket(
    span_seconds: int,
    max_points: int = DEFAULT_MAX_POINTS,
    interval_seconds: int = READING_INTERVAL_SECONDS,
) -> int:
    """Tamaño de bucket en segundos; 0 = devolver las lecturas sin agregar.

    Devuelve 0 cuando en el rango no caben más de `max_points` lecturas ni en
    el peor caso (todas presentes): ahí agregar solo perdería detalle.

    Los buckets van alineados con la época, no con el inicio del rango, así
    que un rango de `n` buckets de largo puede tocar `n + 1` (el primero y el
    último a medias). Ese bucket de más se cuenta: por ejemplo, 7 días con
    `max_points=168` daban buckets de 1 h y 169 puntos.
    """
    max_points = max(MIN_MAX_POINTS, min(MAX_MAX_POINTS, max_points))
    span_seconds = max(1, span_seconds)
    if span_seconds / max(1, interval_seconds) <= max_points:
        return 0
    for bucket in BUCKET_CHOICES:
        if math.ceil(span_seconds / bucket) + 1 <= max_points:
            return bucket
    # Rango más largo que MAX_SPAN_DAYS (no debería llegar aquí): se redondea
    # a días completos para no devolver un bucket de tamaño arbitrario.
    days = math.ceil(span_seconds / (max_points - 1) / 86400)
    return days * 86400


def smallest_bucket_for(window: "Window", max_points: int) -> int:
    """El bucket más fino cuya rejilla sobre la ventana cabe en `max_points`.

    Para rangos cortos en los que llegan lecturas más seguido de lo normal
    (el simulador en time-lapse, o dos gateways en el mismo vivero):
    `choose_bucket` diría "caben crudas" contando una cada 2 s, y no es así.
    """
    for bucket in BUCKET_CHOICES:
        if grid(window, bucket).count <= max_points:
            return bucket
    return MAX_BUCKET_SECONDS


@dataclass(frozen=True)
class Grid:
    """Los buckets (alineados con la época) que cubren TODA una ventana.

    `start_epoch` es el inicio del bucket donde cae `from` y `end_epoch` el
    final del bucket donde cae el último instante antes de `to`. Como `from`
    casi nunca cae justo en un bucket, el primero y el último pueden quedar
    a medias: se cuentan igual, porque ahí están las lecturas de las orillas
    del rango (las más nuevas, en el último).
    """

    start_epoch: int
    end_epoch: int
    bucket_seconds: int

    @property
    def count(self) -> int:
        return (self.end_epoch - self.start_epoch) // self.bucket_seconds


def grid(window: Window, bucket_seconds: int) -> Grid:
    """Rejilla de buckets de `bucket_seconds` que cubre la ventana completa.

    Es la misma cuenta que hace SQLite al agrupar (`strftime('%s', ts) / b`):
    una lectura en el segundo `s` cae en el bucket `⌊s / b⌋ · b`. La última
    lectura posible es la del último segundo entero antes de `to` (`to` no se
    incluye), así que el último bucket es el que contiene `⌈to⌉ − 1`.
    """
    if bucket_seconds <= 0:
        raise ValueError("bucket_seconds debe ser mayor que 0")
    first_second = math.floor(window.start.timestamp())
    last_second = max(first_second, math.ceil(window.end.timestamp()) - 1)
    start = (first_second // bucket_seconds) * bucket_seconds
    end = (last_second // bucket_seconds + 1) * bucket_seconds
    return Grid(start_epoch=start, end_epoch=end, bucket_seconds=bucket_seconds)


def from_epoch(epoch: int) -> datetime:
    """Inicio de un bucket (segundos Unix) como datetime en UTC."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def ts_text(value: datetime) -> str:
    """Un límite del rango en el MISMO formato con el que se guarda `ts`.

    La ingesta escribe siempre UTC con milisegundos (app/routers/ingest.py), y
    por eso comparar `ts` como texto ordena igual que comparar fechas y usa el
    índice ix_readings_greenhouse_ts. Un límite escrito de otra forma (con
    microsegundos, o con otro desfase) rompería esa comparación.
    """
    return _utc(value).isoformat(timespec="milliseconds")
