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


# ── Widgets de DASH-01 ─────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/js/dashboard.js", "/js/metrics.js"])
async def test_dashboard_modules_have_javascript_mime(api, path):
    """Los módulos de los widgets también salen como JavaScript."""
    resp = await api.get(path)
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_no_module_builds_html_from_data():
    """Regla del frontend: el DOM se arma con h()/textContent. El único
    innerHTML permitido es el de icon() en ui.js, con cadenas fijas."""
    from app.config import settings

    offenders = []
    for path in sorted((settings.frontend_dir / "js").glob("*.js")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("//")[0]
            if any(risky in code for risky in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write")):
                offenders.append(path.name)
    assert offenders == ["ui.js"], offenders


def test_frontend_and_backend_agree_on_background_header():
    """api.js manda el mismo header que get_current_user reconoce."""
    from app.config import settings
    from app.dependencies import BACKGROUND_HEADER

    api_js = (settings.frontend_dir / "js" / "api.js").read_text(encoding="utf-8")
    assert f'headers["{BACKGROUND_HEADER}"] = "1"' in api_js
