"""
Tests de NFR-03 — task "Implement password hashing (bcrypt/argon2) +
migration check".

Criterios de aceptación:
  1. Al guardar una contraseña solo se persiste un hash con salt.
  2. Ni la BD, ni los logs, ni las respuestas de la API traen una contraseña
     en texto plano.
  3. Las cuentas de prueba que ya existían se migran al esquema nuevo.

bcrypt es opcional (ver app/security.py): las pruebas que lo necesitan se
saltan donde no está instalado; las de scrypt corren siempre.
"""

import logging

import pytest

from app import passwords, security
from tests.conftest import PASSWORDS, USERS, bearer, login

LOGIN_URL = "/api/v1/auth/login"
needs_bcrypt = pytest.mark.skipif(
    not security.bcrypt_available(), reason="bcrypt no está instalado")


@pytest.fixture
def no_bcrypt(monkeypatch):
    """Simula una instalación sin la librería (el Python de MSYS2)."""
    monkeypatch.setattr(security, "_bcrypt", None)


async def stored_hash(db, username: str) -> str:
    cursor = await db.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
    return (await cursor.fetchone())["password_hash"]


async def set_hash(db, username: str, value: str) -> None:
    await db.execute("UPDATE users SET password_hash = ? WHERE username = ?", (value, username))
    await db.commit()


# ── Criterio 1: solo un hash con salt ──────────────────────────────────

@needs_bcrypt
def test_new_passwords_use_bcrypt():
    stored = security.hash_password("rabanito-123")
    assert stored.startswith("bcrypt-sha256$2b$")
    assert security.scheme_of(stored) == security.SCHEME_BCRYPT
    assert "rabanito-123" not in stored
    assert security.verify_password("rabanito-123", stored)
    assert not security.verify_password("rabanito-124", stored)
    assert security.hash_password("rabanito-123") != stored   # salt aleatorio


@needs_bcrypt
def test_bcrypt_counts_the_whole_password():
    """bcrypt solo mira 72 bytes; con el SHA-256 previo cuenta todo."""
    long = "a" * 100
    stored = security.hash_password(long)
    assert security.verify_password(long, stored)
    assert not security.verify_password("a" * 99, stored)
    assert not security.verify_password("a" * 72, stored)
    assert security.verify_password("ñandú-🌱" * 20, security.hash_password("ñandú-🌱" * 20))


def test_without_bcrypt_scrypt_is_used(no_bcrypt):
    stored = security.hash_password("rabanito-123")
    assert stored.startswith("scrypt$n=16384,r=8,p=1$")
    assert security.verify_password("rabanito-123", stored)
    assert not security.verify_password("rabanito-124", stored)
    assert security.hash_password("rabanito-123") != stored
    assert not security.needs_rehash(stored)


@needs_bcrypt
def test_a_bcrypt_hash_without_the_library_never_verifies(monkeypatch, caplog):
    stored = security.hash_password("rabanito-123")
    monkeypatch.setattr(security, "_bcrypt", None)
    with caplog.at_level(logging.ERROR, logger="smartgreenai"):
        assert not security.verify_password("rabanito-123", stored)
    assert "bcrypt" in caplog.text and "rabanito-123" not in caplog.text
    assert not security.needs_rehash(stored)   # no se "baja" a scrypt


@pytest.mark.parametrize("stored", [
    "", "texto-plano", "Rabano-Prod-2026!", "bcrypt$algo$raro$aqui",
    "bcrypt-sha256$2b$12$corto", "bcrypt-sha256$9z$12$" + "a" * 53,
    "scrypt$n=16384,r=8,p=1$%%%$%%%", "$2b$12$" + "a" * 53,
])
def test_unknown_or_broken_hashes_never_verify(stored):
    assert security.scheme_of(stored) is None
    assert not security.verify_password("x", stored)
    assert not security.verify_password(stored, stored)
    assert security.needs_rehash(stored)


# ── Criterio 3: las cuentas que ya existían se migran ──────────────────

@needs_bcrypt
@pytest.mark.asyncio
async def test_old_scrypt_account_is_migrated_on_login(api, db):
    """Entra con su contraseña de siempre y sale con hash bcrypt."""
    old = security._hash_scrypt(PASSWORDS["producer"])
    await set_hash(db, "productor", old)
    assert security.needs_rehash(old)

    token = await login(api, "producer")
    new = await stored_hash(db, "productor")
    assert new != old and security.scheme_of(new) == security.SCHEME_BCRYPT
    assert security.verify_password(PASSWORDS["producer"], new)
    assert not security.needs_rehash(new)

    # La sesión sirve y la contraseña sigue siendo la misma.
    assert (await api.get("/api/v1/auth/me", headers=bearer(token))).status_code == 200
    await login(api, "producer")
    assert await stored_hash(db, "productor") == new   # ya no se vuelve a migrar

    cursor = await db.execute(
        "SELECT detail FROM audit_log WHERE action = 'login' ORDER BY id")
    details = [row["detail"] for row in await cursor.fetchall()]
    assert '"password_rehashed": true' in details[0]
    assert '"password_rehashed": false' in details[1]


@needs_bcrypt
@pytest.mark.asyncio
async def test_a_wrong_password_does_not_migrate_anything(api, db):
    old = security._hash_scrypt(PASSWORDS["producer"])
    await set_hash(db, "productor", old)
    resp = await api.post(LOGIN_URL, json={"login": "productor", "password": "no-es-esta"})
    assert resp.status_code == 401
    assert await stored_hash(db, "productor") == old


@needs_bcrypt
@pytest.mark.asyncio
async def test_weaker_bcrypt_cost_is_upgraded_on_login(api, db, monkeypatch):
    weak = await stored_hash(db, "productor")
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 5)
    assert security.needs_rehash(weak)
    await login(api, "producer")
    assert (await stored_hash(db, "productor")).startswith("bcrypt-sha256$2b$05$")


@pytest.mark.asyncio
async def test_login_still_works_without_bcrypt(api, db, no_bcrypt):
    """Donde no hay bcrypt, las cuentas scrypt entran y se quedan en scrypt."""
    old = security._hash_scrypt(PASSWORDS["producer"])
    await set_hash(db, "productor", old)
    await login(api, "producer")
    assert await stored_hash(db, "productor") == old


@needs_bcrypt
@pytest.mark.asyncio
async def test_migration_check_reports_each_account(db):
    await set_hash(db, "productor", security._hash_scrypt("x"))
    await set_hash(db, "admin", "contraseña-en-texto-plano")
    report = {user: (scheme, state) for user, scheme, state in await passwords.review(db)}
    assert report == {
        "productor": ("scrypt", "pending"),
        "admin": ("invalid", "invalid"),
        "superadmin": ("bcrypt-sha256", "ok"),
    }


@pytest.mark.asyncio
async def test_a_plain_text_password_in_the_database_cannot_log_in(api, db):
    """Si alguien mete una contraseña a mano en la BD, no sirve para entrar."""
    await set_hash(db, "productor", PASSWORDS["producer"])
    resp = await api.post(
        LOGIN_URL, json={"login": "productor", "password": PASSWORDS["producer"]})
    assert resp.status_code == 401


# ── Criterio 2: ni en la BD, ni en los logs, ni en las respuestas ──────

@pytest.mark.asyncio
async def test_no_plain_text_password_anywhere(api, db, caplog):
    """Se usa la plataforma (login bueno, malo, sesión, bitácora) y después se
    revisa TODO lo que quedó: cada tabla, cada respuesta y el log."""
    typed_wrong = "Clave-Equivocada-77!"
    secrets = [*PASSWORDS.values(), typed_wrong]
    responses = []

    with caplog.at_level(logging.DEBUG):
        for role in USERS:
            resp = await api.post(LOGIN_URL, json={
                "login": USERS[role]["username"], "password": PASSWORDS[role]})
            responses.append(resp)
        token = responses[1].json()["access_token"]   # admin
        responses.append(await api.post(
            LOGIN_URL, json={"login": "productor", "password": typed_wrong}))
        responses.append(await api.post(
            LOGIN_URL, json={"login": "nadie", "password": typed_wrong}))
        responses.append(await api.get("/api/v1/auth/me", headers=bearer(token)))
        responses.append(await api.get("/api/v1/audit/events", headers=bearer(token)))
        responses.append(await api.post("/api/v1/auth/logout", headers=bearer(token)))

    for resp in responses:
        for secret in secrets:
            assert secret not in resp.text
        assert "password_hash" not in resp.text
        assert "bcrypt-sha256$" not in resp.text and "scrypt$" not in resp.text

    for secret in secrets:
        assert secret not in caplog.text

    cursor = await db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    tables = [row["name"] for row in await cursor.fetchall()]
    assert {"users", "sessions", "audit_log"} <= set(tables)
    for table in tables:
        cursor = await db.execute(f"SELECT * FROM {table}")
        dump = "\n".join(str(tuple(row)) for row in await cursor.fetchall())
        for secret in secrets:
            assert secret not in dump, f"contraseña en texto plano en la tabla {table}"

    cursor = await db.execute("SELECT password_hash FROM users")
    for row in await cursor.fetchall():
        assert security.scheme_of(row["password_hash"]) is not None
