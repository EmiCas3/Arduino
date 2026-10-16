/**
 * Pruebas de la lógica de la gráfica histórica y las tendencias
 * (frontend/js/series.js — DASH-02 y AI-01).
 *
 *     node --test frontend/tests/series.test.mjs
 *
 * Usa solo el runner que trae Node (18 o más): no hay dependencias ni build.
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

// Igual que metrics.test.mjs: el frontend no tiene package.json, así que los
// módulos se cargan como data: URL para que Node los trate como ES modules.
// series.js sí importa metrics.js, así que primero se vuelve data: URL ese
// import y luego se carga el módulo completo.
const dataUrl = (source) => `data:text/javascript;charset=utf-8,${encodeURIComponent(source)}`;

const metricsUrl = dataUrl(await readFile(new URL("../js/metrics.js", import.meta.url), "utf8"));
const seriesSource = (await readFile(new URL("../js/series.js", import.meta.url), "utf8"))
  .replace('from "./metrics.js"', `from "${metricsUrl}"`);

const {
  DEFAULT_RANGE,
  RANGES,
  TREND_LABELS,
  describePeriod,
  describeTrend,
  emptyNote,
  formatSpan,
  formatStamp,
  formatTimeTick,
  formatValue,
  formatWithUnit,
  medianStep,
  niceTicks,
  presentation,
  rangeById,
  rangeStart,
  resolutionNote,
  splitSegments,
  timeTicks,
  yDomain,
} = await import(dataUrl(seriesSource));

const HOUR = 3600 * 1000;

/** Puntos cada `stepSeconds` a partir de `startMs`. */
const points = (values, { startMs = Date.UTC(2026, 9, 15, 12, 0, 0), stepSeconds = 600 } = {}) =>
  values.map((value, index) => ({
    ts: new Date(startMs + index * stepSeconds * 1000).toISOString(),
    value,
    min: null,
    max: null,
    count: 1,
  }));

const series = (overrides = {}) => ({
  metric: "temp_c",
  label: "Temperatura del aire",
  unit: "°C",
  has_data: true,
  aggregated: false,
  bucket_seconds: 0,
  reading_count: 3,
  points: points([20, 21, 22]),
  ...overrides,
});

// ── Rangos ────────────────────────────────────────────────────────────

test("los rangos que ofrece la página caben en los 90 días del servidor", () => {
  assert.ok(RANGES.length >= 3);
  assert.ok(RANGES.every((range) => range.hours > 0 && range.hours <= 90 * 24));
  assert.ok(RANGES.some((range) => range.id === DEFAULT_RANGE));
});

test("un rango desconocido cae en el de por defecto", () => {
  assert.equal(rangeById("no-existe").id, DEFAULT_RANGE);
  assert.equal(rangeById("6h").hours, 6);
});

test("rangeStart da el `from` que espera la API", () => {
  const now = Date.UTC(2026, 9, 15, 12, 0, 0);
  const from = rangeStart("6h", now);
  assert.equal(from, new Date(now - 6 * HOUR).toISOString());
  assert.match(from, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
});

// ── Escala vertical ───────────────────────────────────────────────────

test("el rango vertical se ajusta a los datos, no a 0-100", () => {
  const [min, max] = yDomain(points([74, 75, 76]));
  assert.ok(min > 70 && min < 74, `min fuera de lugar: ${min}`);
  assert.ok(max > 76 && max < 80, `max fuera de lugar: ${max}`);
});

test("una serie plana se abre un mínimo para que la línea se vea", () => {
  const [min, max] = yDomain(points([22, 22, 22]), { minSpan: 1 });
  assert.ok(max - min >= 1);
  assert.equal((min + max) / 2, 22);
});

test("el rango vertical toma el mínimo y el máximo de cada bucket", () => {
  const [min, max] = yDomain([
    { ts: "2026-10-15T12:00:00Z", value: 22, min: 19, max: 25, count: 10 },
    { ts: "2026-10-15T12:10:00Z", value: 23, min: 21, max: 30, count: 10 },
  ]);
  assert.ok(min < 19 && max > 30);
});

test("sin puntos no hay rango vertical", () => {
  assert.equal(yDomain([]), null);
  assert.equal(yDomain([{ ts: "2026-10-15T12:00:00Z", value: null }]), null);
});

// ── Marcas de los ejes ────────────────────────────────────────────────

test("las marcas verticales son números redondos dentro del rango", () => {
  const ticks = niceTicks(19.4, 26.6, 4);
  assert.ok(ticks.length >= 3 && ticks.length <= 6, `fueron ${ticks.length}`);
  assert.ok(ticks.every((tick) => tick >= 19.4 && tick <= 26.6));
  assert.deepEqual(ticks, [20, 22, 24, 26]);
});

test("con mediciones enteras las marcas no se repiten", () => {
  const ticks = niceTicks(61.5, 63.5, 4, { minStep: 1 });
  const labels = ticks.map((tick) => tick.toFixed(0));
  assert.equal(new Set(labels).size, labels.length, `etiquetas repetidas: ${labels}`);
});

test("un rango sin ancho devuelve una sola marca", () => {
  assert.deepEqual(niceTicks(22, 22), [22]);
  assert.deepEqual(niceTicks(NaN, 1), []);
});

test("las marcas de tiempo caen en horas redondas", () => {
  const to = Date.UTC(2026, 9, 15, 12, 7, 30);
  const ticks = timeTicks(to - 24 * HOUR, to, 5);
  assert.ok(ticks.length >= 3 && ticks.length <= 8, `fueron ${ticks.length}`);
  assert.ok(ticks.every((tick) => tick % (3600 * 1000) === 0), "no caen en hora en punto");
  const steps = ticks.slice(1).map((tick, i) => tick - ticks[i]);
  assert.equal(new Set(steps).size, 1, "el paso no es constante");
});

test("un rango invertido no tiene marcas de tiempo", () => {
  const now = Date.now();
  assert.deepEqual(timeTicks(now, now - HOUR), []);
  assert.deepEqual(timeTicks(now, now), []);
});

test("la etiqueta de tiempo es la hora en rangos cortos y el día en los largos", () => {
  const moment = Date.UTC(2026, 9, 15, 18, 30, 0);
  assert.match(formatTimeTick(moment, 6 * HOUR), /\d{1,2}:\d{2}/);
  assert.match(formatTimeTick(moment, 7 * 24 * HOUR), /\d{1,2}/);
  assert.notEqual(formatTimeTick(moment, 6 * HOUR), formatTimeTick(moment, 7 * 24 * HOUR));
});

test("formatStamp aguanta una fecha inválida", () => {
  assert.equal(formatStamp("no es una fecha"), "—");
  assert.ok(formatStamp("2026-10-15T18:30:00Z").length > 5);
});

// ── Cortes en los huecos ──────────────────────────────────────────────

test("la mediana del paso entre puntos", () => {
  assert.equal(medianStep(points([1, 2, 3], { stepSeconds: 600 })), 600_000);
  assert.equal(medianStep([]), 0);
  assert.equal(medianStep(points([1])), 0);
});

test("una serie seguida es un solo tramo", () => {
  const segments = splitSegments(points([20, 21, 22, 23]), { bucketSeconds: 600 });
  assert.equal(segments.length, 1);
  assert.equal(segments[0].length, 4);
});

test("donde faltan datos la línea se corta en vez de cruzar el hueco", () => {
  const start = Date.UTC(2026, 9, 15, 12, 0, 0);
  const withHole = [
    ...points([20, 21], { startMs: start, stepSeconds: 600 }),
    ...points([25, 26], { startMs: start + 3 * 3600 * 1000, stepSeconds: 600 }),
  ];
  const segments = splitSegments(withHole, { bucketSeconds: 600 });
  assert.equal(segments.length, 2);
  assert.deepEqual(segments.map((segment) => segment.length), [2, 2]);
});

test("sin bucket el corte se decide con la mediana del paso", () => {
  const start = Date.UTC(2026, 9, 15, 12, 0, 0);
  const uneven = [
    ...points([20, 21, 22], { startMs: start, stepSeconds: 2 }),
    ...points([30], { startMs: start + 600_000, stepSeconds: 2 }),
  ];
  assert.equal(splitSegments(uneven).length, 2);
});

test("un solo punto es un tramo y ninguno no es nada", () => {
  assert.equal(splitSegments(points([20])).length, 1);
  assert.deepEqual(splitSegments([]), []);
  assert.deepEqual(splitSegments(undefined), []);
});

// ── Textos de la gráfica ──────────────────────────────────────────────

test("cada medición se presenta con su unidad y sus decimales", () => {
  assert.equal(presentation({ metric: "temp_c" }).unit, "°C");
  assert.equal(presentation({ metric: "temp_c" }).decimals, 1);
  assert.equal(presentation({ metric: "nivel_pct" }).decimals, 0);
  // Una medición que el frontend todavía no conozca usa lo que mande la API.
  assert.equal(presentation({ metric: "co2_ppm", label: "CO2", unit: "ppm" }).unit, "ppm");
});

test("los valores se escriben con los decimales de su medición", () => {
  assert.equal(formatValue(series(), 24.25), "24.3");
  assert.equal(formatValue(series({ metric: "nivel_pct" }), 74.5), "75");
  assert.equal(formatValue(series(), null), "—");
  assert.equal(formatWithUnit(series(), 24.2), "24.2 °C");
  assert.equal(formatWithUnit(series(), null), "—");
});

test("la nota dice si son lecturas o promedios, y cuántas lecturas hay", () => {
  assert.match(resolutionNote(series()), /lectura por lectura · 3 lecturas/);
  const aggregated = series({
    aggregated: true, bucket_seconds: 300, reading_count: 1440,
  });
  assert.match(resolutionNote(aggregated), /promedio cada 5 min · 1440 lecturas/);
  assert.equal(resolutionNote(series({ has_data: false })), "");
  assert.match(resolutionNote(series({ reading_count: 1 })), /1 lectura$/);
});

test("un periodo sin datos se explica nombrando la medición", () => {
  assert.match(emptyNote(series()), /No hay lecturas de temperatura/);
  assert.match(emptyNote(series({ metric: "nivel_pct" })), /nivel del tanque/);
});

// ── Tendencias (AI-01) ────────────────────────────────────────────────

const variable = (overrides = {}) => ({
  key: "temp_c",
  label: "Temperatura del aire",
  unit: "°C",
  trend: "rising",
  slope_per_hour: 1.2,
  change: 7.2,
  stable_band: 0.5,
  observed_hours: 6,
  first: 18,
  last: 25.2,
  mean: 21.6,
  min: 18,
  max: 25.2,
  fit_r2: 0.98,
  samples: 36,
  expected_samples: 36,
  coverage_pct: 100,
  readings_used: 360,
  readings_missing: 0,
  gaps: [],
  ...overrides,
});

test("una variable que sube se describe con su cambio y su periodo", () => {
  const view = describeTrend(variable());
  assert.equal(view.trend, TREND_LABELS.rising);
  assert.equal(view.tone, "up");
  assert.equal(view.value, "25.2 °C");
  assert.match(view.change, /^\+7\.2 °C en 6 h$/);
  assert.equal(view.quality, "");
});

test("una variable que baja lleva el signo negativo", () => {
  const view = describeTrend(variable({ trend: "falling", change: -12.5, last: 61 }));
  assert.equal(view.tone, "down");
  assert.match(view.change, /^-12\.5 °C en 6 h$/);
});

test("sin datos suficientes no se inventa un cambio", () => {
  const view = describeTrend(variable({
    trend: "insufficient_data", change: null, slope_per_hour: null,
    observed_hours: null, last: null, samples: 1,
  }));
  assert.equal(view.trend, TREND_LABELS.insufficient_data);
  assert.equal(view.tone, "none");
  assert.equal(view.change, "");
  assert.equal(view.value, "—");
});

test("la calidad del dato dice la cobertura, los huecos y lo que no mandó el sensor", () => {
  const view = describeTrend(variable({
    coverage_pct: 75,
    gaps: [{ from: "2026-10-15T12:00:00Z", to: "2026-10-15T12:30:00Z", seconds: 1800, buckets: 30 }],
    readings_missing: 20,
  }));
  assert.match(view.quality, /cobertura 75 %/);
  assert.match(view.quality, /1 hueco · 30 min/);
  assert.match(view.quality, /20 sin dato del sensor/);
});

test("varios huecos se cuentan en plural y se suman", () => {
  const view = describeTrend(variable({
    coverage_pct: 50,
    gaps: [
      { seconds: 600, buckets: 10 },
      { seconds: 1200, buckets: 20 },
    ],
  }));
  assert.match(view.quality, /2 huecos · 30 min/);
});

test("el periodo del análisis queda a la vista", () => {
  assert.match(
    describePeriod({ period: { hours: 24, bucket_seconds: 600 } }),
    /últimas 24 h · pasos de 10 min/,
  );
  assert.equal(describePeriod(null), "");
});

test("formatSpan habla como los botones de rango", () => {
  assert.equal(formatSpan(6), "6 h");
  assert.equal(formatSpan(24), "24 h");
  assert.equal(formatSpan(24 * 7), "7 días");
  assert.equal(formatSpan(0), "—");
});
