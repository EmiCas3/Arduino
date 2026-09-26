"""
Reloj del gateway: ¿la Pi ya sabe qué hora es?

La Raspberry Pi 5 no tiene RTC con pila: al prender cree que es la última hora
que guardó (o 1970) hasta que sincroniza por internet (NTP). Reglas:

- `ts` se pone SOLO si el reloj está sincronizado (`timedatectl`).
- Si todavía no lo está, la lectura se guarda con el tiempo desde el arranque
  (CLOCK_BOOTTIME, que no depende de la hora) y el identificador del arranque.
  Cuando el reloj sincroniza, su hora exacta se calcula así:
      ts = hora_actual - (arranque_actual - arranque_de_la_lectura)
- Lecturas de un arranque anterior sin hora ya no se pueden fechar: se quedan
  en el buffer como `sin_hora` y `status` lo avisa. Nunca se inventa una hora.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("gateway.clock")

# Una hora anterior a esto es un reloj sin sincronizar, diga lo que diga el
# sistema. Es el mismo mínimo que usa el backend (SMARTGREENAI_MIN_READING_TS).
MIN_VALID_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()

BOOT_ID_FILE = Path("/proc/sys/kernel/random/boot_id")


def utc_ms(epoch_seconds: float) -> str:
    """Hora UTC con milisegundos, el formato que guarda el backend."""
    moment = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds")


def boot_ms() -> int:
    """Milisegundos desde que arrancó la máquina. No cambia si cambia la hora."""
    clock_id = getattr(time, "CLOCK_BOOTTIME", None)
    if clock_id is not None:
        return int(time.clock_gettime(clock_id) * 1000)
    return int(time.monotonic() * 1000)   # Windows / macOS (solo desarrollo)


def boot_id() -> str:
    """Identifica el arranque actual (cambia en cada reinicio de la Pi)."""
    try:
        return BOOT_ID_FILE.read_text().strip()
    except OSError:
        return "sin-boot-id"


def resolve_epoch(reading_boot_ms: int, now_epoch: float, now_boot_ms: int) -> float:
    """Hora real de una lectura guardada sin hora, ya con el reloj sincronizado."""
    return now_epoch - (now_boot_ms - reading_boot_ms) / 1000.0


def _run(cmd) -> Optional[str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout


def timedatectl_synced(run: Callable = _run) -> Optional[bool]:
    """True/False según systemd; None si no se pudo preguntar."""
    out = run(["timedatectl", "show", "--property=NTPSynchronized", "--value"])
    if out is not None and out.strip() in ("yes", "no"):
        return out.strip() == "yes"
    out = run(["timedatectl", "status"])   # systemd viejo: sin `show`
    if out is None:
        return None
    for line in out.splitlines():
        if "synchronized:" in line:
            return line.split(":", 1)[1].strip() == "yes"
    return None


class Clock:
    """Dice si el reloj está sincronizado. Cachea la respuesta unos segundos.

    mode:
      timedatectl  pregunta a systemd (la Pi).
      system       confía en el reloj del sistema si ya pasó de 2026 (laptops).
      auto         timedatectl si existe; si no, system (Windows, macOS).
    """

    RECHECK_UNSYNCED = 15.0
    RECHECK_SYNCED = 120.0

    def __init__(
        self,
        mode: str = "auto",
        *,
        time_fn: Callable[[], float] = time.time,
        boot_ms_fn: Callable[[], int] = boot_ms,
        boot_id_fn: Callable[[], str] = boot_id,
        sync_fn: Optional[Callable[[], Optional[bool]]] = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if mode == "auto":
            mode = "timedatectl" if shutil.which("timedatectl") else "system"
            if mode == "system":
                log.warning(
                    "No hay timedatectl: se confía en el reloj de esta máquina "
                    "(válido para pruebas en una laptop, no en la Pi)."
                )
        self.mode = mode
        self.time = time_fn
        self.boot_ms = boot_ms_fn
        self.boot_id = boot_id_fn()
        self._sync_fn = sync_fn or (timedatectl_synced if mode == "timedatectl" else None)
        self._monotonic = monotonic_fn
        self._lock = threading.Lock()
        self._cached: Optional[bool] = None
        self._checked_at = float("-inf")

    def _ask(self) -> bool:
        if self.time() < MIN_VALID_EPOCH:
            return False            # 1970 o una hora vieja guardada: no sirve
        if self._sync_fn is None:
            return True             # modo system
        answer = self._sync_fn()
        if answer is None:
            log.warning("No se pudo consultar timedatectl; se asume reloj SIN sincronizar.")
            return False
        return answer

    def is_synced(self, force: bool = False) -> bool:
        with self._lock:
            now = self._monotonic()
            wait = self.RECHECK_SYNCED if self._cached else self.RECHECK_UNSYNCED
            if force or self._cached is None or now - self._checked_at >= wait:
                previous = self._cached
                self._cached = self._ask()
                self._checked_at = now
                if previous is not None and previous != self._cached:
                    if self._cached:
                        log.info("Reloj sincronizado: %s", utc_ms(self.time()))
                    else:
                        log.warning("El reloj dejó de estar sincronizado.")
            return bool(self._cached)

    def stamp(self) -> tuple:
        """(ts o None, boot_ms) para una lectura que acaba de llegar."""
        # Primero se toma la hora y luego se pregunta si es confiable: la
        # consulta a timedatectl puede tardar unos milisegundos.
        now_wall = self.time()
        now_boot = self.boot_ms()
        if self.is_synced():
            return utc_ms(now_wall), now_boot
        return None, now_boot
