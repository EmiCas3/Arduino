"""
Criptografía de usuarios: hash de contraseñas y tokens JWT (AUTH-01).

Contraseñas
-----------
Se usa hashlib.scrypt de la librería estándar: lleva salt, es costoso de
atacar por fuerza bruta y NO requiere dependencias que compilen (el equipo
usa Python de MSYS2 sin Rust, igual que por eso se eligió Pydantic v1).
Cada hash guarda su propio esquema y parámetros:

    scrypt$n=16384,r=8,p=1$<salt_base64>$<hash_base64>

Así NFR-03 (Sprint 4) puede introducir bcrypt/argon2 y usar needs_rehash()
para migrar cada cuenta la siguiente vez que inicie sesión.

JWT
---
HS256 con PyJWT (puro Python). El token solo identifica la sesión
(sub = id del usuario, sid = id de la sesión). El rol que manda es el que
está en la BD, nunca el que viaja en el token.
"""

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Optional, Tuple

import jwt

from app.config import settings

# ── Contraseñas ────────────────────────────────────────────────────────

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SALT_BYTES = 16
_SCHEME = "scrypt"
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=_SCRYPT_MAXMEM,
    )


def hash_password(password: str) -> str:
    """Genera el hash con salt aleatorio. Nunca se guarda la contraseña."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)
    params = f"n={SCRYPT_N},r={SCRYPT_R},p={SCRYPT_P}"
    return f"{_SCHEME}${params}${_b64encode(salt)}${_b64encode(digest)}"


def _parse_hash(stored: str) -> Optional[Tuple[int, int, int, bytes, bytes]]:
    """Separa un hash guardado en (n, r, p, salt, digest). None si no es válido."""
    try:
        scheme, params, salt_b64, digest_b64 = stored.split("$")
        if scheme != _SCHEME:
            return None
        values = dict(item.split("=", 1) for item in params.split(","))
        return (
            int(values["n"]), int(values["r"]), int(values["p"]),
            _b64decode(salt_b64), _b64decode(digest_b64),
        )
    except (ValueError, KeyError, AttributeError):
        # binascii.Error hereda de ValueError
        return None


def verify_password(password: str, stored: str) -> bool:
    """Compara en tiempo constante. False ante cualquier hash inválido."""
    parsed = _parse_hash(stored)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    candidate = _scrypt(password, salt, n, r, p, len(expected))
    return hmac.compare_digest(candidate, expected)


def needs_rehash(stored: str) -> bool:
    """True si el hash usa otro esquema o parámetros distintos a los actuales.

    Preparado para NFR-03: al cambiar de algoritmo, el login puede rehashear
    la contraseña (que en ese momento sí conoce) sin pedir nada al usuario.
    """
    parsed = _parse_hash(stored)
    if parsed is None:
        return True
    n, r, p, _salt, digest = parsed
    return (n, r, p, len(digest)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)


# Hash de una contraseña aleatoria que nadie conoce. El login lo verifica
# cuando el usuario no existe, para que responder "no existe" tarde lo mismo
# que "contraseña incorrecta" y no se pueda adivinar qué usuarios hay.
DUMMY_HASH = hash_password(secrets.token_urlsafe(24))


# ── JWT ────────────────────────────────────────────────────────────────

class TokenError(Exception):
    """Token mal formado, con firma inválida o vencido."""


def create_access_token(
    user_id: int,
    session_id: str,
    role: str,
    expires_at: datetime,
    now: Optional[datetime] = None,
) -> str:
    """Firma el JWT de una sesión. `role` es informativo para el frontend."""
    issued_at = now or datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "sid": session_id,
        "role": role,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Valida firma y vencimiento. Lanza TokenError si algo no cuadra."""
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "sid", "iat", "exp"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
