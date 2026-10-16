/**
 * Vista "Historial": la gráfica de una medición en el tiempo (DASH-02) y las
 * tendencias que calcula el análisis (AI-01).
 *
 * Qué hace:
 * - Deja elegir medición y rango (6 h, 24 h, 7 días, 30 días) y pide la serie
 *   a /readings/series. El servidor decide si la devuelve lectura por lectura
 *   o promediada por bucket; aquí solo se dice cuál fue (criterio 3).
 * - Un rango sin lecturas muestra el aviso en la gráfica, no una gráfica en
 *   blanco (criterio 2).
 * - Pide /analysis del mismo periodo y muestra, por variable, si va subiendo,
 *   estable o bajando, con el periodo usado y la calidad del dato — huecos
 *   incluidos (AI-01). No redacta recomendaciones: eso es AI-05.
 *
 * El refresco es el mismo patrón del resto del panel (poller.js): solo con la
 * pestaña visible, y un refresco automático no renueva la sesión (AUTH-01).
 * Aquí es más lento que los 15 s de "Mi vivero" y depende del rango
 * (refreshSeconds): 60 s en 6 h y 24 h, 5 min en 7 días y 15 min en 30 días.
 * Un histórico largo cambia poco y cuesta mucho agruparlo en la Pi.
 */
import { apiFetch } from "./api.js";
import { createChart } from "./chart.js";
import { createPoller } from "./poller.js";
import {
  DEFAULT_RANGE,
  RANGES,
  describePeriod,
  describeTrend,
  rangeStart,
  refreshSeconds,
  resolutionNote,
} from "./series.js";
import { METRICS } from "./metrics.js";
import { h, icon } from "./ui.js";

const TREND_ICONS = { up: "trendUp", down: "trendDown", flat: "minus", none: "help" };

/** Errores que no se arreglan reintentando. */
function isPermanent(error) {
  return error.status === 403 || error.status === 404;
}

/**
 * Monta la vista en `root` para un vivero.
 * @returns {() => void} función para detener el refresco al salir.
 */
export function mountHistory(root, greenhouseId) {
  const base = `/greenhouses/${encodeURIComponent(greenhouseId)}`;
  const state = {
    metric: "temp_c",
    range: DEFAULT_RANGE,
    series: null,
    analysis: null,
    problem: null,
  };

  const chart = createChart();
  const resolution = h("p", { class: "chart-note" });
  const notice = h("div", { class: "live-notice" });
  const trends = h("div", { class: "trends" });
  const period = h("span", { class: "trends-period" });

  const metricSelect = h("select", {
    class: "input input-sm",
    id: "history-metric",
    onchange: (event) => {
      state.metric = event.currentTarget.value;
      poller.now();
    },
  }, Object.entries(METRICS).map(([key, look]) =>
    h("option", { value: key, selected: key === state.metric }, look.label)));

  const rangeButtons = h("div", {
    class: "segmented",
    role: "group",
    "aria-label": "Periodo de la gráfica",
  }, RANGES.map((range) => h("button", {
    type: "button",
    "aria-pressed": String(range.id === state.range),
    onclick: (event) => {
      state.range = range.id;
      for (const button of rangeButtons.querySelectorAll("button")) {
        button.setAttribute("aria-pressed", "false");
      }
      event.currentTarget.setAttribute("aria-pressed", "true");
      poller.setSeconds(refreshSeconds(state.range));
      poller.now();
    },
  }, range.label)));

  const refreshButton = h("button", {
    class: "btn btn-quiet btn-sm",
    type: "button",
    title: "Pedir los datos ahora",
    onclick: () => poller.now(),
  }, icon("refresh", "icon-sm"), "Actualizar");

  root.replaceChildren(
    h("div", { class: "grid" },
      h("section", { class: "card span-12", "aria-labelledby": "t-grafica" },
        h("div", { class: "card-head" },
          h("div", {},
            h("h2", { id: "t-grafica" }, "Cómo evolucionó"),
            h("p", {}, "Una medición en el tiempo. Si el periodo trae demasiadas lecturas se grafican promedios, con su mínimo y su máximo.")),
          h("div", { class: "chart-controls" },
            h("label", { class: "sr-only", for: "history-metric" }, "Medición"),
            metricSelect,
            rangeButtons,
            refreshButton)),
        notice,
        chart.el,
        resolution),

      h("section", { class: "card span-12", "aria-labelledby": "t-tendencias" },
        h("div", { class: "card-head" },
          h("div", {},
            h("h2", { id: "t-tendencias" }, "Tendencias del periodo"),
            h("p", {}, "Lo que el análisis saca de las lecturas guardadas: por cada medición, si va subiendo, estable o bajando, y con cuántos datos lo dice.")),
          period),
        trends),
    ),
  );

  // ── Datos ───────────────────────────────────────────────────────────

  async function load({ background }) {
    const params = { from: rangeStart(state.range) };
    try {
      const [series, analysis] = await Promise.all([
        apiFetch(`${base}/readings/series`, { params: { ...params, metric: state.metric }, background }),
        apiFetch(`${base}/analysis`, { params, background }),
      ]);
      state.series = series;
      state.analysis = analysis;
      state.problem = null;
    } catch (error) {
      if (error.status === 401) return;        // api.js ya regresa al login
      state.problem = error;
      if (isPermanent(error)) poller.halt();
    }
    paint();
  }

  // ── Dibujo ──────────────────────────────────────────────────────────

  function paintNotice() {
    if (!state.problem) {
      notice.replaceChildren();
      return;
    }
    notice.replaceChildren(h("div", { class: "notice notice-warn", role: "status" },
      icon(state.series ? "cloudOff" : "alert"),
      h("p", {}, state.series && !isPermanent(state.problem)
        ? "Sin conexión con el servidor. Se muestra lo último que llegó; se reintenta solo."
        : state.problem.message)));
  }

  function paintTrends() {
    const analysis = state.analysis;
    if (!analysis) return;

    // Sin datos no se escribe el periodo: replaceChildren(null) pondría la
    // palabra "null" en la página (no filtra nulls como h()).
    period.replaceChildren(...(analysis.has_data
      ? [h("span", { class: "pill pill-plain" }, icon("clock", "icon-sm"), describePeriod(analysis))]
      : []));

    if (!analysis.has_data) {
      trends.replaceChildren(h("div", { class: "empty" },
        h("strong", {}, "Todavía no hay lecturas en este periodo."),
        "El análisis necesita mediciones guardadas: en cuanto la Raspberry Pi envíe lecturas, aquí aparecen las tendencias."));
      return;
    }

    trends.replaceChildren(...analysis.variables.map((variable) => {
      const view = describeTrend(variable);
      const look = METRICS[variable.key];
      return h("article", { class: "trend", dataset: { tone: view.tone } },
        h("div", { class: "trend-top" },
          h("span", { class: "trend-label" }, icon(look?.icon || "gauge", "icon-sm"), view.label),
          h("span", { class: "trend-pill" }, icon(TREND_ICONS[view.tone], "icon-sm"), view.trend)),
        h("div", { class: "trend-reading" }, view.value),
        view.change ? h("p", { class: "trend-change" }, view.change) : null,
        view.quality ? h("p", { class: "trend-quality" }, view.quality) : null);
    }));
  }

  function paint() {
    paintNotice();
    if (state.series) {
      chart.paint(state.series);
      const note = resolutionNote(state.series);
      resolution.textContent = note;
      resolution.hidden = !note;
    } else if (state.problem) {
      chart.clear(state.problem.message, "error");
      resolution.hidden = true;
    }
    paintTrends();
  }

  const poller = createPoller({ seconds: refreshSeconds(state.range), run: load });
  poller.now();    // la primera carga sí cuenta como actividad de la persona
  return () => poller.stop();
}
