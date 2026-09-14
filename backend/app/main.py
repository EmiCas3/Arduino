"""
SmartGreenAI Backend — FastAPI application.

Punto de entrada del servidor. Inicializa la base de datos al arrancar
y registra los routers bajo /api/v1.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import close_db, init_db
from app.routers import ingest


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la DB al arrancar y la cierra al apagar."""
    await init_db()
    yield
    await close_db()


app = FastAPI(
    title="SmartGreenAI - Radish Crop API",
    version="1.0.0",
    description="Backend de ingesta y almacenamiento para el vivero de rábanos.",
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


@app.get("/health", tags=["system"])
async def health_check():
    """Endpoint de salud para verificar que el server está vivo."""
    return {"status": "ok"}
