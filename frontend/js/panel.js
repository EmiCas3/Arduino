/**
 * Panel principal: shell con menú por rol (AUTH-02) y pantallas de inicio.
 *
 * Secciones (roles.js):
 *   #/inicio         todos           estado del vivero y equipos activos
 *   #/configuracion  admin, super    actuadores registrados (CONF-05)
 *   #/plataforma     super           permisos por rol y módulos próximos
 *
 * El menú solo muestra lo que el rol puede usar, pero el control real es del
 * servidor: si una llamada responde 403 se muestra el aviso y nada más.
 */
import { apiFetch, ApiError } from "./api.js";
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

const state = { user: null, actuatorStatus: "active" };

// ── Arranque ──────────────────────────────────────────────────────────

async function boot() {
  initThemeToggle(document.getElementById("theme-toggle"));
  menuBtn.replaceChildren(icon("menu"));
  menuBtn.addEventListener("click", () => setNavOpen(!app.classList.contains("nav-open")));
  backdrop.addEventListener("click", () => setNavOpen(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setNavOpen(false); });
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
}

function currentSectionId() {
  const id = location.hash.replace(/^#\/?/, "");
  return id || LANDING[state.user.role];
}

function route() {
  setNavOpen(false);
  const id = currentSectionId();
  const section = SECTIONS.find((s) => s.id === id);

  for (const link of nav.querySelectorAll(".nav-link")) {
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

// Rangos del firmware vivero_sensoresV2.ino; los valores en vivo llegan con
// los widgets de DASH-01 (la API ya existe: /greenhouses/{id}/readings/latest).
const METRIC_SLOTS = [
  { label: "Temperatura", unit: "°C", ideal: "ideal 20–25" },
  { label: "Humedad del aire", unit: "% HR", ideal: "ideal 60–80" },
  { label: "Humedad de la tierra", unit: "%", ideal: "ideal 60–65" },
  { label: "Nivel del tanque", unit: "%", ideal: "aviso ≤ 50 · crítico ≤ 25" },
  { label: "Luz", unit: "0–1023", ideal: "sol pleno ≥ 600" },
];

function renderInicio() {
  const { user } = state;
  const greenhouse = user.greenhouse_id;

  const equipmentBody = h("div", {}, loadingLines());
  const metrics = h("div", { class: "metrics" },
    METRIC_SLOTS.map((m) => h("div", { class: "metric" },
      h("span", { class: "metric-label" }, m.label),
      h("div", { class: "metric-reading" },
        h("span", { class: "metric-value", "aria-label": "Sin dato todavía" }, "—"),
        h("span", { class: "metric-unit" }, m.unit)),
      h("span", { class: "metric-ideal" }, m.ideal))));

  view.replaceChildren(
    pageHead({
      eyebrow: today(),
      title: `Hola, ${firstName(user)}`,
      text: greenhouse
        ? "Este es el resumen de tu vivero. Desde aquí verás sus lecturas, alertas y equipos."
        : "Desde aquí verás el estado de los viveros, sus lecturas y sus equipos.",
      meta: [
        rolePill(user.role),
        greenhouse
          ? h("span", { class: "pill pill-plain" }, icon("sprout", "icon-sm"), greenhouse)
          : h("span", { class: "pill pill-plain" }, "Todos los viveros"),
      ],
    }),
    h("div", { class: "grid" },
      h("section", { class: "card span-12", "aria-labelledby": "t-estado" },
        h("div", { class: "card-head" },
          h("div", {},
            h("h2", { id: "t-estado" }, "Estado actual"),
            h("p", {}, "Las lecturas en vivo aparecerán aquí con los widgets del dashboard.")),
          h("span", { class: "tag" }, "DASH-01 · widgets")),
        metrics),

      h("section", { class: "card span-7", "aria-labelledby": "t-equipos" },
        cardHead("Equipos del vivero", "Actuadores registrados y activos. Son los que podrás controlar.", null, "t-equipos"),
        equipmentBody),

      h("section", { class: "card span-5", "aria-labelledby": "t-acceso" },
        cardHead("Tu acceso", `Lo que puede hacer un ${ROLE_LABELS[user.role]}.`, null, "t-acceso"),
        h("div", {},
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
  loadEquipment(equipmentBody);
}

async function loadEquipment(container) {
  try {
    const actuators = await apiFetch("/actuators");
    if (!actuators.length) {
      container.replaceChildren(h("div", { class: "empty" },
        h("strong", {}, "Aún no hay equipos registrados."),
        "Un Administrador puede registrar la bomba, el ventilador y demás equipos del vivero."));
      return;
    }
    container.replaceChildren(h("ul", { class: "list" },
      actuators.map((a) => h("li", { class: "list-item" },
        equipIcon(a.type),
        h("div", { class: "list-main" },
          h("div", { class: "list-title" }, a.name),
          h("div", { class: "list-sub" },
            `${ACTUATOR_TYPES[a.type]?.label || a.type} · ${a.greenhouse_id} · área ${a.area}`)),
        h("span", { class: "channel", title: "Canal de control" }, a.control_channel)))));
  } catch (error) {
    container.replaceChildren(errorNotice(error));
  }
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

// ── Vista: Plataforma (super admin) ───────────────────────────────────

function renderPlataforma() {
  const roles = ["producer", "admin", "super_admin"];
  view.replaceChildren(
    pageHead({
      eyebrow: "Plataforma",
      title: "Roles y permisos",
      text: "Qué puede hacer cada rol hoy. El servidor aplica estas reglas en cada solicitud; ocultar opciones en el menú no basta.",
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

      soonCard("users", "Usuarios y roles configurables", "Crear roles y elegir sus permisos.", "AUTH-04"),
      soonCard("list", "Bitácora de auditoría", "Inicios de sesión, alertas y acciones sobre equipos.", "AUTH-05"),
    ),
  );
}

function soonCard(iconName, title, text, tag) {
  return h("section", { class: "card card-flat span-6" },
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
  configuracion: renderConfiguracion,
  plataforma: renderPlataforma,
};

boot();
