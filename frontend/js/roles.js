/**
 * Roles y secciones del frontend (AUTH-02, parte de UI).
 *
 * ÚNICA fuente en el frontend del mapa rol → secciones del menú y rol →
 * pantalla de inicio. Ocultar opciones NO es control de acceso: cada acción
 * la valida el servidor con require_roles() (backend/app/dependencies.py).
 * Si cambias permisos, cámbialos primero en el backend y luego aquí.
 */

export const ROLE_LABELS = {
  producer: "Productor",
  admin: "Administrador",
  super_admin: "Super Administrador",
};

const ALL = ["producer", "admin", "super_admin"];
const ADMINS = ["admin", "super_admin"];

export const SECTIONS = [
  { id: "inicio", label: "Mi vivero", icon: "sprout", roles: ALL },
  { id: "configuracion", label: "Configuración", icon: "sliders", roles: ADMINS },
  { id: "plataforma", label: "Plataforma", icon: "shield", roles: ["super_admin"] },
];

/** Pantalla a la que llega cada rol después del login. */
export const LANDING = {
  producer: "inicio",
  admin: "configuracion",
  super_admin: "configuracion",
};

/** Espejo informativo de las reglas del servidor, para mostrarlas en la UI. */
export const PERMISSIONS = [
  { label: "Ver el estado de su vivero", roles: ALL },
  { label: "Ver los equipos activos de su vivero", roles: ALL },
  { label: "Consultar cualquier vivero", roles: ADMINS },
  { label: "Ver equipos desactivados", roles: ADMINS },
  { label: "Registrar, editar y desactivar equipos", roles: ADMINS },
  { label: "Administrar roles y bitácora (próximamente)", roles: ["super_admin"] },
];

export const ACTUATOR_TYPES = {
  pump: { label: "Bomba de riego", icon: "drop" },
  fan: { label: "Ventilador", icon: "fan" },
  shade: { label: "Malla sombra", icon: "shade" },
  light: { label: "Iluminación", icon: "bulb" },
};

export function sectionsFor(role) {
  return SECTIONS.filter((section) => section.roles.includes(role));
}

export function canAccess(role, sectionId) {
  const section = SECTIONS.find((s) => s.id === sectionId);
  return Boolean(section && section.roles.includes(role));
}

export function landingPath(role) {
  return `panel.html#/${LANDING[role] || "inicio"}`;
}
