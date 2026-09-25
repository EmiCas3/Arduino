"""
SmartGreenAI Backend — FastAPI application.

Punto de entrada del servidor. Inicializa la base de datos al arrancar,
registra los routers bajo /api/v1 y sirve el frontend (carpeta frontend/)
desde el mismo origen, así el navegador no necesita CORS.
"""

import mimetypes
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import close_db, init_db
from app.routers import actuators
from app.routers import auth as auth_routes
from app.routers import ingest, readings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la DB al arrancar y la cierra al apagar."""
    await init_db()
    yield
    await close_db()


app = FastAPI(
    title="SmartGreenAI - Radish Crop API",
    version="1.1.0",
    description=(
        "Backend del vivero de rábanos: ingesta de lecturas (X-API-Key del "
        "gateway), sesiones de usuario con JWT, control de acceso por rol, "
        "registro de actuadores y últimas lecturas."
    ),
    lifespan=lifespan,
)

# CORS para desarrollo local (dashboard web)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registrar routers bajo /api/v1
app.include_router(ingest.router, prefix="/api/v1")
app.include_router(auth_routes.router, prefix="/api/v1")
app.include_router(actuators.router, prefix="/api/v1")
app.include_router(readings.router, prefix="/api/v1")


@app.get("/health", tags=["system"])
async def health_check():
    """Endpoint de salud para verificar que el server está vivo."""
    return {"status": "ok"}


# ── Frontend (AUTH-01 UI) ──────────────────────────────────────────────
# En algunas PCs con Windows el registro asocia .js a text/plain y el
# navegador rechaza los módulos ES. Se fija el tipo correcto a mano.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")

# Se monta AL FINAL para no tapar /api, /docs ni /health. check_dir=False
# permite importar la app (tests) aunque la carpeta no exista.
app.mount(
    "/",
    StaticFiles(directory=str(settings.frontend_dir), html=True, check_dir=False),
    name="frontend",
)
