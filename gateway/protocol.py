"""
Órdenes de la plataforma al Arduino (ACT-02). Ver PROTOCOLO_ARDUINO.md.

El backend manda las órdenes en la respuesta 202 de la ingesta, como datos:

    {"id": 17, "kind": "mode", "value": "MANUAL", "actuator_id": null}
    {"id": 18, "kind": "pump", "value": "ON", "actuator_id": "bomba-riego-01"}

Aquí se convierten en la línea que entiende el firmware:

    SG,17,MODO,MANUAL
    SG,18,BOMBA,ON

El gateway SOLO escribe por Serial líneas que pasan esta validación: nunca
texto libre que venga del backend. Cualquier otra cosa se rechaza y se
registra en el log.
"""

from __future__ import annotations

import re
from typing import Optional

# kind del backend → (equipo en el firmware, acciones válidas)
TARGETS = {
    "mode": ("MODO", ("AUTO", "MANUAL")),
    "pump": ("BOMBA", ("ON", "OFF")),
}
MAX_ID = 999_999_999           # cabe de sobra en un unsigned long del Uno
LINE_RE = re.compile(r"^SG,[1-9]\d{0,8},(MODO,(AUTO|MANUAL)|BOMBA,(ON|OFF))$")

# Una orden que no se pudo escribir en este tiempo (Arduino desconectado) se
# descarta: el backend ya la dio por perdida y escribirla tarde sería peor.
MAX_COMMAND_AGE_SECONDS = 30.0


class CommandError(ValueError):
    """La orden que mandó el backend no es válida: no se escribe."""


def to_line(command: dict) -> bytes:
    """Orden del backend → línea para el Arduino (con salto de línea)."""
    if not isinstance(command, dict):
        raise CommandError(f"no es un objeto: {command!r}")
    command_id = command.get("id")
    if isinstance(command_id, bool) or not isinstance(command_id, int) or not 0 < command_id <= MAX_ID:
        raise CommandError(f"id inválido: {command_id!r}")
    target = TARGETS.get(command.get("kind"))
    if target is None:
        raise CommandError(f"tipo de orden desconocido: {command.get('kind')!r}")
    name, values = target
    value = command.get("value")
    if value not in values:
        raise CommandError(f"acción inválida para {name}: {value!r}")
    line = f"SG,{command_id},{name},{value}"
    if not LINE_RE.match(line):   # segunda red de seguridad
        raise CommandError(f"línea inválida: {line!r}")
    return (line + "\n").encode("ascii")


def parse(line: str) -> Optional[tuple]:
    """Lo que hace el firmware con una línea: (id, equipo, acción) o None.

    Lo usa el Arduino simulado y copia a aplicarOrden() del .ino:
    - Se parte con `strtok(linea, ",")`, que se salta los campos vacíos
      ("SG,,7" es igual a "SG,7"; una coma al final no cuenta).
    - Si el primer campo no es exactamente "SG" o el id no son de 1 a 9
      dígitos (esNumeroDeOrden), se ignora: None.
    - Si trae id pero el resto no cuadra (falta o sobra un campo), la acción
      es None y el firmware contesta FORMATO.
    """
    parts = [part for part in line.split(",") if part != ""]
    if not parts or parts[0] != "SG":
        return None
    text_id = parts[1] if len(parts) > 1 else ""
    if not (text_id.isdigit() and text_id.isascii() and len(text_id) <= 9):
        return None
    command_id = int(text_id)
    if command_id == 0:
        return None
    if len(parts) != 4:
        return (command_id, None, None)
    return (command_id, parts[2], parts[3])
