"""
Endpoints de sesión de usuario (AUTH-01): login, logout y "quién soy".

Reglas de AUTH-01:
  - Se entra con usuario O email + contraseña.
  - Credenciales inválidas → el MISMO 401 genérico, sin decir qué campo falló
    (usuario inexistente, contraseña incorrecta y usuario inactivo responden
    igual y tardan lo mismo) y SIN crear sesión.
  - La sesión se cierra tras SMARTGREENAI_SESSION_IDLE_MINUTES sin actividad
    (lo valida get_current_user en cada request).
"""

import uuid
from datetime import timedelta

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Response
from starlette.concurrency import run_in_threadpool

from app import audit
from app.config import settings
from app.database import get_db
from app.dependencies import ALL_ROLES, require_roles, utcnow
from app.models import ErrorResponse, LoginRequest, LoginResponse, UserPublic
from app.security import (
    DUMMY_HASH,
    create_access_token,
    hash_password,
    needs_rehash,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

INVALID_CREDENTIALS = {
    "error": "invalid_credentials",
    "message": "Usuario o contraseña incorrectos.",
}


def to_public(user) -> UserPublic:
    """Construye la vista pública de un usuario (fila de BD o dict)."""
    return UserPublic(
        id=user["id"],
        username=user["username"],
        email=user["email"],
        full_name=user["full_name"],
        role=user["role"],
        greenhouse_id=user["greenhouse_id"],
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    responses={401: {"model": ErrorResponse}},
    summary="Iniciar sesión",
    description=(
        "Recibe usuario o email y contraseña. Si son válidos crea una sesión y "
        "devuelve un JWT para el header `Authorization: Bearer`. Cualquier "
        "credencial inválida responde el mismo 401 genérico."
    ),
)
async def login(
    body: LoginRequest,
    db: aiosqlite.Connection = Depends(get_db),
) -> LoginResponse:
    cursor = await db.execute(
        # username y email tienen COLLATE NOCASE: la búsqueda ignora mayúsculas
        "SELECT * FROM users WHERE username = ? OR email = ? LIMIT 1",
        (body.login, body.login),
    )
    user = await cursor.fetchone()

    # Siempre se verifica un hash (el dummy si el usuario no existe) para que
    # el tiempo de respuesta no delate qué usuarios existen. scrypt es costoso
    # a propósito, así que corre fuera del event loop.
    stored_hash = user["password_hash"] if user is not None else DUMMY_HASH
    password_ok = await run_in_threadpool(verify_password, body.password, stored_hash)

    now = utcnow()
    if user is None or not password_ok or user["status"] != "active":
        # Bitácora (AUTH-05). El motivo queda aquí, nunca en la respuesta. Si la
        # cuenta no existe NO se guarda lo que se tecleó: podría ser una
        # contraseña escrita en el campo equivocado (NFR-03).
        if user is None:
            actor, reason = audit.ANONYMOUS_ACTOR, "unknown_user"
        else:
            actor = audit.user_actor(user)
            reason = "wrong_password" if not password_ok else "inactive_user"
        await audit.record(
            db, action="login_failed", actor=actor,
            greenhouse_id=user["greenhouse_id"] if user is not None else None,
            summary="Intento de inicio de sesión rechazado",
            detail={"reason": reason}, now=now,
        )
        await db.commit()
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)

    # NFR-03: si el hash guardado no está en el esquema preferido (una cuenta
    # de antes de bcrypt), se vuelve a guardar ahora que se conoce la contraseña.
    rehashed = needs_rehash(stored_hash)
    if rehashed:
        new_hash = await run_in_threadpool(hash_password, body.password)
        await db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user["id"]))

    session_id = uuid.uuid4().hex
    expires_at = now + timedelta(hours=settings.session_max_hours)

    await db.execute(
        """
        INSERT INTO sessions (session_id, user_id, created_at, last_seen_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (session_id, user["id"], now.isoformat(), now.isoformat(), expires_at.isoformat()),
    )
    await db.execute(
        "UPDATE users SET last_login_at = ? WHERE id = ?",
        (now.isoformat(), user["id"]),
    )
    await audit.record(
        db, action="login", actor=audit.user_actor(user),
        greenhouse_id=user["greenhouse_id"], target_type="session", target_id=session_id,
        summary="Inicio de sesión", now=now,
        detail={"role": user["role"], "password_rehashed": rehashed},
    )
    await db.commit()

    token = create_access_token(user["id"], session_id, user["role"], expires_at, now)
    return LoginResponse(
        access_token=token,
        expires_at=expires_at,
        idle_timeout_minutes=settings.session_idle_minutes,
        user=to_public(user),
    )


@router.post(
    "/logout",
    status_code=204,
    response_class=Response,
    responses={401: {"model": ErrorResponse}},
    summary="Cerrar sesión",
    description="Revoca la sesión actual: el token deja de servir de inmediato.",
)
async def logout(
    user: dict = Depends(require_roles(*ALL_ROLES)),
    db: aiosqlite.Connection = Depends(get_db),
) -> Response:
    now = utcnow()
    await db.execute(
        "UPDATE sessions SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL",
        (now.isoformat(), user["session_id"]),
    )
    await audit.record(
        db, action="logout", actor=audit.user_actor(user),
        greenhouse_id=user["greenhouse_id"], target_type="session",
        target_id=user["session_id"], summary="Cierre de sesión", now=now,
    )
    await db.commit()
    return Response(status_code=204)


@router.get(
    "/me",
    response_model=UserPublic,
    responses={401: {"model": ErrorResponse}},
    summary="Usuario de la sesión actual",
    description="Lo usa el frontend para saber el rol y armar el menú.",
)
async def me(user: dict = Depends(require_roles(*ALL_ROLES))) -> UserPublic:
    return to_public(user)
