/**
 * Lógica de los widgets de DASH-01, sin DOM ni red: qué estado tiene cada
 * medición y cómo se escribe. Al no tocar el navegador se puede probar con
 * `node --test frontend/tests`.
 *
 * De dónde sale el estado de cada tarjeta (decisión D1 del plan):
 * de las `alertas` que el Arduino manda en cada lectura, con la severidad que
 * les da el firmware (AVISO o ALERTA). Aquí NO se comparan valores contra
 * umbrales: si el firmware cambia un umbral, el dashboard lo refleja solo.
 * Cuando existan CONF-06 y ALERT-01, el estado vendrá evaluado del backend.
 */

/** Segundos entre refrescos automáticos del dashboard. */
export const REFRESH_SECONDS = 15;

/**
 * Cómo se presenta cada medición. La `reference` es solo texto de ayuda:
 * - Temperatura, humedad y tierra muestran el rango ideal del rábano.
 * - Agua y luz NO muestran números: esos umbrales están por cambiar en el
 *   firmware, así que solo se explica la escala.
 * ÚNICO lugar del frontend con estos rangos; con CONF-06 vendrán de la API.
 */
export const METRICS = {
  temp_c: { label: "Temperatura", unit: "°C", icon: "thermo", decimals: 1, reference: "ideal 20–25 °C" },
  hum_aire_pct: { label: "Humedad del aire", unit: "% HR", icon: "wind", decimals: 0, reference: "ideal 60–80 %" },
  suelo_pct: { label: "Humedad de la tierra", unit: "%", icon: "soil", decimals: 0, reference: "ideal 60–65 %" },
  nivel_pct: { label: "Nivel del tanque", unit: "%", icon: "drop", decimals: 0, reference: "0 % vacío · 100 % lleno" },
  luz_nivel: { label: "Luz", unit: "de 1023", icon: "sun", decimals: 0, reference: "más alto = más luz" },
};

/**
 * Códigos de alerta del firmware (vivero_sensoresV2.ino) → a qué tarjeta
 * pertenecen y con qué severidad. `crit` = ALERTA, `warn` = AVISO y
 * `down` = el sensor no responde. Los textos no mencionan números para que
 * sigan siendo ciertos aunque cambien los umbrales.
 */
export const ALERTS = {
  TEMP_ALTA: { keys: ["temp_c"], level: "crit", text: "Temperatura muy alta" },
  TEMP_BAJA: { keys: ["temp_c"], level: "crit", text: "Temperatura muy baja" },
  TEMP_ALTA_LEVE: { keys: ["temp_c"], level: "warn", text: "Un poco alta" },
  TEMP_BAJA_LEVE: { keys: ["temp_c"], level: "warn", text: "Un poco baja" },
  HUM_AIRE_ALTA: { keys: ["hum_aire_pct"], level: "crit", text: "Aire muy húmedo" },
  HUM_AIRE_BAJA: { keys: ["hum_aire_pct"], level: "crit", text: "Aire muy seco" },
  HUM_AIRE_ALTA_LEVE: { keys: ["hum_aire_pct"], level: "warn", text: "Aire algo húmedo" },
  HUM_AIRE_BAJA_LEVE: { keys: ["hum_aire_pct"], level: "warn", text: "Aire algo seco" },
  SUELO_ENCHARCADO: { keys: ["suelo_pct"], level: "crit", text: "Tierra encharcada" },
  SUELO_SECO: { keys: ["suelo_pct"], level: "crit", text: "Tierra seca" },
  SUELO_HUMEDO_LEVE: { keys: ["suelo_pct"], level: "warn", text: "Tierra algo húmeda" },
  SUELO_SECO_LEVE: { keys: ["suelo_pct"], level: "warn", text: "Tierra algo seca" },
  TANQUE_UN_CUARTO: { keys: ["nivel_pct"], level: "crit", text: "Nivel de agua crítico" },
  TANQUE_MITAD: { keys: ["nivel_pct"], level: "warn", text: "Nivel de agua bajo" },
  SOMBRA_PROLONGADA: { keys: ["luz_nivel"], level: "warn", text: "Mucho rato sin sol pleno" },
  SOL_INSUFICIENTE: { keys: ["luz_nivel"], level: "warn", text: "El día cerró con poco sol" },
  FALLA_DHT: { keys: ["temp_c", "hum_aire_pct"], level: "down", text: "El sensor no responde" },
  FALLA_SUELO: { keys: ["suelo_pct"], level: "down", text: "El sensor no responde" },
  FALLA_NIVEL: { keys: ["nivel_pct"], level: "down", text: "El sensor no responde" },
};

export const STATE_LABELS = {
  ok: "Normal",
  warn: "Aviso",
  crit: "Alerta",
  stale: "Dato viejo",
  down: "Sensor caído",
  empty: "Sin datos",
};

/** Presentación de una métrica; si la API manda una nueva, usa lo que traiga. */
export function presentationFor(metric) {
  return METRICS[metric.key] || {
    label: metric.label || metric.key,
    unit: metric.unit || "",
    icon: "gauge",
    decimals: 1,
    reference: "",
  };
}

/** Alertas del Arduino que le tocan a una tarjeta: [{ code, level, text }]. */
export function alertsFor(key, codes = []) {
  return (codes || [])
    .filter((code) => ALERTS[code]?.keys.includes(key))
    .map((code) => ({ code, level: ALERTS[code].level, text: ALERTS[code].text }));
}

/** Códigos que el dashboard no conoce (firmware más nuevo que esta página). */
export function unknownAlerts(codes = []) {
  return (codes || []).filter((code) => !ALERTS[code]);
}

/**
 * Estado de una tarjeta. Se muestra UNO, en este orden de prioridad:
 *   empty  nunca ha llegado un valor
 *   down   la última lectura trae este sensor en null (se enseña el último válido)
 *   stale  el dato es más viejo que el intervalo configurado
 *   crit   el Arduino marcó una ALERTA para esta medición
 *   warn   el Arduino marcó un AVISO
 *   ok     nada de lo anterior
 * Un dato viejo nunca se pinta como alerta ni como normal: si el valor no es
 * reciente, la alerta tampoco lo es.
 *
 * @param metric        elemento de `metrics` de /readings/latest
 * @param codes         `last_reading.alertas`
 * @param ageSeconds    edad actual del dato (la del servidor + lo transcurrido)
 * @param staleAfter    segundos tras los que un dato es viejo
 */
export function metricState(metric, codes = [], ageSeconds = null, staleAfter = Infinity) {
  const hasValue = metric.value !== null && metric.value !== undefined;
  if (metric.sensor_down) {
    return {
      state: "down",
      detail: hasValue ? "No responde. Este es su último valor válido." : "No ha dado ninguna lectura válida.",
    };
  }
  if (!hasValue) return { state: "empty", detail: "Aún no hay lecturas." };

  const age = ageSeconds ?? metric.age_seconds ?? null;
  if (metric.stale || (age !== null && age > staleAfter)) {
    return { state: "stale", detail: "No han llegado lecturas nuevas." };
  }

  const mine = alertsFor(metric.key, codes);
  for (const level of ["crit", "warn"]) {
    const hits = mine.filter((alert) => alert.level === level);
    if (hits.length) return { state: level, detail: hits.map((a) => a.text).join(" · ") };
  }
  return { state: "ok", detail: "" };
}

/** "hace 12 s", "hace 3 min", "hace 2 h 5 min", "hace 4 d". */
export function formatAge(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  if (s < 2) return "ahora mismo";
  if (s < 60) return `hace ${s} s`;
  const minutes = Math.floor(s / 60);
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    const rest = minutes % 60;
    return rest ? `hace ${hours} h ${rest} min` : `hace ${hours} h`;
  }
  return `hace ${Math.floor(hours / 24)} d`;
}

/** Valor listo para mostrar. null/undefined → "—" (nunca 0: null = sensor caído). */
export function formatValue(metric) {
  if (metric.value === null || metric.value === undefined) return "—";
  const { decimals } = presentationFor(metric);
  const value = Number(metric.value);
  return Number.isInteger(value) && decimals === 0 ? String(value) : value.toFixed(decimals);
}

/**
 * Resumen de la última lectura tal como la reporta el Arduino:
 * [{ tone: "ok" | "warn" | "crit" | "plain", text }].
 * No son las alertas del sistema (eso es ALERT-01); es el dato del firmware.
 */
export function summarize(last) {
  if (!last) return [];
  const chips = [];
  if (last.estado === "OK") chips.push({ tone: "ok", text: "Arduino: todo en rango" });
  else if (last.estado === "AVISO") chips.push({ tone: "warn", text: "Arduino: aviso" });
  else if (last.estado === "ALERTA") chips.push({ tone: "crit", text: "Arduino: alerta" });

  if (last.riego_sugerido === true) chips.push({ tone: "warn", text: "Riego sugerido" });
  if (last.bomba_habilitada === false) chips.push({ tone: "crit", text: "Bomba bloqueada: falta agua" });
  if (last.vent === "ALTA") chips.push({ tone: "plain", text: "Ventilación alta" });
  else if (last.vent === "BASE") chips.push({ tone: "plain", text: "Ventilación base" });
  if (last.es_dia === true) chips.push({ tone: "plain", text: "De día" });
  else if (last.es_dia === false) chips.push({ tone: "plain", text: "De noche" });

  for (const code of unknownAlerts(last.alertas)) chips.push({ tone: "plain", text: code });
  return chips;
}
