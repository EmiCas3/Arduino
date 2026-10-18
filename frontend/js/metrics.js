/**
 * Lógica de los widgets de "Mi vivero", sin DOM ni red: qué estado tiene cada
 * medición (DASH-01), cada equipo (ACT-04) y cada alerta (ALERT-01), y cómo se
 * escribe. Al no tocar el navegador se puede probar con
 * `node --test frontend/tests/metrics.test.mjs`.
 *
 * De dónde sale el estado de cada tarjeta: de las `alertas` que el Arduino
 * manda en cada lectura, con la severidad que les da el firmware (AVISO o
 * ALERTA). Los umbrales viven en el firmware: aquí NO se comparan valores, y
 * si el firmware cambia un umbral, el dashboard lo refleja solo. Las alertas
 * con historia (ALERT-01) las crea el backend con esos mismos códigos.
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
  co2_ppm: { label: "CO₂", unit: "ppm", icon: "co2", decimals: 0, reference: "ideal 400–1000 ppm" },
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
  CO2_ALTO: { keys: ["co2_ppm"], level: "crit", text: "CO₂ muy alto" },
  CO2_BAJO: { keys: ["co2_ppm"], level: "crit", text: "CO₂ muy bajo" },
  CO2_ALTO_LEVE: { keys: ["co2_ppm"], level: "warn", text: "CO₂ un poco alto" },
  CO2_BAJO_LEVE: { keys: ["co2_ppm"], level: "warn", text: "CO₂ un poco bajo" },
  FALLA_DHT: { keys: ["temp_c", "hum_aire_pct"], level: "down", text: "El sensor no responde" },
  FALLA_SUELO: { keys: ["suelo_pct"], level: "down", text: "El sensor no responde" },
  FALLA_NIVEL: { keys: ["nivel_pct"], level: "down", text: "El sensor no responde" },
  FALLA_CO2: { keys: ["co2_ppm"], level: "down", text: "El sensor no responde" },
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

// ── Estado de los equipos (ACT-04) ────────────────────────────────────

export const POWER_LABELS = {
  on: "Encendido",
  off: "Apagado",
  unavailable: "No disponible",
  unknown: "Desconocido",
};

/**
 * Cómo se muestra un actuador de /actuators/status.
 *
 * La confirmación del Arduino envejece también en el navegador: si entre dos
 * refrescos pasa el límite (`staleAfter`), el equipo cambia a "Desconocido".
 * Un equipo sin confirmación NUNCA se muestra como apagado (criterio 3).
 *
 * @param item        elemento de `actuators`
 * @param elapsed     segundos desde que llegó la respuesta
 * @param staleAfter  segundos tras los que una confirmación ya no vale
 * @returns {{ state, label, note, age }}
 */
export function powerView(item, elapsed = 0, staleAfter = Infinity) {
  const age = item.age_seconds === null || item.age_seconds === undefined
    ? null
    : item.age_seconds + elapsed;
  let state = POWER_LABELS[item.state] ? item.state : "unknown";
  let note = item.detail || "";

  // on/off/no disponible por tanque necesitan una confirmación con hora.
  const needsReport = state === "on" || state === "off" || item.reason === "tank_low";
  if (needsReport && age === null) {
    state = "unknown";
    note = "El Arduino aún no ha confirmado su estado.";
  } else if (needsReport && age > staleAfter) {
    state = "unknown";
    note = `Última confirmación ${formatAge(age)}.`;
  } else if (state === "unknown" && item.reason === "stale" && age !== null) {
    note = `Última confirmación ${formatAge(age)}.`;
  } else if (item.reason === "reported") {
    note = `Confirmado ${formatAge(age)}`;
  }
  return { state, label: POWER_LABELS[state], note, age };
}

// ── Alertas (ALERT-01) ────────────────────────────────────────────────

/** Valor con su unidad, con los decimales de la tarjeta: "31.2 °C", "20 %". */
export function formatMeasure(metricKey, value, unit = "") {
  if (value === null || value === undefined) return "—";
  const look = METRICS[metricKey];
  const text = formatValue({ key: metricKey, value });
  return `${text} ${look ? look.unit : unit}`.trim();
}

/** "45 s", "18 min", "2 h 5 min", "3 d". */
export function formatDuration(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  if (s < 60) return `${s} s`;
  const minutes = Math.floor(s / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  return `${Math.floor(hours / 24)} d`;
}

/** Título de una alerta: el mismo texto que usa la tarjeta para ese código. */
export function alertTitle(alert) {
  return ALERTS[alert.code]?.text || `${alert.label || alert.metric}: fuera de límite`;
}

/**
 * Textos de una alerta de /alerts, listos para dibujar:
 *   value   "31.2 °C (límite 30.0 °C)"
 *   since   "desde las 14:02 · hace 3 min" (activa) o "se normalizó a las 14:20 · duró 18 min"
 *   extra   "ahora 31.0 °C · peor 31.6 °C" (solo activas, si cambió)
 * `elapsed` son los segundos desde que llegó la respuesta; `clock` formatea horas.
 */
export function describeAlert(alert, elapsed = 0, clock = (iso) => iso) {
  const measure = (value) => formatMeasure(alert.metric, value, alert.unit);
  const value = `${measure(alert.value)} (límite ${measure(alert.threshold)})`;
  if (alert.status === "cleared") {
    return {
      value,
      since: `se normalizó a las ${clock(alert.cleared_ts)} · duró ${formatDuration(alert.duration_seconds)}`,
      extra: "",
    };
  }
  const extras = [];
  if (alert.last_value !== alert.value) extras.push(`ahora ${measure(alert.last_value)}`);
  if (alert.worst_value !== alert.value && alert.worst_value !== alert.last_value) {
    extras.push(`peor ${measure(alert.worst_value)}`);
  }
  return {
    value,
    since: `desde las ${clock(alert.reading_ts)} · ${formatAge(alert.duration_seconds + elapsed)}`,
    extra: extras.join(" · "),
  };
}

/** Alertas activas que no estaban en `knownIds` (para avisar de las nuevas). */
export function newActiveAlerts(knownIds, alerts = []) {
  return alerts.filter((alert) => alert.status === "active" && !knownIds.has(alert.id));
}

/**
 * Qué se muestra en la tarjeta: todas las activas y hasta `maxCleared`
 * cerradas en las últimas `recentHours` horas (las más recientes primero).
 */
export function visibleAlerts(alerts = [], { maxCleared = 5, recentHours = 24, now = Date.now() } = {}) {
  const active = alerts.filter((alert) => alert.status === "active");
  const since = now - recentHours * 3600 * 1000;
  const cleared = alerts
    .filter((alert) => alert.status === "cleared" && Date.parse(alert.cleared_ts) >= since)
    .slice(0, maxCleared);
  return { active, cleared };
}

/** Título de la pestaña con el número de alertas activas: "(2) Mi vivero · …". */
export function titleWithCount(title, count) {
  const base = String(title || "").replace(/^\(\d+\)\s+/, "");
  return count > 0 ? `(${count}) ${base}` : base;
}

// ── Riego: modo, órdenes y acciones (ACT-02) ──────────────────────────

const COMMAND_ERRORS = {
  TANQUE: "el tanque está bajo",
  MODO: "estaba en automático",
  FORMATO: "la orden no era válida",
};
const ORDER_TEXT = { ON: "regar", OFF: "detener", AUTO: "pasar a automático", MANUAL: "pasar a manual" };

/**
 * Qué decir de la última orden (de bomba o de modo): null si no hay nada
 * que avisar (ya se aplicó o es vieja). { tone: "wait" | "warn", text }.
 */
export function commandFeedback(command) {
  if (!command) return null;
  const what = ORDER_TEXT[command.value] || command.value;
  if (command.status === "pending" || command.status === "sent") {
    return { tone: "wait", text: `Orden enviada (${what}). Esperando al Arduino…` };
  }
  if (command.status === "rejected") {
    const why = COMMAND_ERRORS[command.error] || command.error || "sin motivo";
    return { tone: "warn", text: `El Arduino no aplicó la orden (${what}): ${why}.` };
  }
  if (command.status === "expired") {
    return { tone: "warn", text: `El Arduino no confirmó la orden (${what}). Revisa que el gateway y el Arduino estén conectados.` };
  }
  return null;
}

/** Explicación del modo de riego con los números del firmware. */
export function modeHelp(irrigation) {
  if (!irrigation) return "";
  const { target_pct: target, max_run_seconds: max } = irrigation.firmware;
  return irrigation.mode === "manual"
    ? `Manual: el Arduino no riega solo. Solo actúan las órdenes (cada riego dura como máximo ${max} s).`
    : `Automático: el Arduino riega solo cuando la tierra baja de ${target} % y para al llegar (máximo ${max} s por riego).`;
}

const EVENT_NOUNS = {
  pump: ["Bomba encendida", "Bomba apagada"],
  fan: ["Ventilador encendido", "Ventilador apagado"],
};

/** Una acción de /actuators/events, lista para mostrar. */
export function eventText(event) {
  const nouns = EVENT_NOUNS[event.actuator_type] || [`${event.actuator_name}: encendido`, `${event.actuator_name}: apagado`];
  const manual = event.origin === "manual";
  return {
    what: event.event === "on" ? nouns[0] : nouns[1],
    origin: manual ? (event.user_name ? `Manual · ${event.user_name}` : "Manual") : "Automático",
    manual,
    reason: event.reason || "",
  };
}
