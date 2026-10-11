/**
 * "Equipos del vivero": estado de cada equipo (ACT-04) y control del riego
 * (ACT-02), refrescado cada 15 s sin recargar la página.
 *
 * - El estado lo calcula el backend con lo que reporta el Arduino; aquí solo
 *   se dibuja. powerView() (metrics.js) lo pasa a "Desconocido" si la
 *   confirmación envejece entre dos refrescos: nunca se muestra "Apagado"
 *   sin confirmación. El estado se comunica con color, ícono Y texto.
 * - Riego: el Arduino decide en automático con sus constantes (objetivo y
 *   tiempo máximo). En la fila de la bomba se elige Automático / Manual y,
 *   en manual, se manda Regar / Detener. Tras un clic el refresco se acelera
 *   un rato (poller.boost) para ver la confirmación del Arduino.
 * - "Últimas acciones": cada encendido/apagado, automático o manual (con
 *   quién lo pidió).
 * - Todo el DOM se arma con h()/textContent.
 */
import { apiFetch } from "./api.js";
import {
  POWER_LABELS,
  REFRESH_SECONDS,
  commandFeedback,
  eventText,
  modeHelp,
  powerView,
} from "./metrics.js";
import { createPoller } from "./poller.js";
import { ACTUATOR_TYPES } from "./roles.js";
import { formatDateTime, formatTime, h, icon } from "./ui.js";

const POWER_ICONS = { on: "power", off: "power", unavailable: "lock", unknown: "help" };
const EVENTS_SHOWN = 6;

function isPermanent(error) {
  return error.status === 403 || error.status === 404;
}

/**
 * Monta la lista de equipos de `greenhouseId` en `root` y la refresca.
 * @returns {() => void} detiene el refresco (al salir de la vista).
 */
export function mountEquipment(root, greenhouseId) {
  const base = `/greenhouses/${encodeURIComponent(greenhouseId)}`;
  let data = null;          // /actuators/status
  let irrigation = null;    // /irrigation
  let events = [];          // /actuators/events
  let receivedAt = 0;
  let receivedClock = null;
  let problem = null;
  let stopped = false;
  let busy = false;         // hay un clic de control en camino
  let actions = 0;          // cuenta los clics: una carga que empezó antes se descarta
  let actionError = null;   // { text, until }
  const rows = new Map();
  let controls = null;      // controles de la bomba (se crean una vez)

  const notice = h("div", { class: "live-notice" });
  const list = h("ul", { class: "list equipment" });
  const history = h("div", { class: "equip-history" });
  root.replaceChildren(notice, list, history);

  // ── Datos ───────────────────────────────────────────────────────────

  async function load({ background }) {
    const startedAt = actions;
    const [status, irrigationResult, eventsResult] = await Promise.allSettled([
      apiFetch(`${base}/actuators/status`, { background }),
      apiFetch(`${base}/irrigation`, { background }),
      apiFetch(`${base}/actuators/events`, { params: { limit: EVENTS_SHOWN }, background }),
    ]);
    if (stopped) return;
    if (status.status === "fulfilled") {
      data = status.value;
      receivedAt = performance.now();
      receivedClock = new Date();
      problem = null;
    } else {
      const error = status.reason;
      if (error.status === 401) return;   // api.js ya regresa al login
      problem = error;
      if (isPermanent(error)) poller.halt();
    }
    // Si hubo un clic mientras esta carga iba en camino, su `irrigation` es
    // de antes del clic: se ignora (la siguiente carga ya trae el nuevo).
    if (irrigationResult.status === "fulfilled" && startedAt === actions) irrigation = irrigationResult.value;
    if (eventsResult.status === "fulfilled") events = eventsResult.value.events;
    paint();
  }

  async function act(request) {
    if (busy) return;
    busy = true;
    actions += 1;
    actionError = null;
    paint();
    try {
      await request();
      poller.boost();
    } catch (error) {
      if (error.status !== 401) actionError = { text: error.message, until: Date.now() + 10000 };
    } finally {
      busy = false;
      poller.now();
    }
  }

  function setMode(mode) {
    if (!irrigation || irrigation.mode === mode) return;
    act(async () => {
      irrigation = await apiFetch(`${base}/irrigation/mode`, { method: "PUT", body: { mode } });
    });
  }

  function command(actuatorId, action) {
    act(() => apiFetch(`/actuators/${encodeURIComponent(actuatorId)}/commands`, {
      method: "POST", body: { action },
    }));
  }

  // ── Dibujo ──────────────────────────────────────────────────────────

  function ensureRow(item) {
    let refs = rows.get(item.actuator_id);
    if (refs) return refs;
    const type = ACTUATOR_TYPES[item.type] || { label: item.type, icon: "chip" };
    refs = {
      badge: h("span", { class: "power", role: "status" }),
      note: h("span", { class: "power-note" }),
      name: h("div", { class: "list-title" }),
      sub: h("div", { class: "list-sub" }),
      slot: h("div", { class: "equip-controls-slot" }),
    };
    refs.el = h("li", { class: "list-item equip-row", dataset: { actuator: item.actuator_id } },
      h("span", { class: `equip-icon type-${item.type}` }, icon(type.icon)),
      h("div", { class: "list-main" }, refs.name, refs.sub),
      h("div", { class: "power-col" }, refs.badge, refs.note),
      refs.slot);
    rows.set(item.actuator_id, refs);
    return refs;
  }

  function ensureControls() {
    if (controls) return controls;
    const modeButton = (mode, label) => h("button", {
      type: "button",
      dataset: { mode },
      onclick: () => setMode(mode),
    }, label);
    controls = {
      auto: modeButton("auto", "Automático"),
      manual: modeButton("manual", "Manual"),
      help: h("p", { class: "equip-help" }),
      water: h("button", { class: "btn btn-primary btn-sm", type: "button" }, icon("drop", "icon-sm"), "Regar"),
      stop: h("button", { class: "btn btn-sm", type: "button" }, icon("power", "icon-sm"), "Detener"),
      feedback: h("p", { class: "equip-feedback", role: "status" }),
    };
    controls.buttons = h("div", { class: "equip-buttons" }, controls.water, controls.stop);
    controls.el = h("div", { class: "equip-controls" },
      h("div", { class: "equip-mode" },
        h("span", { class: "equip-mode-label", id: "riego-modo" }, "Riego"),
        h("div", { class: "segmented", role: "group", "aria-labelledby": "riego-modo" },
          controls.auto, controls.manual),
        controls.buttons),
      controls.help,
      controls.feedback);
    return controls;
  }

  function paintControls(item) {
    const c = ensureControls();
    c.water.onclick = () => command(item.actuator_id, "on");
    c.stop.onclick = () => command(item.actuator_id, "off");
    if (!irrigation) {
      c.el.hidden = true;
      return c.el;
    }
    c.el.hidden = false;
    const controllable = irrigation.controllable;
    for (const button of [c.auto, c.manual]) {
      button.setAttribute("aria-pressed", String(irrigation.mode === button.dataset.mode));
      button.disabled = busy || !controllable;
    }
    const manual = irrigation.mode === "manual";
    c.buttons.hidden = !manual || !controllable;
    c.water.disabled = busy || item.state === "unavailable";
    c.water.title = item.state === "unavailable" ? item.detail : "";
    c.stop.disabled = busy;
    c.help.textContent = controllable ? modeHelp(irrigation) : irrigation.detail;
    c.help.classList.toggle("is-blocked", !controllable);

    // Lo más importante primero: un error del clic, luego la orden de modo y
    // luego la de la bomba.
    let feedback = null;
    if (actionError && Date.now() < actionError.until) feedback = { tone: "warn", text: actionError.text };
    else feedback = commandFeedback(irrigation.last_mode_command) || commandFeedback(item.last_command);
    c.feedback.hidden = !feedback;
    c.feedback.dataset.tone = feedback ? feedback.tone : "";
    c.feedback.replaceChildren(...(feedback
      ? [icon(feedback.tone === "wait" ? "clock" : "alert", "icon-sm"), feedback.text]
      : []));
    return c.el;
  }

  function paintRow(item, elapsed, staleAfter) {
    const refs = ensureRow(item);
    const type = ACTUATOR_TYPES[item.type] || { label: item.type };
    const view = powerView(item, elapsed, staleAfter);

    refs.name.textContent = item.name;
    refs.sub.replaceChildren(
      `${type.label} · área ${item.area} `,
      h("span", { class: "channel", title: "Canal de control" }, item.control_channel));
    refs.badge.dataset.power = view.state;
    refs.badge.replaceChildren(icon(POWER_ICONS[view.state], "icon-sm"), view.label);
    refs.badge.title = view.state === "on" || view.state === "off"
      ? "Es lo que el Arduino ordenó en el pin; no mide si el motor gira."
      : "";
    refs.note.textContent = view.note;
    refs.note.title = item.reported_at ? `Lectura del ${formatDateTime(item.reported_at)}` : "";
    refs.el.setAttribute("aria-label", `${item.name}: ${POWER_LABELS[view.state]}. ${view.note}`);

    const isController = item.type === "pump" && irrigation
      && item.gateway_device_id === irrigation.controller_device_id;
    if (isController) {
      const el = paintControls(item);
      if (refs.slot.firstChild !== el) refs.slot.replaceChildren(el);
    } else if (refs.slot.firstChild) {
      refs.slot.replaceChildren();
    }
    return refs.el;
  }

  function paintHistory() {
    if (!events.length) {
      history.replaceChildren();
      return;
    }
    history.replaceChildren(
      h("p", { class: "equip-history-title" }, "Últimas acciones"),
      h("ul", { class: "event-list" }, events.map((event) => {
        const text = eventText(event);
        return h("li", { class: "event-item", dataset: { origin: event.origin } },
          h("span", { class: "event-time", title: formatDateTime(event.reading_ts) }, formatTime(event.reading_ts)),
          h("span", { class: "event-what" }, text.what),
          h("span", { class: `pill ${text.manual ? "pill-admin" : "pill-plain"}` }, text.origin),
          text.reason ? h("span", { class: "event-reason" }, text.reason) : null);
      })));
  }

  function paint() {
    if (stopped) return;
    if (problem) {
      const body = data && !isPermanent(problem)
        ? `Sin conexión con el servidor. Estado de las ${formatTime(receivedClock)}; se reintenta solo.`
        : problem.message;
      notice.replaceChildren(h("div", { class: "notice notice-warn", role: "status" },
        icon(data ? "cloudOff" : "alert"), h("p", {}, body)));
    } else {
      notice.replaceChildren();
    }
    if (!data) return;

    if (!data.actuators.length) {
      rows.clear();
      list.replaceChildren(h("li", { class: "empty" },
        h("strong", {}, "Aún no hay equipos registrados en este vivero."),
        "Un Administrador puede registrar la bomba, el ventilador y demás equipos."));
      history.replaceChildren();
      return;
    }
    const elapsed = (performance.now() - receivedAt) / 1000;
    const staleAfter = data.stale_after_minutes * 60;
    const nodes = data.actuators.map((item) => paintRow(item, elapsed, staleAfter));
    const ids = new Set(data.actuators.map((item) => item.actuator_id));
    for (const id of rows.keys()) if (!ids.has(id)) rows.delete(id);
    if (list.children.length !== nodes.length || nodes.some((node, i) => list.children[i] !== node)) {
      list.replaceChildren(...nodes);
    }
    paintHistory();
  }

  const poller = createPoller({ seconds: REFRESH_SECONDS, run: load, onTick: paint });
  list.replaceChildren(h("li", { class: "list-item" },
    h("div", { class: "skeleton-line", style: "width:70%" })));
  poller.now();

  return () => {
    stopped = true;
    poller.stop();
  };
}
