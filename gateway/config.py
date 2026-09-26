"""
Configuración del gateway, leída de gateway/.env y del entorno.

Las variables del entorno ganan sobre las del archivo, así systemd o una
terminal pueden cambiar algo sin editar el .env. El .env NO se sube a git
(tiene la API key); copia .env.example y rellénalo.

Variables:
    SG_API_URL        URL base de la API                 (http://localhost:8000/api/v1)
    SG_DEVICE_ID      device_id del gateway              (pi-vivero-01)
    SG_API_KEY        API key que imprimió el seed       (obligatoria para `run`)
    SG_SERIAL_PORT    Puerto del Arduino o "auto"        (auto)
    SG_BAUD           Velocidad del Serial               (9600, igual que el firmware)
    SG_BATCH_SIZE     Lecturas por envío, de 1 a 500     (100)
    SG_SEND_INTERVAL  Segundos entre envíos              (10)
    SG_HTTP_TIMEOUT   Segundos de espera por respuesta   (10)
    SG_BUFFER_DB      Archivo SQLite del buffer          (gateway/buffer.db)
    SG_CLOCK          auto | timedatectl | system        (auto)
    SG_PURGE_DAYS     Días que se guardan las enviadas   (7)
    SG_LOG_LEVEL      DEBUG | INFO | WARNING             (INFO)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional

GATEWAY_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = GATEWAY_DIR / ".env"

CLOCK_MODES = ("auto", "timedatectl", "system")


class ConfigError(Exception):
    """Configuración inválida. El mensaje se muestra tal cual al usuario."""


def read_env_file(path: Path) -> Dict[str, str]:
    """Lee un archivo KEY=VALUE. Ignora líneas vacías y comentarios (#)."""
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            raise ConfigError(f"{path.name}, línea {number}: falta '=' en {line!r}")
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass
class Config:
    api_url: str = "http://localhost:8000/api/v1"
    device_id: str = "pi-vivero-01"
    api_key: str = field(default="", repr=False)   # nunca se imprime
    serial_port: str = "auto"
    baud: int = 9600
    batch_size: int = 100
    send_interval: float = 10.0
    http_timeout: float = 10.0
    buffer_db: Path = GATEWAY_DIR / "buffer.db"
    clock: str = "auto"
    purge_days: int = 7
    log_level: str = "INFO"

    @property
    def ingest_url(self) -> str:
        return f"{self.api_url.rstrip('/')}/devices/{self.device_id}/readings"

    def require_api_key(self) -> None:
        if not self.api_key:
            raise ConfigError(
                "Falta SG_API_KEY en gateway/.env. Es la API key que imprime "
                "`python -m app.seed` al crear el gateway."
            )


def _number(values: Mapping[str, str], key: str, default, cast, minimum, maximum=None):
    raw = values.get(key, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} debe ser un número, se recibió {raw!r}") from exc
    if value < minimum or (maximum is not None and value > maximum):
        rango = f"entre {minimum} y {maximum}" if maximum is not None else f"mayor o igual a {minimum}"
        raise ConfigError(f"{key} debe estar {rango}, se recibió {raw!r}")
    return value


def load_config(
    env_file: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Config:
    """Arma la configuración: valores por defecto < archivo .env < entorno."""
    env_file = DEFAULT_ENV_FILE if env_file is None else env_file
    environ = os.environ if environ is None else environ
    values = read_env_file(env_file)
    values.update({k: v for k, v in environ.items() if k.startswith("SG_")})

    cfg = Config()
    cfg.api_url = values.get("SG_API_URL", "").strip() or cfg.api_url
    if not cfg.api_url.startswith(("http://", "https://")):
        raise ConfigError(f"SG_API_URL debe empezar con http:// o https://, se recibió {cfg.api_url!r}")
    cfg.device_id = values.get("SG_DEVICE_ID", "").strip() or cfg.device_id
    cfg.api_key = values.get("SG_API_KEY", "").strip()
    cfg.serial_port = values.get("SG_SERIAL_PORT", "").strip() or cfg.serial_port
    cfg.baud = _number(values, "SG_BAUD", cfg.baud, int, 300)
    cfg.batch_size = _number(values, "SG_BATCH_SIZE", cfg.batch_size, int, 1, 500)
    cfg.send_interval = _number(values, "SG_SEND_INTERVAL", cfg.send_interval, float, 1)
    cfg.http_timeout = _number(values, "SG_HTTP_TIMEOUT", cfg.http_timeout, float, 1)
    cfg.purge_days = _number(values, "SG_PURGE_DAYS", cfg.purge_days, int, 1)

    buffer_db = values.get("SG_BUFFER_DB", "").strip()
    if buffer_db:
        path = Path(buffer_db).expanduser()
        cfg.buffer_db = path if path.is_absolute() else GATEWAY_DIR / path

    cfg.clock = (values.get("SG_CLOCK", "").strip() or cfg.clock).lower()
    if cfg.clock not in CLOCK_MODES:
        raise ConfigError(f"SG_CLOCK debe ser uno de {', '.join(CLOCK_MODES)}, se recibió {cfg.clock!r}")

    cfg.log_level = (values.get("SG_LOG_LEVEL", "").strip() or cfg.log_level).upper()
    if cfg.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError(f"SG_LOG_LEVEL inválido: {cfg.log_level!r}")
    return cfg
