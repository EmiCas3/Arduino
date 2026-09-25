"""
Tests del frontend servido por FastAPI (AUTH-01 UI).

El frontend es estático (HTML + CSS + JS sin build) y vive en ../frontend.
Se sirve desde el mismo origen que la API, al final de las rutas.
"""

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/login.html", "/panel.html"])
async def test_pages_are_served(api, path):
    """Caso 1: las páginas se sirven como HTML."""
    resp = await api.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/js/login.js", "/js/panel.js", "/js/api.js"])
async def test_modules_have_javascript_mime(api, path):
    """Caso 2: los módulos ES salen como JavaScript (evita el bug de Windows)."""
    resp = await api.get(path)
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_static_mount_does_not_hide_api(api):
    """Caso 3: el montaje estático no tapa la API, /health ni /docs."""
    assert (await api.get("/health")).json() == {"status": "ok"}
    assert (await api.get("/docs")).status_code == 200
    assert (await api.get("/openapi.json")).status_code == 200
    assert (await api.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_login_page_has_no_secrets(api):
    """Caso 4: la página de login no trae credenciales de demo embebidas."""
    html = (await api.get("/login.html")).text
    assert "value=" not in html          # ningún campo viene prellenado
    assert "Rabano-" not in html
