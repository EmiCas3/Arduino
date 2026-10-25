"""
Catálogo de las mediciones del vivero: ÚNICA fuente de columna, etiqueta y
unidad en el backend.

Lo usan las últimas lecturas (DASH-01), las alertas (ALERT-01), las gráficas
históricas (DASH-02), el análisis de tendencias (AI-01) y la detección de
anomalías (AI-02). Todos hablan de la
misma medición con el mismo nombre y la misma unidad, y el nombre de columna
que llega a una consulta SQL sale SIEMPRE de aquí, nunca de texto del cliente.

Cada medición declara además:

- `raw_column`: la lectura ADC cruda del mismo momento, si la hay.
- `decimals`: con cuántos decimales tiene sentido promediarla y mostrarla.
  La temperatura se guarda con uno; los porcentajes son enteros, pero al
  promediar un bucket de la gráfica (DASH-02) un decimal dice más que
  redondear a entero, así que el promedio usa `decimals + 1`.
- `stable_band`: cuánto puede cambiar en el periodo analizado sin que AI-01
  lo llame subida ni bajada. Es un parámetro de ANÁLISIS, no un umbral del
  rábano: los umbrales viven en el firmware (app/firmware.py). Sale del
  ruido que se le ve a cada sensor en el prototipo y está pendiente de
  ajustar con datos de varios días (ver la nota de AI-01 en el README).
- `anomaly_floor`: lo mínimo que una medición tiene que alejarse de su
  patrón reciente para que AI-02 la marque como anomalía, aunque el sensor
  haya estado muy quieto (ver app/anomalies.py). También es un parámetro de
  análisis, no un umbral del rábano. `None` = esa medición no se revisa: la
  luz sube y baja sola con el sol y las nubes, y cada amanecer saldría como
  "inusual".
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class Metric:
    """Una medición consultable del vivero."""

    key: str                      # columna de `readings`
    label: str
    unit: str
    raw_column: Optional[str]
    decimals: int
    stable_band: float
    anomaly_floor: Optional[float] = None

    @property
    def average_decimals(self) -> int:
        """Decimales del promedio de un bucket: uno más que el del valor."""
        return self.decimals + 1


METRICS: Tuple[Metric, ...] = (
    Metric("temp_c", "Temperatura del aire", "°C", None, 1, 0.5, 1.0),
    Metric("hum_aire_pct", "Humedad del aire", "%", None, 0, 2.0, 4.0),
    Metric("suelo_pct", "Humedad de la tierra", "%", "suelo_raw", 0, 2.0, 4.0),
    Metric("nivel_pct", "Nivel del tanque", "%", "nivel_raw", 0, 2.0, 4.0),
    Metric("luz_nivel", "Luz", "nivel 0-1023", "luz_raw", 0, 25.0, None),
    Metric("co2_ppm", "CO₂", "ppm", None, 0, 30.0, 60.0),
)

BY_KEY: Dict[str, Metric] = {metric.key: metric for metric in METRICS}
KEYS: Tuple[str, ...] = tuple(metric.key for metric in METRICS)


def get(key: str) -> Optional[Metric]:
    """La medición con esa clave, o None si no existe (texto del cliente)."""
    return BY_KEY.get(key)
