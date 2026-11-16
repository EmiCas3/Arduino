"""
Tests del frontend servido por FastAPI (AUTH-01 UI).

El frontend es estático (HTML + CSS + JS sin build) y vive en ../frontend.
Se sirve desde el mismo origen que la API, al final de las rutas.
"""

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/login.html", "/panel.html", "/display.html"])
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
@pytest.mark.parametrize(
    "path",
    ["/js/dashboard.js", "/js/display.js", "/js/metrics.js", "/js/equipment.js", "/js/poller.js",
     "/js/alerts.js", "/js/audit.js", "/js/chart.js", "/js/series.js", "/js/history.js",
     "/js/anomalies.js", "/js/recommendation.js"])
async def test_dashboard_modules_have_javascript_mime(api, path):
    """Los módulos de los widgets, histórico y bitácora (DASH-01, ACT-04, ALERT-01,
    DASH-02, DASH-03, AI-01, AI-02, AUTH-05) salen como JavaScript."""
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


def test_dash03_unified_dashboard_and_audit_wired():
    """DASH-03: bitácora conectada (no 'próximamente'), secciones unidas y nav móvil."""
    from app.config import settings

    panel_js = (settings.frontend_dir / "js" / "panel.js").read_text(encoding="utf-8")
    roles_js = (settings.frontend_dir / "js" / "roles.js").read_text(encoding="utf-8")
    styles_css = (settings.frontend_dir / "css" / "styles.css").read_text(encoding="utf-8")

    assert "mountAudit" in panel_js
    assert '"AUTH-05"' not in panel_js
    assert "section-nav" in panel_js
    assert "mobile-nav" in panel_js
    assert 'id: "bitacora"' in roles_js
    assert "overflow-x: clip" in styles_css
    assert ".mobile-nav" in styles_css
    assert ".table-audit" in styles_css


def test_dash03_external_display_dashboard():
    """DASH-03: pantalla externa dedicada en tiempo real para la Raspberry Pi."""
    from app.config import settings

    display_html = settings.frontend_dir / "display.html"
    display_js = (settings.frontend_dir / "js" / "display.js").read_text(encoding="utf-8")
    panel_js = (settings.frontend_dir / "js" / "panel.js").read_text(encoding="utf-8")
    roles_js = (settings.frontend_dir / "js" / "roles.js").read_text(encoding="utf-8")
    styles_css = (settings.frontend_dir / "css" / "styles.css").read_text(encoding="utf-8")

    assert display_html.exists()
    assert "mountDisplay" in display_js
    assert "DISPLAY_REFRESH_SECONDS" in display_js
    assert "mountDisplay" in panel_js
    assert "pantalla" in panel_js
    assert 'id: "pantalla"' in roles_js
    assert ".display-board" in styles_css
    assert ".display-tile" in styles_css

    deploy_dir = settings.frontend_dir.parent / "deploy" / "pi5"
    assert (deploy_dir / "open-display.sh").exists()
    install_sh = (deploy_dir / "install.sh").read_text(encoding="utf-8")
    assert "SMARTGREENAI_DISPLAY_TOKEN" in install_sh
    assert "open-display.sh" in install_sh


@pytest.mark.asyncio
async def test_display_token_allows_read_only_display_endpoints(api, monkeypatch):
    """El token de pantalla externa permite leer el dashboard sin sesión ni expiración."""
    from app.config import settings
    from tests.conftest import GREENHOUSE_ID, bearer

    monkeypatch.setattr(settings, "display_token", "kiosk-secret-token")
    headers = bearer("kiosk-secret-token")

    me_resp = await api.get("/api/v1/auth/me", headers=headers)
    assert me_resp.status_code == 200
    assert me_resp.json()["username"] == "display"

    gh_resp = await api.get("/api/v1/greenhouses", headers=headers)
    assert gh_resp.status_code == 200
    assert [g["greenhouse_id"] for g in gh_resp.json()] == [GREENHOUSE_ID]

    for suffix in ("readings/latest", "actuators/status", "irrigation", "alerts"):
        resp = await api.get(f"/api/v1/greenhouses/{GREENHOUSE_ID}/{suffix}", headers=headers)
        assert resp.status_code == 200, suffix

    rec_resp = await api.get(
        f"/api/v1/greenhouses/{GREENHOUSE_ID}/irrigation/recommendation", headers=headers
    )
    assert rec_resp.status_code in (200, 503)


@pytest.mark.asyncio
async def test_display_token_blocks_writes_and_non_display_routes(api, monkeypatch):
    """El token de pantalla externa rechaza escrituras y rutas fuera del display con 403."""
    from app.config import settings
    from tests.conftest import GREENHOUSE_ID, bearer

    monkeypatch.setattr(settings, "display_token", "kiosk-secret-token")
    headers = bearer("kiosk-secret-token")

    for method, path, body in (
        ("POST", "/api/v1/actuators", {"name": "x"}),
        ("PUT", f"/api/v1/greenhouses/{GREENHOUSE_ID}/irrigation/mode", {"mode": "manual"}),
        ("POST", "/api/v1/auth/logout", None),
        ("GET", "/api/v1/audit/events", None),
        ("GET", "/api/v1/actuators", None),
    ):
        resp = await api.request(method, path, json=body, headers=headers)
        assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"


@pytest.mark.asyncio
async def test_display_token_disabled_by_default(api):
    """Sin SMARTGREENAI_DISPLAY_TOKEN configurado, cualquier token no JWT devuelve 401."""
    from app.config import settings
    from tests.conftest import bearer

    assert settings.display_token == ""
    resp = await api.get("/api/v1/auth/me", headers=bearer("kiosk-secret-token"))
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_token"
