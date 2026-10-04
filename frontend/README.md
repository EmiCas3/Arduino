# SmartGreenAI — Frontend

Interfaz web del vivero de rábano. Hoy cubre **AUTH-01** (login), la parte
de UI de **AUTH-02** (menú por rol), los widgets de lecturas en vivo de
**DASH-01** y muestra los actuadores de **CONF-05**.

Es **HTML + CSS + JavaScript sin framework ni paso de build** (decisión D1 = A
del plan del Sprint 2). FastAPI sirve esta carpeta desde el mismo origen que
la API, así que no hay CORS ni un segundo servidor que levantar.

## Cómo abrirlo

```bash
cd backend
python -m app.seed                          # crea usuarios de demo (una vez)
uvicorn app.main:app --reload --port 8000
```

Abre <http://localhost:8000> y entra con `productor`, `admin` o `superadmin`
(las contraseñas las imprime el seed). No hace falta Node ni `npm install`.

## Estructura

```
frontend/
├── index.html        → redirige a login o al panel
├── login.html        → AUTH-01: usuario o correo + contraseña
├── panel.html        → shell con menú por rol (AUTH-02)
├── css/styles.css    → tokens de diseño (claro/oscuro) y componentes
├── js/
│   ├── api.js        → fetch a /api/v1; 401 → login; 403 → aviso
│   ├── session.js    → JWT en sessionStorage + mensajes entre pantallas
│   ├── roles.js      → rol → secciones del menú y pantalla de inicio
│   ├── login.js      → lógica del login
│   ├── panel.js      → vistas: Mi vivero, Configuración, Plataforma
│   ├── dashboard.js  → DASH-01: tarjetas en vivo, refresco cada 15 s
│   ├── metrics.js    → DASH-01: estados y textos (lógica pura, sin DOM)
│   ├── theme.js      → tema claro/oscuro
│   └── ui.js         → h() para crear nodos de forma segura, íconos, fechas
├── tests/
│   └── metrics.test.mjs → pruebas de metrics.js con `node --test` (opcional)
└── assets/
    ├── favicon.svg
    └── fonts/        → Bricolage Grotesque, Instrument Sans, IBM Plex Mono (OFL)
```

## Reglas del frontend

- **El servidor manda.** `roles.js` solo decide qué se *muestra*. Cada acción
  la valida el backend con `require_roles()`. Si cambias permisos, cámbialos
  primero en `backend/app/dependencies.py` y en el router, y luego aquí.
- **Nada de `innerHTML` con datos del servidor.** Todo se arma con `h()`, que
  inserta texto con `textContent` (evita XSS).
- **Sesión:** el token vive en `sessionStorage` (se borra al cerrar la
  pestaña y no hay CSRF). El timeout por inactividad lo decide el servidor;
  cualquier 401 regresa al login con el mensaje que mandó el backend.
- **Colores solo desde tokens** de `:root`. El tema oscuro redefine tokens,
  nunca estilos sueltos.
- **Fuentes locales** en `assets/fonts`: la demo funciona sin internet.
- **Lo que una vista deja corriendo, lo detiene.** Si una vista arranca un
  intervalo (como los widgets), guarda su función de paro en `state.cleanup`;
  `route()` la llama al cambiar de sección.

## Widgets de "Estado actual" (DASH-01)

"Mi vivero" muestra el último valor de cada sensor con su unidad, área,
"hace N s" y su estado. Los datos salen de
`GET /greenhouses/{id}/readings/latest`.

| Estado | Cuándo | De dónde sale |
|---|---|---|
| Sin datos | Nunca ha llegado un valor | `value: null` |
| Sensor caído | La última lectura trae ese sensor en `null`; se enseña el último valor válido, atenuado | `sensor_down` |
| Dato viejo | Más viejo que `stale_after_minutes` | `stale`, y la edad que avanza en el navegador |
| Alerta / Aviso | El Arduino marcó una alerta para esa medición | `last_reading.alertas` |
| Normal | Nada de lo anterior | — |

Se muestra uno solo, en ese orden de prioridad. Un `null` nunca se pinta como 0.

- **El estado lo decide el Arduino, no el navegador.** `metrics.js` solo
  traduce los códigos de alerta del firmware (`TEMP_ALTA`, `SUELO_SECO_LEVE`,
  `TANQUE_MITAD`…) a la tarjeta y severidad que les toca. No hay umbrales
  copiados en JavaScript: si el firmware cambia un umbral, el dashboard lo
  refleja solo. Cuando existan CONF-06 y ALERT-01, el estado vendrá evaluado
  del backend y solo cambia `metricState()`.
- **Rangos de referencia** ("ideal 20–25 °C"): son texto de ayuda y viven en
  un solo lugar, `METRICS` de `metrics.js`. Agua y luz no muestran números
  porque esos umbrales están en ajuste.
- **Un código de alerta nuevo** que el dashboard no conozca no rompe nada: se
  muestra tal cual en el resumen. Para darle tarjeta y texto, agrégalo a
  `ALERTS`. (El backend sí rechaza lecturas con códigos fuera del contrato.)
- **Refresco:** cada 15 s, solo con la pestaña visible, y al volver a ella.
  "Hace N s" usa la edad que calcula el servidor más el tiempo transcurrido
  en el navegador, así que no depende del reloj de la laptop.
- **Sesión:** los refrescos automáticos mandan `X-SG-Background: 1` y el
  servidor no los cuenta como actividad. Si la persona hizo clic, tecleó o
  tocó la pantalla desde el refresco anterior, ese sí cuenta. Dejar el
  dashboard abierto sin tocarlo cierra la sesión a los 30 min, como pide
  AUTH-01.
- **Sin conexión:** se conservan los últimos valores con un aviso y se
  reintenta solo.
- **Administradores:** no tienen vivero asignado; eligen uno de
  `GET /greenhouses` (con un solo vivero se elige solo).

Pruebas de la lógica (opcional, necesita Node 18 o más; no hay `npm install`):

```bash
node --test frontend/tests/metrics.test.mjs
```

## Identidad visual

| Token | Uso |
|---|---|
| `--night` `#0E2419` | Panel de marca y menú lateral (el invernadero de noche) |
| `--leaf` `#1D7A4C` | Acción principal, estados correctos |
| `--radish` `#C92C63` | Acento con medida: el rábano, Super Administrador |
| `--ground` `#F0F4F0` | Fondo con un toque verde |
| Bricolage Grotesque | Títulos |
| Instrument Sans | Texto |
| IBM Plex Mono | Unidades, canales (`L298N-B/D5`) e ids |

## Cómo agregar una sección

1. Backend: crea el endpoint con `Depends(require_roles(...))`.
2. `roles.js`: agrega la sección a `SECTIONS` con sus roles.
3. `panel.js`: agrega la función de render a `VIEWS`.

---

## Pasos siguientes para los Sprints 3 y 4

Elegir HTML/JS sin framework ahorró tiempo hoy (cero build, un solo deploy),
pero hay cosas que un framework daría hechas y que aquí toca construir.
Estos son los pasos, en orden, con el costo extra estimado frente a haber
empezado con React + Vite.

### Resto del Sprint 2 (partes 2/3 y 3/3)

| Item | Qué hacer en el frontend | Costo extra vs React |
|---|---|---|
| DASH-01 widgets | ✅ Hecho: `dashboard.js` + `metrics.js` (ver "Widgets de Estado actual"). | ~1 h (el polling y la limpieza del intervalo son manuales) |
| ALERT-01 UI | Lista de alertas y contador en el menú. Crear `js/store.js` (ver abajo) para compartir las alertas entre el menú y la vista sin recargar. Puede reutilizar el refresco de `dashboard.js`. | ~1 h |
| ACT-04 | Badge de estado por actuador (on / off / desconocido) reutilizando `equipIcon()` y `.pill`, con el mismo refresco de 15 s. | ~0.5 h |

### Sprint 3 (historia y AI)

1. **Librería de gráficas (DASH-02).** Copiar un archivo UMD con versión fija
   a `frontend/vendor/` (recomendado **uPlot**: ~50 KB y rápido con miles de
   puntos; alternativa **Chart.js**). Se carga con un `<script>` y no requiere
   build. Envolverlo en `js/charts.js` con una función
   `lineChart(container, series, options)` que lea los colores de los tokens
   CSS, para que las gráficas respeten el tema claro/oscuro.
   *Costo extra: ~2 h* (sin wrappers de React, el redimensionado y la
   destrucción del gráfico al cambiar de vista son manuales).
2. **Rango de fechas y "sin datos".** Controles `<input type="date">`
   nativos; el mensaje explícito de rango vacío ya tiene el estilo `.empty`.
3. **Agregación en el servidor.** El endpoint de histórico
   (`/greenhouses/{id}/readings`) debe devolver los datos ya muestreados;
   no conviene mandar 40 000 puntos al navegador.
4. **Vistas de AI (AI-02, AI-05).** Tarjetas con la anomalía y la explicación
   en lenguaje claro. Reutilizan `.card`, `.notice` y `.pill`.

### Sprint 4 (integración)

1. **Estado compartido (DASH-03).** Con varias secciones vivas en una sola
   pantalla, crear `js/store.js`: un objeto con `get()`, `set()` y
   `subscribe()` (unas 30 líneas). Cada widget se vuelve un módulo con
   `mount(contenedor)` y `update(datos)`. *Costo extra: ~3 h* (esto es lo que
   React resuelve con componentes y estado).
2. **Responsivo.** La retícula de 12 columnas y el menú móvil ya existen;
   solo hay que revisar las vistas nuevas a 390 px.
3. **Bitácora (AUTH-05).** Tabla con filtros y paginación reutilizando
   `.table`. *Costo extra: ~1 h* por la paginación manual.
4. **Pruebas end-to-end.** Agregar pruebas con Playwright (login por rol,
   403, sesión expirada). Ya se usaron para revisar estas pantallas.
   *~3 h, igual con o sin framework.*
5. **Caché del navegador.** Al desplegar, agregar `?v=<versión>` a los
   `<script>` y `<link>` para que nadie se quede con un JS viejo.

**Costo extra de seguir con esta opción: 10 a 12 h**, repartidas en los
tres sprints. Migrar hoy a React + Vite costaría 8–12 h, más el tiempo de
aprender el stack y de instalar Node en la máquina de cada integrante. Por
eso conviene seguir así, salvo que aparezca alguna de las señales de abajo.

### Cuándo sí migrar a React + Vite

Si pasa cualquiera de estas cosas, conviene migrar:

- Más de ~8 pantallas.
- Formularios grandes con validación en vivo.
- Mucho estado compartido entre vistas.

La migración es barata porque:

- La API no cambia.
- `api.js`, `session.js` y `roles.js` no dependen de ningún framework y se copian tal cual.
- `styles.css` y sus tokens se reutilizan sin cambios.
- El build de Vite (`dist/`) se sirve igual con FastAPI apuntando `SMARTGREENAI_FRONTEND_DIR` a esa carpeta.
