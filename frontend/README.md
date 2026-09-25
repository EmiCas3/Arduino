# SmartGreenAI — Frontend

Interfaz web del vivero de rábano. Hoy cubre **AUTH-01** (login), la parte
de UI de **AUTH-02** (menú por rol) y muestra los actuadores de **CONF-05**.

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
│   ├── theme.js      → tema claro/oscuro
│   └── ui.js         → h() para crear nodos de forma segura, íconos, fechas
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
| DASH-01 widgets | Consumir `GET /greenhouses/{id}/readings/latest` en las fichas de "Estado actual" de `renderInicio()`. Refrescar cada 15 s con `setInterval` y pausar cuando la pestaña no está visible (`visibilitychange`). Usar `stale` y `sensor_down` para pintar el estado. | ~1 h (el polling y la limpieza del intervalo son manuales) |
| ALERT-01 UI | Lista de alertas y contador en el menú. Crear `js/store.js` (ver abajo) para compartir las alertas entre el menú y la vista sin recargar. | ~1 h |
| ACT-04 | Badge de estado por actuador (on / off / desconocido) reutilizando `equipIcon()` y `.pill`. | ~0.5 h |

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
