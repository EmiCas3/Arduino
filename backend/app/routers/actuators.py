"""
Registro de actuadores (CONF-05).

Un Administrador registra los equipos que realmente existen en un vivero
(bomba, ventilador, malla sombra, luz) con su vivero-área y su canal de
control. Solo lo registrado y ACTIVO aparece en las pantallas de control y
puede recibir comandos. Los comandos llegan con ACT-02 / ACT-04 y deben pasar
por get_active_actuator_or_404().

Desactivar NO borra: el registro y su historial se conservan (no hay DELETE).

Permisos (AUTH-02):
    POST  /actuators          admin, super_admin
    GET   /actuators          todos; un productor solo ve los ACTIVOS de su vivero
    GET   /actuators/{id}     todos; un productor solo ve los ACTIVOS de su vivero
    PATCH /actuators/{id}     admin, super_admin
"""

import sqlite3
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.database import get_db
from app.dependencies import (
    ADMIN_ROLES,
    ALL_ROLES,
    forbidden,
    get_current_user,
    require_roles,
    utcnow,
    visible_greenhouse,
)
from app.models import (
    SLUG_REGEX,
    Actuator,
    ActuatorCreate,
    ActuatorStatus,
    ActuatorType,
    ActuatorUpdate,
    ErrorResponse,
)

# Denegado por default: cualquier endpoint de este router exige sesión.
router = APIRouter(
    prefix="/actuators",
    tags=["actuators"],
    dependencies=[Depends(get_current_user)],
)

_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422)}

# Columnas que un PATCH puede tocar. Los nombres de columna del UPDATE salen
# SOLO de esta lista, nunca de texto del cliente.
_EDITABLE = {"name", "area", "control_channel", "model", "status"}
_NOT_NULLABLE = {"name", "area", "control_channel", "status"}


def _error(status: int, error: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": error, "message": message})


async def _fetch(db: aiosqlite.Connection, actuator_id: str) -> Optional[aiosqlite.Row]:
    cursor = await db.execute("SELECT * FROM actuators WHERE actuator_id = ?", (actuator_id,))
    return await cursor.fetchone()


async def _channel_taken(
    db: aiosqlite.Connection,
    greenhouse_id: str,
    control_channel: str,
    exclude_id: str = "",
) -> bool:
    """¿Otro actuador ACTIVO del mismo vivero ya usa ese canal?"""
    cursor = await db.execute(
        """
        SELECT 1 FROM actuators
        WHERE greenhouse_id = ? AND control_channel = ? AND status = 'active'
          AND actuator_id != ?
        """,
        (greenhouse_id, control_channel, exclude_id),
    )
    return await cursor.fetchone() is not None


def _can_see(user: dict, row) -> bool:
    if user["role"] in ADMIN_ROLES:
        return True
    return row["status"] == "active" and row["greenhouse_id"] == user.get("greenhouse_id")


def _to_model(row) -> Actuator:
    return Actuator(**dict(row))


async def get_active_actuator_or_404(
    db: aiosqlite.Connection,
    actuator_id: str,
    user: Optional[dict] = None,
) -> dict:
    """Puerta obligatoria para COMANDAR un actuador (ACT-02 / ACT-04).

    Un actuador que no está registrado, que está desactivado o que el usuario
    no puede ver responde 404: no aparece y no puede comandarse (CONF-05).
    """
    row = await _fetch(db, actuator_id)
    if row is None or row["status"] != "active" or (user is not None and not _can_see(user, row)):
        raise _error(
            404,
            "actuator_not_available",
            "El actuador no está registrado o está desactivado.",
        )
    return dict(row)


@router.post(
    "",
    response_model=Actuator,
    status_code=201,
    responses=_ERRORS,
    summary="Registrar un actuador",
    description=(
        "Solo Administradores. Registra un equipo instalado (pump, fan, shade, "
        "light) con su vivero, área y canal de control."
    ),
)
async def register_actuator(
    body: ActuatorCreate,
    user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> Actuator:
    if await _fetch(db, body.actuator_id) is not None:
        raise _error(
            409,
            "actuator_already_exists",
            f"Ya existe un actuador con el id {body.actuator_id}.",
        )

    if body.gateway_device_id is not None:
        cursor = await db.execute(
            "SELECT greenhouse_id FROM devices WHERE device_id = ?",
            (body.gateway_device_id,),
        )
        device = await cursor.fetchone()
        if device is None:
            raise _error(
                404,
                "gateway_not_registered",
                f"El gateway {body.gateway_device_id} no está registrado.",
            )
        if device["greenhouse_id"] != body.greenhouse_id:
            raise _error(
                422,
                "gateway_greenhouse_mismatch",
                "El gateway pertenece a otro vivero.",
            )

    if await _channel_taken(db, body.greenhouse_id, body.control_channel):
        raise _error(
            409,
            "control_channel_in_use",
            f"El canal {body.control_channel} ya lo usa otro actuador activo de este vivero.",
        )

    try:
        await db.execute(
            """
            INSERT INTO actuators (actuator_id, name, type, greenhouse_id, area,
                                   control_channel, gateway_device_id, model,
                                   status, registered_at, registered_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                body.actuator_id,
                body.name,
                body.type.value,
                body.greenhouse_id,
                body.area,
                body.control_channel,
                body.gateway_device_id,
                body.model,
                utcnow().isoformat(),
                user["id"],
            ),
        )
        await db.commit()
    except sqlite3.IntegrityError:
        # Carrera: otra request guardó el mismo id o canal entre la validación
        # y el INSERT. El índice único de la BD es la última línea de defensa.
        await db.rollback()
        raise _error(
            409,
            "actuator_conflict",
            "Otro registro con el mismo id o canal se guardó al mismo tiempo.",
        )

    return _to_model(await _fetch(db, body.actuator_id))


@router.get(
    "",
    response_model=List[Actuator],
    responses=_ERRORS,
    summary="Listar actuadores",
    description=(
        "Lo que consumen las pantallas de control. Por defecto solo los "
        "activos. Un productor solo ve los de su vivero y no puede pedir "
        "`status=inactive`."
    ),
)
async def list_actuators(
    greenhouse_id: Optional[str] = Query(None, description="Filtrar por vivero"),
    area: Optional[str] = Query(None, description="Filtrar por área"),
    actuator_type: Optional[ActuatorType] = Query(None, alias="type"),
    status: ActuatorStatus = Query(ActuatorStatus.active),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> List[Actuator]:
    if status != ActuatorStatus.active and user["role"] not in ADMIN_ROLES:
        raise forbidden("Solo un Administrador puede ver actuadores desactivados.")

    where = ["status = ?"]
    params: list = [status.value]

    scope = visible_greenhouse(user)
    if scope is not None:
        if greenhouse_id is not None and greenhouse_id != scope:
            raise forbidden("No tienes acceso a este vivero.")
        where.append("greenhouse_id = ?")
        params.append(scope)
    elif greenhouse_id is not None:
        where.append("greenhouse_id = ?")
        params.append(greenhouse_id)

    if area is not None:
        where.append("area = ?")
        params.append(area)
    if actuator_type is not None:
        where.append("type = ?")
        params.append(actuator_type.value)

    cursor = await db.execute(
        f"SELECT * FROM actuators WHERE {' AND '.join(where)} "
        "ORDER BY greenhouse_id, area, name",
        params,
    )
    return [_to_model(row) for row in await cursor.fetchall()]


@router.get(
    "/{actuator_id}",
    response_model=Actuator,
    responses=_ERRORS,
    summary="Obtener un actuador",
)
async def get_actuator(
    actuator_id: str = Path(..., pattern=SLUG_REGEX),
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> Actuator:
    row = await _fetch(db, actuator_id)
    if row is None or not _can_see(user, row):
        raise _error(404, "actuator_not_found", "El actuador no existe.")
    return _to_model(row)


@router.patch(
    "/{actuator_id}",
    response_model=Actuator,
    responses=_ERRORS,
    summary="Editar, desactivar o reactivar un actuador",
    description=(
        "Solo Administradores. `status=inactive` lo quita de las pantallas de "
        "control sin borrar su historial. `type` y `greenhouse_id` no se editan."
    ),
)
async def update_actuator(
    body: ActuatorUpdate,
    actuator_id: str = Path(..., pattern=SLUG_REGEX),
    user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> Actuator:
    current = await _fetch(db, actuator_id)
    if current is None:
        raise _error(404, "actuator_not_found", "El actuador no existe.")

    changes = body.dict(exclude_unset=True)
    if not changes:
        raise _error(422, "nothing_to_update", "Envía al menos un campo para actualizar.")
    nulls = sorted(field for field in changes if field in _NOT_NULLABLE and changes[field] is None)
    if nulls:
        raise _error(422, "invalid_null", f"Estos campos no pueden ser null: {', '.join(nulls)}.")
    if "status" in changes:
        changes["status"] = changes["status"].value

    new_status = changes.get("status", current["status"])
    new_channel = changes.get("control_channel", current["control_channel"])
    if new_status == "active" and await _channel_taken(
        db, current["greenhouse_id"], new_channel, exclude_id=actuator_id
    ):
        raise _error(
            409,
            "control_channel_in_use",
            f"El canal {new_channel} ya lo usa otro actuador activo de este vivero.",
        )

    now = utcnow().isoformat()
    assert set(changes) <= _EDITABLE  # extra="forbid" en el modelo ya lo garantiza
    changes["updated_at"] = now
    if new_status != current["status"]:
        changes["deactivated_at"] = now if new_status == "inactive" else None

    assignments = ", ".join(f"{column} = ?" for column in changes)
    try:
        await db.execute(
            f"UPDATE actuators SET {assignments} WHERE actuator_id = ?",
            (*changes.values(), actuator_id),
        )
        await db.commit()
    except sqlite3.IntegrityError:
        await db.rollback()
        raise _error(
            409,
            "actuator_conflict",
            "Otro actuador activo tomó ese canal al mismo tiempo.",
        )

    return _to_model(await _fetch(db, actuator_id))
