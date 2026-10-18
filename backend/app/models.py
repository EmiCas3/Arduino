"""
Schemas Pydantic basados en el openapi.yaml del proyecto.

Convención clave: null = sensor caído, 0 = lectura real.
Todos los campos de medición son Optional (nullable).

Usa Pydantic v1 (compatible con MSYS2 Python sin Rust).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Optional, Union

from pydantic import BaseModel, Field, StrictFloat, StrictInt, validator

from app.analysis import TrendDirection


# ── Enums ──────────────────────────────────────────────────────────────

class VentEnum(str, Enum):
    BASE = "BASE"
    ALTA = "ALTA"


class EstadoEnum(str, Enum):
    OK = "OK"
    AVISO = "AVISO"
    ALERTA = "ALERTA"


class ModoEnum(str, Enum):
    """Modo de riego que reporta el firmware (ACT-02)."""

    AUTO = "AUTO"
    MANUAL = "MANUAL"


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
    CO2_ALTO = "CO2_ALTO"
    CO2_BAJO = "CO2_BAJO"
    CO2_ALTO_LEVE = "CO2_ALTO_LEVE"
    CO2_BAJO_LEVE = "CO2_BAJO_LEVE"
    FALLA_DHT = "FALLA_DHT"
    FALLA_SUELO = "FALLA_SUELO"
    FALLA_NIVEL = "FALLA_NIVEL"
    FALLA_CO2 = "FALLA_CO2"


# ── Reading ────────────────────────────────────────────────────────────

# AAAA-MM-DDThh:mm al inicio. Evita que Pydantic v1 acepte números
# (1758900000) o textos numéricos como si fueran horas Unix.
ISO_TS_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


class Reading(BaseModel):
    """Una lectura del Arduino, enriquecida por la Pi con el timestamp.

    `bomba_activa` y `vent_activo` dicen qué hizo el Arduino con cada equipo
    en esa lectura (ACT-04). Cualquier otro campo extra se ignora.
    """

    ts: datetime = Field(
        ...,
        description=(
            "Momento de la lectura en ISO 8601 CON zona horaria, asignado por la "
            "Pi. Se normaliza a UTC"
        ),
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

    # CO2 (SCD40 / SCD41 por I2C)
    co2_ppm: Optional[int] = Field(None, ge=0, le=40000)

    # Ciclo de luz
    es_dia: Optional[bool] = None
    sol_min_hoy: Optional[int] = Field(None, ge=0)
    sol_min_prev: Optional[int] = Field(None, ge=0)

    # Decisiones
    riego_sugerido: Optional[bool] = None
    bomba_habilitada: Optional[bool] = None
    vent: Optional[VentEnum] = None

    # Estado de los actuadores según el Arduino (ACT-04). Es lo que el
    # firmware ORDENÓ en el pin, no una medición de que el motor gire.
    bomba_activa: Optional[bool] = Field(None, description="La bomba quedó encendida")
    vent_activo: Optional[bool] = Field(None, description="El ventilador quedó encendido")

    # Riego (ACT-02). Solo los manda un firmware que acepta órdenes de la Pi;
    # si `modo` no viene, el Arduino no acepta órdenes.
    modo: Optional[ModoEnum] = Field(None, description="AUTO: riega solo. MANUAL: solo órdenes")
    cmd_id: Optional[int] = Field(None, ge=0, description="Última orden aplicada (0 = ninguna)")
    cmd_err: Optional[str] = Field(
        None, regex=r"^[A-Z_]{1,20}$", description="Por qué no aplicó esa orden: MODO, TANQUE, FORMATO"
    )
    bomba_paro: Optional[str] = Field(
        None, regex=r"^[A-Z_]{1,20}$",
        description="Por qué se apagó la bomba la última vez: OBJETIVO, TIEMPO, TANQUE, SENSOR, MODO, ORDEN",
    )

    # Estado y alertas
    estado: Optional[EstadoEnum] = None
    alertas: Optional[List[AlertaEnum]] = None

    @validator("ts", pre=True)
    def _ts_es_texto_iso(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str) or not ISO_TS_REGEX.match(value.strip()):
            raise ValueError("debe ser texto ISO 8601, por ejemplo 2026-09-26T18:04:22.123Z")
        return value.strip()

    @validator("ts")
    def _ts_con_zona(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("sin zona horaria: agrega Z (UTC) o un desfase como -06:00")
        return value.astimezone(timezone.utc)


# ── Batch ──────────────────────────────────────────────────────────────

def _readings_schema(schema: dict, model: Any) -> None:
    """En /docs cada elemento de `readings` se muestra como una Reading."""
    item = Reading.schema(ref_template="#/components/schemas/{model}")
    item.pop("definitions", None)
    schema["properties"]["readings"]["items"] = item


class ReadingBatch(BaseModel):
    """Lote de lecturas. La Pi manda de 1 a 500 por request.

    Aquí solo se valida el SOBRE (de 1 a 500 elementos). Cada lectura se
    valida por separado en el endpoint con `Reading`, para que una lectura
    mala no tumbe el lote completo (validación parcial del contrato, MON-04).
    """

    readings: List[Any] = Field(..., min_items=1, max_items=500)

    class Config:
        schema_extra = staticmethod(_readings_schema)


# ── Respuesta de ingesta ───────────────────────────────────────────────

class RejectedReading(BaseModel):
    index: int
    reason: str


class GatewayCommand(BaseModel):
    """Orden para el Arduino que el gateway escribe por Serial (ACT-02).

    El gateway la convierte en una línea `SG,<id>,MODO,AUTO`,
    `SG,<id>,BOMBA,ON`, etc. (gateway/PROTOCOLO_ARDUINO.md).
    """

    id: int = Field(..., example=17)
    kind: str = Field(..., example="pump", description="mode | pump")
    value: str = Field(..., example="ON", description="AUTO | MANUAL (mode) · ON | OFF (pump)")
    actuator_id: Optional[str] = Field(None, example="bomba-riego-01")


class IngestResult(BaseModel):
    accepted: int = 0
    duplicates: int = 0
    rejected: List[RejectedReading] = Field(default_factory=list)
    commands: List[GatewayCommand] = Field(
        default_factory=list,
        description="Órdenes pendientes para el Arduino de este gateway (ACT-02)",
    )


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


# ── Últimas lecturas (DASH-01) ─────────────────────────────────────────

class LatestMetric(BaseModel):
    """Último valor conocido de una medición del vivero."""

    key: str = Field(..., example="temp_c")
    label: str = Field(..., example="Temperatura del aire")
    unit: str = Field(..., example="°C")
    # Strict: los enteros siguen siendo enteros y los decimales no se truncan
    value: Optional[Union[StrictInt, StrictFloat]] = Field(
        None, description="Último valor NO nulo; null si nunca ha llegado uno"
    )
    raw: Optional[int] = Field(None, description="Lectura ADC cruda del mismo momento")
    ts: Optional[datetime] = None
    age_seconds: Optional[int] = Field(None, description="Segundos desde la lectura")
    stale: bool = Field(..., description="Más vieja que el intervalo configurado")
    sensor_down: bool = Field(
        ..., description="La lectura más reciente del vivero trae este campo en null"
    )
    device_id: Optional[str] = None
    device_location: Optional[str] = None
    area: str = Field(
        "general",
        description=(
            "Área del vivero a la que pertenece la medición. El prototipo es un "
            "solo ambiente, así que hoy siempre es SMARTGREENAI_DEFAULT_AREA"
        ),
    )


class LatestReadingSummary(BaseModel):
    """Resumen de la lectura más reciente del vivero."""

    ts: datetime
    received_at: datetime
    age_seconds: int
    stale: bool
    device_id: str
    estado: Optional[EstadoEnum] = None
    alertas: List[AlertaEnum] = Field(default_factory=list)
    riego_sugerido: Optional[bool] = None
    bomba_habilitada: Optional[bool] = None
    vent: Optional[VentEnum] = None
    es_dia: Optional[bool] = None


class LatestReadings(BaseModel):
    greenhouse_id: str
    generated_at: datetime
    stale_after_minutes: int
    has_data: bool
    last_reading: Optional[LatestReadingSummary] = None
    metrics: List[LatestMetric]


class GreenhouseSummary(BaseModel):
    """Un vivero que el usuario puede consultar (selector del dashboard)."""

    greenhouse_id: str = Field(..., example="vivero-rabano-01")
    devices: int = Field(..., description="Dispositivos registrados en el vivero")
    last_seen_at: Optional[datetime] = Field(
        None, description="Última vez que uno de sus dispositivos envió lecturas"
    )


# ── Estado de los actuadores (ACT-04) ──────────────────────────────────

class PowerState(str, Enum):
    """Lo que el dashboard muestra de cada equipo.

    `unknown` = nadie confirmó el estado (o la confirmación es vieja). Un
    equipo sin confirmación NUNCA se reporta como `off`.
    """

    on = "on"
    off = "off"
    unavailable = "unavailable"
    unknown = "unknown"


class PowerReason(str, Enum):
    """Por qué el estado es el que es (para la UI y para depurar)."""

    reported = "reported"                  # el Arduino lo confirmó hace poco
    tank_low = "tank_low"                  # la bomba está bloqueada por falta de agua
    gateway_inactive = "gateway_inactive"  # su gateway está desactivado
    no_gateway = "no_gateway"              # no tiene gateway: nadie reporta su estado
    not_reported = "not_reported"          # el firmware no reporta este tipo de equipo
    never_reported = "never_reported"      # aún no llega ninguna lectura con su estado
    stale = "stale"                        # la última confirmación es vieja


class ActuatorLive(BaseModel):
    """Un actuador activo del vivero con su estado actual."""

    actuator_id: str = Field(..., example="bomba-riego-01")
    name: str = Field(..., example="Bomba de riego")
    type: ActuatorType
    area: str = Field(..., example="general")
    control_channel: str = Field(..., example="L298N-B/D5")
    gateway_device_id: Optional[str] = None
    state: PowerState
    reason: PowerReason
    detail: str = Field(..., description="Explicación corta en español para la UI")
    reported_at: Optional[datetime] = Field(
        None, description="Hora de la lectura que reportó el estado (UTC)"
    )
    age_seconds: Optional[int] = Field(None, description="Segundos desde esa lectura")
    last_command: Optional["CommandOut"] = Field(
        None, description="Última orden manual a este equipo en los últimos 10 min (ACT-02)"
    )


class ActuatorLiveList(BaseModel):
    greenhouse_id: str
    generated_at: datetime
    stale_after_minutes: int = Field(
        ..., description="Una confirmación más vieja que esto se muestra como unknown"
    )
    actuators: List[ActuatorLive]


# ── Alertas (ALERT-01) ─────────────────────────────────────────────────

class AlertDirection(str, Enum):
    high = "high"   # por arriba del límite
    low = "low"     # por abajo del límite


class AlertState(str, Enum):
    active = "active"
    cleared = "cleared"


class AlertOut(BaseModel):
    """Una alerta: el Arduino reportó que una medición cruzó su límite."""

    id: int
    greenhouse_id: str
    area: str = Field(..., example="general")
    metric: str = Field(..., example="temp_c", description="Sensor (campo de la lectura)")
    label: str = Field(..., example="Temperatura del aire")
    unit: str = Field(..., example="°C")
    device_id: str = Field(..., example="pi-vivero-01")
    code: str = Field(..., example="TEMP_ALTA", description="Código que mandó el Arduino")
    direction: AlertDirection
    threshold: float = Field(..., example=30.0, description="Umbral que cruzó (del firmware)")
    threshold_name: str = Field(..., example="TEMP_LIMITE_MAX")
    comparison: str = Field(..., example=">", description="Cómo compara el firmware")
    value: float = Field(..., example=31.2, description="Valor que la disparó")
    reading_ts: datetime = Field(..., description="Hora de la medición que la disparó")
    created_at: datetime = Field(..., description="Hora en que el backend la creó")
    last_value: float
    last_ts: datetime = Field(..., description="Última lectura que seguía fuera del límite")
    worst_value: float
    status: AlertState
    cleared_at: Optional[datetime] = None
    cleared_ts: Optional[datetime] = Field(None, description="Lectura con la que se cerró")
    cleared_value: Optional[float] = None
    duration_seconds: int = Field(..., description="Desde la medición hasta el cierre (o hasta ahora)")


class AlertList(BaseModel):
    greenhouse_id: str
    generated_at: datetime
    active_count: int
    clear_after_seconds: int = Field(
        ..., description="Segundos seguidos sin el código para cerrar una alerta"
    )
    alerts: List[AlertOut]


# ── Riego: modo, órdenes y acciones (ACT-02) ───────────────────────────

class IrrigationMode(str, Enum):
    auto = "auto"      # el Arduino riega solo con sus umbrales
    manual = "manual"  # no riega solo: solo actúan las órdenes manuales


class CommandAction(str, Enum):
    on = "on"
    off = "off"


class CommandStatus(str, Enum):
    pending = "pending"        # esperando a que el gateway la recoja
    sent = "sent"              # entregada al gateway; falta que el Arduino confirme
    confirmed = "confirmed"    # el Arduino la aplicó (cmd_id)
    rejected = "rejected"      # el Arduino no la aplicó (cmd_err)
    expired = "expired"        # nadie la confirmó a tiempo
    superseded = "superseded"  # otra orden más nueva la reemplazó antes de salir


class CommandOut(BaseModel):
    id: int
    actuator_id: Optional[str] = None
    kind: str = Field(..., description="mode | pump")
    value: str = Field(..., description="AUTO | MANUAL | ON | OFF")
    trigger: str = Field(..., description="manual (una persona) | system (re-sincronizar el modo)")
    reason: str
    status: CommandStatus
    error: Optional[str] = Field(None, description="cmd_err del Arduino si la rechazó")
    user_name: Optional[str] = None
    created_at: datetime
    sent_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None


class FirmwareIrrigation(BaseModel):
    """Parámetros de riego que usa el Arduino (viven en el firmware)."""

    target_pct: float = Field(..., example=50, description="SUELO_LIMITE_MIN")
    max_run_seconds: int = Field(..., example=60, description="BOMBA_MAX_MS")
    rest_seconds: int = Field(..., example=300, description="BOMBA_DESCANSO_MS")


class IrrigationStatus(BaseModel):
    greenhouse_id: str
    mode: IrrigationMode = Field(..., description="Modo elegido en la plataforma (auto por defecto)")
    reported_mode: Optional[IrrigationMode] = Field(
        None, description="Modo que reporta el Arduino; null si su firmware no acepta órdenes"
    )
    controllable: bool = Field(..., description="Se le pueden mandar órdenes al Arduino")
    detail: str = Field(..., description="Explicación corta para la UI")
    controller_device_id: Optional[str] = None
    reported_at: Optional[datetime] = None
    mode_changed_at: Optional[datetime] = None
    mode_changed_by: Optional[str] = None
    last_mode_command: Optional[CommandOut] = Field(
        None, description="Última orden de cambio de modo en los últimos 10 min"
    )
    firmware: FirmwareIrrigation


class ModeChange(BaseModel):
    mode: IrrigationMode


class CommandCreate(BaseModel):
    action: CommandAction

    class Config:
        extra = "forbid"


class ActuatorEventOut(BaseModel):
    """Un encendido o apagado que reportó el Arduino (ACT-02 #4)."""

    id: int
    actuator_id: str
    actuator_name: str
    actuator_type: ActuatorType
    event: CommandAction
    origin: str = Field(..., description="auto (lo decidió el Arduino) | manual (una persona)")
    reason: str
    user_name: Optional[str] = None
    reading_ts: datetime


class ActuatorEventList(BaseModel):
    greenhouse_id: str
    events: List[ActuatorEventOut]


ActuatorLive.update_forward_refs(CommandOut=CommandOut)


# ── Gráficas históricas (DASH-02) ──────────────────────────────────────

class MetricKey(str, Enum):
    """Mediciones que se pueden graficar y analizar.

    Es el mismo catálogo de app/metrics.py (lo comprueba
    tests/test_readings_history.py): el nombre de columna que llega a una
    consulta SQL sale de ahí, nunca del texto que mandó el cliente.
    """

    temp_c = "temp_c"
    hum_aire_pct = "hum_aire_pct"
    suelo_pct = "suelo_pct"
    nivel_pct = "nivel_pct"
    luz_nivel = "luz_nivel"
    co2_ppm = "co2_ppm"


class SeriesPoint(BaseModel):
    """Un punto de la gráfica: una lectura, o el promedio de un bucket.

    En modo agregado `min` y `max` son el rango real del bucket, para que la
    gráfica pueda dibujar la banda y no esconda un pico dentro del promedio.
    """

    ts: datetime = Field(..., description="Hora de la lectura, o inicio del bucket (UTC)")
    value: Union[StrictInt, StrictFloat] = Field(..., description="Lectura o promedio del bucket")
    min: Optional[Union[StrictInt, StrictFloat]] = Field(None, description="Mínimo del bucket")
    max: Optional[Union[StrictInt, StrictFloat]] = Field(None, description="Máximo del bucket")
    count: int = Field(..., description="Lecturas que representa (1 en modo crudo)")


class ReadingSeries(BaseModel):
    """Serie lista para dibujar de UNA medición en un rango de tiempo.

    `bucket_seconds` 0 y `aggregated` false significan que son las lecturas
    tal como se guardaron. Si en el rango hay más de `max_points` lecturas se
    devuelven promediadas por bucket para que la gráfica siga siendo legible
    (criterio 3 de DASH-02); `bucket_seconds` dice de cuánto es cada paso.
    """

    greenhouse_id: str
    metric: MetricKey
    label: str = Field(..., example="Temperatura del aire")
    unit: str = Field(..., example="°C")
    from_: datetime = Field(..., alias="from", description="Inicio del rango consultado (incluido)")
    to: datetime = Field(..., description="Fin del rango consultado (excluido)")
    generated_at: datetime
    bucket_seconds: int = Field(..., description="Tamaño del bucket; 0 = lecturas sin agregar")
    aggregated: bool = Field(..., description="Los puntos son promedios, no lecturas")
    max_points: int = Field(..., description="Tope de puntos que se pidió")
    reading_count: int = Field(..., description="Lecturas con valor en el rango")
    point_count: int
    has_data: bool = Field(..., description="false = rango válido pero sin lecturas")
    value_min: Optional[Union[StrictInt, StrictFloat]] = Field(
        None, description="Mínimo de la serie (para la escala vertical)"
    )
    value_max: Optional[Union[StrictInt, StrictFloat]] = Field(
        None, description="Máximo de la serie (para la escala vertical)"
    )
    points: List[SeriesPoint] = Field(default_factory=list)

    class Config:
        allow_population_by_field_name = True


# ── Análisis histórico (AI-01) ─────────────────────────────────────────

class AnalysisGap(BaseModel):
    """Tramo del rango sin lecturas de esa medición.

    Se reportan en vez de rellenarse: el criterio 3 de AI-01 pide que los
    huecos se digan, no que se tapen con el valor anterior.
    """

    from_: datetime = Field(..., alias="from")
    to: datetime
    seconds: int
    buckets: int

    class Config:
        allow_population_by_field_name = True


class VariableAnalysis(BaseModel):
    """Tendencia de una medición en el periodo, con la calidad del dato."""

    key: MetricKey
    label: str
    unit: str
    trend: TrendDirection = Field(
        ..., description="rising | falling | stable | insufficient_data"
    )
    slope_per_hour: Optional[float] = Field(
        None, description="Pendiente de la recta ajustada, en unidades por hora"
    )
    change: Optional[float] = Field(
        None, description="Cambio que estima la recta entre el primer y el último dato"
    )
    stable_band: float = Field(
        ..., description="Cambio que se considera ruido: debajo de esto, `stable`"
    )
    observed_hours: Optional[float] = Field(
        None, description="Horas entre el primer y el último dato (puede ser menos que el rango)"
    )
    first: Optional[float] = None
    last: Optional[float] = None
    mean: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None
    fit_r2: Optional[float] = Field(
        None, description="0 a 1: qué tan bien la recta explica la serie"
    )
    samples: int = Field(..., description="Buckets con dato")
    expected_samples: int = Field(..., description="Buckets que cubren el rango")
    coverage_pct: float = Field(..., description="Porcentaje del periodo con dato")
    readings_used: int = Field(..., description="Lecturas que entraron a los promedios")
    readings_missing: int = Field(
        ..., description="Lecturas del rango con esta medición en null (sensor caído)"
    )
    gaps: List[AnalysisGap] = Field(default_factory=list)


class AnalysisPeriod(BaseModel):
    """Periodo y resolución con los que se corrió el análisis.

    Va en la respuesta porque el criterio 1 de AI-01 pide que el indicador
    venga "junto con el periodo usado", y porque es lo que hace falta para
    repetir el mismo análisis y obtener el mismo resultado (criterio 2).
    """

    from_: datetime = Field(..., alias="from")
    to: datetime
    hours: float
    bucket_seconds: int
    expected_samples: int

    class Config:
        allow_population_by_field_name = True


class GreenhouseAnalysis(BaseModel):
    greenhouse_id: str
    generated_at: datetime = Field(
        ..., description="Metadato de la respuesta; no entra en el cálculo"
    )
    period: AnalysisPeriod
    has_data: bool
    readings: int = Field(..., description="Lecturas del vivero en el rango")
    variables: List[VariableAnalysis]
