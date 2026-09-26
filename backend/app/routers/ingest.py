"""
Endpoint de ingesta: POST /devices/{device_id}/readings  (MON-04)

Recibe lotes de 1-500 lecturas de la Raspberry Pi y valida CADA lectura por
separado ("validación parcial" del contrato): las inválidas se listan en
`rejected` con su índice y motivo, y las válidas se guardan. Descarta
duplicados por (device_id, ts) y responde 202 con el resumen.

Reglas del timestamp (ajuste de MON-04, Sprint 2):
- `ts` es obligatorio, texto ISO 8601 y CON zona horaria. Sin zona se rechaza.
- Se guarda siempre en UTC con milisegundos (2026-09-26T18:04:22.123+00:00).
  Así la misma lectura escrita en dos formatos cuenta como duplicada y el
  orden por texto de la columna `ts` es el orden real en el tiempo.
- Se rechazan horas imposibles: antes de SMARTGREENAI_MIN_READING_TS (una Pi
  sin sincronizar cree que es 1970) o más de SMARTGREENAI_MAX_CLOCK_SKEW_SECONDS
  en el futuro (se quedaría para siempre como la "última lectura").
- El backend NUNCA pone la hora de la lectura; solo agrega `received_at`.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import ValidationError

import aiosqlite

from app.auth import verify_api_key
from app.config import settings
from app.database import get_db
from app.models import (
    ErrorResponse,
    IngestResult,
    Reading,
    ReadingBatch,
    RejectedReading,
)

router = APIRouter(prefix="/devices", tags=["ingest"])


def utc_ms(ts: datetime) -> str:
    """Formato único con el que se guarda `ts`: UTC y milisegundos."""
    return ts.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def describe_errors(exc: ValidationError) -> str:
    """Resume los errores de Pydantic en una línea: 'campo: motivo; ...'."""
    parts = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err["loc"]) or "lectura"
        parts.append(f"{field}: {err['msg']}")
    return "; ".join(parts)


def ts_out_of_range(ts: datetime, now: datetime) -> Optional[str]:
    """Motivo de rechazo si la hora es imposible; None si es aceptable."""
    if ts < settings.min_reading_ts:
        return (
            f"ts: anterior a {utc_ms(settings.min_reading_ts)}; "
            "el reloj del gateway no está sincronizado"
        )
    max_skew = settings.max_clock_skew_seconds
    if ts > now + timedelta(seconds=max_skew):
        return f"ts: en el futuro, más de {max_skew} s adelante del servidor"
    return None


@router.post(
    "/{device_id}/readings",
    response_model=IngestResult,
    status_code=202,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
    },
    summary="Enviar un lote de mediciones",
    description=(
        "Endpoint que llama la Raspberry Pi. Acepta de 1 a 500 lecturas por "
        "request. La API key del header debe pertenecer al device_id de la ruta. "
        "Cada lectura se valida por separado: las inválidas regresan en "
        "`rejected` y las válidas se guardan. `ts` debe traer zona horaria y se "
        "guarda en UTC con milisegundos."
    ),
)
async def ingest_readings(
    batch: ReadingBatch,
    device: dict = Depends(verify_api_key),
    db: aiosqlite.Connection = Depends(get_db),
) -> IngestResult:
    """
    Flujo:
    1. Auth ya fue validada por verify_api_key (401/404 si falla).
    2. Pydantic ya validó el SOBRE del lote: 1 a 500 elementos (422 si no).
    3. Cada elemento se valida como `Reading` y su `ts` contra el rango
       válido. Si falla, va a `rejected` con su índice y motivo.
    4. Las válidas: INSERT OR IGNORE → contar accepted/duplicates.
    5. Actualizar last_seen_at del device si se guardó algo.
    6. Retornar 202 con IngestResult.
    """
    device_id = device["device_id"]
    greenhouse_id = device["greenhouse_id"]
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    accepted = 0
    duplicates = 0
    rejected: List[RejectedReading] = []

    for idx, raw in enumerate(batch.readings):
        if not isinstance(raw, dict):
            rejected.append(RejectedReading(index=idx, reason="la lectura no es un objeto JSON"))
            continue

        try:
            reading = Reading.parse_obj(raw)
        except ValidationError as exc:
            rejected.append(RejectedReading(index=idx, reason=describe_errors(exc)))
            continue

        reason = ts_out_of_range(reading.ts, now_dt)
        if reason:
            rejected.append(RejectedReading(index=idx, reason=reason))
            continue

        try:
            # Serializar alertas como JSON string
            alertas_json = (
                json.dumps([a.value for a in reading.alertas])
                if reading.alertas is not None
                else None
            )

            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO readings (
                    device_id, greenhouse_id, received_at, ts, ms,
                    temp_c, hum_aire_pct,
                    suelo_raw, suelo_pct, nivel_raw, nivel_pct,
                    luz_raw, luz_nivel,
                    es_dia, sol_min_hoy, sol_min_prev,
                    riego_sugerido, bomba_habilitada, vent,
                    estado, alertas
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    greenhouse_id,
                    now,
                    utc_ms(reading.ts),
                    reading.ms,
                    reading.temp_c,
                    reading.hum_aire_pct,
                    reading.suelo_raw,
                    reading.suelo_pct,
                    reading.nivel_raw,
                    reading.nivel_pct,
                    reading.luz_raw,
                    reading.luz_nivel,
                    int(reading.es_dia) if reading.es_dia is not None else None,
                    reading.sol_min_hoy,
                    reading.sol_min_prev,
                    int(reading.riego_sugerido) if reading.riego_sugerido is not None else None,
                    int(reading.bomba_habilitada) if reading.bomba_habilitada is not None else None,
                    reading.vent.value if reading.vent is not None else None,
                    reading.estado.value if reading.estado is not None else None,
                    alertas_json,
                ),
            )

            if cursor.rowcount > 0:
                accepted += 1
            else:
                duplicates += 1

        except Exception as e:
            rejected.append(RejectedReading(index=idx, reason=f"no se pudo guardar: {e}"))

    await db.commit()

    # Actualizar last_seen_at del dispositivo
    if accepted > 0:
        await db.execute(
            "UPDATE devices SET last_seen_at = ? WHERE device_id = ?",
            (now, device_id),
        )
        await db.commit()

    return IngestResult(
        accepted=accepted,
        duplicates=duplicates,
        rejected=rejected,
    )
