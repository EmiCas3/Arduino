# SmartGreenAI — Radish Crop

Documentación de la API en SwaggerHub (v1.12.0):
https://app.swaggerhub.com/apis/upaep-7de/smartgreenai-radish-crop-api/1.12.0

La fuente oficial del contrato es `openapi.yaml` de este repositorio
(**v1.12.0**: AI-07 deja aceptar o rechazar la recomendación de riego y
regar desde la misma tarjeta. Antes, v1.11.0: SYS-04 cablea el flujo completo sensor → nube → alerta/IA →
actuador — cada lote que manda la Pi evalúa al llegar actuadores, riego,
alertas y la recomendación de riego, y la respuesta reporta en `errors` los
pasos que fallen. Antes, v1.10.0, Sprint 4: agrega la bitácora de auditoría de AUTH-05 y el
hash de contraseñas de NFR-03 a lo del Sprint 3: histórico y gráficas de
DASH-02, análisis de AI-01, sensor de CO₂ de NF-01, condiciones inusuales de
AI-02, recomendación de riego de AI-04 y su explicación de AI-05).
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
| `vivero_sensores/` | Firmware del Arduino Uno | `vivero_sensores/README.md` |
