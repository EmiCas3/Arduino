/**
 * Tema claro/oscuro. Por defecto sigue al sistema; el botón fija una
 * preferencia por navegador (localStorage, envuelto en try/catch).
 */
import { icon } from "./ui.js";

const KEY = "sg.theme";
const media = window.matchMedia("(prefers-color-scheme: dark)");

function currentTheme() {
  const explicit = document.documentElement.dataset.theme;
  if (explicit === "dark" || explicit === "light") return explicit;
  return media.matches ? "dark" : "light";
}

function paint(button) {
  const dark = currentTheme() === "dark";
  button.replaceChildren(icon(dark ? "sun" : "moon"));
  button.setAttribute("aria-label", dark ? "Cambiar a tema claro" : "Cambiar a tema oscuro");
  button.title = dark ? "Tema claro" : "Tema oscuro";
}

export function initThemeToggle(button) {
  if (!button) return;
  paint(button);
  button.addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem(KEY, next); } catch { /* sin almacenamiento: solo esta vista */ }
    paint(button);
  });
  media.addEventListener?.("change", () => paint(button));
}
