/**
 * Componente de gráfica de líneas (DASH-02, task "Build chart UI component").
 *
 * Dibuja una serie de /readings/series en un SVG hecho a mano: sin librerías
 * ni build, como el resto del frontend, y sin innerHTML (los nodos se crean
 * con svg()/h() de ui.js). Las escalas y los textos los calcula series.js.
 *
 * Qué resuelve de los criterios del item:
 * - Eje de tiempo con marcas en horas redondas y eje vertical con la unidad
 *   de la medición (criterio 1).
 * - Un rango sin lecturas no dibuja una gráfica vacía: muestra el aviso
 *   (criterio 2).
 * - Si la serie viene agregada, además de la línea del promedio se pinta la
 *   banda del mínimo al máximo de cada bucket: promediar no esconde un pico
 *   (criterio 3).
 * - Donde faltan datos la línea se corta, en vez de cruzar el hueco con una
 *   recta que nadie midió.
 *
 * Accesible: el SVG es una imagen con su descripción y el pie de la gráfica
 * dice en texto el punto señalado. Con el mouse o con las flechas del teclado
 * se recorre la serie punto por punto.
 */
import {
  emptyNote,
  formatStamp,
  formatTimeTick,
  formatValue,
  formatWithUnit,
  niceTicks,
  pointTime,
  presentation,
  splitSegments,
  timeTicks,
  yDomain,
} from "./series.js";
import { h, icon, svg } from "./ui.js";

// Lienzo en unidades del viewBox; el SVG se escala al ancho de la tarjeta.
const WIDTH = 720;
const HEIGHT = 260;
const PAD = { top: 16, right: 14, bottom: 26, left: 52 };
const PLOT = {
  x: PAD.left,
  y: PAD.top,
  width: WIDTH - PAD.left - PAD.right,
  height: HEIGHT - PAD.top - PAD.bottom,
};

/**
 * Crea la gráfica. Devuelve `{ el, paint, clear }`:
 *   paint(series)   dibuja una respuesta de /readings/series
 *   clear(mensaje)  deja el lienzo con un aviso (cargando, error, sin datos)
 */
export function createChart() {
  const canvas = svg("svg", {
    viewBox: `0 0 ${WIDTH} ${HEIGHT}`,
    class: "chart-svg",
    role: "img",
    preserveAspectRatio: "xMidYMid meet",
  });
  const legend = h("div", { class: "chart-legend" });
  const caption = h("p", { class: "chart-caption", "aria-live": "polite" });
  const el = h("div", { class: "chart" }, canvas, legend, caption);

  let state = null;     // { series, points, domain, fromMs, toMs, look }
  let rule = null;      // línea vertical del punto señalado
  let dot = null;
  let cursor = -1;

  function clear(message, tone = "info") {
    state = null;
    rule = null;
    dot = null;
    cursor = -1;
    el.dataset.state = tone;
    el.removeAttribute("tabindex");
    canvas.removeAttribute("aria-label");
    canvas.replaceChildren(svg("text", {
      class: "chart-placeholder", x: WIDTH / 2, y: HEIGHT / 2, "text-anchor": "middle",
    }, message || ""));
    legend.replaceChildren();
    caption.textContent = "";
  }

  function paint(series) {
    // `at`: dónde se dibuja cada punto (el centro de su bucket si es promedio).
    const points = (series?.points || [])
      .filter((point) => Number.isFinite(Number(point.value)))
      .map((point) => ({ ...point, at: pointTime(point, series.bucket_seconds) }));
    if (!series?.has_data || !points.length) {
      clear(emptyNote(series), "empty");
      return;
    }

    const look = presentation(series);
    const domain = yDomain(points, { minSpan: look.decimals === 0 ? 2 : 1 });
    state = {
      series,
      points,
      domain,
      look,
      fromMs: Date.parse(series.from),
      toMs: Date.parse(series.to),
    };
    el.dataset.state = "ok";
    el.tabIndex = 0;

    const { fromMs, toMs } = state;
    const segments = splitSegments(points, { bucketSeconds: series.bucket_seconds });
    const nodes = [
      svg("title", {}, describe(series, points, look)),
      ...gridNodes(series, domain, look),
      ...timeAxisNodes(fromMs, toMs),
    ];

    if (series.aggregated) {
      for (const segment of segments) {
        const band = bandPath(segment, domain, fromMs, toMs);
        if (band) nodes.push(svg("path", { class: "chart-band", d: band }));
      }
    }
    for (const segment of segments) {
      if (segment.length === 1) {
        // Un tramo aislado no tiene línea que dibujar: se marca con un punto.
        const [only] = segment;
        nodes.push(svg("circle", {
          class: "chart-dot-lone",
          cx: xOf(only.at, fromMs, toMs).toFixed(2),
          cy: yOf(only.value, domain).toFixed(2),
          r: 2.5,
        }));
        continue;
      }
      nodes.push(svg("path", {
        class: "chart-line", fill: "none", d: linePath(segment, domain, fromMs, toMs),
      }));
    }

    rule = svg("line", { class: "chart-rule", y1: PLOT.y, y2: PLOT.y + PLOT.height });
    dot = svg("circle", { class: "chart-dot", r: 4 });
    nodes.push(rule, dot);
    canvas.replaceChildren(...nodes);

    // replaceChildren() no filtra nulls como lo hace h(): un `null` suelto
    // se vería escrito en la página. La leyenda se arma antes y se pasa ya lista.
    const keys = [h("span", { class: "chart-key" },
      h("span", { class: "chart-swatch" }),
      series.aggregated ? "Promedio del bucket" : "Lectura")];
    if (series.aggregated) {
      keys.push(h("span", { class: "chart-key" },
        h("span", { class: "chart-swatch chart-swatch-band" }), "Mínimo y máximo"));
    }
    legend.replaceChildren(...keys);

    moveCursor(points.length - 1);   // arranca en la lectura más reciente
  }

  /** Señala el punto `index` y lo dice en el pie de la gráfica. */
  function moveCursor(index) {
    if (!state) return;
    const { points, domain, fromMs, toMs, series, look } = state;
    cursor = Math.max(0, Math.min(points.length - 1, index));
    const point = points[cursor];
    const x = xOf(point.at, fromMs, toMs).toFixed(2);

    if (rule) {
      rule.setAttribute("x1", x);
      rule.setAttribute("x2", x);
    }
    if (dot) {
      dot.setAttribute("cx", x);
      dot.setAttribute("cy", yOf(point.value, domain).toFixed(2));
    }

    const parts = [formatStamp(point.ts), formatWithUnit(series, point.value)];
    if (series.aggregated) {
      parts.push(`mín ${formatValue(series, point.min)} · máx ${formatValue(series, point.max)}`);
      parts.push(`${point.count} ${point.count === 1 ? "lectura" : "lecturas"}`);
    }
    caption.replaceChildren(icon("clock", "icon-sm"), parts.join(" · "));
    canvas.setAttribute("aria-label", `${look.label}. ${parts.join(". ")}`);
  }

  /** Índice del punto más cercano a una posición horizontal del lienzo. */
  function nearest(canvasX) {
    const { points, fromMs, toMs } = state;
    let best = 0;
    let bestDistance = Infinity;
    for (let i = 0; i < points.length; i += 1) {
      const distance = Math.abs(xOf(points[i].at, fromMs, toMs) - canvasX);
      if (distance < bestDistance) {
        best = i;
        bestDistance = distance;
      }
    }
    return best;
  }

  canvas.addEventListener("pointermove", (event) => {
    if (!state) return;
    const box = canvas.getBoundingClientRect();
    if (!box.width) return;
    moveCursor(nearest(((event.clientX - box.left) / box.width) * WIDTH));
  });

  el.addEventListener("keydown", (event) => {
    if (!state) return;
    const jumps = { ArrowLeft: -1, ArrowRight: 1, Home: "first", End: "last" };
    const jump = jumps[event.key];
    if (jump === undefined) return;
    event.preventDefault();
    if (jump === "first") moveCursor(0);
    else if (jump === "last") moveCursor(state.points.length - 1);
    else moveCursor(cursor + jump);
  });

  clear("Cargando…");
  return { el, paint, clear };
}

// ── Geometría ─────────────────────────────────────────────────────────

function xOf(ms, fromMs, toMs) {
  const span = toMs - fromMs || 1;
  const ratio = Math.max(0, Math.min(1, (ms - fromMs) / span));
  return PLOT.x + ratio * PLOT.width;
}

function yOf(value, [min, max]) {
  const span = max - min || 1;
  const ratio = Math.max(0, Math.min(1, (value - min) / span));
  return PLOT.y + PLOT.height - ratio * PLOT.height;
}

function linePath(points, domain, fromMs, toMs) {
  return points
    .map((point, index) => {
      const x = xOf(point.at, fromMs, toMs).toFixed(2);
      const y = yOf(point.value, domain).toFixed(2);
      return `${index === 0 ? "M" : "L"}${x} ${y}`;
    })
    .join(" ");
}

/** Banda del mínimo al máximo de cada bucket: ida por arriba, vuelta por abajo. */
function bandPath(points, domain, fromMs, toMs) {
  const usable = points.filter(
    (point) => Number.isFinite(Number(point.min)) && Number.isFinite(Number(point.max)),
  );
  if (usable.length < 2) return null;
  const forward = usable.map((point, index) => {
    const x = xOf(point.at, fromMs, toMs).toFixed(2);
    return `${index === 0 ? "M" : "L"}${x} ${yOf(point.max, domain).toFixed(2)}`;
  });
  const back = [...usable].reverse().map((point) => {
    const x = xOf(point.at, fromMs, toMs).toFixed(2);
    return `L${x} ${yOf(point.min, domain).toFixed(2)}`;
  });
  return `${forward.join(" ")} ${back.join(" ")} Z`;
}

// ── Ejes ──────────────────────────────────────────────────────────────

function gridNodes(series, domain, look) {
  const nodes = [];
  // Con mediciones enteras (porcentajes, nivel de luz) el paso no baja de 1:
  // de otro modo dos marcas se redondearían a la misma etiqueta.
  const ticks = niceTicks(domain[0], domain[1], 4, { minStep: look.decimals === 0 ? 1 : 0 });
  for (const tick of ticks) {
    const y = yOf(tick, domain).toFixed(2);
    nodes.push(svg("line", {
      class: "chart-grid", x1: PLOT.x, x2: PLOT.x + PLOT.width, y1: y, y2: y,
    }));
    nodes.push(svg("text", {
      class: "chart-tick", x: PLOT.x - 8, y: (Number(y) + 3.5).toFixed(2), "text-anchor": "end",
    }, formatValue(series, tick)));
  }
  nodes.push(svg("text", {
    class: "chart-unit", x: PLOT.x - 8, y: PLOT.y - 5, "text-anchor": "end",
  }, look.unit));
  return nodes;
}

function timeAxisNodes(fromMs, toMs) {
  const nodes = [svg("line", {
    class: "chart-axis",
    x1: PLOT.x, x2: PLOT.x + PLOT.width,
    y1: PLOT.y + PLOT.height, y2: PLOT.y + PLOT.height,
  })];
  for (const tick of timeTicks(fromMs, toMs, 5)) {
    const x = xOf(tick, fromMs, toMs).toFixed(2);
    nodes.push(svg("line", {
      class: "chart-grid chart-grid-x", x1: x, x2: x, y1: PLOT.y, y2: PLOT.y + PLOT.height,
    }));
    nodes.push(svg("text", {
      class: "chart-tick", x, y: HEIGHT - 8, "text-anchor": "middle",
    }, formatTimeTick(tick, toMs - fromMs)));
  }
  return nodes;
}

/** Descripción del SVG para quien no lo ve. */
function describe(series, points, look) {
  const first = points[0];
  const last = points[points.length - 1];
  return [
    `${look.label}, del ${formatStamp(series.from)} al ${formatStamp(series.to)}.`,
    `${points.length} ${points.length === 1 ? "punto" : "puntos"}`,
    series.aggregated ? "(promedio por bucket)," : "(lectura por lectura),",
    `de ${formatWithUnit(series, first.value)} a ${formatWithUnit(series, last.value)}.`,
    `Mínimo ${formatWithUnit(series, series.value_min)}, máximo ${formatWithUnit(series, series.value_max)}.`,
  ].join(" ");
}
