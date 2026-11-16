/**
 * Bitácora de auditoría (AUTH-05): quién, qué y cuándo en toda la plataforma.
 *
 * Consulta GET /api/v1/audit/events (solo admin y super_admin) con filtros por
 * rango/fecha, tipo de evento, actor y vivero, más paginación. Se refresca
 * sola cada 30 s sin recargar la página y sin renovar el timeout de inactividad.
 * Todo el DOM se construye con h()/textContent.
 */
import { apiFetch } from "./api.js";
import {
  AUDIT_ACTORS,
  AUDIT_EVENT_TYPES,
  describeAuditEvent,
  formatAge,
} from "./metrics.js";
import { createPoller } from "./poller.js";
import { rangeStart } from "./series.js";
import { formatDateTime, formatTime, h, icon } from "./ui.js";

const REFRESH_SECONDS = 30;
const PAGE_SIZE = 20;
const AUDIT_RANGES = [
  { id: "24h", label: "24 h" },
  { id: "7d", label: "7 días" },
  { id: "30d", label: "30 días" },
];

function isPermanent(error) {
  return error.status === 403 || error.status === 404;
}

function dateBoundary(dateStr, endOfDay) {
  if (!dateStr) return null;
  const suffix = endOfDay ? "T23:59:59.999" : "T00:00:00.000";
  const parsed = new Date(`${dateStr}${suffix}`);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

/**
 * Monta la bitácora de auditoría dentro de `root`.
 * @returns {() => void} detiene el refresco al salir de la vista.
 */
export function mountAudit(root, { greenhouseId = null } = {}) {
  const state = {
    range: "24h",
    fromDate: "",
    toDate: "",
    eventType: "",
    actorType: "",
    greenhouse: greenhouseId || "",
    offset: 0,
    data: null,
    receivedAt: 0,
    receivedClock: null,
    problem: null,
    loading: false,
    stopped: false,
  };

  const notice = h("div", { class: "live-notice" });
  const content = h("div", { class: "audit-content" });
  const pagination = h("div", { class: "audit-pagination" });
  const updated = h("span", { class: "live-updated" });

  const rangeButtons = h("div", {
    class: "segmented",
    role: "group",
    "aria-label": "Periodo de la bitácora",
  }, AUDIT_RANGES.map((item) => h("button", {
    type: "button",
    dataset: { range: item.id },
    "aria-pressed": String(item.id === state.range),
    onclick: () => {
      state.range = item.id;
      state.fromDate = "";
      state.toDate = "";
      fromInput.value = "";
      toInput.value = "";
      state.offset = 0;
      syncRangeButtons();
      poller.now();
    },
  }, item.label)));

  function syncRangeButtons() {
    const usingCustomDates = Boolean(state.fromDate || state.toDate);
    for (const button of rangeButtons.querySelectorAll("button")) {
      const pressed = !usingCustomDates && button.dataset.range === state.range;
      button.setAttribute("aria-pressed", String(pressed));
    }
  }

  const fromInput = h("input", {
    class: "input input-sm",
    type: "date",
    id: "audit-from",
    "aria-label": "Desde fecha",
    onchange: (event) => {
      state.fromDate = event.currentTarget.value;
      state.offset = 0;
      syncRangeButtons();
      poller.now();
    },
  });

  const toInput = h("input", {
    class: "input input-sm",
    type: "date",
    id: "audit-to",
    "aria-label": "Hasta fecha",
    onchange: (event) => {
      state.toDate = event.currentTarget.value;
      state.offset = 0;
      syncRangeButtons();
      poller.now();
    },
  });

  const typeSelect = h("select", {
    class: "input input-sm",
    id: "audit-type",
    "aria-label": "Tipo de evento",
    onchange: (event) => {
      state.eventType = event.currentTarget.value;
      state.offset = 0;
      poller.now();
    },
  }, [
    h("option", { value: "" }, "Todos los tipos"),
    ...Object.entries(AUDIT_EVENT_TYPES).map(([value, meta]) =>
      h("option", { value }, meta.label)),
  ]);

  const actorSelect = h("select", {
    class: "input input-sm",
    id: "audit-actor",
    "aria-label": "Tipo de actor",
    onchange: (event) => {
      state.actorType = event.currentTarget.value;
      state.offset = 0;
      poller.now();
    },
  }, [
    h("option", { value: "" }, "Todos los actores"),
    ...Object.entries(AUDIT_ACTORS).map(([value, label]) =>
      h("option", { value }, label)),
  ]);

  const greenhouseSelect = h("select", {
    class: "input input-sm",
    id: "audit-greenhouse",
    "aria-label": "Vivero",
    hidden: true,
    onchange: (event) => {
      state.greenhouse = event.currentTarget.value;
      state.offset = 0;
      poller.now();
    },
  }, [h("option", { value: "" }, "Todos los viveros")]);

  const refreshButton = h("button", {
    class: "btn btn-quiet btn-sm",
    type: "button",
    title: "Actualizar bitácora ahora",
    onclick: () => poller.now(),
  }, icon("refresh", "icon-sm"), "Actualizar");

  const prevButton = h("button", {
    class: "btn btn-quiet btn-sm",
    type: "button",
    disabled: true,
    onclick: () => {
      if (state.offset >= PAGE_SIZE) {
        state.offset = Math.max(0, state.offset - PAGE_SIZE);
        poller.now();
      }
    },
  }, "Anterior");

  const nextButton = h("button", {
    class: "btn btn-quiet btn-sm",
    type: "button",
    disabled: true,
    onclick: () => {
      if (state.data && state.offset + PAGE_SIZE < state.data.total) {
        state.offset += PAGE_SIZE;
        poller.now();
      }
    },
  }, "Siguiente", icon("arrowRight", "icon-sm"));

  const pageInfo = h("span", { class: "audit-page-info" });

  root.replaceChildren(
    h("div", { class: "audit-controls" },
      rangeButtons,
      h("div", { class: "audit-filters" },
        h("label", { class: "audit-field", for: "audit-type" },
          h("span", { class: "audit-field-label" }, "Tipo"),
          typeSelect),
        h("label", { class: "audit-field", for: "audit-actor" },
          h("span", { class: "audit-field-label" }, "Actor"),
          actorSelect),
        h("label", { class: "audit-field", for: "audit-from" },
          h("span", { class: "audit-field-label" }, "Desde"),
          fromInput),
        h("label", { class: "audit-field", for: "audit-to" },
          h("span", { class: "audit-field-label" }, "Hasta"),
          toInput),
        greenhouseSelect,
        refreshButton)),
    notice,
    content,
    pagination,
    h("div", { class: "live-bar" },
      h("span", { class: "live-hint" }, icon("lock", "icon-sm"),
        "Bitácora inmutable: desde la aplicación no se puede modificar ni borrar ningún registro."),
      updated),
  );

  async function loadGreenhouses() {
    if (greenhouseId) return;
    try {
      const list = await apiFetch("/greenhouses", { background: true });
      if (state.stopped || !Array.isArray(list) || !list.length) return;
      greenhouseSelect.replaceChildren(
        h("option", { value: "" }, "Todos los viveros"),
        ...list.map((g) => h("option", { value: g.greenhouse_id }, g.greenhouse_id)),
      );
      greenhouseSelect.value = state.greenhouse;
      greenhouseSelect.hidden = false;
    } catch { /* sin catálogo de viveros sigue funcionando con todos */ }
  }

  function queryParams() {
    const fromIso = state.fromDate
      ? dateBoundary(state.fromDate, false)
      : state.toDate
        ? undefined
        : rangeStart(state.range);
    const toIso = state.toDate ? dateBoundary(state.toDate, true) : undefined;
    return {
      from: fromIso,
      to: toIso,
      event_type: state.eventType || undefined,
      actor_type: state.actorType || undefined,
      greenhouse_id: state.greenhouse || undefined,
      limit: PAGE_SIZE,
      offset: state.offset,
    };
  }

  async function load({ background }) {
    if (state.stopped) return;
    state.loading = true;
    refreshButton.disabled = true;
    prevButton.disabled = true;
    nextButton.disabled = true;
    try {
      const fresh = await apiFetch("/audit/events", { params: queryParams(), background });
      if (state.stopped) return;
      state.data = fresh;
      state.receivedAt = performance.now();
      state.receivedClock = new Date();
      state.problem = null;
    } catch (error) {
      if (state.stopped || error.status === 401) return;
      state.problem = error;
      if (isPermanent(error)) poller.halt();
    } finally {
      state.loading = false;
      refreshButton.disabled = false;
    }
    paint();
  }

  function paintNotice() {
    if (!state.problem) {
      notice.replaceChildren();
      return;
    }
    const body = state.data && !isPermanent(state.problem) && state.problem.status === 0
      ? `Sin conexión con el servidor. Bitácora de las ${formatTime(state.receivedClock)}; se reintenta solo.`
      : state.problem.message;
    notice.replaceChildren(h("div", { class: "notice notice-warn", role: "status" },
      icon(state.data && state.problem.status === 0 ? "cloudOff" : "alert"),
      h("p", {}, body)));
  }

  function paintRows() {
    if (!state.data) {
      if (!state.problem) {
        content.replaceChildren(h("div", { style: "display:grid;gap:10px" },
          Array.from({ length: 4 }, (_, i) =>
            h("div", { class: "skeleton-line", style: `width:${92 - i * 14}%` }))));
      } else {
        content.replaceChildren();
      }
      pagination.replaceChildren();
      return;
    }

    const { items, total, offset } = state.data;
    if (!items.length) {
      content.replaceChildren(h("div", { class: "empty" },
        h("strong", {}, "No hay eventos en este periodo con esos filtros."),
        "Prueba ampliar las fechas o quitar el filtro por tipo o actor."));
      pagination.replaceChildren();
      return;
    }

    content.replaceChildren(h("div", { class: "table-wrap" },
      h("table", { class: "table table-audit" },
        h("thead", {}, h("tr", {},
          ["Cuándo", "Evento", "Actor", "Detalle"].map((label) =>
            h("th", { scope: "col" }, label)))),
        h("tbody", {}, items.map((event) => {
          const view = describeAuditEvent(event);
          return h("tr", {},
            h("td", { class: "nowrap", dataset: { label: "Cuándo" } },
              h("span", { title: `Registrado ${formatDateTime(event.recorded_at)}` },
                formatDateTime(event.occurred_at))),
            h("td", { dataset: { label: "Evento" } },
              h("div", { class: "audit-event-cell" },
                h("span", { class: `pill ${view.pillClass}` },
                  icon(view.icon, "icon-sm"), view.actionLabel),
                h("span", { class: "audit-type-sub" }, view.typeLabel))),
            h("td", { dataset: { label: "Actor" } },
              h("div", { class: "audit-actor" },
                h("span", { class: "list-title" }, view.actorName),
                h("span", { class: "list-sub" }, view.actorKind))),
            h("td", { dataset: { label: "Detalle" } },
              h("div", { class: "audit-summary" },
                h("span", {}, view.summary),
                view.greenhouse ? h("span", { class: "channel" }, view.greenhouse) : null)));
        })))));

    const start = offset + 1;
    const end = Math.min(offset + items.length, total);
    pageInfo.textContent = `Mostrando ${start}–${end} de ${total}`;
    prevButton.disabled = state.loading || offset <= 0;
    nextButton.disabled = state.loading || offset + PAGE_SIZE >= total;
    pagination.replaceChildren(
      pageInfo,
      h("div", { class: "audit-page-buttons" }, prevButton, nextButton),
    );
  }

  function tick() {
    if (!state.stopped && state.data) {
      updated.textContent = `Actualizado ${formatAge((performance.now() - state.receivedAt) / 1000)}`;
    }
  }

  function paint() {
    if (state.stopped) return;
    paintNotice();
    paintRows();
    tick();
  }

  const poller = createPoller({ seconds: REFRESH_SECONDS, run: load, onTick: tick });
  paint();
  loadGreenhouses();
  poller.now();

  return () => {
    state.stopped = true;
    poller.stop();
  };
}
