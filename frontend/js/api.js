/**
 * Cliente de la API REST (/api/v1).
 *
 * - Agrega Authorization: Bearer <jwt> a las llamadas autenticadas.
 * - 401 en una llamada autenticada = la sesión terminó (inactividad, logout,
 *   vencimiento): borra el token y regresa al login con el mensaje del servidor.
 * - 403 = el rol no tiene permiso. El control real está en el servidor; la UI
 *   solo muestra el aviso.
 */
import { clearSession, getToken, setFlash } from "./session.js";

const API_BASE = "/api/v1";

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

const DEFAULT_MESSAGES = {
  0: "No hay conexión con el servidor. Revisa que el backend esté encendido e inténtalo de nuevo.",
  401: "Tu sesión terminó. Inicia sesión de nuevo.",
  403: "No tienes permiso para realizar esta acción.",
  404: "No encontramos lo que buscas.",
  422: "Revisa los datos enviados.",
  500: "El servidor tuvo un problema. Inténtalo de nuevo en un momento.",
};

export function goToLogin() {
  location.replace("login.html");
}

export async function apiFetch(path, { method = "GET", body, auth = true, params } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  let url = API_BASE + path;
  if (params) {
    const query = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")
    );
    if ([...query].length) url += `?${query}`;
  }

  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "network_error", DEFAULT_MESSAGES[0]);
  }

  if (response.status === 204) return null;

  let data = null;
  try { data = await response.json(); } catch { /* respuesta sin JSON */ }

  if (!response.ok) {
    const detail = data && typeof data.detail === "object" && !Array.isArray(data.detail) ? data.detail : null;
    const code = detail?.error || (response.status === 422 ? "validation_error" : "http_error");
    const message = detail?.message || DEFAULT_MESSAGES[response.status] || DEFAULT_MESSAGES[500];

    if (response.status === 401 && auth) {
      clearSession();
      setFlash("info", message);
      goToLogin();
    }
    throw new ApiError(response.status, code, message);
  }
  return data;
}
