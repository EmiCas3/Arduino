"""
Arduino simulado, para probar y hacer la demo sin el hardware.

Imita a vivero_sensoresV2.ino: una línea JSON cada 2 s, con los mismos campos,
la misma calibración y las mismas reglas de alertas del rábano. Como el
Arduino real al abrir el puerto, la PRIMERA línea llega cortada (y su estado
vuelve al de arranque: modo AUTO, cmd_id 0).

Riego (ACT-02), igual que controlarBomba() y aplicarOrden() del firmware:
modo AUTO/MANUAL, objetivo SUELO_LIMITE_MIN, tiempo máximo BOMBA_MAX_MS,
descanso BOMBA_DESCANSO_MS y órdenes `SG,<id>,...` por `write()`. Con
`legacy=True` (--firmware-actual) se comporta como el firmware de antes:
riega solo con la regla vieja, no acepta órdenes y no manda `modo`.

    python -m gateway simulate           gateway completo con este Arduino
    python -m gateway simulate --seco    arranca con la tierra seca
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

from gateway.protocol import parse

# ── Mismas constantes que el firmware ──────────────────────────────────
SUELO_EN_AIRE, SUELO_EN_AGUA = 555, 267
NIVEL_VACIO, NIVEL_LLENO = 0, 150
SUELO_MIN_VALIDO, SUELO_MAX_VALIDO = 100, 1013

TEMP_IDEAL = (20.0, 25.0)
TEMP_LIMITE = (6.0, 30.0)
HUMA_IDEAL = (60.0, 80.0)
HUMA_LIMITE = (50.0, 85.0)
SUELO_IDEAL = (60, 65)
SUELO_LIMITE = (50, 80)
NIVEL_PCT_AVISO, NIVEL_PCT_CRITICO = 50, 25
LUZ_UMBRAL_DIA, LUZ_UMBRAL_SOL = 250, 600
CO2_IDEAL = (400, 1000)
CO2_LIMITE = (300, 1500)
# Cada lectura simulada son 2 s del Arduino (INTERVALO_MS del firmware), aunque
# --interval las mande más rápido: así --interval 0.5 es un time-lapse (x4) y
# los tiempos de riego cuadran con la "física de juguete", que va por lectura.
STEP_MS = 2000
BOMBA_MAX_MS = 60 * 1000          # ningún riego dura más de 1 min
BOMBA_MIN_MS = 10 * 1000          # un riego automático dura al menos 10 s
ORDEN_MAX = 31                    # char orden[32] del firmware
BOMBA_DESCANSO_MS = 5 * 60000     # espera entre riegos automáticos


def a_porcentaje(crudo: int, en_cero: int, en_cien: int) -> int:
    """map() + constrain() de Arduino (división entera truncada)."""
    p = int((crudo - en_cero) * 100 / (en_cien - en_cero))
    return max(0, min(100, p))


def evaluar(temp, hum, suelo_pct, nivel_pct, co2_ppm=450) -> List[str]:
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

    if co2_ppm is None:
        alertas.append("FALLA_CO2")
    elif co2_ppm > CO2_LIMITE[1]:
        alertas.append("CO2_ALTO")
    elif co2_ppm < CO2_LIMITE[0]:
        alertas.append("CO2_BAJO")
    elif co2_ppm > CO2_IDEAL[1]:
        alertas.append("CO2_ALTO_LEVE")
    elif co2_ppm < CO2_IDEAL[0]:
        alertas.append("CO2_BAJO_LEVE")
    return alertas


GRAVES = {
    "FALLA_DHT", "TEMP_ALTA", "TEMP_BAJA", "HUM_AIRE_ALTA", "HUM_AIRE_BAJA",
    "FALLA_SUELO", "SUELO_ENCHARCADO", "SUELO_SECO", "FALLA_NIVEL", "TANQUE_UN_CUARTO",
    "FALLA_CO2", "CO2_ALTO", "CO2_BAJO",
}


class SimulatedArduino:
    """Se usa como un puerto Serial: readline(), write() y close()."""

    def __init__(
        self,
        interval: float = 2.0,
        failures: bool = False,
        seed: Optional[int] = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = datetime.now,
        dry: bool = False,
        legacy: bool = False,
    ) -> None:
        self.interval = interval
        self.failures = failures
        self.rng = random.Random(seed)
        self.sleep = sleep
        self.now = now
        self.legacy = legacy
        self.first = True
        self.ms = 2000                 # el firmware espera 2 s al arrancar
        # dry: la tierra arranca justo arriba del objetivo (50 %) y en unas
        # cuantas lecturas baja de él, así se ve al Arduino empezar a regar.
        self.suelo_raw = 410.0 if dry else 382.0   # ~50 % o ~60 %
        self.nivel_raw = 120.0         # ~80 % del tanque
        self.sol_ms = 0
        self.bomba_activa = False
        # Riego (ACT-02): el mismo estado que el firmware
        self.modo_auto = True
        self.bomba_desde = 0
        self.bomba_paro_ms = 0
        self.hubo_paro = False
        self.bomba_paro: Optional[str] = None
        self.bomba_habilitada = False
        self.cmd_id = 0
        self.cmd_err: Optional[str] = None
        self.written: List[str] = []   # órdenes que llegaron, para las pruebas
        self._incoming = b""

    # ── Bomba y órdenes (ACT-02), como el firmware ─────────────────────

    def _encender(self) -> None:
        self.bomba_activa = True
        self.bomba_desde = self.ms

    def _apagar(self, motivo: str) -> None:
        if not self.bomba_activa:
            return
        self.bomba_activa = False
        self.bomba_paro_ms = self.ms
        self.hubo_paro = True
        self.bomba_paro = motivo

    def _controlar_bomba(self, suelo_pct, riego_sugerido: bool, bomba_habilitada: bool) -> None:
        if self.legacy:   # firmware de antes: riega mientras la tierra esté seca
            self.bomba_activa = riego_sugerido and bomba_habilitada
            return
        if self.bomba_activa:
            if not bomba_habilitada:
                self._apagar("TANQUE")
            elif self.ms - self.bomba_desde >= BOMBA_MAX_MS:
                self._apagar("TIEMPO")
            elif self.modo_auto and suelo_pct is None:
                self._apagar("SENSOR")
            elif self.modo_auto and not riego_sugerido and self.ms - self.bomba_desde >= BOMBA_MIN_MS:
                self._apagar("OBJETIVO")
        elif (self.modo_auto and riego_sugerido and bomba_habilitada
              and (not self.hubo_paro or self.ms - self.bomba_paro_ms >= BOMBA_DESCANSO_MS)):
            self._encender()

    def _aplicar_orden(self, line: str) -> None:
        parsed = parse(line)
        if parsed is None:
            return                     # ruido o eco: se ignora
        command_id, equipo, accion = parsed
        self.cmd_id, self.cmd_err = command_id, None
        if equipo == "MODO" and accion == "AUTO":
            if not self.modo_auto:
                self._apagar("MODO")
            self.modo_auto = True
        elif equipo == "MODO" and accion == "MANUAL":
            self._apagar("MODO")
            self.modo_auto = False
        elif equipo == "BOMBA" and accion in ("ON", "OFF"):
            if self.modo_auto:
                self.cmd_err = "MODO"
            elif accion == "OFF":
                self._apagar("ORDEN")
            elif not self.bomba_habilitada:
                self.cmd_err = "TANQUE"
            elif not self.bomba_activa:
                self._encender()
        else:
            self.cmd_err = "FORMATO"

    # ── Física de juguete ──────────────────────────────────────────────

    def _step(self) -> dict:
        rng = self.rng
        self.ms += STEP_MS
        moment = self.now()
        hour = moment.hour + moment.minute / 60

        es_dia_real = 7 <= hour < 19
        temp = 22.5 + 2.8 * math.sin((hour - 9) / 24 * 2 * math.pi) + rng.gauss(0, 0.2)
        hum = 70 - (temp - 22.5) * 3 + rng.gauss(0, 0.8)
        if self.failures and rng.random() < 0.03:
            temp = hum = None          # el DHT11 falla de vez en cuando

        # La tierra se seca poco a poco; la bomba la moja y vacía el tanque
        # (con el tanque de 150 crudo, un riego de 60 s gasta ~16 %).
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
            self.sol_ms += STEP_MS

        co2_ppm: Optional[int] = int((460 if es_dia_real else 560) + rng.gauss(0, 12))
        if self.failures and rng.random() < 0.02:
            co2_ppm = None

        suelo_ok = SUELO_MIN_VALIDO <= suelo_raw <= SUELO_MAX_VALIDO
        suelo_pct = a_porcentaje(suelo_raw, SUELO_EN_AIRE, SUELO_EN_AGUA) if suelo_ok else None
        nivel_pct = a_porcentaje(nivel_raw, NIVEL_VACIO, NIVEL_LLENO)

        bomba_habilitada = nivel_pct > NIVEL_PCT_CRITICO
        riego_sugerido = suelo_pct is not None and suelo_pct < SUELO_LIMITE[0]
        self.bomba_habilitada = bomba_habilitada
        self._controlar_bomba(suelo_pct, riego_sugerido, bomba_habilitada)
        vent_alta = (temp is not None and temp > TEMP_IDEAL[1]) or (hum is not None and hum > HUMA_IDEAL[1])

        alertas = evaluar(temp, hum, suelo_pct, nivel_pct, co2_ppm)
        estado = "OK" if not alertas else ("ALERTA" if GRAVES & set(alertas) else "AVISO")
        line = {
            "ms": self.ms,
            "temp_c": None if temp is None else round(temp, 1),
            "hum_aire_pct": None if hum is None else round(hum, 1),
            "suelo_raw": suelo_raw,
            "suelo_pct": suelo_pct,
            "nivel_raw": nivel_raw,
            "nivel_pct": nivel_pct,
            "luz_raw": 1023 - luz_nivel,      # LUZ_INVERTIDA
            "luz_nivel": luz_nivel,
            "co2_ppm": co2_ppm,
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
        if not self.legacy:
            line.update(modo="AUTO" if self.modo_auto else "MANUAL", cmd_id=self.cmd_id,
                        cmd_err=self.cmd_err, bomba_paro=self.bomba_paro)
        return line

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

    def write(self, data: bytes) -> int:
        """Lo que escribe el gateway. El firmware de antes no lee el Serial."""
        if self.legacy:
            return len(data)
        self._incoming += data.replace(b"\r", b"\n")
        while b"\n" in self._incoming:
            raw, self._incoming = self._incoming.split(b"\n", 1)
            if not raw or len(raw) > ORDEN_MAX:
                continue               # el firmware descarta una línea demasiado larga
            text = raw.decode("ascii", "replace")
            self.written.append(text)
            self._aplicar_orden(text)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass
