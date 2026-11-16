/**
 * Panel principal: shell con menú por rol (AUTH-02) y pantallas de inicio.
 *
 * Secciones (roles.js):
 *   #/inicio         todos           lecturas en vivo (DASH-01), recomendación de riego
 *                                    (AI-05), equipos y riego (ACT-04/ACT-02), alertas
 *                                    (ALERT-01) y anomalías (AI-02) en un dashboard (DASH-03)
 *   #/pantalla       todos           dashboard en tiempo real para pantalla externa (DASH-03)
 *   #/historial      todos           gráficas históricas (DASH-02) y tendencias (AI-01)
 *   #/bitacora       admin, super    bitácora de auditoría (AUTH-05)
 *   #/configuracion  admin, super    actuadores registrados (CONF-05)
 *   #/plataforma     super           permisos por rol y bitácora (AUTH-05)
 *
 * El menú solo muestra lo que el rol puede usar, pero el control real es del
 * servidor: si una llamada responde 403 se muestra el aviso y nada más.
 */
import { apiFetch, ApiError } from "./api.js";
import { mountAlerts } from "./alerts.js";
import { mountAnomalies } from "./anomalies.js";
import { mountAudit } from "./audit.js";
import { mountLiveReadings } from "./dashboard.js";
import { mountDisplay } from "./display.js";
import { mountEquipment } from "./equipment.js";
import { mountHistory } from "./history.js";
import { mountRecommendation } from "./recommendation.js";
import { clearSession, loadSession, setFlash } from "./session.js";
import {
  ACTUATOR_TYPES,
  LANDING,
  PERMISSIONS,
  ROLE_LABELS,
  SECTIONS,
  canAccess,
  sectionsFor,
} from "./roles.js";
import { initThemeToggle } from "./theme.js";
import { firstName, formatDateTime, h, icon, initials, today } from "./ui.js";

const app = document.getElementById("app");
const view = document.getElementById("view");
const nav = document.getElementById("nav");
const crumb = document.getElementById("crumb");
const backdrop = document.getElementById("backdrop");
const menuBtn = document.getElementById("menu-btn");

const state = {
  user: null,
  actuatorStatus: "active",
  greenhouse: null,   // vivero elegido por un admin (los productores tienen el suyo)
  cleanup: null,      // detiene lo que la vista actual dejó corriendo (refrescos)
  viewToken: 0,       // cambia en cada navegación: descarta respuestas tardías
};

// ── Arranque ──────────────────────────────────────────────────────────

async function boot() {
  initThemeToggle(document.getElementById("theme-toggle"));
  menuBtn.replaceChildren(icon("menu"));
  menuBtn.addEventListener("click", () => setNavOpen(!app.classList.contains("nav-open")));
  backdrop.addEventListener("click", () => setNavOpen(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      setNavOpen(false);
      if (app.classList.contains("is-kiosk")) {
        app.classList.remove("is-kiosk");
        const kioskBtn = document.getElementById("kiosk-toggle");
        if (kioskBtn) {
          kioskBtn.replaceChildren(icon("maximize", "icon-sm"), "Pantalla completa");
          kioskBtn.setAttribute("aria-pressed", "false");
        }
      }
    }
  });
  document.getElementById("logout-btn").addEventListener("click", logout);

  if (!loadSession()?.token) {
    location.replace("login.html");
    return;
  }
  try {
    // El servidor confirma que la sesión sigue viva y cuál es el rol real
    state.user = await apiFetch("/auth/me");
  } catch (error) {
    if (error.status !== 401) renderFatal(error);
    return;
  }

  renderShell();
  window.addEventListener("hashchange", route);
  route();
}

function setNavOpen(open) {
  app.classList.toggle("nav-open", open);
  backdrop.hidden = !open;
  menuBtn.setAttribute("aria-expanded", String(open));
}

async function logout(event) {
  const button = event.currentTarget;
  button.disabled = true;
  button.classList.add("is-loading");
  try {
    await apiFetch("/auth/logout", { method: "POST" });
  } catch { /* si la sesión ya había expirado, igual se sale */ }
  clearSession();
  setFlash("ok", "Cerraste sesión. ¡Hasta pronto!");
  location.replace("login.html");
}

// ── Shell ─────────────────────────────────────────────────────────────

function renderShell() {
  const { user } = state;
  document.getElementById("user-name").textContent = user.full_name || user.username;
  document.getElementById("user-role").textContent = ROLE_LABELS[user.role];
  document.getElementById("user-avatar").textContent = initials(user);

  const idle = loadSession()?.idleMinutes;
  const hint = document.getElementById("session-hint");
  if (idle) hint.replaceChildren(icon("clock", "icon-sm"), `La sesión se cierra tras ${idle} min sin actividad`);

  nav.replaceChildren(
    h("div", { class: "nav-label" }, "Menú"),
    ...sectionsFor(user.role).map((section) =>
      h("a", { class: "nav-link", href: `#/${section.id}`, dataset: { section: section.id } },
        icon(section.icon), section.label)
    ),
  );

  let mobileNav = document.getElementById("mobile-nav");
  if (!mobileNav) {
    mobileNav = h("nav", {
      class: "mobile-nav only-mobile",
      id: "mobile-nav",
      "aria-label": "Navegación rápida",
    });
    const mainEl = app.querySelector(".main");
    if (mainEl) mainEl.append(mobileNav);
  }
  mobileNav.replaceChildren(
    ...sectionsFor(user.role).map((section) =>
      h("a", { class: "mobile-nav-link", href: `#/${section.id}`, dataset: { section: section.id } },
        icon(section.icon, "icon-sm"), h("span", {}, section.label))
    ),
  );
}

function currentSectionId() {
  const id = location.hash.replace(/^#\/?/, "");
  return id || LANDING[state.user.role];
}

function route() {
  setNavOpen(false);
  app.classList.remove("is-kiosk");
  // Al cambiar de sección se detiene el refresco de la anterior: sin esto
  // quedarían intervalos pidiendo lecturas para una vista que ya no existe.
  if (state.cleanup) {
    state.cleanup();
    state.cleanup = null;
  }
  state.viewToken += 1;
  const id = currentSectionId();
  const section = SECTIONS.find((s) => s.id === id);

  for (const link of document.querySelectorAll(".nav-link, .mobile-nav-link")) {
    if (link.dataset.section === id) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }

  if (!section) {
    location.replace(`#/${LANDING[state.user.role]}`);
    return;
  }
  crumb.replaceChildren("SmartGreenAI / ", h("strong", {}, section.label));
  document.title = `${section.label} · SmartGreenAI`;

  if (!canAccess(state.user.role, id)) {
    renderForbidden();
  } else {
    VIEWS[id]();
  }
  view.focus({ preventScroll: true });
  window.scrollTo({ top: 0 });
}

// ── Piezas reutilizables ──────────────────────────────────────────────

function pageHead({ eyebrow, title, text, meta = [] }) {
  return h("header", { class: "page-head" },
    h("p", { class: "eyebrow" }, eyebrow),
    h("h1", {}, title),
    text ? h("p", {}, text) : null,
    meta.length ? h("div", { class: "page-meta" }, meta) : null,
  );
}

function rolePill(role) {
  return h("span", { class: `pill pill-plain pill-${role}` }, ROLE_LABELS[role]);
}

function cardHead(title, subtitle, action, id) {
  return h("div", { class: "card-head" },
    h("div", {}, h("h2", { id }, title), subtitle ? h("p", {}, subtitle) : null),
    action || null,
  );
}

function equipIcon(type, extra = "") {
  const meta = ACTUATOR_TYPES[type] || { icon: "chip" };
  return h("span", { class: `equip-icon type-${type} ${extra}`.trim() }, icon(meta.icon));
}

function statusPill(status) {
  return status === "active"
    ? h("span", { class: "pill pill-ok" }, "Activo")
    : h("span", { class: "pill pill-off" }, "Desactivado");
}

function errorNotice(error) {
  return h("div", { class: "notice notice-crit", role: "alert" },
    icon("alert"), h("p", {}, error instanceof ApiError ? error.message : "Algo salió mal."));
}

function loadingLines(count = 3) {
  return h("div", { style: "display:grid;gap:12px" },
    Array.from({ length: count }, (_, i) =>
      h("div", { class: "skeleton-line", style: `width:${90 - i * 18}%` })));
}

// ── Vista: Mi vivero ──────────────────────────────────────────────────

function renderInicio() {
  const { user } = state;
  const greenhouse = user.greenhouse_id;

  const equipmentBody = h("div", {}, loadingLines());
  const picker = h("div", { class: "greenhouse-picker" });
  const liveBody = h("div", { class: "live" }, loadingLines());
  const recommendationBody = h("div", { class: "alerts" }, loadingLines(2));
  const recommendationPill = h("div", { class: "alerts-count" });
  const alertsBody = h("div", { class: "alerts" }, loadingLines(2));
  const alertsCount = h("div", { class: "alerts-count" });
  const anomaliesBody = h("div", { class: "alerts" }, loadingLines(2));
  const anomaliesCount = h("div", { class: "alerts-count" });

  const jumps = [
    { id: "sec-sensores", label: "Sensores" },
    { id: "sec-ia", label: "Recomendación IA" },
    { id: "sec-equipos", label: "Equipos y riego" },
    { id: "sec-alertas", label: "Alertas" },
    { id: "sec-anomalias", label: "Condiciones inusuales" },
  ];
  const otherSections = sectionsFor(user.role).filter((s) => s.id !== "inicio");

  view.replaceChildren(
    pageHead({
      eyebrow: today(),
      title: `Hola, ${firstName(user)}`,
      text: greenhouse
        ? "Sensores, recomendación de IA, alertas y equipos del vivero en una sola pantalla."
        : "Elige un vivero para ver sus sensores, recomendación de IA, alertas y equipos en una sola pantalla.",
      meta: [
        rolePill(user.role),
        greenhouse
          ? h("span", { class: "pill pill-plain" }, icon("sprout", "icon-sm"), greenhouse)
          : h("span", { class: "pill pill-plain" }, "Todos los viveros"),
      ],
    }),
    h("nav", { class: "section-nav", "aria-label": "Secciones del panel" },
      h("div", { class: "section-nav-group" },
        jumps.map((item) => h("button", {
          class: "section-jump",
          type: "button",
          onclick: () => document.getElementById(item.id)?.scrollIntoView({ behavior: "smooth", block: "start" }),
        }, item.label))),
      otherSections.length
        ? h("div", { class: "section-nav-links" },
            otherSections.map((section) => h("a", {
              class: "section-nav-link",
              href: `#/${section.id}`,
            }, icon(section.icon, "icon-sm"), section.label)))
        : null),
    h("div", { class: "grid" },
      h("section", { class: "card span-12", id: "sec-sensores", "aria-labelledby": "t-estado" },
        h("div", { class: "card-head" },
          h("div", {},
            h("h2", { id: "t-estado" }, "Estado actual"),
            h("p", {}, "El último valor de cada sensor, cuándo se midió y si está en rango.")),
          picker),
        liveBody),

      h("section", { class: "card span-6", id: "sec-ia", "aria-labelledby": "t-riego" },
        cardHead("Recomendación de riego", "Qué conviene hacer con el riego y por qué, según la tierra, la temperatura y el último riego.", recommendationPill, "t-riego"),
        recommendationBody),

      h("section", { class: "card span-6", id: "sec-equipos", "aria-labelledby": "t-equipos" },
        cardHead("Equipos del vivero", "Lo que el Arduino reporta de cada equipo y el control del riego. Se actualiza solo cada 15 s.", null, "t-equipos"),
        equipmentBody),

      h("section", { class: "card span-6", id: "sec-alertas", "aria-labelledby": "t-alertas" },
        cardHead("Alertas", "Cuando el Arduino reporta que una medición cruzó su límite. Aparecen solas, sin recargar.", alertsCount, "t-alertas"),
        alertsBody),

      h("section", { class: "card span-6", id: "sec-anomalias", "aria-labelledby": "t-anomalias" },
        cardHead("Condiciones inusuales", "Cuando una medición se sale de lo que su sensor venía haciendo, aunque siga dentro de los límites. No es una alerta: es un aviso temprano.", anomaliesCount, "t-anomalias"),
        anomaliesBody),

      h("section", { class: "card span-12", id: "sec-acceso", "aria-labelledby": "t-acceso" },
        cardHead("Tu acceso", `Lo que puede hacer un ${ROLE_LABELS[user.role]}.`, null, "t-acceso"),
        h("div", { class: "perm-grid" },
          PERMISSIONS.map((p) => {
            const allowed = p.roles.includes(user.role);
            return h("div", { class: `perm ${allowed ? "perm-yes" : "perm-no"}` },
              icon(allowed ? "check" : "minus"),
              h("span", {}, p.label),
              allowed ? null : h("span", { class: "sr-only" }, "(no permitido)"));
          })),
      ),
    ),
  );
  startLiveReadings({
    body: liveBody, picker, equipmentBody, recommendationBody, recommendationPill,
    alertsBody, alertsCount, anomaliesBody, anomaliesCount,
  });
}

/**
 * Arranca los widgets en vivo (DASH-01, AI-05, ALERT-01, AI-02, ACT-04) en el vivero
 * del usuario, o en el que elija un admin. Todos se detienen juntos al salir
 * de la vista o al cambiar de vivero.
 */
function startLiveReadings({
  body, picker, equipmentBody, recommendationBody, recommendationPill,
  alertsBody, alertsCount, anomaliesBody, anomaliesCount,
}) {
  return withGreenhouse({
    picker,
    mount: (greenhouseId) => [
      mountLiveReadings(body, greenhouseId),
      mountRecommendation(recommendationBody, greenhouseId, recommendationPill),
      mountAlerts(alertsBody, greenhouseId, alertsCount),
      mountAnomalies(anomaliesBody, greenhouseId, anomaliesCount),
      mountEquipment(equipmentBody, greenhouseId),
    ],
    onError: (error) => {
      body.replaceChildren(errorNotice(error));
      equipmentBody.replaceChildren();
      recommendationBody.replaceChildren();
      alertsBody.replaceChildren();
      anomaliesBody.replaceChildren();
    },
    onEmpty: () => {
      equipmentBody.replaceChildren(h("div", { class: "empty" }, "Sin vivero no hay equipos que mostrar."));
      recommendationBody.replaceChildren(h("div", { class: "empty" }, "Sin vivero no hay nada que recomendar."));
      alertsBody.replaceChildren(h("div", { class: "empty" }, "Sin vivero no hay alertas."));
      anomaliesBody.replaceChildren(h("div", { class: "empty" }, "Sin vivero no hay nada que comparar."));
      body.replaceChildren(noGreenhouseNotice());
    },
  });
}

/**
 * En qué vivero trabaja una vista y cómo se cambia de uno a otro.
 *
 * - Productor: su vivero asignado. El servidor lo vuelve a comprobar en cada
 *   llamada (403 si no es suyo): el selector no es control de acceso.
 * - Admin y super admin: los viveros con dispositivos registrados, con un
 *   <select> si hay más de uno.
 *
 * `mount(greenhouseId)` devuelve las funciones que detienen lo que la vista
 * dejó corriendo; se guardan en state.cleanup para que route() las llame al
 * salir de la sección o al cambiar de vivero.
 */
async function withGreenhouse({ picker, mount, onError, onEmpty }) {
  const token = state.viewToken;
  const show = (greenhouseId) => {
    if (state.cleanup) state.cleanup();
    state.greenhouse = greenhouseId;
    const stops = [mount(greenhouseId)].flat().filter(Boolean);
    state.cleanup = () => stops.forEach((stop) => stop());
  };

  if (state.user.greenhouse_id) {
    show(state.user.greenhouse_id);
    return;
  }

  let greenhouses;
  try {
    greenhouses = await apiFetch("/greenhouses");
  } catch (error) {
    if (token === state.viewToken) onError(error);
    return;
  }
  if (token !== state.viewToken) return;   // ya se navegó a otra sección

  if (!greenhouses.length) {
    onEmpty();
    return;
  }

  const ids = greenhouses.map((g) => g.greenhouse_id);
  const selected = ids.includes(state.greenhouse) ? state.greenhouse : ids[0];
  if (ids.length > 1) {
    const select = h("select", {
      class: "input input-sm",
      id: "greenhouse-select",
      onchange: (event) => show(event.currentTarget.value),
    }, ids.map((id) => h("option", { value: id, selected: id === selected }, id)));
    picker.replaceChildren(h("label", { class: "sr-only", for: "greenhouse-select" }, "Vivero"), select);
  } else {
    picker.replaceChildren(h("span", { class: "pill pill-plain" }, icon("sprout", "icon-sm"), selected));
  }
  show(selected);
}

/** Aviso de "todavía no hay un vivero que mirar", según el rol. */
function noGreenhouseNotice() {
  const producer = state.user.role === "producer";
  return h("div", { class: "empty" },
    h("strong", {}, producer
      ? "Tu usuario aún no tiene un vivero asignado."
      : "Aún no hay viveros con dispositivos registrados."),
    producer
      ? "Pídele a un Administrador que te asigne uno."
      : "Cuando se registre un gateway, su vivero aparecerá aquí.");
}

// ── Vista: Pantalla externa (DASH-03) ─────────────────────────────────

function renderPantalla() {
  const { user } = state;
  const picker = h("div", { class: "greenhouse-picker" });
  const body = h("div", {}, loadingLines(4));

  const kioskBtn = h("button", {
    class: "btn btn-quiet btn-sm",
    id: "kiosk-toggle",
    type: "button",
    "aria-pressed": "false",
  }, icon("maximize", "icon-sm"), "Pantalla completa");

  const paintKioskBtn = () => {
    const active = app.classList.contains("is-kiosk");
    kioskBtn.replaceChildren(
      icon(active ? "minimize" : "maximize", "icon-sm"),
      active ? "Salir pantalla completa" : "Pantalla completa",
    );
    kioskBtn.setAttribute("aria-pressed", String(active));
  };

  kioskBtn.addEventListener("click", async () => {
    const entering = !app.classList.contains("is-kiosk");
    app.classList.toggle("is-kiosk", entering);
    paintKioskBtn();
    try {
      if (entering && document.documentElement.requestFullscreen) {
        await document.documentElement.requestFullscreen();
      } else if (!entering && document.fullscreenElement) {
        await document.exitFullscreen();
      }
    } catch { /* fallback CSS is-kiosk sigue activo */ }
  });

  const onFullscreenChange = () => {
    if (!document.fullscreenElement && app.classList.contains("is-kiosk")) {
      app.classList.remove("is-kiosk");
      paintKioskBtn();
    }
  };
  document.addEventListener("fullscreenchange", onFullscreenChange);

  const standaloneLink = h("a", {
    class: "btn btn-quiet btn-sm",
    href: "display.html",
    title: "Abrir versión independiente sin menú lateral",
  }, icon("monitor", "icon-sm"), "Vista independiente");

  const controlsSlot = h("div", { class: "display-slot-actions" }, kioskBtn, standaloneLink);

  view.replaceChildren(
    pageHead({
      eyebrow: "Display Raspberry Pi · Tiempo real",
      title: "Pantalla externa",
      text: "Lectura rápida de sensores, equipos, recomendación IA y alertas con actualización automática cada 5 segundos.",
      meta: [
        rolePill(user.role),
        user.greenhouse_id
          ? h("span", { class: "pill pill-plain" }, icon("sprout", "icon-sm"), user.greenhouse_id)
          : h("span", { class: "pill pill-plain" }, "Monitor en vivo"),
      ],
    }),
    body,
  );

  return withGreenhouse({
    picker,
    mount: (greenhouseId) => [
      mountDisplay(body, greenhouseId, { picker, kioskToggle: controlsSlot }),
      () => document.removeEventListener("fullscreenchange", onFullscreenChange),
    ],
    onError: (error) => body.replaceChildren(errorNotice(error)),
    onEmpty: () => body.replaceChildren(noGreenhouseNotice()),
  });
}

// ── Vista: Historial (DASH-02 / AI-01) ────────────────────────────────

function renderHistorial() {
  const { user } = state;
  const picker = h("div", { class: "greenhouse-picker" });
  const body = h("div", {}, loadingLines(4));

  view.replaceChildren(
    pageHead({
      eyebrow: "Historial",
      title: "Cómo evolucionó el vivero",
      text: "Las lecturas guardadas en el tiempo y lo que el análisis saca de ellas: qué sube, qué baja y con cuántos datos.",
      meta: [
        rolePill(user.role),
        user.greenhouse_id
          ? h("span", { class: "pill pill-plain" }, icon("sprout", "icon-sm"), user.greenhouse_id)
          : picker,
      ],
    }),
    body,
  );
  // Para un productor el vivero ya está en el encabezado; el selector solo
  // hace falta cuando un admin puede elegir entre varios.
  startHistory({ body, picker: user.greenhouse_id ? h("div", {}) : picker });
}

function startHistory({ body, picker }) {
  return withGreenhouse({
    picker,
    mount: (greenhouseId) => mountHistory(body, greenhouseId),
    onError: (error) => body.replaceChildren(errorNotice(error)),
    onEmpty: () => body.replaceChildren(noGreenhouseNotice()),
  });
}

// ── Vista: Configuración (admins) ─────────────────────────────────────

function renderConfiguracion() {
  const tableBody = h("div", {}, loadingLines(4));
  const segmented = h("div", { class: "segmented", role: "group", "aria-label": "Filtrar por estado" },
    [["active", "Activos"], ["inactive", "Desactivados"]].map(([value, label]) =>
      h("button", {
        type: "button",
        "aria-pressed": String(state.actuatorStatus === value),
        onclick: (e) => {
          state.actuatorStatus = value;
          for (const b of segmented.querySelectorAll("button")) b.setAttribute("aria-pressed", "false");
          e.currentTarget.setAttribute("aria-pressed", "true");
          loadActuatorTable(tableBody);
        },
      }, label)));

  view.replaceChildren(
    pageHead({
      eyebrow: "Configuración",
      title: "Equipos del vivero",
      text: "Actuadores registrados que el sistema puede controlar. Desactivar uno lo quita de las pantallas de control sin borrar su historial.",
      meta: [rolePill(state.user.role)],
    }),
    h("div", { class: "grid" },
      h("section", { class: "card span-12", "aria-label": "Actuadores registrados" },
        cardHead("Actuadores registrados", "Bomba, ventilador, malla sombra e iluminación.", segmented),
        tableBody,
        h("div", { class: "notice notice-info" }, icon("info"),
          h("p", {},
            "El formulario de registro llega en un siguiente incremento. Mientras tanto se registran con ",
            h("code", {}, "POST /api/v1/actuators"),
            " desde ", h("a", { href: "/docs", target: "_blank", rel: "noopener" }, "la documentación de la API"), "."))),

      h("section", { class: "card card-flat span-6" },
        h("div", { class: "soon" },
          h("span", { class: "equip-icon" }, icon("chip")),
          h("div", {},
            h("div", { class: "list-title" }, "Dispositivos y sensores IoT"),
            h("p", { class: "list-sub" }, "Registro de gateways y sensores por vivero y área."))),
        h("span", { class: "tag" }, "CONF-04 · arrastre de Sprint 1")),

      h("section", { class: "card card-flat span-6" },
        h("div", { class: "soon" },
          h("span", { class: "equip-icon" }, icon("gauge")),
          h("div", {},
            h("div", { class: "list-title" }, "Umbrales y parámetros de riego"),
            h("p", { class: "list-sub" }, "Rangos por vivero y área que usa el motor de alertas."))),
        h("span", { class: "tag" }, "CONF-06 · CONF-08")),
    ),
  );
  loadActuatorTable(tableBody);
}

async function loadActuatorTable(container) {
  container.replaceChildren(loadingLines(4));
  try {
    const actuators = await apiFetch("/actuators", { params: { status: state.actuatorStatus } });
    if (!actuators.length) {
      container.replaceChildren(h("div", { class: "empty" },
        h("strong", {}, state.actuatorStatus === "active"
          ? "No hay actuadores activos."
          : "No hay actuadores desactivados."),
        state.actuatorStatus === "active"
          ? "Registra la bomba o el ventilador del vivero para que aparezcan aquí."
          : "Cuando desactives un equipo aparecerá aquí con su historial intacto."));
      return;
    }
    container.replaceChildren(h("div", { class: "table-wrap" },
      h("table", { class: "table" },
        h("thead", {}, h("tr", {},
          ["Equipo", "Vivero · área", "Canal", "Estado", "Registrado"].map((t) => h("th", { scope: "col" }, t)))),
        h("tbody", {},
          actuators.map((a) => h("tr", {},
            h("td", {}, h("div", { class: "cell-equip" },
              equipIcon(a.type),
              h("div", {},
                h("div", { class: "list-title" }, a.name),
                h("div", { class: "list-sub mono" }, a.actuator_id)))),
            h("td", { class: "nowrap" }, `${a.greenhouse_id} · ${a.area}`),
            h("td", {}, h("span", { class: "channel" }, a.control_channel)),
            h("td", {}, statusPill(a.status)),
            h("td", {}, formatDateTime(a.registered_at))))))));
  } catch (error) {
    container.replaceChildren(errorNotice(error));
  }
}

// ── Vista: Bitácora (admins, AUTH-05) ─────────────────────────────────

function renderBitacora() {
  const auditBody = h("div", { class: "audit" });
  view.replaceChildren(
    pageHead({
      eyebrow: "Auditoría",
      title: "Bitácora de auditoría",
      text: "Inicios de sesión, alertas, acciones sobre los equipos y recomendaciones de la IA. Cada registro conserva actor, acción y hora.",
      meta: [rolePill(state.user.role)],
    }),
    h("div", { class: "grid" },
      h("section", { class: "card span-12", "aria-labelledby": "t-bitacora" },
        cardHead("Eventos registrados", "Filtra por rango de fechas, tipo de evento, actor o vivero. Se actualiza solo cada 30 s.", null, "t-bitacora"),
        auditBody),
    ),
  );
  state.cleanup = mountAudit(auditBody);
}

// ── Vista: Plataforma (super admin) ───────────────────────────────────

function renderPlataforma() {
  const roles = ["producer", "admin", "super_admin"];
  const auditBody = h("div", { class: "audit" });
  view.replaceChildren(
    pageHead({
      eyebrow: "Plataforma",
      title: "Roles, permisos y bitácora",
      text: "Qué puede hacer cada rol hoy y el registro central de auditoría. El servidor aplica estas reglas en cada solicitud.",
      meta: [rolePill(state.user.role)],
    }),
    h("div", { class: "grid" },
      h("section", { class: "card span-12", "aria-label": "Permisos por rol" },
        cardHead("Permisos por rol", "Espejo de las reglas del backend (require_roles)."),
        h("div", { class: "table-wrap" },
          h("table", { class: "table matrix" },
            h("thead", {}, h("tr", {},
              h("th", { scope: "col" }, "Acción"),
              roles.map((r) => h("th", { scope: "col" }, ROLE_LABELS[r])))),
            h("tbody", {},
              PERMISSIONS.map((p) => h("tr", {},
                h("td", {}, p.label),
                roles.map((r) => h("td", {},
                  p.roles.includes(r)
                    ? h("span", { class: "yes" }, icon("check"), h("span", { class: "sr-only" }, "Sí"))
                    : h("span", { class: "no" }, icon("minus"), h("span", { class: "sr-only" }, "No")))))))))),

      h("section", { class: "card span-12", "aria-labelledby": "t-plataforma-bitacora" },
        cardHead("Bitácora de auditoría", "Inicios de sesión, alertas, acciones sobre equipos y recomendaciones de la IA.", null, "t-plataforma-bitacora"),
        auditBody),

      soonCard("users", "Usuarios y roles configurables", "Crear roles y elegir sus permisos.", "AUTH-04", "span-12"),
    ),
  );
  state.cleanup = mountAudit(auditBody);
}

function soonCard(iconName, title, text, tag, span = "span-6") {
  return h("section", { class: `card card-flat ${span}` },
    h("div", { class: "soon" },
      h("span", { class: "equip-icon" }, icon(iconName)),
      h("div", {}, h("div", { class: "list-title" }, title), h("p", { class: "list-sub" }, text))),
    h("span", { class: "tag" }, tag));
}

// ── Vistas de error ───────────────────────────────────────────────────

function renderForbidden() {
  view.replaceChildren(
    h("section", { class: "card forbidden", role: "alert" },
      h("span", { class: "equip-icon" }, icon("lock")),
      h("h1", { style: "font-size: var(--step-2)" }, "Esta sección no está disponible para tu rol"),
      h("p", { class: "muted" },
        `Tu rol es ${ROLE_LABELS[state.user.role]}. Si necesitas acceso, pídelo a un Administrador.`),
      h("a", { class: "btn btn-primary", href: `#/${LANDING[state.user.role]}` },
        "Ir a mi inicio", icon("arrowRight"))));
}

function renderFatal(error) {
  view.replaceChildren(
    pageHead({ eyebrow: "Sin conexión", title: "No pudimos cargar tu panel" }),
    errorNotice(error),
    h("button", { class: "btn", type: "button", onclick: () => location.reload() }, "Reintentar"));
}

const VIEWS = {
  inicio: renderInicio,
  pantalla: renderPantalla,
  historial: renderHistorial,
  bitacora: renderBitacora,
  configuracion: renderConfiguracion,
  plataforma: renderPlataforma,
};

boot();
