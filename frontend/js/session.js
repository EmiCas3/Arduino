/**
 * Sesión en el navegador (decisión D5 del plan).
 *
 * El JWT vive en sessionStorage: se borra al cerrar la pestaña y no viaja en
 * cookies, así que no hay CSRF. El servidor es quien decide si la sesión
 * sigue viva (timeout por inactividad); aquí solo se guarda el token.
 */

const SESSION_KEY = "sg.session";
const FLASH_KEY = "sg.flash";
const KIOSK_KEY = "sg.kiosk_token";

let memorySession = null; // respaldo si el navegador bloquea sessionStorage

export function saveSession(loginResponse) {
  const session = {
    token: loginResponse.access_token,
    user: loginResponse.user,
    expiresAt: loginResponse.expires_at,
    idleMinutes: loginResponse.idle_timeout_minutes,
  };
  memorySession = session;
  try { sessionStorage.setItem(SESSION_KEY, JSON.stringify(session)); } catch { /* usa memoria */ }
  return session;
}

export function loadSession() {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (raw) return JSON.parse(raw);
  } catch { /* usa memoria */ }
  return memorySession;
}

export function getToken() {
  return loadSession()?.token ?? null;
}

export function clearSession() {
  memorySession = null;
  try { sessionStorage.removeItem(SESSION_KEY); } catch { /* nada que borrar */ }
  try { localStorage.removeItem(KIOSK_KEY); } catch { /* nada que borrar */ }
}

/** Activa y recuerda el token de solo lectura para pantalla externa (kiosko). */
export function activateKioskSession(token) {
  const clean = String(token || "").trim();
  if (!clean) return null;
  try { localStorage.setItem(KIOSK_KEY, clean); } catch { /* usa memoria/sesión */ }
  const session = { token: clean, user: null, expiresAt: null, idleMinutes: null, kiosk: true };
  memorySession = session;
  try { sessionStorage.setItem(SESSION_KEY, JSON.stringify(session)); } catch { /* usa memoria */ }
  return session;
}

export function loadKioskToken() {
  try { return localStorage.getItem(KIOSK_KEY); } catch { return null; }
}

/** Mensaje de una sola lectura para la siguiente pantalla (p. ej. el login). */
export function setFlash(kind, text) {
  try { sessionStorage.setItem(FLASH_KEY, JSON.stringify({ kind, text })); } catch { /* opcional */ }
}

export function takeFlash() {
  try {
    const raw = sessionStorage.getItem(FLASH_KEY);
    sessionStorage.removeItem(FLASH_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}
