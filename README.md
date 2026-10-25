# SmartGreenAI — Radish Crop

Documentación de la API en SwaggerHub (v1.7.0):
https://app.swaggerhub.com/apis/upaep-5f9/smartgreenai-radish-crop-api-3/1.7.0

La fuente oficial del contrato es `openapi.yaml` de este repositorio
(**v1.7.0**, Sprint 3: histórico y gráficas de DASH-02, análisis de AI-01,
sensor de CO₂ de NF-01 y condiciones inusuales de AI-02).
SwaggerHub tiene una copia: cada vez que cambie la versión de `openapi.yaml`
hay que subirla ahí y actualizar este link. Las versiones anteriores se
publicaron en otras cuentas de SwaggerHub cuya prueba ya venció (hasta la
v1.3.0 en `upaep-9f5` y de la v1.5.0 a la v1.6.0 en `upaep-9a0`).

| Carpeta | Qué hay | Cómo correrlo |
|---|---|---|
| `backend/` | API FastAPI + la página web | `backend/README.md` |
| `gateway/` | Cliente de la Raspberry Pi: Arduino → buffer → API (MON-03) | `gateway/README.md` |
| `deploy/pi5/` | Instalación de todo en la Raspberry Pi 5 como servicios | `deploy/pi5/README.md` |
| `frontend/` | HTML + CSS + JS que sirve el backend | `frontend/README.md` |
| `vivero_sensores/` | Firmware del Arduino Uno | — |
