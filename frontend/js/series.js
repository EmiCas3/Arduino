/**
 * Lógica de la gráfica histórica (DASH-02) y de las tendencias (AI-01), sin
 * DOM ni red: escalas, marcas de los ejes, cortes en los huecos y textos.
 *
 * Igual que metrics.js: al no tocar el navegador se puede probar con
 * `node --test frontend/tests/series.test.mjs`. Lo que dibuja los nodos SVG
 * está en chart.js y lo que pide los datos en history.js.
 *
 * El rango vertical NO se fija en 0–100: se ajusta a los datos del periodo.
 * Un tanque que bajó de 76 % a 74 % se tiene que ver bajando; con el eje de
 * 0 a 100 sería una raya plana.
 */
import { METRICS, formatDuration } from "./metrics.js";

/** Rangos que ofrece la página. El servidor acepta hasta 90 días. */
export const RANGES = [
  { id: "6h", label: "6 h", hours: 6 },
  { id: "24h", label: "24 h", hours: 24 },
  { id: "7d", label: "7 días", hours: 24 * 7 },
  { id: "30d", label: "30 días", hours: 24 * 30 },
];

export const DEFAULT_RANGE = "24h";

/** Cada cuánto se vuelven a pedir los datos del histórico. */
export const HISTORY_REFRESH_SECONDS = 60;

export function rangeById(id) {
  return RANGES.find((range) => range.id === id) || RANGES.find((range) => range.id === DEFAULT_RANGE);
}

/** `from` que se le manda a la API para un rango (el `to` lo pone el servidor). */
export function rangeStart(rangeId, now = Date.now()) {
  return new Date(now - rangeById(rangeId).hours * 3600 * 1000).toISOString();
}

// ── Escalas ───────────────────────────────────────────────────────────

/**
 * Rango vertical [min, max] con un margen, a partir de los puntos.
 *
 * Una serie plana (todos los valores iguales) no puede tener min === max: la
 * línea quedaría pegada a un borde o con altura cero. Se le abre un margen
 * mínimo para que se dibuje en el centro.
 */
export function yDomain(points, { padding = 0.08, minSpan = 1 } = {}) {
  const lows = [];
  const highs = [];
  for (const point of points || []) {
    const low = point.min ?? point.value;
    const high = point.max ?? point.value;
    if (Number.isFinite(low)) lows.push(low);
    if (Number.isFinite(high)) highs.push(high);
  }
  if (!lows.length) return null;

  let min = Math.min(...lows);
  let max = Math.max(...highs);
  const span = max - min;
  if (span < minSpan) {
    const grow = (minSpan - span) / 2;
    min -= grow;
    max += grow;
  } else {
    min -= span * padding;
    max += span * padding;
  }
  return [min, max];
}

const TICK_SHAPES = [1, 2, 2.5, 5, 10];

/**
 * Marcas "redondas" dentro de [min, max]: 20, 22, 24… en vez de 20.37, 21.9…
 *
 * `minStep` evita marcas más finas que lo que se puede escribir: con una
 * medición entera (porcentajes), un paso de 0.5 daría dos etiquetas "63".
 */
export function niceTicks(min, max, count = 4, { minStep = 0 } = {}) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (max <= min) return [min];
  const rough = (max - min) / Math.max(1, count);
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const shape = TICK_SHAPES.find((candidate) => rough <= candidate * magnitude) ?? 10;
  const step = Math.max(shape * magnitude, minStep);
  const ticks = [];
  for (let value = Math.ceil(min / step) * step; value <= max + step / 1000; value += step) {
    // Math.ceil sobre flotantes deja colas (20.000000000000004): se redondea
    // al paso para que la etiqueta salga limpia.
    ticks.push(Number((Math.round(value / step) * step).toFixed(10)));
  }
  return ticks;
}

// Pasos del eje de tiempo, de 1 minuto a 7 días: así las marcas caen en horas
// en punto o en días completos, no en "cada 1 h 23 min".
const TIME_STEPS = [
  60, 5 * 60, 15 * 60, 30 * 60, 3600, 2 * 3600, 3 * 3600, 6 * 3600,
  12 * 3600, 86400, 2 * 86400, 7 * 86400,
].map((seconds) => seconds * 1000);

/** Marcas del eje de tiempo (ms), alineadas al reloj. */
export function timeTicks(fromMs, toMs, count = 5) {
  if (!(toMs > fromMs)) return [];
  const rough = (toMs - fromMs) / Math.max(1, count);
  const step = TIME_STEPS.find((candidate) => candidate >= rough) ?? TIME_STEPS[TIME_STEPS.length - 1];
  const ticks = [];
  // Alineado a la época UTC: con pasos de hora o menos cae en minutos exactos
  // en cualquier zona (todas las zonas usadas en el proyecto son de hora o
  // media hora completa).
  for (let value = Math.ceil(fromMs / step) * step; value <= toMs; value += step) {
    ticks.push(value);
  }
  return ticks;
}

const clockFormat = new Intl.DateTimeFormat("es-MX", { hour: "2-digit", minute: "2-digit" });
const dayFormat = new Intl.DateTimeFormat("es-MX", { day: "numeric", month: "short" });
const stampFormat = new Intl.DateTimeFormat("es-MX", {
  day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
});

/** Etiqueta de una marca de tiempo: la hora, o el día si el rango es largo. */
export function formatTimeTick(ms, spanMs) {
  return spanMs > 36 * 3600 * 1000 ? dayFormat.format(new Date(ms)) : clockFormat.format(new Date(ms));
}

/** Momento completo, para el punto que está señalando la persona. */
export function formatStamp(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : stampFormat.format(date);
}

// ── Cortes en los huecos ──────────────────────────────────────────────

/** Mediana de la distancia entre puntos (ms). 0 si hay menos de dos. */
export function medianStep(points) {
  const steps = [];
  for (let i = 1; i < (points || []).length; i += 1) {
    steps.push(Date.parse(points[i].ts) - Date.parse(points[i - 1].ts));
  }
  if (!steps.length) return 0;
  steps.sort((a, b) => a - b);
  const middle = Math.floor(steps.length / 2);
  return steps.length % 2 ? steps[middle] : (steps[middle - 1] + steps[middle]) / 2;
}

/**
 * Parte los puntos en tramos continuos: donde faltan datos, la línea se corta
 * en vez de cruzar el hueco con una recta que nadie midió (es la misma idea
 * con la que AI-01 reporta los huecos en vez de rellenarlos).
 */
export function splitSegments(points, { bucketSeconds = 0, gapFactor = 1.75 } = {}) {
  const list = points || [];
  if (list.length < 2) return list.length ? [list.slice()] : [];

  const step = bucketSeconds > 0 ? bucketSeconds * 1000 : medianStep(list);
  const limit = step > 0 ? step * gapFactor : Infinity;

  const segments = [];
  let current = [list[0]];
  for (let i = 1; i < list.length; i += 1) {
    const delta = Date.parse(list[i].ts) - Date.parse(list[i - 1].ts);
    if (delta > limit) {
      segments.push(current);
      current = [];
    }
    current.push(list[i]);
  }
  segments.push(current);
  return segments;
}

// ── Textos ────────────────────────────────────────────────────────────

/** Decimales y unidad de la medición que se está graficando. */
export function presentation(series) {
  const look = METRICS[series?.metric];
  return {
    label: look?.label || series?.label || series?.metric || "",
    unit: look?.unit || series?.unit || "",
    decimals: look?.decimals ?? 1,
    icon: look?.icon || "gauge",
  };
}

/** Valor con los decimales de su medición: 24.2, 62, 717. */
export function formatValue(series, value, { decimals } = {}) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const places = decimals ?? presentation(series).decimals;
  return Number(value).toFixed(places);
}

/** Valor con unidad: "24.2 °C". */
export function formatWithUnit(series, value) {
  const text = formatValue(series, value);
  const { unit } = presentation(series);
  return text === "—" ? text : `${text} ${unit}`.trim();
}

/** "promedio cada 5 min · 1440 lecturas" o "lectura por lectura · 120 lecturas". */
export function resolutionNote(series) {
  if (!series?.has_data) return "";
  const readings = `${series.reading_count} ${series.reading_count === 1 ? "lectura" : "lecturas"}`;
  if (!series.aggregated) return `lectura por lectura · ${readings}`;
  return `promedio cada ${formatDuration(series.bucket_seconds)} · ${readings}`;
}

/** Qué decir cuando el rango no tiene datos (criterio 2 de DASH-02). */
export function emptyNote(series) {
  const { label } = presentation(series);
  return `No hay lecturas de ${label.toLowerCase()} en este periodo.`;
}

// ── Tendencias (AI-01) ────────────────────────────────────────────────

export const TREND_LABELS = {
  rising: "Subiendo",
  falling: "Bajando",
  stable: "Estable",
  insufficient_data: "Sin datos suficientes",
};

export const TREND_TONES = {
  rising: "up",
  falling: "down",
  stable: "flat",
  insufficient_data: "none",
};

/**
 * Una variable de /analysis lista para mostrar:
 *   label     "Temperatura del aire"
 *   trend     "Subiendo"
 *   change    "+7.2 °C en 6 h"  (vacío si no hay tendencia)
 *   quality   "cobertura 75 % · 1 hueco de 30 min"
 *
 * No redacta ninguna recomendación: eso es AI-05.
 */
export function describeTrend(variable) {
  const decimals = METRICS[variable.key]?.decimals ?? 1;
  const unit = METRICS[variable.key]?.unit || variable.unit || "";
  const label = METRICS[variable.key]?.label || variable.label || variable.key;

  let change = "";
  if (variable.trend !== "insufficient_data" && Number.isFinite(variable.change)) {
    const sign = variable.change > 0 ? "+" : "";
    const amount = `${sign}${variable.change.toFixed(decimals)} ${unit}`.trim();
    change = `${amount} en ${formatDuration((variable.observed_hours || 0) * 3600)}`;
  }

  const quality = [];
  if (Number.isFinite(variable.coverage_pct) && variable.coverage_pct < 99.5) {
    quality.push(`cobertura ${variable.coverage_pct} %`);
  }
  if (variable.gaps?.length) {
    const seconds = variable.gaps.reduce((total, gap) => total + gap.seconds, 0);
    quality.push(`${variable.gaps.length} ${variable.gaps.length === 1 ? "hueco" : "huecos"} · ${formatDuration(seconds)}`);
  }
  if (variable.readings_missing) {
    quality.push(`${variable.readings_missing} sin dato del sensor`);
  }

  return {
    label,
    trend: TREND_LABELS[variable.trend] || variable.trend,
    tone: TREND_TONES[variable.trend] || "none",
    change,
    quality: quality.join(" · "),
    value: Number.isFinite(variable.last) ? `${variable.last.toFixed(decimals)} ${unit}`.trim() : "—",
  };
}

/** Duración de un periodo en el lenguaje de los botones: "6 h", "7 días". */
export function formatSpan(hours) {
  if (!Number.isFinite(hours) || hours <= 0) return "—";
  if (hours < 1) return formatDuration(hours * 3600);
  if (hours < 48) return `${Math.round(hours)} h`;
  return `${Math.round(hours / 24)} días`;
}

/** Periodo que usó el análisis, para dejarlo a la vista (criterio 1). */
export function describePeriod(analysis) {
  if (!analysis?.period) return "";
  const { hours, bucket_seconds: bucket } = analysis.period;
  return `últimas ${formatSpan(hours)} · pasos de ${formatDuration(bucket)}`;
}
