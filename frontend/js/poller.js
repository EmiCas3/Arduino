/**
 * Refresco periódico de los widgets de "Mi vivero" (ACT-04, ALERT-01).
 *
 * Mismas reglas que los widgets de DASH-01 (dashboard.js):
 * - Solo pide datos con la pestaña visible; al volver a ella, refresca ya.
 * - Un refresco automático va marcado como `background` (no renueva la
 *   sesión), salvo que la persona haya hecho clic, tecleado o tocado la
 *   pantalla desde el refresco anterior. Así el cierre por inactividad de
 *   AUTH-01 sigue funcionando con el dashboard abierto.
 * - `boost()` acelera el refresco un rato (después de mandar una orden, para
 *   ver la confirmación del Arduino sin esperar 15 s).
 *
 * Uso:
 *   const poller = createPoller({ seconds: 15, run: async ({ background }) => {…}, onTick: paint });
 *   poller.now();   // primera carga (cuenta como actividad)
 *   poller.setSeconds(300);  // cambiar el intervalo (p. ej. al cambiar de rango)
 *   poller.stop();  // al salir de la vista
 */
const ACTIVITY_EVENTS = ["pointerdown", "keydown", "touchstart"];

export function createPoller({ seconds, run, onTick = null, tickSeconds = 1 }) {
  let stopped = false;
  let halted = false;
  let busy = false;
  let queued = false;
  let interacted = false;
  let fastTimer = null;
  let fastUntil = 0;

  async function execute(automatic) {
    if (stopped) return;
    if (busy) {           // ya hay una petición en camino: se repite al terminar
      queued = queued || !automatic;
      return;
    }
    busy = true;
    const background = automatic && !interacted;
    interacted = false;
    try {
      await run({ background });
    } catch (error) {
      // run() maneja sus propios errores; esto solo evita un rechazo sin atender.
      console.error(error);
    } finally {
      busy = false;
    }
    if (queued && !stopped) {
      queued = false;
      execute(false);
    }
  }

  const visible = () => document.visibilityState === "visible";
  const markActive = () => { interacted = true; };
  const onVisibility = () => { if (visible() && !halted) execute(true); };

  const schedule = (every) => setInterval(() => { if (visible()) execute(true); }, every * 1000);
  let poll = schedule(seconds);
  const tick = onTick ? setInterval(onTick, tickSeconds * 1000) : null;
  document.addEventListener("visibilitychange", onVisibility);
  for (const name of ACTIVITY_EVENTS) {
    document.addEventListener(name, markActive, { capture: true, passive: true });
  }

  function stopFast() {
    if (fastTimer) clearInterval(fastTimer);
    fastTimer = null;
  }

  return {
    /** Pide los datos ya (un clic de la persona: sí cuenta como actividad). */
    now() { execute(false); },

    /** Refresca cada `every` segundos durante `forSeconds` (tras una orden). */
    boost(forSeconds = 40, every = 3) {
      if (stopped || halted) return;
      fastUntil = Date.now() + forSeconds * 1000;
      if (fastTimer) return;
      fastTimer = setInterval(() => {
        if (Date.now() > fastUntil) stopFast();
        else if (visible()) execute(true);
      }, every * 1000);
    },

    /** Cambia cada cuánto se refresca (cuenta desde ahora). */
    setSeconds(every) {
      if (stopped || halted || !(every > 0)) return;
      clearInterval(poll);
      poll = schedule(every);
    },

    /** Deja de pedir datos pero conserva lo dibujado (errores permanentes). */
    halt() {
      halted = true;
      clearInterval(poll);
      poll = null;
      stopFast();
      document.removeEventListener("visibilitychange", onVisibility);
    },

    /** Al salir de la vista: no deja nada corriendo. */
    stop() {
      stopped = true;
      this.halt();
      if (tick) clearInterval(tick);
      for (const name of ACTIVITY_EVENTS) {
        document.removeEventListener(name, markActive, { capture: true });
      }
    },
  };
}
