/**
 * Tarjeta "Condiciones inusuales" de "Mi vivero" (AI-02).
 *
 * - Muestra las anomalías de las últimas 6 h: mediciones que se salieron de
 *   lo que su sensor venía haciendo, aunque sigan dentro de los límites del
 *   rábano. Las que siguen pasando van primero; de las que ya pasaron, las 5
 *   más recientes.
 * - Va en su propia tarjeta, en azul y con otro ícono: una anomalía NO es una
 *   alerta (criterio 2). La tarjeta "Alertas" es la de los límites que marca
 *   el Arduino; esta es el aviso temprano.
 * - Se refresca sola cada minuto (poller.js): el patrón se mide en buckets de
 *   2 min, no tiene caso pedirla cada 15 s.
 * - Todo el DOM se arma con h()/textContent.
 */
import { apiFetch } from "./api.js";
import { describeAnomaly, formatAge, formatDuration, presentationFor, visibleAnomalies } from "./metrics.js";
import { createPoller } from "./poller.js";
import { rangeStart } from "./series.js";
import { formatDateTime, formatTime, h, icon } from "./ui.js";

const RANGE = "6h";
const REFRESH_SECONDS = 60;

function isPermanent(error) {
  return error.status === 403 || error.status === 404;
}

/**
 * Monta la tarjeta de anomalías de `greenhouseId` dentro de `root`.
 * `countSlot` (opcional) es donde va el resumen del encabezado de la tarjeta.
 * @returns {() => void} detiene el refresco.
 */
export function mountAnomalies(root, greenhouseId, countSlot = null) {
  const path = `/greenhouses/${encodeURIComponent(greenhouseId)}/anomalies`;
  let data = null;
  let receivedAt = 0;
  let receivedClock = null;
  let problem = null;
  let stopped = false;

  const notice = h("div", { class: "live-notice" });
  const ongoingList = h("ul", { class: "alert-list", "aria-label": "Anomalías en curso" });
  const pastWrap = h("div", { class: "alert-cleared" });
  const method = h("p", { class: "anomaly-method" });
  const updated = h("span", { class: "live-updated" });
  root.replaceChildren(
    notice,
    ongoingList,
    pastWrap,
    method,
    h("div", { class: "live-bar" },
      h("span", { class: "live-hint" }, icon("clock", "icon-sm"), "Se actualiza solo cada minuto"),
      updated));

  async function load({ background }) {
    try {
      const fresh = await apiFetch(path, { params: { from: rangeStart(RANGE) }, background });
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

  function anomalyItem(anomaly) {
    const look = presentationFor({ key: anomaly.metric, label: anomaly.label, unit: anomaly.unit });
    const text = describeAnomaly(anomaly, formatTime);
    // El título ya nombra el sensor; aquí solo va su ícono, sin repetir el nombre.
    return h("li", {
      class: "alert-item anomaly-item",
      dataset: { ongoing: String(anomaly.ongoing), kind: anomaly.kind },
    },
      h("span", { class: "alert-icon" }, icon("pulse")),
      h("div", { class: "alert-main" },
        h("div", { class: "alert-title" },
          h("span", {}, text.title),
          h("span", { class: "alert-sensor", "aria-hidden": "true" }, icon(look.icon, "icon-sm"))),
        h("div", { class: "alert-value" }, text.value,
          h("span", { class: "anomaly-expected" }, ` · ${text.expected}`)),
        h("div", { class: "alert-meta", title: `Del ${formatDateTime(anomaly.from)} al ${formatDateTime(anomaly.to)}` },
          text.since)));
  }

  function emptyItem() {
    const evaluated = data.variables.reduce((sum, item) => sum + item.evaluated, 0);
    const irrigation = data.variables.reduce((sum, item) => sum + item.explained_by_irrigation, 0);
    if (!data.has_data) {
      return h("li", { class: "alert-empty anomaly-empty" }, icon("info"),
        h("span", {}, h("strong", {}, "Sin lecturas en las últimas 6 h."),
          " En cuanto lleguen, se comparan con su patrón."));
    }
    if (!evaluated) {
      return h("li", { class: "alert-empty anomaly-empty" }, icon("info"),
        h("span", {}, h("strong", {}, "Todavía no hay suficiente historia."),
          " Hace falta un rato de lecturas para saber qué es lo habitual."));
    }
    return h("li", { class: "alert-empty" }, icon("check"),
      h("span", {}, h("strong", {}, "Nada fuera de lo habitual."),
        " Cada sensor se comporta como venía haciéndolo.",
        irrigation ? ` Los cambios por riego (${irrigation}) no cuentan.` : ""));
  }

  function paint() {
    if (stopped) return;
    if (problem) {
      const body = data && !isPermanent(problem)
        ? `Sin conexión con el servidor. Datos de las ${formatTime(receivedClock)}; se reintenta solo.`
        : problem.message;
      notice.replaceChildren(h("div", { class: "notice notice-warn", role: "status" },
        icon(data ? "cloudOff" : "alert"), h("p", {}, body)));
    } else {
      notice.replaceChildren();
    }
    if (!data) return;

    const { ongoing, past } = visibleAnomalies(data.anomalies);
    ongoingList.replaceChildren(...(ongoing.length ? ongoing.map(anomalyItem) : [emptyItem()]));
    pastWrap.replaceChildren(...(past.length
      ? [h("p", { class: "alert-cleared-title" }, "Ya pasaron (últimas 6 h)"),
         h("ul", { class: "alert-list" }, past.map(anomalyItem))]
      : []));

    const skipped = data.variables.filter((item) => !item.checked).map((item) => presentationFor(item).label);
    method.textContent = `Cada medición se compara con sus ${formatDuration(data.baseline_seconds)} anteriores.`
      + (skipped.length ? ` No se revisa: ${skipped.join(", ")} (cambia sola con el sol).` : "");

    if (countSlot) {
      countSlot.replaceChildren(ongoing.length
        ? h("span", { class: "pill pill-info" }, ongoing.length === 1 ? "1 en curso" : `${ongoing.length} en curso`)
        : data.anomalies.length
          ? h("span", { class: "pill pill-plain" }, `${data.anomalies.length} en 6 h`)
          : h("span", { class: "pill pill-ok" }, "Todo habitual"));
    }
    updated.textContent = `Actualizado ${formatAge((performance.now() - receivedAt) / 1000)}`;
  }

  const poller = createPoller({ seconds: REFRESH_SECONDS, run: load, onTick: paint });
  ongoingList.replaceChildren(h("li", {}, h("div", { class: "skeleton-line", style: "width:60%" })));
  poller.now();

  return () => {
    stopped = true;
    poller.stop();
  };
}
