/**
 * Tarjeta "Alertas" de "Mi vivero" (ALERT-01, task "Build alert notification UI").
 *
 * - Muestra las alertas activas del vivero (las crea el backend cuando el
 *   Arduino reporta que una medición cruzó su límite) y hasta 5 que se
 *   normalizaron en las últimas 24 h.
 * - Se refresca sola cada 15 s (poller.js): una alerta nueva aparece SIN
 *   recargar la página, con un aviso flotante y el número de alertas activas
 *   en el título de la pestaña: "(1) Mi vivero · SmartGreenAI".
 * - En la primera carga no se avisa de nada: solo de las que llegan después.
 * - Todo el DOM se arma con h()/textContent.
 */
import { apiFetch } from "./api.js";
import {
  REFRESH_SECONDS,
  alertTitle,
  describeAlert,
  formatAge,
  newActiveAlerts,
  presentationFor,
  titleWithCount,
  visibleAlerts,
} from "./metrics.js";
import { createPoller } from "./poller.js";
import { formatDateTime, formatTime, h, icon } from "./ui.js";

const TOAST_SECONDS = 10;

function isPermanent(error) {
  return error.status === 403 || error.status === 404;
}

/** Región única de avisos flotantes (aria-live: el lector de pantalla los lee). */
function toastRegion() {
  let region = document.getElementById("toasts");
  if (!region) {
    region = h("div", { id: "toasts", class: "toasts", role: "status", "aria-live": "polite" });
    document.body.append(region);
  }
  return region;
}

function showToast(alert) {
  const describe = describeAlert(alert, 0, formatTime);
  const close = h("button", {
    class: "icon-btn toast-close",
    type: "button",
    "aria-label": "Cerrar aviso",
    onclick: () => toast.remove(),
  }, icon("x", "icon-sm"));
  const toast = h("div", { class: "toast" },
    h("span", { class: "toast-icon" }, icon("bell")),
    h("div", { class: "toast-body" },
      h("strong", {}, `Nueva alerta: ${alertTitle(alert)}`),
      h("span", {}, describe.value)),
    close);
  toastRegion().append(toast);
  setTimeout(() => toast.remove(), TOAST_SECONDS * 1000);
}

/**
 * Monta la tarjeta de alertas de `greenhouseId` dentro de `root`.
 * `countSlot` (opcional) es donde va el contador del encabezado de la tarjeta.
 * @returns {() => void} detiene el refresco y limpia el título.
 */
export function mountAlerts(root, greenhouseId, countSlot = null) {
  const path = `/greenhouses/${encodeURIComponent(greenhouseId)}/alerts`;
  let data = null;
  let receivedAt = 0;
  let receivedClock = null;
  let problem = null;
  let stopped = false;
  let known = null;   // ids de alertas activas ya vistas (null = primera carga)

  const notice = h("div", { class: "live-notice" });
  const activeList = h("ul", { class: "alert-list", "aria-label": "Alertas activas" });
  const clearedWrap = h("div", { class: "alert-cleared" });
  const updated = h("span", { class: "live-updated" });
  root.replaceChildren(
    notice,
    activeList,
    clearedWrap,
    h("div", { class: "live-bar" },
      h("span", { class: "live-hint" }, icon("clock", "icon-sm"), `Se actualiza solo cada ${REFRESH_SECONDS} s`),
      updated));

  async function load({ background }) {
    try {
      const fresh = await apiFetch(path, { params: { status: "all", limit: 30 }, background });
      if (stopped) return;
      if (known !== null) newActiveAlerts(known, fresh.alerts).forEach(showToast);
      known = new Set(fresh.alerts.filter((a) => a.status === "active").map((a) => a.id));
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

  function alertItem(alert, elapsed) {
    const look = presentationFor({ key: alert.metric, label: alert.label, unit: alert.unit });
    const text = describeAlert(alert, elapsed, formatTime);
    return h("li", { class: "alert-item", dataset: { status: alert.status, code: alert.code } },
      h("span", { class: "alert-icon" }, icon(alert.status === "active" ? "alert" : "check")),
      h("div", { class: "alert-main" },
        h("div", { class: "alert-title" },
          h("span", {}, alertTitle(alert)),
          h("span", { class: "alert-sensor" }, icon(look.icon, "icon-sm"), look.label)),
        h("div", { class: "alert-value" }, text.value),
        h("div", { class: "alert-meta", title: `Medición del ${formatDateTime(alert.reading_ts)} · ${alert.device_id}` },
          text.since, text.extra ? ` · ${text.extra}` : "")));
  }

  function paint() {
    if (stopped) return;
    if (problem) {
      const body = data && !isPermanent(problem)
        ? `Sin conexión con el servidor. Alertas de las ${formatTime(receivedClock)}; se reintenta solo.`
        : problem.message;
      notice.replaceChildren(h("div", { class: "notice notice-warn", role: "status" },
        icon(data ? "cloudOff" : "alert"), h("p", {}, body)));
    } else {
      notice.replaceChildren();
    }
    if (!data) return;

    const elapsed = (performance.now() - receivedAt) / 1000;
    const { active, cleared } = visibleAlerts(data.alerts);
    activeList.replaceChildren(...(active.length
      ? active.map((alert) => alertItem(alert, elapsed))
      : [h("li", { class: "alert-empty" }, icon("check"),
          h("span", {}, h("strong", {}, "Sin alertas activas."),
            " Todo dentro de los límites que marca el Arduino."))]));
    clearedWrap.replaceChildren(...(cleared.length
      ? [h("p", { class: "alert-cleared-title" }, "Se normalizaron (últimas 24 h)"),
         h("ul", { class: "alert-list" }, cleared.map((alert) => alertItem(alert, elapsed)))]
      : []));

    const count = data.active_count;
    if (countSlot) {
      countSlot.replaceChildren(count
        ? h("span", { class: "pill pill-crit" }, count === 1 ? "1 activa" : `${count} activas`)
        : h("span", { class: "pill pill-ok" }, "Sin alertas"));
    }
    document.title = titleWithCount(document.title, count);
    updated.textContent = `Actualizado ${formatAge(elapsed)}`;
  }

  const poller = createPoller({ seconds: REFRESH_SECONDS, run: load, onTick: paint });
  activeList.replaceChildren(h("li", {}, h("div", { class: "skeleton-line", style: "width:60%" })));
  poller.now();

  return () => {
    stopped = true;
    poller.stop();
    document.title = titleWithCount(document.title, 0);
  };
}
