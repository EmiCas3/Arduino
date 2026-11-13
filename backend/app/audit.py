"""
Bitácora de auditoría (AUTH-05).

Una sola tabla, `audit_log`, para todo lo que pasa en la plataforma: inicios
de sesión, alertas, acciones sobre los equipos y recomendaciones de la IA.
Cada fila dice QUIÉN (actor), QUÉ (tipo + acción) y CUÁNDO.

Esquema de un evento
--------------------
    event_type    familia del evento: auth, alert, actuator, recommendation
    action        qué pasó, del catálogo ACTIONS (login, alert_raised, …)
    actor_type    user (una persona), device (el Arduino, vía su gateway),
                  system (el backend por su cuenta) o anonymous (un intento
                  de entrar que no corresponde a ninguna cuenta)
    actor_id      id del usuario o del dispositivo; null en system/anonymous
    actor_name    nombre para mostrar, tal como era en ese momento
    greenhouse_id vivero al que pertenece el evento (null = de la plataforma)
    target_type   sobre qué se actuó: session, alert, actuator, command,
    target_id     irrigation_mode, recommendation… y su id
    summary       una frase en lenguaje claro
    detail        JSON con los datos del evento (valor, umbral, motivo…)
    occurred_at   cuándo pasó (UTC, ms). En lo que reporta el Arduino es la
                  hora de la LECTURA, no la de llegada al servidor
    recorded_at   cuándo se escribió en la bitácora (UTC, ms)

Reglas
------
- Solo se agrega. La tabla tiene triggers que abortan cualquier UPDATE o
  DELETE (ver app/database.py), y la API solo tiene un GET.
- record() NO hace commit: el evento se guarda en la misma transacción que
  la acción que registra. Así no hay acción sin registro ni registro de algo
  que al final no se guardó.
- Nunca se guarda una contraseña ni lo que alguien tecleó en el formulario
  de login (NFR-03): un intento fallido contra una cuenta que no existe
  queda como `anonymous`, sin el texto que se escribió.

Para registrar un evento nuevo (por ejemplo AI-07): agrega la acción a
ACTIONS y llama a record() antes del commit de tu endpoint.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

# Familia → acciones válidas. Es el catálogo que documenta el spec.
ACTIONS: Dict[str, Tuple[str, ...]] = {
    "auth": (
        "login",            # inicio de sesión correcto
        "login_failed",     # credenciales inválidas
        "logout",           # la persona cerró su sesión
        "session_expired",  # el servidor la cerró (inactividad, vencimiento, cuenta inactiva)
    ),
    "alert": (
        "alert_raised",     # una medición cruzó su límite
        "alert_cleared",    # volvió a estar dentro del límite
    ),
    "actuator": (
        "actuator_on",          # el Arduino reportó que encendió
        "actuator_off",         # el Arduino reportó que apagó
        "command_requested",    # alguien (o el backend) mandó una orden
        "mode_changed",         # riego automático ↔ manual
        "actuator_registered",  # alta de un equipo
        "actuator_updated",     # edición de sus datos
        "actuator_deactivated",
        "actuator_reactivated",
    ),
    "recommendation": (
        "recommendation_generated",  # la IA produjo una recomendación distinta
        "recommendation_accepted",   # AI-07
        "recommendation_rejected",   # AI-07
    ),
}

EVENT_TYPES: Tuple[str, ...] = tuple(ACTIONS)
ACTOR_TYPES: Tuple[str, ...] = ("user", "device", "system", "anonymous")

_ACTION_TYPE: Dict[str, str] = {
    action: event_type for event_type, actions in ACTIONS.items() for action in actions
}


def ts_text(moment: datetime) -> str:
    """UTC con milisegundos: el mismo formato que `readings.ts`.

    Todas las horas de la bitácora se guardan así para que comparar texto
    ordene igual que comparar fechas (el filtro por rango usa el índice).
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _normalize(value) -> str:
    """Acepta datetime o texto ISO 8601 y lo deja en el formato de ts_text()."""
    if isinstance(value, datetime):
        return ts_text(value)
    return ts_text(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def user_actor(user) -> Dict[str, Any]:
    """Actor para una persona (fila de `users` o el dict de get_current_user)."""
    return {
        "actor_type": "user",
        "actor_id": str(user["id"]),
        "actor_name": user["full_name"] or user["username"],
    }


def device_actor(device_id: str) -> Dict[str, Any]:
    """Actor para lo que decide o reporta el Arduino (por su gateway)."""
    return {"actor_type": "device", "actor_id": device_id, "actor_name": device_id}


SYSTEM_ACTOR: Dict[str, Any] = {
    "actor_type": "system", "actor_id": None, "actor_name": "Plataforma",
}
ANONYMOUS_ACTOR: Dict[str, Any] = {
    "actor_type": "anonymous", "actor_id": None, "actor_name": None,
}


async def record(
    db: aiosqlite.Connection,
    *,
    action: str,
    actor: Dict[str, Any],
    summary: str,
    greenhouse_id: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id=None,
    detail: Optional[Dict[str, Any]] = None,
    occurred_at=None,
    now: Optional[datetime] = None,
) -> int:
    """Agrega un evento a la bitácora y devuelve su id. No hace commit.

    `action` debe estar en ACTIONS (de ahí sale el tipo). `occurred_at` es
    cuándo pasó (por defecto, ahora); acepta datetime o texto ISO 8601.
    """
    event_type = _ACTION_TYPE.get(action)
    if event_type is None:
        raise ValueError(f"Acción de bitácora desconocida: {action!r}")
    if actor.get("actor_type") not in ACTOR_TYPES:
        raise ValueError(f"Tipo de actor desconocido: {actor.get('actor_type')!r}")

    recorded = ts_text(now or datetime.now(timezone.utc))
    cursor = await db.execute(
        """
        INSERT INTO audit_log (event_type, action, actor_type, actor_id, actor_name,
                               greenhouse_id, target_type, target_id, summary, detail,
                               occurred_at, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type, action, actor["actor_type"], actor.get("actor_id"),
            actor.get("actor_name"), greenhouse_id, target_type,
            None if target_id is None else str(target_id), summary,
            json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None,
            _normalize(occurred_at) if occurred_at is not None else recorded,
            recorded,
        ),
    )
    return cursor.lastrowid


async def query(
    db: aiosqlite.Connection,
    *,
    since: datetime,
    until: datetime,
    event_types: Optional[List[str]] = None,
    actions: Optional[List[str]] = None,
    actor_type: Optional[str] = None,
    actor_id: Optional[str] = None,
    greenhouse_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[int, List[dict]]:
    """Eventos con `since <= occurred_at <= until`, del más nuevo al más viejo.

    Devuelve (total que cumple los filtros, la página pedida). `detail` ya
    viene como dict.
    """
    where = ["occurred_at >= ?", "occurred_at <= ?"]
    params: list = [ts_text(since), ts_text(until)]
    for column, values in (("event_type", event_types), ("action", actions)):
        if values:
            where.append(f"{column} IN ({','.join('?' * len(values))})")
            params.extend(values)
    for column, value in (
        ("actor_type", actor_type), ("actor_id", actor_id), ("greenhouse_id", greenhouse_id),
    ):
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
    condition = " AND ".join(where)

    cursor = await db.execute(f"SELECT COUNT(*) AS n FROM audit_log WHERE {condition}", params)
    total = (await cursor.fetchone())["n"]
    cursor = await db.execute(
        f"""
        SELECT * FROM audit_log WHERE {condition}
        ORDER BY occurred_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    rows = []
    for row in await cursor.fetchall():
        event = dict(row)
        event["detail"] = json.loads(event["detail"]) if event["detail"] else None
        rows.append(event)
    return total, rows
