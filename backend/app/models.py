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
