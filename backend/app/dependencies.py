"""
Dependencias de FastAPI para autenticar USUARIOS (AUTH-01).

get_current_user valida, en este orden:
  1. Que venga el header Authorization: Bearer <jwt>        → si no, 401 missing_token
  2. Firma y vencimiento del JWT                            → si no, 401 invalid_token
  3. Que la sesión exista, no esté revocada ni vencida      → si no, 401 session_expired
  4. Que no lleve más de SESSION_IDLE_MINUTES sin actividad → revoca y 401 session_expired
  5. Que el usuario siga activo                             → revoca y 401 session_expired
Si todo pasa, renueva last_seen_at y devuelve el usuario con el rol LEÍDO DE
LA BD (no el del token).

La autenticación de DISPOSITIVOS (header X-API-Key) sigue en app/auth.py.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.database import get_db
from app.models import Role
from app.security import TokenError, decode_access_token

# auto_error=False: con True, FastAPI 0.109 responde 403 cuando falta el
# header, y AUTH-02 exige 401. El 401 lo lanzamos nosotros.
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="JWT obtenido en POST /api/v1/auth/login",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: str) -> datetime:
    """Convierte un timestamp ISO 8601 guardado en la BD a datetime con zona UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def unauthorized(error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"error": error, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _revoke_session(db: aiosqlite.Connection, session_id: str, now: datetime) -> None:
    await db.execute(
        "UPDATE sessions SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL",
        (now.isoformat(), session_id),
    )
    await db.commit()


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Usuario autenticado de la request actual (ver docstring del módulo)."""
    if credentials is None or not credentials.credentials:
        raise unauthorized("missing_token", "Inicia sesión para continuar.")

    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
        session_id = str(payload["sid"])
    except (TokenError, ValueError, KeyError):
        raise unauthorized("invalid_token", "Tu sesión no es válida. Inicia sesión de nuevo.")

    cursor = await db.execute(
        """
        SELECT s.session_id, s.last_seen_at, s.expires_at, s.revoked_at,
               u.id, u.username, u.email, u.full_name, u.role,
               u.greenhouse_id, u.status
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.session_id = ? AND s.user_id = ?
        """,
        (session_id, user_id),
    )
    row = await cursor.fetchone()
    now = utcnow()

    if row is None or row["revoked_at"] is not None:
        raise unauthorized("session_expired", "Tu sesión terminó. Inicia sesión de nuevo.")

    if now >= parse_utc(row["expires_at"]):
        await _revoke_session(db, session_id, now)
        raise unauthorized("session_expired", "Tu sesión venció. Inicia sesión de nuevo.")

    idle_limit = timedelta(minutes=settings.session_idle_minutes)
    if now - parse_utc(row["last_seen_at"]) > idle_limit:
        await _revoke_session(db, session_id, now)
        raise unauthorized(
            "session_expired",
            "Tu sesión se cerró por inactividad. Inicia sesión de nuevo.",
        )

    if row["status"] != "active":
        await _revoke_session(db, session_id, now)
        raise unauthorized("session_expired", "Tu sesión terminó. Inicia sesión de nuevo.")

    await db.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE session_id = ?",
        (now.isoformat(), session_id),
    )
    await db.commit()

    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "full_name": row["full_name"],
        "role": Role(row["role"]),
        "greenhouse_id": row["greenhouse_id"],
        "session_id": session_id,
    }
