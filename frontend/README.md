# SmartGreenAI — Frontend

Interfaz web del vivero de rábano. Hoy cubre **AUTH-01** (login), la parte
de UI de **AUTH-02** (menú por rol), los widgets de lecturas en vivo de
**DASH-01**, las alertas de **ALERT-01**, los actuadores de **CONF-05**, su
estado en vivo (**ACT-04**), el control del riego (**ACT-02**), las gráficas
históricas de **DASH-02** y las tendencias de **AI-01**.

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
│   ├── panel.js      → vistas: Mi vivero, Historial, Configuración, Plataforma
│   ├── dashboard.js  → DASH-01: tarjetas en vivo, refresco cada 15 s
│   ├── metrics.js    → estados y textos de los widgets (lógica pura, sin DOM)
│   ├── alerts.js     → ALERT-01: tarjeta "Alertas", avisos flotantes y contador
│   ├── equipment.js  → ACT-04 y ACT-02: estado de los equipos y control del riego
│   ├── poller.js     → refresco cada 15 s compartido por los widgets nuevos
│   ├── history.js    → DASH-02 y AI-01: vista "Historial" (gráfica + tendencias)
│   ├── chart.js      → DASH-02: la gráfica de líneas en SVG, sin librerías
│   ├── series.js     → escalas, marcas y textos de la gráfica (lógica pura)
│   ├── theme.js      → tema claro/oscuro
│   └── ui.js         → h() y svg() para crear nodos de forma segura, íconos, fechas
├── tests/
│   ├── metrics.test.mjs → pruebas de metrics.js con `node --test` (opcional)
│   └── series.test.mjs  → pruebas de series.js (escalas, cortes, textos)
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

## Alertas (ALERT-01)

La tarjeta "Alertas" de "Mi vivero" (`alerts.js`) muestra las alertas que
crea el backend cuando el Arduino reporta que una medición cruzó su límite
(`GET /greenhouses/{id}/alerts`):

- **Activas:** título (el mismo texto de la tarjeta del sensor), valor y
  límite del firmware ("31.2 °C (límite 30.0 °C)"), desde cuándo, y el valor
  actual y el peor si cambiaron.
- **Se normalizaron:** hasta 5 de las últimas 24 h, atenuadas, con cuánto
  duraron.
- **Sin recargar:** se refresca cada 15 s (`poller.js`). Una alerta nueva
  aparece sola, con un aviso flotante (`aria-live`, se cierra solo a los
  10 s) y el número de activas en el título de la pestaña:
  "(1) Mi vivero · SmartGreenAI". En la primera carga no se avisa: solo de
  las que llegan después.
- Los umbrales viven en el firmware: el número del límite llega de la API,
  no hay umbrales en JavaScript.

## Estado de los equipos (ACT-04)

"Equipos del vivero" muestra cada actuador activo con su estado, de
`GET /greenhouses/{id}/actuators/status`, refrescado cada 15 s
(`equipment.js` + `poller.js`, mismas reglas de sesión y visibilidad que los
widgets de DASH-01):

| Badge | Cuándo |
|---|---|
| Encendido / Apagado | El Arduino lo confirmó hace poco ("Confirmado hace 8 s") |
| No disponible | Bomba bloqueada por tanque bajo, o gateway desactivado |
| Desconocido (borde punteado, sin relleno) | Sin confirmación, confirmación vieja, sin gateway, o malla/iluminación |

- **Nunca "Apagado" sin confirmación.** `powerView()` (`metrics.js`) también
  lo pasa a "Desconocido" si la confirmación envejece entre dos refrescos.
- "Encendido" es lo que el Arduino ordenó en el pin; no mide si el motor gira
  (lo dice el tooltip).

## Control del riego (ACT-02)

Debajo de la bomba, en "Equipos del vivero":

- **Riego: Automático / Manual.** En automático el Arduino riega solo con sus
  constantes; la ayuda dice el objetivo y el tiempo máximo que manda
  `GET /irrigation` (no hay números en JavaScript). El productor y los
  admins lo pueden cambiar.
- **Regar / Detener**, solo en manual. "Regar" se desactiva si la bomba está
  "No disponible" (tanque bajo).
- **Después de un clic** se ve "Orden enviada… Esperando al Arduino" y el
  refresco se acelera (cada 3 s durante 40 s, `poller.boost()`) hasta que el
  Arduino confirma; el badge cambia sin recargar. Si el Arduino la rechaza o
  no confirma, se dice por qué (`commandFeedback()` en `metrics.js`).
- **Firmware sin el protocolo:** el switch queda deshabilitado con la
  explicación; el estado de los equipos se sigue mostrando.
- **Últimas acciones:** los 6 encendidos/apagados más recientes con su
  origen: "Automático" o "Manual · nombre" (`eventText()`).

Pruebas de la lógica (opcional, necesita Node 18 o más; no hay `npm install`):

```bash
node --test frontend/tests/metrics.test.mjs frontend/tests/series.test.mjs
```

## Historial: gráficas y tendencias (DASH-02 / AI-01)

La sección "Historial" (`history.js`) tiene dos tarjetas:

**Cómo evolucionó** — una medición en el tiempo, de
`GET /greenhouses/{id}/readings/series`. Se elige la medición y el rango
(6 h, 24 h, 7 días, 30 días).

- **El servidor decide la resolución.** Si en el rango caben las lecturas
  (`max_points`, 240), vienen tal cual; si no, vienen promediadas por bucket
  y la gráfica pinta además la banda del mínimo al máximo de cada bucket, así
  promediar no esconde un pico. El pie dice cuál fue: "promedio cada 10 min ·
  639 lecturas" o "lectura por lectura · 179 lecturas".
- **Donde faltan datos la línea se corta** (`splitSegments()`), en vez de
  cruzar el hueco con una recta que nadie midió.
- **Un rango sin lecturas** muestra el aviso dentro del lienzo ("No hay
  lecturas de temperatura en este periodo."), no una gráfica vacía.
- **La escala vertical se ajusta a los datos**, no a 0–100: un tanque que
  bajó de 76 % a 74 % se tiene que ver bajando.
- **Accesible:** el SVG es una imagen con su descripción y el pie dice en
  texto el punto señalado. Con el mouse o con las flechas ←/→ (y Inicio/Fin)
  se recorre la serie punto por punto.
- **Un promedio se dibuja al centro de su bucket** (`pointTime()`): la API
  manda el inicio del bucket, y dibujarlo ahí corría la línea medio bucket a
  la izquierda.
- **Las marcas del eje van en la hora de quien mira** (`timeTicks()`): las de
  días caen en la medianoche local y las de horas se cuentan desde ella.

**Tendencias del periodo** — de `GET /greenhouses/{id}/analysis`: por cada
medición, si va subiendo, estable o bajando, con el cambio del periodo y la
calidad del dato ("cobertura 89.6 % · 2 huecos · 2 h 30 min · 20 sin dato del
sensor"). El periodo y el paso usados se muestran arriba, porque el indicador
no significa nada sin ellos. La explicación en lenguaje natural es AI-05: aquí
solo se muestran los números que devuelve el análisis. El cambio se escribe
con los decimales de la medición, salvo que el redondeo contradiga el
indicador (`formatChange()`): una humedad que cambió 1.6 ("Estable", banda
de 2) sale "+1.6 % HR", no "+2 % HR" como una que sí sube.

El refresco depende del rango (`refreshSeconds()`): 60 s en 6 h y 24 h, 5 min
en 7 días y 15 min en 30 días (`poller.js`, mismas reglas de sesión y
visibilidad que los widgets; `poller.setSeconds()` lo cambia al elegir otro
rango). Un histórico largo cambia poco y agruparlo cuesta: 30 días son ~1.3 M
lecturas para SQLite, con la misma conexión que usa la ingesta de la Pi.

### Por qué la gráfica es SVG a mano y no uPlot

El plan de este README (sección "Pasos siguientes") proponía vendorizar
**uPlot**. Al implementarlo se eligió dibujar el SVG a mano (`chart.js`,
~250 líneas) por tres razones:

1. El backend ya entrega como máximo 240 puntos (criterio 3 de DASH-02), así
   que la ventaja de uPlot —decenas de miles de puntos— no aplica.
2. No se agrega un archivo de terceros al repo, con su licencia y su versión
   que seguir.
3. Los colores salen de los tokens de `:root`, así que el tema claro/oscuro
   funciona sin puentes entre CSS y JavaScript.

Si más adelante hace falta zoom, brush o varias series a la vez, vendorizar
uPlot sigue siendo la salida: `history.js` solo llama a `createChart()` y
`paint(series)`, y eso es lo único que habría que reimplementar.

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
| ALERT-01 UI | ✅ Hecho: `alerts.js` (ver "Alertas"). El contador va en la tarjeta y en el título de la pestaña; ponerlo también en el menú lateral necesitará `js/store.js` (ver abajo). | ~1 h |
| ACT-04 | ✅ Hecho: `equipment.js` + `poller.js` (ver "Estado de los equipos"). | ~0.5 h |
| ACT-02 | ✅ Hecho: control del riego en `equipment.js` (ver "Control del riego"). | ~1 h (estado optimista y refresco acelerado a mano) |

### Sprint 3 (historia y AI)

1. **Gráficas (DASH-02).** ✅ Hecho, pero **sin** la librería que proponía
   este plan: `chart.js` dibuja el SVG a mano (ver "Por qué la gráfica es SVG
   a mano"). *Costo real: parecido a las ~2 h estimadas.*
2. **Rango y "sin datos".** ✅ Hecho con botones de rango (6 h / 24 h /
   7 días / 30 días) en vez de `<input type="date">`: es lo que se usa en una
   demo y evita validar rangos a mano. El aviso de rango vacío se dibuja
   dentro del lienzo. Si hace falta elegir fechas exactas, la API ya acepta
   `from` y `to`.
3. **Agregación en el servidor.** ✅ Hecho: `/readings/series` devuelve como
   máximo `max_points` puntos, promediados por bucket y con el mínimo y el
   máximo de cada uno.
4. **Vistas de AI.** AI-01 ✅ (tarjetas de tendencia en "Historial"). Faltan
   AI-02 (anomalías) y AI-05 (explicación en lenguaje claro); reutilizan
   `.card`, `.notice` y `.pill`.

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
