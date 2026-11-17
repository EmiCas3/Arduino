/**
 * Tarjeta "Recomendación de riego" de "Mi vivero" (AI-04, AI-05 y AI-07).
 *
 * - Muestra qué se recomienda, por qué en lenguaje claro y, al abrir "Datos
 *   que se usaron", las entradas exactas (criterios 1 y 2 de AI-05).
 * - Una recomendación sin explicación no se dibuja (criterio 4): si la API
 *   responde 503 o llega sin texto, se quita también la que estaba en pantalla.
 * - "Aceptar" / "Rechazar" guardan la decisión con quién y cuándo (AI-07).
 *   Aceptar no riega: si recomendaba regar aparece "Regar ahora", que pide
 *   confirmación y manda la orden manual de la bomba (ACT-02). Si el riego
 *   está en automático, la API lo dice y se muestra ese mensaje.
 * - "Recomendaciones anteriores" son las guardadas, con sus propias entradas
 *   y su decisión: una rechazada sigue ahí (criterio 3 de AI-07).
 * - Se refresca sola cada minuto (poller.js): las mediciones salen de una
 *   mediana de 5 min, no tiene caso pedirla cada 15 s.
 * - Todo el DOM se arma con h()/textContent.
 */
import { apiFetch } from "./api.js";
import { commandFeedback, describeRecommendation, formatAge } from "./metrics.js";
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
  // AI-07: una acción a la vez, el paso de confirmar el riego y su resultado.
  let busy = false;
  let confirming = false;
  let feedback = null;   // { tone: "ok" | "warn", text }

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
      if (fresh.id !== view?.id) {
        // Llegó otra recomendación: lo que se decidía era sobre la anterior.
        confirming = false;
        feedback = null;
      }
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

  async function act(work) {
    if (busy) return;
    busy = true;
    paint();
    try {
      feedback = await work();
    } catch (error) {
      if (error.status === 401) return;              // api.js ya regresa al login
      feedback = { tone: "warn", text: error.message };
    } finally {
      busy = false;
    }
    await load({ background: false });
  }

  function decide(decision) {
    const id = view.id;
    confirming = false;
    act(async () => {
      await apiFetch(`${base}/recommendations/${id}/decision`, { method: "POST", body: { decision } });
      return { tone: "ok", text: decision === "accepted" ? "Guardaste que la aceptas." : "Guardaste que la rechazas." };
    });
  }

  function irrigate() {
    confirming = false;
    act(async () => {
      const status = await apiFetch(`/greenhouses/${encodeURIComponent(greenhouseId)}/actuators/status`);
      const pump = status.actuators.find((item) => item.type === "pump");
      if (!pump) return { tone: "warn", text: "Este vivero no tiene una bomba registrada." };
      const command = await apiFetch(`/actuators/${encodeURIComponent(pump.actuator_id)}/commands`, {
        method: "POST", body: { action: "on" },
      });
      const result = commandFeedback(command);
      return { tone: result?.tone === "warn" ? "warn" : "ok", text: result?.text ?? "Orden enviada a la bomba." };
    });
  }

  function button(label, { primary = false, onclick }) {
    return h("button", {
      type: "button", class: `btn btn-sm${primary ? " btn-primary" : ""}`, disabled: busy, onclick,
    }, label);
  }

  function decisionSection() {
    const decision = view.decision;
    let actions = null;
    if (decision.canDecide) {
      actions = h("div", { class: "rec-actions", role: "group", "aria-label": "Tu decisión" },
        h("span", { class: "rec-ask" }, "¿Qué decides?"),
        button("Aceptar", { primary: true, onclick: () => decide("accepted") }),
        button("Rechazar", { onclick: () => decide("rejected") }));
    } else if (decision.canIrrigate) {
      actions = h("div", { class: "rec-actions" },
        confirming
          ? [h("span", { class: "rec-ask" }, "¿Encender la bomba ahora?"),
             button("Sí, regar", { primary: true, onclick: irrigate }),
             button("Cancelar", { onclick: () => { confirming = false; paint(); } })]
          : button("Regar ahora", { primary: true, onclick: () => { confirming = true; feedback = null; paint(); } }));
    }
    return h("div", { class: "rec-decision" },
      decision.pill
        ? h("p", { class: "rec-decided" },
            h("span", { class: `pill ${decision.pill.class}` }, decision.pill.text), " ", decision.text)
        : null,
      actions,
      feedback
        ? h("p", { class: `rec-feedback`, dataset: { tone: feedback.tone }, role: "status" }, feedback.text)
        : null);
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
        item.view.decision.pill
          ? h("p", { class: "rec-decided" },
              h("span", { class: `pill ${item.view.decision.pill.class}` }, item.view.decision.pill.text),
              " ", item.view.decision.text)
          : null,
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
      decisionSection(),
      h("details", {
        class: "rec-details",
        open: inputsOpen,
        ontoggle: (event) => { inputsOpen = event.currentTarget.open; },
      },
        h("summary", {}, "Datos que se usaron"),
        inputsTable(view)),
      ...(previous ? [previous] : []),
      h("p", { class: "anomaly-method" }, "Es una recomendación: aceptarla no riega. La decisión de regar sigue siendo tuya o del modo automático."));

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
