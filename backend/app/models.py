"""
Schemas Pydantic basados en el openapi.yaml del proyecto.

Convención clave: null = sensor caído, 0 = lectura real.
Todos los campos de medición son Optional (nullable).

Usa Pydantic v1 (compatible con MSYS2 Python sin Rust).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, validator


# ── Enums ──────────────────────────────────────────────────────────────

class VentEnum(str, Enum):
    BASE = "BASE"
    ALTA = "ALTA"


class EstadoEnum(str, Enum):
    OK = "OK"
    AVISO = "AVISO"
    ALERTA = "ALERTA"


class AlertaEnum(str, Enum):
    TEMP_ALTA = "TEMP_ALTA"
    TEMP_BAJA = "TEMP_BAJA"
    TEMP_ALTA_LEVE = "TEMP_ALTA_LEVE"
    TEMP_BAJA_LEVE = "TEMP_BAJA_LEVE"
    HUM_AIRE_ALTA = "HUM_AIRE_ALTA"
    HUM_AIRE_BAJA = "HUM_AIRE_BAJA"
    HUM_AIRE_ALTA_LEVE = "HUM_AIRE_ALTA_LEVE"
    HUM_AIRE_BAJA_LEVE = "HUM_AIRE_BAJA_LEVE"
    SUELO_ENCHARCADO = "SUELO_ENCHARCADO"
    SUELO_SECO = "SUELO_SECO"
    SUELO_HUMEDO_LEVE = "SUELO_HUMEDO_LEVE"
    SUELO_SECO_LEVE = "SUELO_SECO_LEVE"
    TANQUE_MITAD = "TANQUE_MITAD"
    TANQUE_UN_CUARTO = "TANQUE_UN_CUARTO"
    SOMBRA_PROLONGADA = "SOMBRA_PROLONGADA"
    SOL_INSUFICIENTE = "SOL_INSUFICIENTE"
    FALLA_DHT = "FALLA_DHT"
    FALLA_SUELO = "FALLA_SUELO"
    FALLA_NIVEL = "FALLA_NIVEL"


# ── Reading ────────────────────────────────────────────────────────────

class Reading(BaseModel):
    """Una lectura del Arduino, enriquecida por la Pi con el timestamp."""

    ts: datetime = Field(
        ...,
        description="Momento de la lectura en ISO 8601 UTC, asignado por la Pi",
    )
    ms: Optional[int] = Field(
        None,
        description="Millis desde boot del Arduino, para detectar reinicios",
    )

    # DHT11
    temp_c: Optional[float] = None
    hum_aire_pct: Optional[float] = None

    # Suelo capacitivo
    suelo_raw: Optional[int] = Field(None, ge=0, le=1023)
    suelo_pct: Optional[int] = Field(None, ge=0, le=100)

    # Nivel del tanque
    nivel_raw: Optional[int] = Field(None, ge=0, le=1023)
    nivel_pct: Optional[int] = Field(None, ge=0, le=100)

    # Luz
    luz_raw: Optional[int] = Field(None, ge=0, le=1023)
    luz_nivel: Optional[int] = Field(None, ge=0, le=1023)

    # Ciclo de luz
    es_dia: Optional[bool] = None
    sol_min_hoy: Optional[int] = Field(None, ge=0)
    sol_min_prev: Optional[int] = Field(None, ge=0)

    # Decisiones
    riego_sugerido: Optional[bool] = None
    bomba_habilitada: Optional[bool] = None
    vent: Optional[VentEnum] = None

    # Estado y alertas
    estado: Optional[EstadoEnum] = None
    alertas: Optional[List[AlertaEnum]] = None


# ── Batch ──────────────────────────────────────────────────────────────

class ReadingBatch(BaseModel):
    """Lote de lecturas. La Pi manda de 1 a 500 por request."""

    readings: List[Reading] = Field(..., min_items=1, max_items=500)


# ── Respuesta de ingesta ───────────────────────────────────────────────

class RejectedReading(BaseModel):
    index: int
    reason: str


class IngestResult(BaseModel):
    accepted: int = 0
    duplicates: int = 0
    rejected: List[RejectedReading] = Field(default_factory=list)


# ── StoredReading (para consultas) ─────────────────────────────────────

class StoredReading(Reading):
    """Lectura almacenada, con metadatos del backend."""

    id: int
    device_id: str
    greenhouse_id: str
    received_at: datetime


# ── Error genérico ─────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    error: str
    message: str


# ── Auth (AUTH-01 / AUTH-02) ───────────────────────────────────────────

class Role(str, Enum):
    """Roles de la plataforma. Los textos en español viven en el frontend."""

    producer = "producer"          # Radish Producer
    admin = "admin"                # Administrator
    super_admin = "super_admin"    # Super Administrator


class LoginRequest(BaseModel):
    """Credenciales del formulario. `login` acepta usuario O email."""

    login: str = Field(..., min_length=1, max_length=254, example="productor")
    password: str = Field(..., min_length=1, max_length=128)

    @validator("login")
    def _login_sin_espacios(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("El usuario o email no puede estar vacío")
        return value


class UserPublic(BaseModel):
    """Datos del usuario que se pueden mostrar. Nunca incluye el hash."""

    id: int
    username: str
    email: str
    full_name: Optional[str] = None
    role: Role
    greenhouse_id: Optional[str] = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime = Field(
        ..., description="Vencimiento absoluto de la sesión (UTC)"
    )
    idle_timeout_minutes: int = Field(
        ..., description="La sesión se cierra tras estos minutos sin actividad"
    )
    user: UserPublic


# ── Actuadores (CONF-05) ───────────────────────────────────────────────

SLUG_REGEX = r"^[a-z0-9][a-z0-9-]{2,49}$"      # mismo patrón que DeviceId
AREA_REGEX = r"^[a-z0-9][a-z0-9-]{0,49}$"
CHANNEL_REGEX = r"^[A-Za-z0-9][A-Za-z0-9/_.:-]{0,49}$"


class ActuatorType(str, Enum):
    """Equipos controlables de un vivero."""

    pump = "pump"      # bomba de riego
    fan = "fan"        # ventilador
    shade = "shade"    # malla sombra
    light = "light"    # iluminación


class ActuatorStatus(str, Enum):
    active = "active"
    inactive = "inactive"


def _texto_no_vacio(value: Optional[str]) -> Optional[str]:
    if value is None:
        return value
    value = value.strip()
    if not value:
        raise ValueError("No puede estar vacío")
    return value


def _texto_opcional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return value
    value = value.strip()
    return value or None


class ActuatorCreate(BaseModel):
    """Alta de un actuador que existe físicamente en el vivero."""

    actuator_id: str = Field(..., regex=SLUG_REGEX, example="bomba-riego-01")
    name: str = Field(..., min_length=1, max_length=80, example="Bomba de riego")
    type: ActuatorType
    greenhouse_id: str = Field(..., regex=SLUG_REGEX, example="vivero-rabano-01")
    area: str = Field(..., regex=AREA_REGEX, example="general")
    control_channel: str = Field(
        ...,
        regex=CHANNEL_REGEX,
        example="L298N-B/D5",
        description="Canal físico que lo controla (driver/pin)",
    )
    gateway_device_id: Optional[str] = Field(
        None,
        regex=SLUG_REGEX,
        example="pi-vivero-01",
        description="Gateway que le enviará los comandos (opcional)",
    )
    model: Optional[str] = Field(None, max_length=120, example="Bomba sumergible 12 V")

    _name = validator("name", allow_reuse=True)(_texto_no_vacio)
    _model = validator("model", allow_reuse=True)(_texto_opcional)


class ActuatorUpdate(BaseModel):
    """Cambios permitidos. `type` y `greenhouse_id` NO se editan: cambiarlos
    corrompería el historial; lo correcto es desactivar y registrar uno nuevo."""

    name: Optional[str] = Field(None, min_length=1, max_length=80)
    area: Optional[str] = Field(None, regex=AREA_REGEX)
    control_channel: Optional[str] = Field(None, regex=CHANNEL_REGEX)
    model: Optional[str] = Field(None, max_length=120)
    status: Optional[ActuatorStatus] = None

    class Config:
        extra = "forbid"

    _name = validator("name", allow_reuse=True)(_texto_no_vacio)
    _model = validator("model", allow_reuse=True)(_texto_opcional)


class Actuator(BaseModel):
    actuator_id: str
    name: str
    type: ActuatorType
    greenhouse_id: str
    area: str
    control_channel: str
    gateway_device_id: Optional[str] = None
    model: Optional[str] = None
    status: ActuatorStatus
    registered_at: datetime
    registered_by: Optional[int] = None
    updated_at: Optional[datetime] = None
    deactivated_at: Optional[datetime] = None
