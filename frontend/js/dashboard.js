/**
 * Widgets de "Estado actual" (DASH-01): el último valor de cada sensor con su
 * unidad, área, tiempo desde la lectura y estado, refrescado cada 15 s.
 *
 * Reglas:
 * - El estado de cada tarjeta lo decide metrics.js a partir de las alertas del
 *   Arduino; aquí solo se dibuja. Todo el DOM se arma con h()/textContent.
 * - "Hace N s" usa la edad que calculó el SERVIDOR más el tiempo transcurrido
 *   en el navegador: no depende de que el reloj de la laptop coincida con el
 *   de la Raspberry Pi.
 * - El refresco automático va marcado como `background` y no renueva la
 *   sesión, salvo que la persona haya hecho clic, tecleado o tocado la
 *   pantalla desde el refresco anterior. Así el cierre por inactividad de
 *   AUTH-01 sigue funcionando con el dashboard abierto.
 * - Si falla la red se conservan los últimos valores y se avisa; nunca se
 *   muestra un 0 ni un valor viejo como si fuera actual.
 */
import { apiFetch } from "./api.js";
import {
  REFRESH_SECONDS,
  STATE_LABELS,
  formatAge,
  formatValue,
  metricState,
  presentationFor,
  summarize,
} from "./metrics.js";
import { formatDateTime, formatTime, h, icon } from "./ui.js";

const STATE_ICONS = {
  ok: "check",
  warn: "alert",
  crit: "alert",
  stale: "clock",
  down: "unplug",
  empty: "minus",
};
const ACTIVITY_EVENTS = ["pointerdown", "keydown", "touchstart"];

/** Errores que no se arreglan reintentando (permisos, vivero inexistente). */
function isPermanent(error) {
  return error.status === 403 || error.status === 404;
}

/**
 * Monta los widgets en `root` y empieza a refrescar.
 * @returns {() => void} función para detener el refresco al salir de la vista.
 */
export function mountLiveReadings(root, greenhouseId) {
  const path = `/greenhouses/${encodeURIComponent(greenhouseId)}/readings/latest`;
  let data = null;         // última respuesta correcta
  let receivedAt = 0;      // performance.now() cuando llegó
  let receivedClock = null; // hora local en que llegó (solo para el aviso sin conexión)
  let problem = null;      // último error; los datos se conservan
  let loading = false;
  let stopped = false;
  let interacted = false;
  const cards = new Map();

  const updated = h("span", { class: "live-updated" }, "Cargando lecturas…");
  const refreshButton = h("button", {
    class: "btn btn-quiet btn-sm",
    type: "button",
    title: "Pedir las lecturas ahora",
    onclick: () => refresh(false),
  }, icon("refresh", "icon-sm"), "Actualizar");
  const summary = h("div", { class: "reading-summary" });
  const notice = h("div", { class: "live-notice" });
  const grid = h("div", { class: "metrics" },
    Array.from({ length: 5 }, () => h("div", { class: "metric metric-loading", "aria-hidden": "true" },
      h("div", { class: "skeleton-line", style: "width:60%" }),
      h("div", { class: "skeleton-line", style: "width:40%;height:28px" }),
      h("div", { class: "skeleton-line", style: "width:75%" }))));

  root.replaceChildren(
    h("div", { class: "live-bar" },
      h("span", { class: "live-hint" }, icon("clock", "icon-sm"), `Se actualiza solo cada ${REFRESH_SECONDS} s`),
      h("div", { class: "live-actions" }, updated, refreshButton)),
    notice,
    summary,
    grid,
  );

  // ── Datos ───────────────────────────────────────────────────────────

  async function refresh(automatic) {
    if (stopped || loading) return;
    loading = true;
    refreshButton.disabled = true;
    // Un refresco automático solo cuenta como actividad si hubo interacción real.
    const background = automatic && !interacted;
    interacted = false;
    try {
      const fresh = await apiFetch(path, { background });
      if (stopped) return;
      data = fresh;
      receivedAt = performance.now();
      receivedClock = new Date();
      problem = null;
    } catch (error) {
      if (stopped || error.status === 401) return;   // 401: api.js ya regresa al login
      problem = error;
      if (isPermanent(error)) halt();
    } finally {
      loading = false;
      refreshButton.disabled = false;
    }
    paint();
  }

  // ── Dibujo ──────────────────────────────────────────────────────────

  function elapsedSeconds() {
    return (performance.now() - receivedAt) / 1000;
  }

  function ensureCard(metric) {
    let refs = cards.get(metric.key);
    if (refs) return refs;
    const look = presentationFor(metric);
    refs = {
      pill: h("span", { class: "metric-pill" }),
      value: h("span", { class: "metric-value" }),
      detail: h("p", { class: "metric-detail" }),
      age: h("span", { class: "metric-age" }),
      area: h("span", {}),
      reference: h("span", {}),
      raw: h("span", {}),
    };
    refs.el = h("article", { class: "metric" },
      h("div", { class: "metric-top" },
        h("span", { class: "metric-label" }, icon(look.icon, "icon-sm"), look.label),
        refs.pill),
      h("div", { class: "metric-reading" }, refs.value, h("span", { class: "metric-unit" }, look.unit)),
      refs.detail,
      h("div", { class: "metric-meta" }, refs.age, refs.area),
      h("div", { class: "metric-ideal" }, refs.reference, refs.raw));
    cards.set(metric.key, refs);
    return refs;
  }

  function paintCard(metric, codes, staleAfter, elapsed) {
    const refs = ensureCard(metric);
    const look = presentationFor(metric);
    const age = metric.age_seconds === null || metric.age_seconds === undefined
      ? null
      : metric.age_seconds + elapsed;
    const { state, detail } = metricState(metric, codes, age, staleAfter);
    const value = formatValue(metric);
    const ageText = age === null ? "sin lecturas" : formatAge(age);

    refs.el.dataset.state = state;
    refs.pill.replaceChildren(icon(STATE_ICONS[state], "icon-sm"), STATE_LABELS[state]);
    refs.value.textContent = value;
    refs.detail.textContent = detail;
    refs.detail.hidden = !detail;
    // Sin ninguna lectura no hay "hace cuánto": el estado ya lo dice.
    refs.age.textContent = age === null ? "" : state === "down" ? `último dato ${ageText}` : ageText;
    refs.age.title = metric.ts ? `Lectura del ${formatDateTime(metric.ts)}` : "";
    refs.area.textContent = `área ${metric.area || "general"}`;
    refs.area.title = [metric.device_id, metric.device_location].filter(Boolean).join(" · ");
    refs.reference.textContent = look.reference;
    refs.raw.textContent = metric.raw === null || metric.raw === undefined ? "" : `crudo ${metric.raw}`;
    refs.raw.title = "Lectura cruda del sensor (0–1023), antes de calibrar";
    refs.el.setAttribute("aria-label",
      `${look.label}: ${value === "—" ? "sin dato" : `${value} ${look.unit}`}. ${STATE_LABELS[state]}. ${ageText}.`);
    return refs.el;
  }

  function paintSummary(elapsed, staleAfter) {
    const last = data.last_reading;
    if (!last) {
      summary.replaceChildren(h("div", { class: "empty" },
        h("strong", {}, "Este vivero aún no tiene lecturas."),
        "En cuanto la Raspberry Pi envíe la primera, aparecerá aquí sin recargar la página."));
      return;
    }
    const tones = { ok: "pill-ok", warn: "pill-warn", crit: "pill-crit", plain: "pill-plain" };
    const age = last.age_seconds + elapsed;
    // Si la última lectura ya es vieja, lo que dijo el Arduino tampoco es
    // actual: se avisa y sus chips se muestran sin color.
    const old = last.stale || age > staleAfter;
    summary.replaceChildren(
      h("span", { class: "summary-when", title: `Lectura del ${formatDateTime(last.ts)}` },
        "Última lectura ", h("strong", {}, formatAge(age))),
      h("span", { class: "channel", title: "Dispositivo que la envió" }, last.device_id),
      h("div", { class: "summary-chips" },
        old ? h("span", { class: "pill pill-warn" }, "Sin lecturas nuevas") : null,
        summarize(last).map((chip) =>
          h("span", { class: `pill ${old ? "pill-plain" : tones[chip.tone]}` }, chip.text))));
  }

  function paintNotice() {
    if (!problem) {
      notice.replaceChildren();
      return;
    }
    const body = data && !isPermanent(problem)
      ? `Sin conexión con el servidor. Se muestran los datos de las ${formatTime(receivedClock)}; se reintenta solo.`
      : problem.message;
    notice.replaceChildren(h("div", { class: "notice notice-warn", role: "status" },
      icon(data ? "cloudOff" : "alert"), h("p", {}, body)));
  }

  function paint() {
    if (stopped) return;
    paintNotice();
    if (!data) {
      if (problem) {
        grid.replaceChildren();
        updated.textContent = "";
      }
      return;
    }
    const elapsed = elapsedSeconds();
    const codes = data.last_reading?.alertas ?? [];
    const staleAfter = data.stale_after_minutes * 60;
    const nodes = data.metrics.map((metric) => paintCard(metric, codes, staleAfter, elapsed));
    // Solo se reacomoda el DOM si cambió la lista de métricas (primera carga).
    if (grid.children.length !== nodes.length || nodes.some((node, i) => grid.children[i] !== node)) {
      grid.replaceChildren(...nodes);
    }
    paintSummary(elapsed, staleAfter);
    updated.textContent = `Actualizado ${formatAge(elapsed)}`;
  }

  // ── Ciclo de vida ───────────────────────────────────────────────────

  const markActive = () => { interacted = true; };
  const onVisibility = () => { if (document.visibilityState === "visible") refresh(true); };
  // Con la pestaña oculta no se pide nada; al volver se refresca de inmediato.
  let poll = setInterval(() => {
    if (document.visibilityState === "visible") refresh(true);
  }, REFRESH_SECONDS * 1000);
  const tick = setInterval(paint, 1000);
  document.addEventListener("visibilitychange", onVisibility);
  for (const name of ACTIVITY_EVENTS) {
    document.addEventListener(name, markActive, { capture: true, passive: true });
  }

  /** Deja de pedir datos, pero conserva lo dibujado (errores permanentes). */
  function halt() {
    clearInterval(poll);
    poll = null;
    document.removeEventListener("visibilitychange", onVisibility);
    refreshButton.hidden = true;
  }

  function stop() {
    stopped = true;
    halt();
    clearInterval(tick);
    for (const name of ACTIVITY_EVENTS) {
      document.removeEventListener(name, markActive, { capture: true });
    }
  }

  refresh(false);   // la primera carga sí es actividad del usuario
  return stop;
}
