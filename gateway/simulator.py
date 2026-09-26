"""
Arduino simulado, para probar y hacer la demo sin el hardware.

Imita a vivero_sensoresV2.ino: una línea JSON cada 2 s, con los mismos campos,
la misma calibración y las mismas reglas de alertas del rábano. Como el
Arduino real al abrir el puerto, la PRIMERA línea llega cortada.

    python -m gateway simulate           gateway completo con este Arduino
    python -m gateway simulate --print   solo imprime las líneas (como el
                                         Monitor Serie del IDE de Arduino)
"""

from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime
from typing import Callable, List, Optional

# ── Mismas constantes que el firmware ──────────────────────────────────
SUELO_EN_AIRE, SUELO_EN_AGUA = 555, 267
NIVEL_VACIO, NIVEL_LLENO = 0, 200
SUELO_MIN_VALIDO, SUELO_MAX_VALIDO = 100, 1013

TEMP_IDEAL = (20.0, 25.0)
TEMP_LIMITE = (6.0, 30.0)
HUMA_IDEAL = (60.0, 80.0)
HUMA_LIMITE = (50.0, 85.0)
SUELO_IDEAL = (60, 65)
SUELO_LIMITE = (50, 80)
NIVEL_PCT_AVISO, NIVEL_PCT_CRITICO = 50, 25
LUZ_UMBRAL_DIA, LUZ_UMBRAL_SOL = 250, 600


def a_porcentaje(crudo: int, en_cero: int, en_cien: int) -> int:
    """map() + constrain() de Arduino (división entera truncada)."""
    p = int((crudo - en_cero) * 100 / (en_cien - en_cero))
    return max(0, min(100, p))


def evaluar(temp, hum, suelo_pct, nivel_pct) -> List[str]:
    """Las mismas reglas de alertas que evaluar*() del firmware."""
    alertas = []
    if temp is None:
        alertas.append("FALLA_DHT")
    elif temp > TEMP_LIMITE[1]:
        alertas.append("TEMP_ALTA")
    elif temp < TEMP_LIMITE[0]:
        alertas.append("TEMP_BAJA")
    elif temp > TEMP_IDEAL[1]:
        alertas.append("TEMP_ALTA_LEVE")
    elif temp < TEMP_IDEAL[0]:
        alertas.append("TEMP_BAJA_LEVE")

    if hum is not None:
        if hum > HUMA_LIMITE[1]:
            alertas.append("HUM_AIRE_ALTA")
        elif hum < HUMA_LIMITE[0]:
            alertas.append("HUM_AIRE_BAJA")
        elif hum > HUMA_IDEAL[1]:
            alertas.append("HUM_AIRE_ALTA_LEVE")
        elif hum < HUMA_IDEAL[0]:
            alertas.append("HUM_AIRE_BAJA_LEVE")

    if suelo_pct is None:
        alertas.append("FALLA_SUELO")
    elif suelo_pct > SUELO_LIMITE[1]:
        alertas.append("SUELO_ENCHARCADO")
    elif suelo_pct < SUELO_LIMITE[0]:
        alertas.append("SUELO_SECO")
    elif suelo_pct > SUELO_IDEAL[1]:
        alertas.append("SUELO_HUMEDO_LEVE")
    elif suelo_pct < SUELO_IDEAL[0]:
        alertas.append("SUELO_SECO_LEVE")

    if nivel_pct is None:
        alertas.append("FALLA_NIVEL")
    elif nivel_pct <= NIVEL_PCT_CRITICO:
        alertas.append("TANQUE_UN_CUARTO")
    elif nivel_pct <= NIVEL_PCT_AVISO:
        alertas.append("TANQUE_MITAD")
    return alertas


GRAVES = {
    "FALLA_DHT", "TEMP_ALTA", "TEMP_BAJA", "HUM_AIRE_ALTA", "HUM_AIRE_BAJA",
    "FALLA_SUELO", "SUELO_ENCHARCADO", "SUELO_SECO", "FALLA_NIVEL", "TANQUE_UN_CUARTO",
}


class SimulatedArduino:
    """Se usa como un puerto Serial: readline() y close()."""

    def __init__(
        self,
        interval: float = 2.0,
        failures: bool = False,
        seed: Optional[int] = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.interval = interval
        self.failures = failures
        self.rng = random.Random(seed)
        self.sleep = sleep
        self.now = now
        self.first = True
        self.ms = 2000                 # el firmware espera 2 s al arrancar
        self.suelo_raw = 382.0         # ~60 % de humedad
        self.nivel_raw = 165.0         # ~82 % del tanque
        self.sol_ms = 0
        self.bomba_activa = False

    # ── Física de juguete ──────────────────────────────────────────────

    def _step(self) -> dict:
        rng = self.rng
        self.ms += int(self.interval * 1000)
        moment = self.now()
        hour = moment.hour + moment.minute / 60

        es_dia_real = 7 <= hour < 19
        temp = 22.5 + 2.8 * math.sin((hour - 9) / 24 * 2 * math.pi) + rng.gauss(0, 0.2)
        hum = 70 - (temp - 22.5) * 3 + rng.gauss(0, 0.8)
        if self.failures and rng.random() < 0.03:
            temp = hum = None          # el DHT11 falla de vez en cuando

        # La tierra se seca poco a poco; la bomba la moja y vacía el tanque.
        if self.bomba_activa:
            self.suelo_raw -= 6.0
            self.nivel_raw = max(0.0, self.nivel_raw - 0.8)
        else:
            self.suelo_raw += 0.15
        self.suelo_raw = max(SUELO_EN_AGUA, min(SUELO_EN_AIRE + 10, self.suelo_raw))
        suelo_raw = int(self.suelo_raw + rng.gauss(0, 1))
        nivel_raw = max(0, int(self.nivel_raw + rng.gauss(0, 1)))

        luz_nivel = int((720 if es_dia_real else 60) + rng.gauss(0, 15))
        luz_nivel = max(0, min(1023, luz_nivel))
        es_dia = luz_nivel > LUZ_UMBRAL_DIA
        if es_dia and luz_nivel >= LUZ_UMBRAL_SOL:
            self.sol_ms += int(self.interval * 1000)

        suelo_ok = SUELO_MIN_VALIDO <= suelo_raw <= SUELO_MAX_VALIDO
        suelo_pct = a_porcentaje(suelo_raw, SUELO_EN_AIRE, SUELO_EN_AGUA) if suelo_ok else None
        nivel_pct = a_porcentaje(nivel_raw, NIVEL_VACIO, NIVEL_LLENO)

        bomba_habilitada = nivel_pct > NIVEL_PCT_CRITICO
        riego_sugerido = suelo_pct is not None and suelo_pct < SUELO_LIMITE[0]
        # Con histéresis de juguete: riega hasta llegar al ideal.
        if riego_sugerido and bomba_habilitada:
            self.bomba_activa = True
        elif self.bomba_activa and (suelo_pct is None or suelo_pct >= SUELO_IDEAL[0] or not bomba_habilitada):
            self.bomba_activa = False
        vent_alta = (temp is not None and temp > TEMP_IDEAL[1]) or (hum is not None and hum > HUMA_IDEAL[1])

        alertas = evaluar(temp, hum, suelo_pct, nivel_pct)
        estado = "OK" if not alertas else ("ALERTA" if GRAVES & set(alertas) else "AVISO")
        return {
            "ms": self.ms,
            "temp_c": None if temp is None else round(temp, 1),
            "hum_aire_pct": None if hum is None else round(hum, 1),
            "suelo_raw": suelo_raw,
            "suelo_pct": suelo_pct,
            "nivel_raw": nivel_raw,
            "nivel_pct": nivel_pct,
            "luz_raw": 1023 - luz_nivel,      # LUZ_INVERTIDA
            "luz_nivel": luz_nivel,
            "es_dia": es_dia,
            "sol_min_hoy": self.sol_ms // 60000,
            "sol_min_prev": None,
            "riego_sugerido": riego_sugerido,
            "bomba_habilitada": bomba_habilitada,
            "bomba_activa": self.bomba_activa,
            "vent": "ALTA" if vent_alta else "BASE",
            "vent_activo": vent_alta,
            "alertas": alertas,
            "estado": estado,
        }

    def line(self) -> str:
        return json.dumps(self._step(), separators=(",", ":"))

    # ── Interfaz de puerto Serial ──────────────────────────────────────

    def readline(self) -> bytes:
        if self.first:
            # Como el Uno real: al abrir el puerto se reinicia y llega un pedazo.
            self.first = False
            return self.line()[25:].encode() + b"\r\n"
        self.sleep(self.interval)
        return self.line().encode() + b"\r\n"

    def close(self) -> None:
        pass
