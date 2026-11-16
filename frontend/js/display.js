/**
 * Dashboard dedicado para pantalla externa en tiempo real (DASH-03).
 *
 * Pensado para conectarse a un monitor HDMI o LCD en la Raspberry Pi:
 * - Lectura rápida a distancia con valores grandes y alto contraste.
 * - Estado general del vivero, 6 sensores en vivo con barra visual, equipos y
 *   riego, recomendación de IA y alertas activas en una sola vista sin scroll
 *   complejo ni navegación lateral obligatoria.
 * - Refresco rápido automático cada 5 s (`DISPLAY_REFRESH_SECONDS`) en segundo
 *   plano (X-SG-Background: 1) y reloj local por segundo.
 * - Todo el DOM se construye de forma segura con h()/textContent.
 */
import { apiFetch } from "./api.js";
import {
  DISPLAY_REFRESH_SECONDS,
  POWER_LABELS,
  STATE_LABELS,
  alertTitle,
  describeAlert,
  describeRecommendation,
  displayOverallStatus,
  formatAge,
  formatValue,
  metricGaugePercent,
  metricState,
  powerView,
  presentationFor,
  titleWithCount,
} from "./metrics.js";
import { createPoller } from "./poller.js";
import { ACTUATOR_TYPES } from "./roles.js";
import { activateKioskSession, loadKioskToken, loadSession } from "./session.js";
import { initThemeToggle } from "./theme.js";
import { formatTime, h, icon } from "./ui.js";

const STATE_ICONS = {
  ok: "check",
  warn: "alert",
  crit: "alert",
  stale: "clock",
  down: "unplug",
  empty: "minus",
};

const POWER_ICONS = {
  on: "power",
  off: "power",
  unavailable: "lock",
  unknown: "help",
};

const clockFmt = new Intl.DateTimeFormat("es-MX", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

function isPermanent(error) {
  return error?.status === 403 || error?.status === 404;
}

/**
 * Monta el tablero en tiempo real de `greenhouseId` dentro de `root`.
 * @returns {() => void} detiene el refresco y restaura el título de la pestaña.
 */
export function mountDisplay(root, greenhouseId, { picker = null, kioskToggle = null } = {}) {
  const base = `/greenhouses/${encodeURIComponent(greenhouseId)}`;
  const id = encodeURIComponent(greenhouseId);

  let readings = null;
  let actuators = null;
  let irrigation = null;
  let alerts = null;
  let recommendation = null;
  let receivedAt = 0;
  let receivedClock = null;
  let problem = null;
  let stopped = false;
  let refreshing = false;

  const tiles = new Map();

  const statusBadge = h("div", { class: "display-status", role: "status", "aria-live": "polite" });
  const metaRow = h("div", { class: "display-meta" });
  const clockEl = h("time", { class: "display-clock", "aria-label": "Hora actual" }, clockFmt.format(new Date()));
  const updatedEl = h("span", { class: "display-updated" }, "Conectando…");
  const refreshBtn = h("button", {
    class: "btn btn-quiet btn-sm",
    type: "button",
    title: "Actualizar lecturas ahora",
    onclick: () => {
      if (!refreshing) poller.now();
    },
  }, icon("refresh", "icon-sm"), "Actualizar");

  const noticeEl = h("div", { class: "display-notice" });
  const metricsGrid = h("div", { class: "display-metrics", "aria-label": "Sensores en tiempo real" },
    Array.from({ length: 6 }, () =>
      h("div", { class: "display-tile display-tile-loading", "aria-hidden": "true" },
        h("div", { class: "skeleton-line", style: "width:55%" }),
        h("div", { class: "skeleton-line", style: "width:45%;height:40px" }),
        h("div", { class: "skeleton-line", style: "width:80%" }))));

  const equipPill = h("span", { class: "display-card-pill" });
  const equipBody = h("div", { class: "display-card-body" }, h("div", { class: "skeleton-line", style: "width:70%" }));

  const recPill = h("span", { class: "display-card-pill" });
  const recBody = h("div", { class: "display-card-body" }, h("div", { class: "skeleton-line", style: "width:75%" }));

  const alertsPill = h("span", { class: "display-card-pill" });
  const alertsBody = h("div", { class: "display-card-body" }, h("div", { class: "skeleton-line", style: "width:65%" }));

  const board = h("div", { class: "display-board" },
    h("header", { class: "display-header" },
      h("div", { class: "display-header-main" },
        statusBadge,
        h("div", { class: "display-title-block" },
          h("div", { class: "display-heading-row" },
            h("h2", { class: "display-title" }, `Vivero ${greenhouseId}`),
            picker),
          metaRow)),
      h("div", { class: "display-header-right" },
        clockEl,
        h("div", { class: "display-controls" },
          h("span", { class: "display-cadence" }, icon("pulse", "icon-sm"), `En vivo · ${DISPLAY_REFRESH_SECONDS} s`),
          updatedEl,
          refreshBtn,
          kioskToggle))),
    noticeEl,
    metricsGrid,
    h("div", { class: "display-strip" },
      h("section", { class: "display-card", "aria-label": "Equipos y riego" },
        h("div", { class: "display-card-head" },
          h("h3", {}, icon("power", "icon-sm"), "Equipos y riego"),
          equipPill),
        equipBody),
      h("section", { class: "display-card", "aria-label": "Recomendación de IA" },
        h("div", { class: "display-card-head" },
          h("h3", {}, icon("drop", "icon-sm"), "Recomendación IA"),
          recPill),
        recBody),
      h("section", { class: "display-card", "aria-label": "Alertas activas" },
        h("div", { class: "display-card-head" },
          h("h3", {}, icon("bell", "icon-sm"), "Alertas activas"),
          alertsPill),
        alertsBody)));

  root.replaceChildren(board);

  // ── Carga en paralelo ───────────────────────────────────────────────

  async function load({ background }) {
    if (stopped) return;
    refreshing = true;
    refreshBtn.disabled = true;
    try {
      const [rRes, aRes, iRes, alRes, recRes] = await Promise.allSettled([
        apiFetch(`/greenhouses/${id}/readings/latest`, { background }),
        apiFetch(`${base}/actuators/status`, { background }),
        apiFetch(`${base}/irrigation`, { background }),
        apiFetch(`${base}/alerts`, { params: { status: "active", limit: 10 }, background }),
        apiFetch(`${base}/irrigation/recommendation`, { background }),
      ]);
      if (stopped) return;

      const rejected = [rRes, aRes, iRes, alRes, recRes]
        .filter((res) => res.status === "rejected")
        .map((res) => res.reason);

      if (rejected.some((err) => err?.status === 401)) return; // api.js ya redirige al login

      if (rRes.status === "fulfilled") {
        readings = rRes.value;
        receivedAt = performance.now();
        receivedClock = new Date();
        problem = null;
      } else {
        problem = rRes.reason;
        if (isPermanent(problem)) poller.halt();
      }

      if (aRes.status === "fulfilled") actuators = aRes.value;
      else if (isPermanent(aRes.reason) && !problem) {
        problem = aRes.reason;
        poller.halt();
      }

      if (iRes.status === "fulfilled") irrigation = iRes.value;

      if (alRes.status === "fulfilled") alerts = alRes.value;

      if (recRes.status === "fulfilled") {
        recommendation = recRes.value;
      } else if (recRes.reason?.status === 503) {
        recommendation = null;
      }
    } finally {
      refreshing = false;
      if (!stopped) refreshBtn.disabled = false;
    }
    paint();
  }

  // ── Renderizado por componente ──────────────────────────────────────

  function ensureTile(metric) {
    let refs = tiles.get(metric.key);
    if (refs) return refs;
    const look = presentationFor(metric);
    const gaugeFill = h("div", { class: "display-gauge-fill" });
    const gauge = h("div", { class: "display-gauge", "aria-hidden": "true" }, gaugeFill);
    refs = {
      pill: h("span", { class: "metric-pill" }),
      value: h("span", { class: "display-value" }),
      gaugeFill,
      gauge,
      detail: h("span", { class: "display-tile-detail" }),
      ref: h("span", { class: "display-tile-ref" }),
    };
    refs.el = h("article", { class: "display-tile" },
      h("div", { class: "display-tile-top" },
        h("span", { class: "display-tile-label" }, icon(look.icon), look.label),
        refs.pill),
      h("div", { class: "display-reading" },
        refs.value,
        look.unit ? h("span", { class: "display-unit" }, look.unit) : null),
      gauge,
      h("div", { class: "display-tile-foot" }, refs.detail, refs.ref));
    tiles.set(metric.key, refs);
    return refs;
  }

  function paintTile(metric, codes, staleAfter, elapsed) {
    const refs = ensureTile(metric);
    const look = presentationFor(metric);
    const age = metric.age_seconds === null || metric.age_seconds === undefined
      ? null
      : metric.age_seconds + elapsed;
    const { state, detail } = metricState(metric, codes, age, staleAfter);
    const value = formatValue(metric);
    const pct = metricGaugePercent(metric);
    const ageText = age === null ? "sin lecturas" : formatAge(age);

    refs.el.dataset.state = state;
    refs.pill.replaceChildren(icon(STATE_ICONS[state], "icon-sm"), STATE_LABELS[state]);
    refs.value.textContent = value;
    refs.gauge.hidden = pct === null;
    if (pct !== null) refs.gaugeFill.style.width = `${pct}%`;

    refs.detail.textContent = detail || (age === null ? "Sin dato" : ageText);
    refs.ref.textContent = look.reference;
    refs.el.setAttribute("aria-label",
      `${look.label}: ${value === "—" ? "sin dato" : `${value} ${look.unit}`}. ${STATE_LABELS[state]}. ${ageText}.`);
    return refs.el;
  }

  function paintHeader(elapsed) {
    clockEl.textContent = clockFmt.format(new Date());
    const overall = displayOverallStatus({ readings, alerts, elapsed });
    statusBadge.dataset.state = overall.state;
    statusBadge.replaceChildren(
      icon(STATE_ICONS[overall.state]),
      h("div", { class: "display-status-text" },
        h("strong", {}, overall.label),
        h("span", {}, overall.detail)));

    const last = readings?.last_reading;
    const chips = [];
    if (last?.device_id) {
      chips.push(h("span", { class: "channel", title: "Dispositivo gateway" }, last.device_id));
    }
    if (last?.es_dia === true) chips.push(h("span", { class: "pill pill-plain" }, icon("sun", "icon-sm"), "De día"));
    else if (last?.es_dia === false) chips.push(h("span", { class: "pill pill-plain" }, icon("moon", "icon-sm"), "De noche"));

    if (last) {
      const age = (last.age_seconds ?? 0) + elapsed;
      chips.push(h("span", { class: "display-last-age" }, `Última lectura ${formatAge(age)}`));
    } else if (readings) {
      chips.push(h("span", { class: "display-last-age" }, "Aún no llegan lecturas"));
    }

    metaRow.replaceChildren(...chips);
    if (receivedAt > 0) {
      updatedEl.textContent = `Sync ${formatAge(elapsed)}`;
    }
  }

  function paintNotice() {
    if (!problem) {
      noticeEl.replaceChildren();
      return;
    }
    const text = readings && !isPermanent(problem)
      ? `Sin conexión con el servidor. Mostrando datos de las ${formatTime(receivedClock)}; reintentando cada ${DISPLAY_REFRESH_SECONDS} s.`
      : problem.message;
    noticeEl.replaceChildren(
      h("div", { class: "notice notice-warn", role: "status" },
        icon(readings ? "cloudOff" : "alert"),
        h("p", {}, text)));
  }

  function paintEquipment(elapsed) {
    if (irrigation) {
      const modeText = irrigation.mode === "manual" ? "Manual" : "Automático";
      equipPill.replaceChildren(
        h("span", { class: `pill ${irrigation.mode === "manual" ? "pill-info" : "pill-ok"}` }, `Modo ${modeText}`));
    } else {
      equipPill.replaceChildren();
    }

    const last = readings?.last_reading;
    const banners = [];
    if (last?.bomba_habilitada === false) {
      banners.push(h("span", { class: "pill pill-crit" }, icon("alert", "icon-sm"), "Bomba bloqueada: tanque bajo"));
    }
    if (last?.vent === "ALTA") {
      banners.push(h("span", { class: "pill pill-warn" }, icon("wind", "icon-sm"), "Ventilación ALTA"));
    } else if (last?.vent === "BASE") {
      banners.push(h("span", { class: "pill pill-plain" }, icon("wind", "icon-sm"), "Ventilación base"));
    }

    const bannerEls = banners.length ? [h("div", { class: "display-badges" }, banners)] : [];

    if (!actuators) {
      equipBody.replaceChildren(...bannerEls, h("p", { class: "muted" }, "Esperando estado de actuadores…"));
      return;
    }

    if (!actuators.actuators.length) {
      equipBody.replaceChildren(...bannerEls, h("p", { class: "muted" }, "Sin equipos registrados en este vivero."));
      return;
    }

    const staleAfter = actuators.stale_after_minutes * 60;
    const rows = actuators.actuators.map((item) => {
      const type = ACTUATOR_TYPES[item.type] || { label: item.type, icon: "chip" };
      const pv = powerView(item, elapsed, staleAfter);
      return h("li", { class: "display-equip-row" },
        h("span", { class: `equip-icon type-${item.type}` }, icon(type.icon, "icon-sm")),
        h("div", { class: "display-equip-info" },
          h("strong", {}, item.name),
          h("span", { class: "display-equip-sub" }, pv.note || type.label)),
        h("span", { class: "power", "data-power": pv.state, role: "status", "aria-label": `${item.name}: ${POWER_LABELS[pv.state]}` },
          icon(POWER_ICONS[pv.state], "icon-sm"),
          pv.label));
    });

    equipBody.replaceChildren(...bannerEls, h("ul", { class: "display-equip-list" }, rows));
  }

  function paintRecommendation() {
    const view = describeRecommendation(recommendation, formatTime);
    if (!view) {
      recPill.replaceChildren(h("span", { class: "pill pill-plain" }, "Sin recomendación"));
      recBody.replaceChildren(
        h("p", { class: "muted" }, "La recomendación de riego aparecerá cuando haya suficientes mediciones recientes."));
      return;
    }
    recPill.replaceChildren(h("span", { class: `pill ${view.pill.class}` }, view.pill.text));
    recBody.replaceChildren(
      h("div", { class: "display-rec", dataset: { tone: view.tone } },
        h("p", { class: "display-rec-summary" }, view.summary),
        view.reasons[0] ? h("p", { class: "display-rec-reason" }, view.reasons[0]) : null));
  }

  function paintAlerts(elapsed) {
    const active = alerts?.alerts?.filter((a) => a.status === "active") ?? [];
    const count = alerts?.active_count ?? active.length;
    if (alerts) {
      alertsPill.replaceChildren(count > 0
        ? h("span", { class: "pill pill-crit" }, count === 1 ? "1 activa" : `${count} activas`)
        : h("span", { class: "pill pill-ok" }, "Sin alertas"));
      document.title = titleWithCount(document.title, count);
    } else {
      alertsPill.replaceChildren();
    }

    if (!alerts) {
      alertsBody.replaceChildren(h("p", { class: "muted" }, "Consultando alertas…"));
      return;
    }

    if (!active.length) {
      alertsBody.replaceChildren(
        h("div", { class: "alert-empty display-alert-ok" },
          icon("check"),
          h("span", {}, h("strong", {}, "Sin alertas activas."), " Todo dentro de los límites del firmware.")));
      return;
    }

    const shown = active.slice(0, 4).map((alert) => {
      const look = presentationFor({ key: alert.metric, label: alert.label, unit: alert.unit });
      const text = describeAlert(alert, elapsed, formatTime);
      return h("li", { class: "display-alert-item" },
        h("span", { class: "alert-icon" }, icon("alert", "icon-sm")),
        h("div", { class: "display-alert-main" },
          h("strong", {}, alertTitle(alert)),
          h("span", { class: "display-alert-meta" }, `${look.label} · ${text.value}`),
          h("span", { class: "display-alert-since" }, text.since)));
    });

    const extra = active.length > 4
      ? [h("p", { class: "display-alert-more" }, `+ ${active.length - 4} alertas activas más`)]
      : [];

    alertsBody.replaceChildren(h("ul", { class: "display-alert-list" }, shown), ...extra);
  }

  function paint() {
    if (stopped) return;
    const elapsed = receivedAt > 0 ? (performance.now() - receivedAt) / 1000 : 0;
    paintNotice();
    paintHeader(elapsed);

    if (readings) {
      const codes = readings.last_reading?.alertas ?? [];
      const staleAfter = readings.stale_after_minutes * 60;
      const nodes = readings.metrics.map((metric) => paintTile(metric, codes, staleAfter, elapsed));
      if (metricsGrid.children.length !== nodes.length || nodes.some((n, i) => metricsGrid.children[i] !== n)) {
        metricsGrid.replaceChildren(...nodes);
      }
    } else if (problem) {
      metricsGrid.replaceChildren();
    }

    paintEquipment(elapsed);
    paintRecommendation();
    paintAlerts(elapsed);
  }

  // ── Ciclo de vida ───────────────────────────────────────────────────

  const poller = createPoller({ seconds: DISPLAY_REFRESH_SECONDS, run: load, onTick: paint });
  poller.now();

  return () => {
    stopped = true;
    poller.stop();
    document.title = titleWithCount(document.title, 0);
  };
}

// ── Arranque autónomo (frontend/display.html) ─────────────────────────

async function toggleFullscreen(button) {
  try {
    if (document.fullscreenElement) {
      await document.exitFullscreen();
    } else if (document.documentElement.requestFullscreen) {
      await document.documentElement.requestFullscreen();
    }
  } catch { /* el navegador o la Pi sin soporte bloqueó fullscreen */ }
  paintFullscreenBtn(button);
}

function paintFullscreenBtn(button) {
  if (!button) return;
  const active = Boolean(document.fullscreenElement);
  button.replaceChildren(icon(active ? "minimize" : "maximize", "icon-sm"), active ? "Salir pantalla completa" : "Pantalla completa");
  button.setAttribute("aria-pressed", String(active));
}

async function bootStandalone() {
  const container = document.getElementById("display-standalone");
  if (!container) return;

  initThemeToggle(document.getElementById("theme-toggle"));
  const fsBtn = document.getElementById("standalone-fullscreen");
  if (fsBtn) {
    paintFullscreenBtn(fsBtn);
    fsBtn.addEventListener("click", () => toggleFullscreen(fsBtn));
    document.addEventListener("fullscreenchange", () => paintFullscreenBtn(fsBtn));
  }

  const hashParams = new URLSearchParams(location.hash.replace(/^#\/?/, ""));
  const queryParams = new URLSearchParams(location.search);
  const kioskFromUrl = hashParams.get("kiosk") || queryParams.get("kiosk");
  if (kioskFromUrl) {
    activateKioskSession(kioskFromUrl);
    queryParams.delete("kiosk");
    const qs = queryParams.toString();
    history.replaceState(null, "", location.pathname + (qs ? `?${qs}` : ""));
  } else if (!loadSession()?.token) {
    const savedKiosk = loadKioskToken();
    if (savedKiosk) activateKioskSession(savedKiosk);
  }

  const session = loadSession();
  if (!session?.token) {
    location.replace("login.html");
    return;
  }

  if (session.kiosk) {
    const panelLink = document.getElementById("standalone-panel-link");
    if (panelLink) panelLink.hidden = true;
  }

  let user;
  try {
    user = await apiFetch("/auth/me");
  } catch (error) {
    if (error.status !== 401) {
      container.replaceChildren(
        h("div", { class: "notice notice-crit", role: "alert" },
          icon("alert"),
          h("p", {}, error.message || "No se pudo conectar con el servidor.")));
    }
    return;
  }

  let stop = null;
  const picker = h("div", { class: "greenhouse-picker" });
  const mountFor = (ghId) => {
    if (stop) stop();
    stop = mountDisplay(container, ghId, { picker });
  };

  if (user.greenhouse_id) {
    mountFor(user.greenhouse_id);
    return;
  }

  let greenhouses = [];
  try {
    greenhouses = await apiFetch("/greenhouses");
  } catch (error) {
    if (error.status !== 401) {
      container.replaceChildren(
        h("div", { class: "notice notice-crit", role: "alert" },
          icon("alert"),
          h("p", {}, error.message || "No se pudieron cargar los viveros.")));
    }
    return;
  }

  if (!greenhouses.length) {
    container.replaceChildren(
      h("div", { class: "empty" },
        h("strong", {}, "Aún no hay viveros con dispositivos registrados."),
        "Cuando se registre un gateway, su pantalla en vivo aparecerá aquí."));
    return;
  }

  const ids = greenhouses.map((g) => g.greenhouse_id);
  const requested = new URLSearchParams(location.search).get("greenhouse");
  const initial = requested && ids.includes(requested) ? requested : ids[0];

  if (ids.length > 1) {
    const select = h("select", {
      class: "input input-sm",
      id: "standalone-greenhouse-select",
      "aria-label": "Elegir vivero",
      onchange: (e) => mountFor(e.currentTarget.value),
    }, ids.map((id) => h("option", { value: id, selected: id === initial }, id)));
    picker.replaceChildren(select);
  }
  mountFor(initial);
}

bootStandalone();
