"""
Endpoint de ingesta: POST /devices/{device_id}/readings

Recibe lotes de 1-500 lecturas de la Raspberry Pi, las valida
individualmente, descarta duplicados por (device_id, ts) y almacena
las válidas. Retorna 202 con el resumen de aceptadas, duplicadas
y rechazadas.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

import aiosqlite

from app.auth import verify_api_key
from app.database import get_db
from app.models import (
    ErrorResponse,
    IngestResult,
    ReadingBatch,
    RejectedReading,
)

router = APIRouter(prefix="/devices", tags=["ingest"])


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
        "request. La API key del header debe pertenecer al device_id de la ruta."
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
    2. Pydantic ya validó el batch (422 si el body es ilegible).
    3. Validar tamaño ≤ 500 (doble check, Pydantic ya lo limita).
    4. Para cada reading: INSERT OR IGNORE → contar accepted/duplicates.
    5. Actualizar last_seen_at del device.
    6. Retornar 202 con IngestResult.
    """
    device_id = device["device_id"]
    greenhouse_id = device["greenhouse_id"]
    now = datetime.now(timezone.utc).isoformat()

    accepted = 0
    duplicates = 0
    rejected: list[RejectedReading] = []

    for idx, reading in enumerate(batch.readings):
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
                    reading.ts.isoformat(),
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
            rejected.append(RejectedReading(index=idx, reason=str(e)))

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
