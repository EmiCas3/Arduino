"""
Espejo de SOLO LECTURA de las constantes del firmware (vivero_sensoresV2.ino).

Los umbrales del rábano y los parámetros de riego viven en el código del
Arduino: él compara cada lectura, manda códigos como `TEMP_ALTA` o
`SUELO_SECO` y decide el riego automático. El backend no vuelve a comparar
ni a decidir nada; este módulo solo sirve para ANOTAR en cada alerta
(ALERT-01) qué umbral cruzó, y para EXPLICAR en la UI y en el registro de
riegos (ACT-02) con qué objetivo y tiempo máximo trabaja el Arduino.

Si alguien cambia un umbral en el `.ino`, `tests/test_firmware_mirror.py`
falla y dice qué línea de aquí corregir. Así el número que guarda la alerta
nunca se aleja del que usa el Arduino.

Solo se convierten en alerta los códigos de nivel ALERTA que salen de cruzar
un LÍMITE (decisión D1 del plan). Los avisos (`*_LEVE`, `TANQUE_MITAD`,
`SOMBRA_PROLONGADA`, `SOL_INSUFICIENTE`) siguen pintando las tarjetas, sin
crear alerta. Las fallas (`FALLA_*`) dicen que el sensor no respondió: no
son un cruce de umbral.
"""

from dataclasses import dataclass
from typing import Dict, Tuple

# Ruta del firmware dentro del repo (la usa el test del espejo).
FIRMWARE_PATH = "vivero_sensores/vivero_sensoresV2/vivero_sensoresV2.ino"

# Constantes del .ino que el backend necesita conocer, con el mismo nombre.
CONSTANTS: Dict[str, float] = {
    "TEMP_LIMITE_MIN": 6.0,
    "TEMP_LIMITE_MAX": 30.0,
    "HUMA_LIMITE_MIN": 50.0,
    "HUMA_LIMITE_MAX": 85.0,
    "SUELO_LIMITE_MIN": 50,
    "SUELO_LIMITE_MAX": 80,
    "NIVEL_PCT_CRITICO": 25,
    # Riego (ACT-02): el objetivo es SUELO_LIMITE_MIN
    "BOMBA_MAX_MS": 60_000,
    "BOMBA_DESCANSO_MS": 300_000,
}


@dataclass(frozen=True)
class LimitAlert:
    """Un código del Arduino que significa "esta medición cruzó su límite"."""

    code: str
    metric: str        # campo de la lectura (temp_c, suelo_pct…)
    direction: str     # "high" o "low"
    constant: str      # nombre de la constante en el .ino
    comparison: str    # cómo compara el firmware: ">", "<" o "<="

    @property
    def threshold(self) -> float:
        return CONSTANTS[self.constant]


LIMIT_ALERTS: Dict[str, LimitAlert] = {
    alert.code: alert
    for alert in (
        LimitAlert("TEMP_ALTA", "temp_c", "high", "TEMP_LIMITE_MAX", ">"),
        LimitAlert("TEMP_BAJA", "temp_c", "low", "TEMP_LIMITE_MIN", "<"),
        LimitAlert("HUM_AIRE_ALTA", "hum_aire_pct", "high", "HUMA_LIMITE_MAX", ">"),
        LimitAlert("HUM_AIRE_BAJA", "hum_aire_pct", "low", "HUMA_LIMITE_MIN", "<"),
        LimitAlert("SUELO_ENCHARCADO", "suelo_pct", "high", "SUELO_LIMITE_MAX", ">"),
        LimitAlert("SUELO_SECO", "suelo_pct", "low", "SUELO_LIMITE_MIN", "<"),
        LimitAlert("TANQUE_UN_CUARTO", "nivel_pct", "low", "NIVEL_PCT_CRITICO", "<="),
    )
}

# Mediciones que pueden tener alerta, en el orden en que se evalúan.
ALERT_METRICS: Tuple[str, ...] = ("temp_c", "hum_aire_pct", "suelo_pct", "nivel_pct")

# Códigos de falla y las mediciones que dejan sin dato confiable.
FAULT_CODES: Dict[str, Tuple[str, ...]] = {
    "FALLA_DHT": ("temp_c", "hum_aire_pct"),
    "FALLA_SUELO": ("suelo_pct",),
    "FALLA_NIVEL": ("nivel_pct",),
}
