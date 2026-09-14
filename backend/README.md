# SmartGreenAI Backend — MON-04

Backend de ingesta y almacenamiento para el proyecto SmartGreenAI.
Recibe lotes de lecturas de sensores enviados por la Raspberry Pi y los
almacena en SQLite.

## Requisitos

- Python 3.11+

## Setup rápido

```bash
cd backend

# Crear y activar entorno virtual
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate  # Linux/Mac

# Instalar dependencias
pip install -r requirements.txt

# Crear dispositivo de prueba (muestra la API key UNA sola vez)
python -m app.seed

# Arrancar el servidor
uvicorn app.main:app --reload --port 8000
```

## Endpoints implementados

### `POST /api/v1/devices/{device_id}/readings`

Ingesta de lotes de mediciones (MON-04).

```bash
curl -X POST http://localhost:8000/api/v1/devices/pi-vivero-01/readings \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <tu-api-key-del-seed>" \
  -d '{
    "readings": [
      {
        "ts": "2026-09-13T21:04:22Z",
        "ms": 148022,
        "temp_c": 24.2,
        "hum_aire_pct": 72.0,
        "suelo_raw": 380,
        "suelo_pct": 62,
        "nivel_raw": 150,
        "nivel_pct": 75,
        "luz_raw": 306,
        "luz_nivel": 717,
        "es_dia": true,
        "sol_min_hoy": 42,
        "sol_min_prev": null,
        "riego_sugerido": false,
        "bomba_habilitada": true,
        "vent": "BASE",
        "estado": "OK",
        "alertas": []
      }
    ]
  }'
```

Respuesta `202`:
```json
{
  "accepted": 1,
  "duplicates": 0,
  "rejected": []
}
```

### `GET /health`

Health check.

## Tests

```bash
cd backend
python -m pytest tests/ -v
```

## Estructura

```
backend/
├── app/
│   ├── main.py         # FastAPI app
│   ├── database.py     # SQLite async
│   ├── models.py       # Pydantic schemas
│   ├── auth.py         # Autenticación X-API-Key
│   ├── seed.py         # Datos de prueba
│   └── routers/
│       └── ingest.py   # POST readings
├── tests/
│   └── test_ingest.py
├── requirements.txt
└── README.md
```
