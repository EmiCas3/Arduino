"""
Tests de AUTH-02 — Role-based access enforcement.

Criterios de aceptación:
  1. Sesión de Radish Producer → ruta de Administrator o Super Administrator
     → 403 y la acción NO se ejecuta.
  2. Las opciones de otro rol no aparecen en el menú, pero ocultarlas en la UI
     no cuenta como control (el control real es el del servidor, criterio 1).
  3. Request sin token o con token expirado a un endpoint protegido → 401.

Además: el test de cobertura garantiza que TODAS las rutas declaran sus roles.
"""

import re

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from app.auth import verify_api_key
from app.dependencies import get_current_user, require_roles
from app.models import Role
from tests.conftest import GATEWAY_ID, bearer, login

# Rutas que a propósito NO piden sesión de usuario
PUBLIC_ROUTES = {
    ("GET", "/health"),
    ("POST", "/api/v1/auth/login"),
}
# Rutas de dispositivos IoT: se autentican con X-API-Key, no con sesión
DEVICE_ROUTES = {
    ("POST", "/api/v1/devices/{device_id}/readings"),
}


def _dependency_calls(dependant):
    """Todas las funciones de dependencia de una ruta, recursivamente."""
    for dep in dependant.dependencies:
        yield dep.call
        yield from _dependency_calls(dep)


def _api_routes():
    from app.main import app

    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in sorted(route.methods):
                yield method, route


def _guarded_routes():
    return [
        (method, route.path)
        for method, route in _api_routes()
        if (method, route.path) not in PUBLIC_ROUTES | DEVICE_ROUTES
    ]


def _fill_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x-prueba-01", path)


# ── Cobertura: "server-side role guards on every route" ────────────────

def test_all_routes_are_guarded():
    """Toda ruta no pública exige sesión Y declara explícitamente sus roles."""
    problems = []
    for method, route in _api_routes():
        key = (method, route.path)
        calls = list(_dependency_calls(route.dependant))
        if key in PUBLIC_ROUTES:
            continue
        if key in DEVICE_ROUTES:
            if verify_api_key not in calls:
                problems.append(f"{key}: ruta de dispositivo sin verify_api_key")
            continue
        if get_current_user not in calls:
            problems.append(f"{key}: no exige sesión")
        if not any(getattr(call, "allowed_roles", None) for call in calls):
            problems.append(f"{key}: no declara roles con require_roles")
    assert problems == []


def test_public_and_device_lists_are_not_stale():
    """Las listas de excepciones solo contienen rutas que existen."""
    existing = {(method, route.path) for method, route in _api_routes()}
    assert PUBLIC_ROUTES <= existing
    assert DEVICE_ROUTES <= existing


# ── Criterio 3: 401 sin token o con token inválido ─────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", _guarded_routes())
async def test_missing_token_401(api, method, path):
    """Caso 1: TODAS las rutas protegidas responden 401 sin token."""
    resp = await api.request(method, _fill_path(path), json={})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "missing_token"
    assert resp.headers.get("www-authenticate") == "Bearer"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", _guarded_routes())
async def test_malformed_token_401(api, method, path):
    """Caso 2: TODAS las rutas protegidas responden 401 con un token basura."""
    resp = await api.request(
        method, _fill_path(path), json={}, headers=bearer("no.es.un.jwt")
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_token"


@pytest.mark.asyncio
async def test_non_bearer_scheme_401(api):
    """Caso 3: un header Authorization que no es Bearer → 401."""
    resp = await api.get("/api/v1/auth/me", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


# ── Criterio 1: 403 y la acción no se ejecuta ──────────────────────────

def _build_restricted_app(calls: list) -> FastAPI:
    """App mínima SOLO para tests con una ruta de Super Admin y otra de Admin.

    Hoy no existe ninguna ruta exclusiva de Super Admin en producción; en vez
    de inventar un endpoint falso, se prueba el guard real en esta app.
    """
    router = APIRouter()

    @router.post("/solo-super")
    async def solo_super(user: dict = Depends(require_roles(Role.super_admin))):
        calls.append(("solo-super", user["username"]))
        return {"ok": True}

    @router.post("/solo-admin")
    async def solo_admin(
        user: dict = Depends(require_roles(Role.admin, Role.super_admin)),
    ):
        calls.append(("solo-admin", user["username"]))
        return {"ok": True}

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,path,expected",
    [
        ("producer", "/solo-admin", 403),
        ("producer", "/solo-super", 403),
        ("admin", "/solo-super", 403),
        ("admin", "/solo-admin", 200),
        ("super_admin", "/solo-super", 200),
        ("super_admin", "/solo-admin", 200),
    ],
)
async def test_role_guard_matrix(api, role, path, expected):
    """Caso 4: matriz rol × ruta; con 403 el handler NUNCA se ejecuta."""
    token = await login(api, role)
    calls: list = []
    restricted = _build_restricted_app(calls)
    async with AsyncClient(
        transport=ASGITransport(app=restricted), base_url="http://test"
    ) as client:
        resp = await client.post(path, headers=bearer(token))

    assert resp.status_code == expected
    if expected == 403:
        assert resp.json()["detail"]["error"] == "forbidden"
        assert calls == []          # la acción no se realizó
    else:
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_role_is_read_from_database_not_token(api, db):
    """Caso 5: si el rol cambia en la BD, manda la BD aunque el JWT diga otra cosa."""
    token = await login(api, "admin")               # el JWT dice role=admin
    await db.execute("UPDATE users SET role = 'producer' WHERE username = 'admin'")
    await db.commit()

    calls: list = []
    async with AsyncClient(
        transport=ASGITransport(app=_build_restricted_app(calls)),
        base_url="http://test",
    ) as client:
        resp = await client.post("/solo-admin", headers=bearer(token))
    assert resp.status_code == 403
    assert calls == []


# ── Dispositivos vs usuarios ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_rejects_user_jwt_without_api_key(api, tokens):
    """Caso 6: un JWT de usuario (incluso super admin) no sirve para la ingesta."""
    resp = await api.post(
        f"/api/v1/devices/{GATEWAY_ID}/readings",
        json={"readings": [{"ts": "2026-09-25T12:00:00Z"}]},
        headers=bearer(tokens["super_admin"]),
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "missing_api_key"


def test_require_roles_needs_at_least_one_role():
    """Caso 7: no se puede crear un guard vacío por accidente."""
    with pytest.raises(ValueError):
        require_roles()
