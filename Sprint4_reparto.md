# Sprint 4 — Reparto de tareas

**Fechas:** 2 – 20 nov 2026 · **Capacidad:** 75 h · **Comprometido:** 61 h (26 sp) · **Reserva:** 14 h para defectos

**Meta del sprint:** todo lo construido funciona como una sola plataforma segura y probada, y el equipo puede
hacer en vivo el escenario de aceptación del brief desde un solo dashboard.

Estado al 11 nov 2026: Sprint 3 completo en `prueba` (NF-01, DASH-02, AI-01, AI-02, AI-04, AI-05).
Ningún item del Sprint 4 está empezado.

---

## Resumen por integrante

| Integrante | Rol | Tareas | Horas |
|---|---|---|---|
| Fernando | IoT y Backend | SYS-04 (cablear el flujo) | 11 h |
| Emiliano | Web y Dashboard | DASH-03 (las dos tareas) | 18 h |
| Xavier | Scrum Master | AI-07 · SYS-04 (pruebas de integración) | 14 h |
| Alexander | Product Owner | AUTH-05 (las dos tareas) · NFR-03 | 18 h |
| **Total** | | | **61 h** |

---

## Fernando — IoT y Backend (11 h)

| Item | Tarea | sp | h |
|---|---|---|---|
| SYS-04 | Cablear el flujo completo: sensor → nube → alerta/IA → actuador | 5 | 11 |

- **SYS-04:** una lectura nueva, sin intervención humana, llega a la nube, se guarda, se revisa contra
  los umbrales, la analiza la IA y produce una alerta o una recomendación. Si un eslabón falla, debe
  verse un error, no fallar en silencio.

## Emiliano — Web y Dashboard (18 h)

| Item | Tarea | sp | h |
|---|---|---|---|
| DASH-03 | Unir sensores, alertas, actuadores e IA en un solo dashboard | 5 | 11 |
| DASH-03 | Layout responsive + navegación entre secciones | 3 | 7 |

- Lecturas actuales, alertas activas, estado de actuadores y la última recomendación de la IA en una
  sola pantalla.
- Cada sección se actualiza sin recargar toda la página.
- En celular se puede usar sin scroll horizontal.
- Incluir la sección de bitácora de AUTH-05 cuando su API esté lista (hoy es una tarjeta de
  "próximamente" en `frontend/js/panel.js`).

## Xavier — Scrum Master (14 h)

| Item | Tarea | sp | h |
|---|---|---|---|
| AI-07 | UI para aceptar/rechazar la recomendación + guardar la decisión | 3 | 7 |
| SYS-04 | Pruebas de integración del flujo de punta a punta | 3 | 7 |

- **AI-07:** la decisión se guarda con usuario y hora; si acepta una recomendación de riego, el riego
  se puede disparar desde la misma pantalla; las rechazadas siguen visibles en el historial. Parte de
  `backend/app/recommendation.py` y `frontend/js/recommendation.js` (los hizo él en AI-04/AI-05).
  La decisión también debe quedar en la bitácora de AUTH-05.
- **Pruebas SYS-04:** las escribe alguien distinto a quien cablea el flujo, para que sirvan de
  verificación independiente.
- **Como SM:** facilitar el Sprint Review y la Retro, llevar el registro de reuniones.

## Alexander — Product Owner (18 h)

| Item | Tarea | sp | h |
|---|---|---|---|
| AUTH-05 | Diseñar el esquema único de eventos (sesiones, alertas, actuadores, IA) | 2 | 5 |
| AUTH-05 | Capa de escritura de la bitácora + API de consulta | 3 | 8 |
| NFR-03 | Hash de contraseñas (bcrypt/argon2) + migración de cuentas | 2 | 5 |

- Cada inicio de sesión, alerta, evento de actuador y recomendación de la IA se guarda con actor,
  acción y hora.
- Se puede consultar por fecha y tipo de evento.
- Desde la aplicación no se puede editar ni borrar ningún registro.
- **NFR-03:** hoy `backend/app/security.py` usa `scrypt` con salt y ya trae `needs_rehash()` para
  migrar cada cuenta en su siguiente login. Decidir si `scrypt` se acepta o si hay que
  pasar a bcrypt/argon2 (ojo con instalarlos en el Python de MSYS2). Verificar que ninguna contraseña
  en texto plano aparezca en la BD, los logs ni las respuestas de la API.
- **Como PO:**
  - Llevar `prueba` a `main`: la Definition of Done pide el código en main.
  - Llenar la columna *Status* del PDF y actualizar ClickUp.
  - Subir el `openapi.yaml` nuevo a SwaggerHub y actualizar el link del README.
  - Aceptar o rechazar cada item en el Sprint Review.

---

## Orden y dependencias

1. **Semana 1 (2 – 6 nov) / ya:** el esquema de AUTH-05 primero, porque AI-07 y DASH-03 escriben y
   muestran eventos ahí. NFR-03 va en paralelo (no depende de nadie).
2. **Semana 2 (9 – 13 nov):** API de bitácora, AI-07, DASH-03 y el cableado de SYS-04.
3. **Semana 3 (16 – 20 nov):** pruebas de integración de SYS-04, merge a `main`, ensayo de la demo
   y Sprint Review.

Si algo se atrasa, usar las 14 h de reserva antes de mover alcance; cualquier trabajo nuevo va al
product backlog para la siguiente planeación.
