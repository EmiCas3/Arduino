/**
 * Pantalla de login (AUTH-01).
 *
 * - Entra con usuario o correo + contraseña (POST /api/v1/auth/login).
 * - Si las credenciales fallan muestra el mensaje genérico del servidor, que
 *   no dice qué campo estuvo mal.
 * - Si entra, guarda la sesión y lleva a la pantalla de inicio de su rol.
 */
import { apiFetch, ApiError } from "./api.js";
import { clearSession, loadSession, saveSession, takeFlash } from "./session.js";
import { landingPath } from "./roles.js";
import { initThemeToggle } from "./theme.js";
import { icon } from "./ui.js";

const form = document.getElementById("login-form");
const loginInput = document.getElementById("login");
const passwordInput = document.getElementById("password");
const toggle = document.getElementById("toggle-password");
const submit = document.getElementById("login-submit");
const errorBox = document.getElementById("login-error");
const notice = document.getElementById("login-notice");

initThemeToggle(document.getElementById("theme-toggle"));

function showBox(box, kind, text) {
  box.className = `notice notice-${kind}`;
  box.replaceChildren(icon(kind === "crit" ? "alert" : "info"), document.createTextNode(text));
  box.hidden = false;
}

function setLoading(loading) {
  submit.disabled = loading;
  submit.classList.toggle("is-loading", loading);
  submit.querySelector(".btn-label").textContent = loading ? "Entrando…" : "Entrar";
}

function markInvalid(invalid) {
  for (const input of [loginInput, passwordInput]) {
    if (invalid && !input.value.trim()) input.setAttribute("aria-invalid", "true");
    else input.removeAttribute("aria-invalid");
  }
}

// Mostrar / ocultar contraseña
function paintToggle() {
  const visible = passwordInput.type === "text";
  toggle.replaceChildren(icon(visible ? "eyeOff" : "eye"));
  toggle.setAttribute("aria-label", visible ? "Ocultar contraseña" : "Mostrar contraseña");
  toggle.setAttribute("aria-pressed", String(visible));
}
toggle.addEventListener("click", () => {
  passwordInput.type = passwordInput.type === "password" ? "text" : "password";
  paintToggle();
  passwordInput.focus();
});
paintToggle();

// Mensaje que dejó otra pantalla (sesión expirada, logout)
const flash = takeFlash();
if (flash) showBox(notice, flash.kind === "ok" ? "ok" : "info", flash.text);

// Si ya hay una sesión válida, no pedir login otra vez
(async () => {
  if (!loadSession()?.token) return;
  try {
    const user = await apiFetch("/auth/me");
    location.replace(landingPath(user.role));
  } catch {
    clearSession();
  }
})();

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.hidden = true;

  const login = loginInput.value.trim();
  const password = passwordInput.value;
  if (!login || !password) {
    markInvalid(true);
    showBox(errorBox, "crit", "Escribe tu usuario o correo y tu contraseña.");
    (login ? passwordInput : loginInput).focus();
    return;
  }
  markInvalid(false);
  setLoading(true);

  try {
    const data = await apiFetch("/auth/login", {
      method: "POST",
      body: { login, password },
      auth: false,
    });
    saveSession(data);
    location.replace(landingPath(data.user.role));
  } catch (error) {
    setLoading(false);
    const message = error instanceof ApiError ? error.message : "No se pudo iniciar sesión. Inténtalo de nuevo.";
    showBox(errorBox, "crit", message);
    passwordInput.select();
    passwordInput.focus();
  }
});

for (const input of [loginInput, passwordInput]) {
  input.addEventListener("input", () => input.removeAttribute("aria-invalid"));
}
