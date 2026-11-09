/**
 * Tarjeta "Recomendación de riego" de "Mi vivero" (AI-04 y AI-05).
 *
 * - Muestra qué se recomienda, por qué en lenguaje claro y, al abrir "Datos
 *   que se usaron", las entradas exactas (criterios 1 y 2 de AI-05).
 * - Una recomendación sin explicación no se dibuja (criterio 4): si la API
 *   responde 503 o llega sin texto, se quita también la que estaba en pantalla.
 * - "Recomendaciones anteriores" son las guardadas, con sus propias entradas.
 * - Se refresca sola cada minuto (poller.js): las mediciones salen de una
 *   mediana de 5 min, no tiene caso pedirla cada 15 s.
 * - Todo el DOM se arma con h()/textContent.
 */
import { apiFetch } from "./api.js";
import { describeRecommendation, formatAge } from "./metrics.js";
import { createPoller } from "./poller.js";
import { formatDateTime, formatTime, h, icon } from "./ui.js";

const REFRESH_SECONDS = 60;
const PREVIOUS = 5;

function isPermanent(error) {
  return error.status === 403 || error.status === 404;
}

function inputsTable(view) {
  return h("dl", { class: "rec-inputs" },
    view.inputs.map((row) => h("div", { class: "rec-input" },
      h("dt", {}, row.label),
      h("dd", {}, h("strong", {}, row.value), row.detail ? h("span", {}, ` · ${row.detail}`) : null))));
}

function whyList(view) {
  return [
    h("ul", { class: "rec-reasons" }, view.reasons.map((reason) => h("li", {}, reason))),
    view.notes.length
      ? h("ul", { class: "rec-notes" }, view.notes.map((note) =>
          h("li", {}, icon("info", "icon-sm"), h("span", {}, note))))
      : null,
  ];
}

/**
 * Monta la tarjeta de `greenhouseId` dentro de `root`.
 * `pillSlot` (opcional) es donde va el resumen del encabezado de la tarjeta.
 * @returns {() => void} detiene el refresco.
 */
export function mountRecommendation(root, greenhouseId, pillSlot = null) {
  const base = `/greenhouses/${encodeURIComponent(greenhouseId)}/irrigation`;
  let view = null;
  let stored = [];
  let receivedAt = 0;
  let receivedClock = null;
  let problem = null;
  let stopped = false;
  // Abrir un detalle no debe cerrarse solo con el refresco de cada minuto.
  let inputsOpen = false;
  let previousOpen = false;

  const notice = h("div", { class: "live-notice" });
  const body = h("div", { class: "rec-body" });
  const updated = h("span", { class: "live-updated" });
  root.replaceChildren(
    notice,
    body,
    h("div", { class: "live-bar" },
      h("span", { class: "live-hint" }, icon("clock", "icon-sm"), "Se actualiza solo cada minuto"),
      updated));

  async function load({ background }) {
    try {
      const fresh = await apiFetch(`${base}/recommendation`, { background });
      // Va después: la consulta de arriba es la que guarda la actual.
      const history = await apiFetch(`${base}/recommendations`, {
        params: { limit: PREVIOUS + 1 }, background,
      });
      if (stopped) return;
      view = describeRecommendation(fresh, formatTime);
      stored = history.items.slice(1).map((item) => ({
        at: item.generated_at, view: describeRecommendation(item, formatTime),
      })).filter((item) => item.view);
      receivedAt = performance.now();
      receivedClock = new Date();
      problem = null;
    } catch (error) {
      if (stopped || error.status === 401) return;   // 401: api.js ya regresa al login
      problem = error;
      if (error.status === 503) view = null;         // sin explicación no se muestra
      if (isPermanent(error)) poller.halt();
    }
    paint();
  }

  function previousSection() {
    if (!stored.length) return null;
    return h("details", {
      class: "rec-details",
      open: previousOpen,
      ontoggle: (event) => { previousOpen = event.currentTarget.open; },
    },
      h("summary", {}, `Recomendaciones anteriores (${stored.length})`),
      h("ul", { class: "rec-previous" }, stored.map((item) => h("li", {},
        h("div", { class: "rec-previous-head" },
          h("strong", {}, item.view.summary),
          h("time", { class: "alert-meta", datetime: item.at }, formatDateTime(item.at))),
        ...whyList(item.view),
        inputsTable(item.view)))));
  }

  function paint() {
    if (stopped) return;
    if (problem) {
      const message = view && !isPermanent(problem)
        ? `Sin conexión con el servidor. Datos de las ${formatTime(receivedClock)}; se reintenta solo.`
        : problem.message;
      notice.replaceChildren(h("div", { class: "notice notice-warn", role: "status" },
        icon(view ? "cloudOff" : "alert"), h("p", {}, message)));
    } else {
      notice.replaceChildren();
    }
    if (!view) {
      body.replaceChildren(problem ? "" : h("div", { class: "empty" },
        "Todavía no hay una recomendación con su explicación."));
      if (pillSlot) pillSlot.replaceChildren();
      return;
    }

    const previous = previousSection();
    body.replaceChildren(
      h("div", { class: "rec-main", dataset: { tone: view.tone } },
        h("span", { class: "alert-icon" }, icon(view.tone === "unknown" ? "help" : "drop")),
        h("div", { class: "alert-main" },
          h("p", { class: "rec-summary" }, view.summary),
          ...whyList(view))),
      h("details", {
        class: "rec-details",
        open: inputsOpen,
        ontoggle: (event) => { inputsOpen = event.currentTarget.open; },
      },
        h("summary", {}, "Datos que se usaron"),
        inputsTable(view)),
      ...(previous ? [previous] : []),
      h("p", { class: "anomaly-method" }, "Es una recomendación: la decisión de regar sigue siendo tuya o del modo automático."));

    if (pillSlot) {
      pillSlot.replaceChildren(h("span", { class: `pill ${view.pill.class}` }, view.pill.text));
    }
    tick();
  }

  // Solo cambia la edad: redibujar en cada tic cerraría el foco de los detalles.
  function tick() {
    if (!stopped && view) updated.textContent = `Actualizado ${formatAge((performance.now() - receivedAt) / 1000)}`;
  }

  const poller = createPoller({ seconds: REFRESH_SECONDS, run: load, onTick: tick });
  body.replaceChildren(h("div", { class: "skeleton-line", style: "width:60%" }));
  poller.now();

  return () => {
    stopped = true;
    poller.stop();
  };
}
