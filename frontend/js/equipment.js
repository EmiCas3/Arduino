/**
 * "Equipos del vivero" (ACT-04): cada actuador activo con su estado actual
 * (Encendido, Apagado, No disponible o Desconocido), refrescado cada 15 s
 * sin recargar la página.
 *
 * - El estado lo calcula el backend con lo que reporta el Arduino; aquí solo
 *   se dibuja. powerView() (metrics.js) lo pasa a "Desconocido" si la
 *   confirmación envejece entre dos refrescos: nunca se muestra "Apagado"
 *   sin confirmación.
 * - El estado se comunica con color, ícono Y texto.
 * - Todo el DOM se arma con h()/textContent.
 */
import { apiFetch } from "./api.js";
import { POWER_LABELS, REFRESH_SECONDS, powerView } from "./metrics.js";
import { createPoller } from "./poller.js";
import { ACTUATOR_TYPES } from "./roles.js";
import { formatDateTime, formatTime, h, icon } from "./ui.js";

const POWER_ICONS = { on: "power", off: "power", unavailable: "lock", unknown: "help" };

function isPermanent(error) {
  return error.status === 403 || error.status === 404;
}

/**
 * Monta la lista de equipos de `greenhouseId` en `root` y la refresca.
 * @returns {() => void} detiene el refresco (al salir de la vista).
 */
export function mountEquipment(root, greenhouseId) {
  const base = `/greenhouses/${encodeURIComponent(greenhouseId)}`;
  let data = null;
  let receivedAt = 0;
  let receivedClock = null;
  let problem = null;
  let stopped = false;
  const rows = new Map();

  const notice = h("div", { class: "live-notice" });
  const list = h("ul", { class: "list equipment" });
  root.replaceChildren(notice, list);

  async function load({ background }) {
    try {
      const fresh = await apiFetch(`${base}/actuators/status`, { background });
      if (stopped) return;
      data = fresh;
      receivedAt = performance.now();
      receivedClock = new Date();
      problem = null;
    } catch (error) {
      if (stopped || error.status === 401) return;   // 401: api.js ya regresa al login
      problem = error;
      if (isPermanent(error)) poller.halt();
    }
    paint();
  }

  function ensureRow(item) {
    let refs = rows.get(item.actuator_id);
    if (refs) return refs;
    const type = ACTUATOR_TYPES[item.type] || { label: item.type, icon: "chip" };
    refs = {
      badge: h("span", { class: "power", role: "status" }),
      note: h("span", { class: "power-note" }),
      name: h("div", { class: "list-title" }),
      sub: h("div", { class: "list-sub" }),
    };
    refs.el = h("li", { class: "list-item equip-row", dataset: { actuator: item.actuator_id } },
      h("span", { class: `equip-icon type-${item.type}` }, icon(type.icon)),
      h("div", { class: "list-main" }, refs.name, refs.sub),
      h("div", { class: "power-col" }, refs.badge, refs.note));
    rows.set(item.actuator_id, refs);
    return refs;
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
    return refs.el;
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
