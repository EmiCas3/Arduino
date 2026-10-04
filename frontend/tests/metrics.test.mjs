/**
 * Pruebas de la lógica de los widgets de DASH-01 (frontend/js/metrics.js).
 *
 *     node --test frontend/tests/metrics.test.mjs
 *
 * Usa solo el runner que trae Node (18 o más): no hay dependencias, npm ni build.
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

// metrics.js es un módulo ES con extensión .js y el frontend no tiene
// package.json (no hay build). Para que cualquier Node lo trate como módulo
// se carga su texto como data: URL; funciona porque no importa nada.
const source = await readFile(new URL("../js/metrics.js", import.meta.url), "utf8");
const {
  ALERTS,
  METRICS,
  alertsFor,
  formatAge,
  formatValue,
  metricState,
  presentationFor,
  summarize,
  unknownAlerts,
} = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(source)}`);

const metric = (overrides = {}) => ({
  key: "temp_c", label: "Temperatura del aire", unit: "°C", value: 24.2, raw: null,
  age_seconds: 4, stale: false, sensor_down: false, ...overrides,
});
const STALE_AFTER = 15 * 60;

// ── Estado de la tarjeta ───────────────────────────────────────────────

test("sin alertas y dato reciente: normal", () => {
  assert.equal(metricState(metric(), [], 4, STALE_AFTER).state, "ok");
});

test("AVISO del Arduino para esa medición: aviso", () => {
  const result = metricState(metric(), ["TEMP_ALTA_LEVE"], 4, STALE_AFTER);
  assert.deepEqual(result, { state: "warn", detail: "Un poco alta" });
});

test("ALERTA del Arduino para esa medición: alerta", () => {
  assert.equal(metricState(metric(), ["TEMP_ALTA"], 4, STALE_AFTER).state, "crit");
});

test("las alertas de otro sensor no pintan esta tarjeta", () => {
  assert.equal(metricState(metric(), ["SUELO_SECO", "TANQUE_UN_CUARTO"], 4, STALE_AFTER).state, "ok");
});

test("ALERTA gana sobre AVISO", () => {
  const tank = metric({ key: "nivel_pct", value: 10 });
  assert.equal(metricState(tank, ["TANQUE_MITAD", "TANQUE_UN_CUARTO"], 4, STALE_AFTER).state, "crit");
});

test("dato marcado stale por la API: viejo, aunque traiga alerta", () => {
  const result = metricState(metric({ stale: true }), ["TEMP_ALTA"], 20 * 60, STALE_AFTER);
  assert.equal(result.state, "stale");
});

test("el dato se vuelve viejo en el navegador sin recargar", () => {
  const fresh = metric({ age_seconds: 890, stale: false });
  assert.equal(metricState(fresh, [], 890, STALE_AFTER).state, "ok");
  assert.equal(metricState(fresh, [], 901, STALE_AFTER).state, "stale");
});

test("sensor caído: se conserva el último valor y nunca es 0", () => {
  const down = metric({ sensor_down: true, value: 22.5, age_seconds: 3 * 3600 });
  const result = metricState(down, ["FALLA_DHT"], 3 * 3600, STALE_AFTER);
  assert.equal(result.state, "down");            // gana sobre "viejo"
  assert.equal(formatValue(down), "22.5");
});

test("sensor que nunca ha dado un valor: caído, sin cifra", () => {
  const never = metric({ sensor_down: true, value: null, age_seconds: null, stale: true });
  assert.equal(metricState(never, [], null, STALE_AFTER).state, "down");
  assert.equal(formatValue(never), "—");
});

test("vivero sin lecturas: sin datos, no 0", () => {
  const empty = metric({ value: null, age_seconds: null, stale: true });
  assert.equal(metricState(empty, [], null, STALE_AFTER).state, "empty");
  assert.equal(formatValue(empty), "—");
});

test("un 0 real se muestra como 0 (tanque vacío), no como dato faltante", () => {
  const tank = metric({ key: "nivel_pct", value: 0 });
  assert.equal(formatValue(tank), "0");
  assert.equal(metricState(tank, ["TANQUE_UN_CUARTO"], 4, STALE_AFTER).state, "crit");
});

// ── Alertas del firmware ───────────────────────────────────────────────

test("FALLA_DHT toca a temperatura y a humedad del aire", () => {
  assert.equal(alertsFor("temp_c", ["FALLA_DHT"]).length, 1);
  assert.equal(alertsFor("hum_aire_pct", ["FALLA_DHT"]).length, 1);
  assert.equal(alertsFor("suelo_pct", ["FALLA_DHT"]).length, 0);
});

test("todas las alertas del contrato tienen tarjeta, severidad y texto", () => {
  const contract = [
    "TEMP_ALTA", "TEMP_BAJA", "TEMP_ALTA_LEVE", "TEMP_BAJA_LEVE",
    "HUM_AIRE_ALTA", "HUM_AIRE_BAJA", "HUM_AIRE_ALTA_LEVE", "HUM_AIRE_BAJA_LEVE",
    "SUELO_ENCHARCADO", "SUELO_SECO", "SUELO_HUMEDO_LEVE", "SUELO_SECO_LEVE",
    "TANQUE_MITAD", "TANQUE_UN_CUARTO", "SOMBRA_PROLONGADA", "SOL_INSUFICIENTE",
    "FALLA_DHT", "FALLA_SUELO", "FALLA_NIVEL",
  ];
  assert.deepEqual(Object.keys(ALERTS).sort(), [...contract].sort());
  for (const code of contract) {
    assert.ok(ALERTS[code].keys.every((key) => METRICS[key]), code);
    assert.ok(["crit", "warn", "down"].includes(ALERTS[code].level), code);
    assert.ok(ALERTS[code].text.length > 3, code);
    assert.doesNotMatch(ALERTS[code].text, /\d/, `${code}: sin números (los umbrales cambian)`);
  }
});

test("un código nuevo del firmware no rompe nada y se reporta", () => {
  assert.deepEqual(unknownAlerts(["TEMP_ALTA", "LUZ_BAJA_NUEVA"]), ["LUZ_BAJA_NUEVA"]);
  assert.equal(metricState(metric({ key: "luz_nivel", value: 700 }), ["LUZ_BAJA_NUEVA"], 4, STALE_AFTER).state, "ok");
  assert.ok(summarize({ estado: "AVISO", alertas: ["LUZ_BAJA_NUEVA"] }).some((c) => c.text === "LUZ_BAJA_NUEVA"));
});

// ── Textos ─────────────────────────────────────────────────────────────

test("tiempo desde la lectura", () => {
  assert.equal(formatAge(0), "ahora mismo");
  assert.equal(formatAge(12), "hace 12 s");
  assert.equal(formatAge(59.9), "hace 59 s");
  assert.equal(formatAge(60), "hace 1 min");
  assert.equal(formatAge(18 * 60 + 30), "hace 18 min");
  assert.equal(formatAge(3600), "hace 1 h");
  assert.equal(formatAge(2 * 3600 + 5 * 60), "hace 2 h 5 min");
  assert.equal(formatAge(4 * 86400 + 10), "hace 4 d");
  assert.equal(formatAge(null), "—");
  assert.equal(formatAge(-3), "ahora mismo");    // relojes desfasados: nunca "hace -3 s"
});

test("formato de valores", () => {
  assert.equal(formatValue(metric({ value: 24 })), "24.0");                       // temperatura: 1 decimal
  assert.equal(formatValue(metric({ key: "suelo_pct", value: 62 })), "62");
  assert.equal(formatValue(metric({ key: "luz_nivel", value: 717 })), "717");
});

test("una métrica nueva de la API usa su propia etiqueta y unidad", () => {
  const co2 = { key: "co2_ppm", label: "CO₂", unit: "ppm", value: 415.25 };
  assert.equal(presentationFor(co2).label, "CO₂");
  assert.equal(presentationFor(co2).unit, "ppm");
  assert.equal(formatValue(co2), "415.3");
});

test("resumen de la última lectura del Arduino", () => {
  const chips = summarize({
    estado: "ALERTA", alertas: ["SUELO_SECO"], riego_sugerido: true,
    bomba_habilitada: false, vent: "ALTA", es_dia: true,
  });
  assert.deepEqual(chips.map((c) => c.tone), ["crit", "warn", "crit", "plain", "plain"]);
  assert.deepEqual(summarize(null), []);
  assert.deepEqual(summarize({ estado: "OK", alertas: [], riego_sugerido: false, bomba_habilitada: true, vent: "BASE", es_dia: false })
    .map((c) => c.text), ["Arduino: todo en rango", "Ventilación base", "De noche"]);
});
