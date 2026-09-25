"""
Tests de AUTH-01 — User login.

Criterios de aceptación:
  1. Usuario registrado y activo + credenciales válidas → token de sesión y
     datos para aterrizar en el dashboard de su rol.
  2. Credenciales inválidas → error genérico que no revela qué campo falló y
     NO se crea sesión.
  3. Sesión inactiva más allá del timeout configurado → se rechaza y hay que
     volver a autenticarse.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.config import settings
from app.security import (
    create_access_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from tests.conftest import PASSWORDS, USERS, bearer, login

LOGIN_URL = "/api/v1/auth/login"
ME_URL = "/api/v1/auth/me"
LOGOUT_URL = "/api/v1/auth/logout"


async def count_sessions(db) -> int:
    cursor = await db.execute("SELECT COUNT(*) AS n FROM sessions")
    return (await cursor.fetchone())["n"]


# ── Criterio 1: login válido ───────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["producer", "admin", "super_admin"])
async def test_login_by_username_ok(api, role):
    """Caso 1: usuario + contraseña válidos → 200, token y rol para la landing."""
    resp = await api.post(
        LOGIN_URL,
        json={"login": USERS[role]["username"], "password": PASSWORDS[role]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["token_type"] == "bearer"
    assert data["access_token"]
    assert data["idle_timeout_minutes"] == settings.session_idle_minutes
    assert data["user"]["role"] == role
    assert data["user"]["username"] == USERS[role]["username"]


@pytest.mark.asyncio
async def test_login_by_email_case_insensitive(api):
    """Caso 2: se puede entrar con el email, sin importar mayúsculas."""
    resp = await api.post(
        LOGIN_URL,
        json={"login": "  PRODUCTOR@Test.Local ", "password": PASSWORDS["producer"]},
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == "producer"


@pytest.mark.asyncio
async def test_login_creates_session_and_me_works(api, db):
    """Caso 3: el login crea exactamente una sesión y el token sirve en /me."""
    token = await login(api, "admin")
    assert await count_sessions(db) == 1

    resp = await api.get(ME_URL, headers=bearer(token))
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_login_response_never_includes_hash(api):
    """Caso 4: la respuesta del login no expone el hash ni la contraseña."""
    resp = await api.post(
        LOGIN_URL,
        json={"login": "productor", "password": PASSWORDS["producer"]},
    )
    body = resp.text
    assert "password" not in body
    assert "scrypt$" not in body
    assert PASSWORDS["producer"] not in body


# ── Criterio 2: credenciales inválidas ─────────────────────────────────

@pytest.mark.asyncio
async def test_invalid_credentials_same_response(api):
    """Caso 5: usuario inexistente y contraseña incorrecta responden IDÉNTICO."""
    wrong_password = await api.post(
        LOGIN_URL, json={"login": "productor", "password": "no-es-esta"}
    )
    unknown_user = await api.post(
        LOGIN_URL, json={"login": "fantasma", "password": "no-es-esta"}
    )
    assert wrong_password.status_code == 401
    assert unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()
    assert wrong_password.json()["detail"]["error"] == "invalid_credentials"
    # El mensaje no dice cuál de los dos campos falló
    message = wrong_password.json()["detail"]["message"].lower()
    assert "usuario o contraseña" in message


@pytest.mark.asyncio
async def test_no_session_created_on_failure(api, db):
    """Caso 6: un login fallido no crea ninguna sesión."""
    await api.post(LOGIN_URL, json={"login": "productor", "password": "mal"})
    await api.post(LOGIN_URL, json={"login": "nadie@test.local", "password": "mal"})
    assert await count_sessions(db) == 0


@pytest.mark.asyncio
async def test_inactive_user_generic_error(api, db):
    """Caso 7: un usuario inactivo recibe el mismo error genérico."""
    await db.execute("UPDATE users SET status = 'inactive' WHERE username = 'productor'")
    await db.commit()

    resp = await api.post(
        LOGIN_URL, json={"login": "productor", "password": PASSWORDS["producer"]}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_credentials"
    assert await count_sessions(db) == 0


# ── Criterio 3: timeout por inactividad ────────────────────────────────

@pytest.mark.asyncio
async def test_idle_session_rejected(api, db):
    """Caso 8: pasado el timeout de inactividad, el token se rechaza con 401."""
    token = await login(api, "producer")
    stale = datetime.now(timezone.utc) - timedelta(
        minutes=settings.session_idle_minutes + 1
    )
    await db.execute("UPDATE sessions SET last_seen_at = ?", (stale.isoformat(),))
    await db.commit()

    resp = await api.get(ME_URL, headers=bearer(token))
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "session_expired"


@pytest.mark.asyncio
async def test_session_revoked_after_idle(api, db):
    """Caso 9: tras el timeout la sesión queda revocada; hay que volver a entrar."""
    token = await login(api, "producer")
    stale = datetime.now(timezone.utc) - timedelta(
        minutes=settings.session_idle_minutes + 5
    )
    await db.execute("UPDATE sessions SET last_seen_at = ?", (stale.isoformat(),))
    await db.commit()
    await api.get(ME_URL, headers=bearer(token))

    cursor = await db.execute("SELECT revoked_at FROM sessions")
    assert (await cursor.fetchone())["revoked_at"] is not None

    # Aunque se "reanime" last_seen_at, la sesión revocada ya no sirve
    await db.execute("UPDATE sessions SET last_seen_at = ?",
                     (datetime.now(timezone.utc).isoformat(),))
    await db.commit()
    assert (await api.get(ME_URL, headers=bearer(token))).status_code == 401

    # Re-autenticarse funciona y da una sesión nueva
    new_token = await login(api, "producer")
    assert (await api.get(ME_URL, headers=bearer(new_token))).status_code == 200


@pytest.mark.asyncio
async def test_activity_keeps_session_alive(api, db):
    """Caso 10: cada request válida renueva last_seen_at."""
    token = await login(api, "producer")
    almost = datetime.now(timezone.utc) - timedelta(
        minutes=settings.session_idle_minutes - 1
    )
    await db.execute("UPDATE sessions SET last_seen_at = ?", (almost.isoformat(),))
    await db.commit()

    assert (await api.get(ME_URL, headers=bearer(token))).status_code == 200
    cursor = await db.execute("SELECT last_seen_at FROM sessions")
    renewed = datetime.fromisoformat((await cursor.fetchone())["last_seen_at"])
    assert renewed > almost


@pytest.mark.asyncio
async def test_absolute_expiry_rejected(api, db):
    """Caso 11: una sesión que pasó su vencimiento absoluto se rechaza."""
    token = await login(api, "admin")
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.execute("UPDATE sessions SET expires_at = ?", (past.isoformat(),))
    await db.commit()

    resp = await api.get(ME_URL, headers=bearer(token))
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "session_expired"


@pytest.mark.asyncio
async def test_user_deactivated_mid_session(api, db):
    """Caso 12: si el usuario se desactiva, su sesión abierta deja de servir."""
    token = await login(api, "producer")
    await db.execute("UPDATE users SET status = 'inactive' WHERE username = 'productor'")
    await db.commit()
    assert (await api.get(ME_URL, headers=bearer(token))).status_code == 401


@pytest.mark.asyncio
async def test_logout_invalidates_token(api):
    """Caso 13: después del logout el mismo token ya no sirve."""
    token = await login(api, "admin")
    resp = await api.post(LOGOUT_URL, headers=bearer(token))
    assert resp.status_code == 204
    assert (await api.get(ME_URL, headers=bearer(token))).status_code == 401


# ── Tokens ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_expired_jwt_401(api, db):
    """Caso 14: un JWT con `exp` vencido → 401 invalid_token."""
    await login(api, "producer")
    cursor = await db.execute("SELECT session_id, user_id FROM sessions")
    session = await cursor.fetchone()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    token = create_access_token(
        session["user_id"], session["session_id"], "producer",
        expires_at=past, now=past - timedelta(hours=1),
    )
    resp = await api.get(ME_URL, headers=bearer(token))
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_token"


@pytest.mark.asyncio
async def test_forged_signature_401(api, db):
    """Caso 15: un JWT firmado con otro secreto → 401."""
    await login(api, "producer")
    cursor = await db.execute("SELECT session_id, user_id FROM sessions")
    session = await cursor.fetchone()
    now = datetime.now(timezone.utc)
    forged = jwt.encode(
        {"sub": str(session["user_id"]), "sid": session["session_id"],
         "role": "super_admin", "iat": int(now.timestamp()),
         "exp": int((now + timedelta(hours=1)).timestamp())},
        "otro-secreto", algorithm="HS256",
    )
    resp = await api.get(ME_URL, headers=bearer(forged))
    assert resp.status_code == 401


# ── Hash de contraseñas ────────────────────────────────────────────────

def test_password_hash_format_and_verify():
    """Caso 16: el hash lleva esquema y salt; verifica la correcta y no otra."""
    stored = hash_password("rabanito-123")
    assert stored.startswith("scrypt$n=16384,r=8,p=1$")
    assert "rabanito-123" not in stored
    assert verify_password("rabanito-123", stored)
    assert not verify_password("rabanito-124", stored)


def test_password_hash_uses_random_salt():
    """Caso 17: la misma contraseña produce hashes distintos (salt aleatorio)."""
    assert hash_password("igual") != hash_password("igual")


def test_invalid_hash_never_verifies():
    """Caso 18: un hash corrupto o de otro esquema nunca valida."""
    assert not verify_password("x", "texto-plano")
    assert not verify_password("x", "bcrypt$algo$raro$aqui")
    assert not verify_password("x", "scrypt$n=16384,r=8,p=1$%%%$%%%")


def test_needs_rehash():
    """Caso 19: needs_rehash detecta hashes viejos (preparado para NFR-03)."""
    assert not needs_rehash(hash_password("x"))
    assert needs_rehash("scrypt$n=1024,r=8,p=1$AAAA$AAAA")
    assert needs_rehash("texto-plano")


@pytest.mark.asyncio
async def test_passwords_not_stored_in_plain_text(db):
    """Caso 20: en la BD solo hay hashes, nunca contraseñas."""
    cursor = await db.execute("SELECT password_hash FROM users")
    stored = [row["password_hash"] for row in await cursor.fetchall()]
    for password in PASSWORDS.values():
        assert all(password not in h for h in stored)
    assert all(h.startswith("scrypt$") for h in stored)
