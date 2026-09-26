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
    SMARTGREENAI_MIN_READING_TS        Lecturas con `ts` anterior a esto se rechazan
                                       (2026-01-01T00:00:00Z). Una Pi sin hora cree que es 1970
    SMARTGREENAI_MAX_CLOCK_SKEW_SECONDS  Segundos que un `ts` puede ir adelante del
                                       reloj del servidor antes de rechazarse (300)
"""

import logging
import os
import secrets
from datetime import datetime, timezone
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


def _datetime_env(name: str, default: str) -> datetime:
    """Lee una fecha ISO 8601 CON zona horaria y la devuelve en UTC."""
    raw = os.environ.get(name, "").strip() or default
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(
            f"{name} debe ser una fecha ISO 8601, se recibió {raw!r}"
        ) from exc
    if value.tzinfo is None:
        raise RuntimeError(
            f"{name} debe incluir zona horaria, por ejemplo 2026-01-01T00:00:00Z"
        )
    return value.astimezone(timezone.utc)


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

        # Ingesta (ajuste de MON-04): rango de horas aceptables para `ts`.
        self.min_reading_ts: datetime = _datetime_env(
            "SMARTGREENAI_MIN_READING_TS", "2026-01-01T00:00:00Z"
        )
        self.max_clock_skew_seconds: int = _int_env("SMARTGREENAI_MAX_CLOCK_SKEW_SECONDS", 300)

        default_frontend = Path(__file__).resolve().parents[2] / "frontend"
        self.frontend_dir: Path = Path(
            os.environ.get("SMARTGREENAI_FRONTEND_DIR", "").strip() or default_frontend
        )


settings = Settings()
