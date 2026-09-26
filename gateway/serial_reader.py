"""
Lectura del Arduino por Serial.

El firmware (vivero_sensoresV2.ino) imprime UNA línea JSON cada 2 s a 9600
baud. Aquí se lee línea por línea y se convierte a dict SIN renombrar campos;
el gateway solo le agrega `ts`.

Detalles del hardware que se cuidan:
- Al abrir el puerto el Arduino Uno se reinicia, así que la primera línea
  puede llegar cortada. Toda línea inválida se cuenta, se registra en el log
  y se ignora.
- El nombre /dev/ttyACM0 puede cambiar a ACM1 al reconectar. Con
  SG_SERIAL_PORT=auto se usa /dev/serial/by-id/..., que es fijo.
- Si el Arduino se desconecta, se reintenta cada 5 s sin tumbar el servicio.
"""

from __future__ import annotations

import glob
import json
import logging
import threading
import time
from typing import Callable, Iterator, Optional

log = logging.getLogger("gateway.serial")

RECONNECT_SECONDS = 5.0
SILENCE_WARNING_SECONDS = 60.0
MAX_LINE_BYTES = 2048

# VID USB de Arduino (2341, 2A03) y de los clones con CH340 (1A86).
ARDUINO_VIDS = {0x2341, 0x2A03, 0x1A86}


def _reject_constant(name: str):
    raise ValueError(f"valor no válido en JSON: {name}")


def parse_line(raw: bytes) -> Optional[dict]:
    """Convierte una línea del Arduino en dict. None si no es una lectura válida."""
    if not raw or len(raw) > MAX_LINE_BYTES:
        return None
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        # NaN / Infinity no son JSON válido: el backend rechazaría el lote entero.
        data = json.loads(text, parse_constant=_reject_constant)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    data.pop("ts", None)   # la hora la pone la Pi, nunca el Arduino
    return data


def find_port(configured: str = "auto") -> Optional[str]:
    """Devuelve el puerto a usar, o None si no se encuentra el Arduino."""
    if configured and configured.lower() != "auto":
        return configured

    by_id = sorted(glob.glob("/dev/serial/by-id/*"))
    if by_id:
        preferred = [p for p in by_id if "arduino" in p.lower()]
        return (preferred or by_id)[0]

    try:   # Windows / macOS, o Linux sin udev
        from serial.tools import list_ports
    except ImportError:
        return None
    ports = list(list_ports.comports())
    for port in ports:
        text = f"{port.description} {port.manufacturer or ''}".lower()
        if port.vid in ARDUINO_VIDS or "arduino" in text or "ch340" in text:
            return port.device
    for port in ports:
        if "ttyACM" in port.device or "ttyUSB" in port.device:
            return port.device
    return None


def open_serial(port: str, baud: int):
    """Abre el puerto real con pyserial (timeout de 1 s por línea)."""
    import serial   # import tardío: los tests y `status` no necesitan pyserial

    conn = serial.Serial(port, baud, timeout=1)
    conn.reset_input_buffer()
    return conn


class SerialReader:
    """Hilo que lee líneas y llama a `on_reading(dict)` por cada lectura válida.

    `open_port(port, baud)` devuelve un objeto con readline() y close(); en la
    Pi es pyserial y en `simulate` es el Arduino simulado.
    """

    def __init__(
        self,
        on_reading: Callable[[dict], None],
        *,
        port: str = "auto",
        baud: int = 9600,
        open_port: Callable = open_serial,
        find: Callable[[str], Optional[str]] = find_port,
        stop: Optional[threading.Event] = None,
        reconnect_seconds: float = RECONNECT_SECONDS,
    ) -> None:
        self.on_reading = on_reading
        self.port_setting = port
        self.baud = baud
        self.open_port = open_port
        self.find = find
        self.stop = stop or threading.Event()
        self.reconnect_seconds = reconnect_seconds
        self.valid_lines = 0
        self.invalid_lines = 0
        self.last_line_at: Optional[float] = None   # time.monotonic()

    def _lines(self, conn) -> Iterator[bytes]:
        while not self.stop.is_set():
            raw = conn.readline()
            if raw:
                yield raw
            else:
                yield b""   # timeout: deja revisar stop y el silencio

    def run(self) -> None:
        """Bucle principal: abrir, leer, y si algo falla, reintentar."""
        warned_missing = False
        while not self.stop.is_set():
            port = self.find(self.port_setting)
            if port is None:
                if not warned_missing:
                    log.error(
                        "No encuentro el Arduino (SG_SERIAL_PORT=%s). ¿Está conectado "
                        "por USB? Reintento cada %.0f s.", self.port_setting, self.reconnect_seconds,
                    )
                    warned_missing = True
                self.stop.wait(self.reconnect_seconds)
                continue
            warned_missing = False
            try:
                conn = self.open_port(port, self.baud)
            except Exception as exc:  # pyserial lanza SerialException / OSError
                log.error("No se pudo abrir %s: %s. Reintento en %.0f s.", port, exc, self.reconnect_seconds)
                self.stop.wait(self.reconnect_seconds)
                continue

            log.info("Leyendo el Arduino en %s a %d baud.", port, self.baud)
            try:
                self._read_from(conn, port)
            except Exception as exc:
                log.error("Se perdió la conexión con %s: %s. Reintento en %.0f s.",
                          port, exc, self.reconnect_seconds)
                self.stop.wait(self.reconnect_seconds)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def _read_from(self, conn, port: str) -> None:
        first = True
        opened_at = time.monotonic()
        silence_logged_at = opened_at
        for raw in self._lines(conn):
            now = time.monotonic()
            if not raw:
                last = self.last_line_at if self.last_line_at and self.last_line_at > opened_at else opened_at
                if now - last >= SILENCE_WARNING_SECONDS and now - silence_logged_at >= SILENCE_WARNING_SECONDS:
                    log.warning("Sin datos del Arduino en %s desde hace %.0f s.", port, now - last)
                    silence_logged_at = now
                continue

            reading = parse_line(raw)
            if reading is None:
                self.invalid_lines += 1
                level = logging.INFO if first else logging.WARNING
                why = "primera línea tras abrir el puerto (el Arduino se reinicia)" if first else "línea inválida"
                log.log(level, "Se ignora %s: %r", why, raw[:120])
            else:
                self.valid_lines += 1
                self.last_line_at = now
                self.on_reading(reading)
            first = False
