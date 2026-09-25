"""
Configuración del backend leída de variables de entorno.

Todas las variables usan el prefijo SMARTGREENAI_ (igual que SMARTGREENAI_DB).
Ningún secreto vive en el código: si falta SMARTGREENAI_JWT_SECRET se genera
uno aleatorio al arrancar. Eso basta para desarrollo, pero las sesiones se
invalidan cada vez que se reinicia el servidor.

Variables:
    SMARTGREENAI_JWT_SECRET            Secreto para firmar los JWT (obligatorio en la demo)
    SMARTGREENAI_SESSION_IDLE_MINUTES  Minutos de inactividad antes de cerrar la sesión (30)
    SMARTGREENAI_SESSION_MAX_HOURS     Duración máxima de una sesión aunque haya actividad (8)
    SMARTGREENAI_READING_STALE_MINUTES Minutos tras los cuales una lectura se marca como vieja (15)
    SMARTGREENAI_FRONTEND_DIR          Carpeta del frontend que sirve FastAPI (../frontend)
"""

import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger("smartgreenai")


def _int_env(name: str, default: int) -> int:
    """Lee un entero positivo del entorno; si no está definido usa el default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} debe ser un número entero, se recibió {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{name} debe ser mayor que 0")
    return value


class Settings:
    """Valores de configuración. Se leen una sola vez al importar el módulo."""

    def __init__(self) -> None:
        secret = os.environ.get("SMARTGREENAI_JWT_SECRET", "").strip()
        self.jwt_secret_is_ephemeral = not secret
        if not secret:
            secret = secrets.token_urlsafe(48)
            logger.warning(
                "SMARTGREENAI_JWT_SECRET no está definido: se generó un secreto "
                "temporal. Las sesiones se cerrarán al reiniciar el servidor."
            )
        self.jwt_secret: str = secret
        self.jwt_algorithm: str = "HS256"

        self.session_idle_minutes: int = _int_env("SMARTGREENAI_SESSION_IDLE_MINUTES", 30)
        self.session_max_hours: int = _int_env("SMARTGREENAI_SESSION_MAX_HOURS", 8)
        self.reading_stale_minutes: int = _int_env("SMARTGREENAI_READING_STALE_MINUTES", 15)

        default_frontend = Path(__file__).resolve().parents[2] / "frontend"
        self.frontend_dir: Path = Path(
            os.environ.get("SMARTGREENAI_FRONTEND_DIR", "").strip() or default_frontend
        )


settings = Settings()
