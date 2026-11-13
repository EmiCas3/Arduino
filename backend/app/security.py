"""
Criptografía de usuarios: hash de contraseñas (AUTH-01, NFR-03) y tokens JWT.

Contraseñas (NFR-03)
--------------------
Solo se guarda un hash con salt; la contraseña nunca se guarda, se registra
ni se devuelve. Hay dos esquemas y cada hash dice cuál es el suyo:

    bcrypt-sha256$2b$12$<salt y hash de bcrypt>        ← el preferido
    scrypt$n=16384,r=8,p=1$<salt_base64>$<hash_base64>  ← el de AUTH-01

- **bcrypt** es el que pide NFR-03. Se usa si la librería `bcrypt` está
  instalada (`pip install -r requirements-bcrypt.txt`; el instalador de la Pi
  lo hace). La contraseña pasa antes por SHA-256 porque bcrypt solo mira los
  primeros 72 bytes: así una contraseña larga cuenta completa.
- **scrypt** viene con Python y no compila nada. Es el respaldo donde `bcrypt`
  no se puede instalar (el Python de MSYS2 sin Rust que usa parte del equipo).
  También lleva salt y también es costoso de atacar por fuerza bruta.

Migración: no se le pide nada a nadie. needs_rehash() dice si un hash no está
en el esquema preferido de ESTA instalación, y el login —que es el único
momento en que se conoce la contraseña— la vuelve a guardar con el nuevo. Las
cuentas que ya existían se migran solas la siguiente vez que entran.
`python -m app.passwords` muestra cuántas faltan.

Un hash de bcrypt en una instalación sin la librería no se puede verificar:
verify_password() devuelve False y lo deja en el log.

JWT
---
HS256 con PyJWT (puro Python). El token solo identifica la sesión
(sub = id del usuario, sid = id de la sesión). El rol que manda es el que
está en la BD, nunca el que viaja en el token.
"""

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone
from typing import Optional, Tuple

import jwt

from app.config import settings

try:  # opcional: ver el docstring
    import bcrypt as _bcrypt
except ImportError:  # pragma: no cover - depende de la instalación
    _bcrypt = None

logger = logging.getLogger("smartgreenai")

# ── Contraseñas ────────────────────────────────────────────────────────

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SALT_BYTES = 16
SCHEME_SCRYPT = "scrypt"
SCHEME_BCRYPT = "bcrypt-sha256"
_SCRYPT_MAXMEM = 64 * 1024 * 1024

# Costo de bcrypt (2^n rondas). 12 tarda ~0.2 s por login en la Pi 5.
BCRYPT_ROUNDS = settings.bcrypt_rounds


def bcrypt_available() -> bool:
    return _bcrypt is not None


def preferred_scheme() -> str:
    """El esquema con el que se guardan las contraseñas nuevas aquí."""
    return SCHEME_BCRYPT if bcrypt_available() else SCHEME_SCRYPT


def scheme_of(stored: str) -> Optional[str]:
    """Esquema de un hash guardado, o None si no es un hash que conozcamos."""
    if not isinstance(stored, str):
        return None
    if stored.startswith(SCHEME_BCRYPT + "$") and _parse_bcrypt(stored) is not None:
        return SCHEME_BCRYPT
    if stored.startswith(SCHEME_SCRYPT + "$") and _parse_scrypt(stored) is not None:
        return SCHEME_SCRYPT
    return None


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


# scrypt ────────────────────────────────────────────────────────────────

def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=_SCRYPT_MAXMEM,
    )


def _hash_scrypt(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)
    params = f"n={SCRYPT_N},r={SCRYPT_R},p={SCRYPT_P}"
    return f"{SCHEME_SCRYPT}${params}${_b64encode(salt)}${_b64encode(digest)}"


def _parse_scrypt(stored: str) -> Optional[Tuple[int, int, int, bytes, bytes]]:
    """Separa un hash scrypt en (n, r, p, salt, digest). None si no es válido."""
    try:
        scheme, params, salt_b64, digest_b64 = stored.split("$")
        if scheme != SCHEME_SCRYPT:
            return None
        values = dict(item.split("=", 1) for item in params.split(","))
        return (
            int(values["n"]), int(values["r"]), int(values["p"]),
            _b64decode(salt_b64), _b64decode(digest_b64),
        )
    except (ValueError, KeyError, AttributeError):
        # binascii.Error hereda de ValueError
        return None


# bcrypt ────────────────────────────────────────────────────────────────

def _bcrypt_input(password: str) -> bytes:
    """SHA-256 en base64 (44 bytes, sin ceros): cabe en los 72 de bcrypt."""
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def _hash_bcrypt(password: str) -> str:
    native = _bcrypt.hashpw(_bcrypt_input(password), _bcrypt.gensalt(rounds=BCRYPT_ROUNDS))
    return f"{SCHEME_BCRYPT}{native.decode('ascii')}"


def _parse_bcrypt(stored: str) -> Optional[Tuple[int, bytes]]:
    """Separa `bcrypt-sha256$2b$12$…` en (rondas, hash nativo). None si no es válido."""
    try:
        scheme, version, rounds, rest = stored.split("$")
        if scheme != SCHEME_BCRYPT or version not in ("2a", "2b", "2y") or len(rest) != 53:
            return None
        return int(rounds), f"${version}${rounds}${rest}".encode("ascii")
    except (ValueError, AttributeError):
        return None


# API ───────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Hash con salt aleatorio en el esquema preferido. Nunca se guarda la contraseña."""
    if bcrypt_available():
        return _hash_bcrypt(password)
    return _hash_scrypt(password)


def verify_password(password: str, stored: str) -> bool:
    """Compara en tiempo constante. False ante cualquier hash inválido."""
    scheme = scheme_of(stored)
    if scheme == SCHEME_BCRYPT:
        if not bcrypt_available():
            logger.error(
                "Hay una contraseña guardada con bcrypt pero la librería no está "
                "instalada: pip install -r requirements-bcrypt.txt"
            )
            return False
        _rounds, native = _parse_bcrypt(stored)
        try:
            return _bcrypt.checkpw(_bcrypt_input(password), native)
        except ValueError:
            return False
    if scheme == SCHEME_SCRYPT:
        n, r, p, salt, expected = _parse_scrypt(stored)
        candidate = _scrypt(password, salt, n, r, p, len(expected))
        return hmac.compare_digest(candidate, expected)
    return False


def needs_rehash(stored: str) -> bool:
    """True si el hash no está en el esquema y parámetros preferidos aquí.

    El login lo usa para migrar la cuenta: en ese momento sí conoce la
    contraseña y puede volver a guardarla sin pedirle nada al usuario.
    Un hash de bcrypt nunca se "baja" a scrypt: sin la librería no se puede
    ni verificar.
    """
    scheme = scheme_of(stored)
    if scheme is None:
        return True
    if scheme == SCHEME_BCRYPT:
        rounds, _native = _parse_bcrypt(stored)
        return bcrypt_available() and rounds != BCRYPT_ROUNDS
    if bcrypt_available():
        return True   # scrypt → bcrypt
    n, r, p, _salt, digest = _parse_scrypt(stored)
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
