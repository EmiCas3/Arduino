"""
Motor de alertas (ALERT-01).

Los umbrales viven en el firmware: el Arduino compara cada lectura y manda
los códigos que se activaron (`TEMP_ALTA`, `SUELO_SECO`…). Este motor
convierte los códigos de LÍMITE en alertas con historia:

  - Se CREA en la misma petición de ingesta en que llega la lectura (dentro
    de un ciclo del gateway), con sensor, dispositivo, valor, umbral (número
    y nombre de la constante del .ino, ver app/firmware.py) y hora.
  - Mientras el código siga llegando se ACTUALIZA la misma alerta (último
    valor, peor valor): no se duplica.
  - Se CIERRA cuando el código lleva SMARTGREENAI_ALERT_CLEAR_SECONDS
    seguidos sin llegar (60 s). Si vuelve antes, es la misma alerta. Un aviso
    `*_LEVE` cuenta como "de vuelta dentro del límite".
  - Un sensor caído (`null` o `FALLA_*`) no abre ni cierra nada: no se sabe.
  - Una lectura que no trae `alertas` no cambia nada.
  - Las lecturas más viejas que lo último evaluado de esa medición se
    ignoran (pueden llegar fuera de orden tras una caída de la red).
"""

from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import aiosqlite

from app.config import settings
from app.firmware import ALERT_METRICS, FAULT_CODES, LIMIT_ALERTS, LimitAlert
from app.models import Reading


def _seconds_between(earlier: str, later: str) -> float:
    return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds()


def _worse(direction: str, current: float, value: float) -> float:
    return max(current, value) if direction == "high" else min(current, value)


def _limit_hit(metric: str, codes: set) -> Optional[LimitAlert]:
    for code in codes:
        alert = LIMIT_ALERTS.get(code)
        if alert is not None and alert.metric == metric:
            return alert
    return None


def _sensor_failed(metric: str, codes: set) -> bool:
    return any(metric in FAULT_CODES[code] for code in codes if code in FAULT_CODES)


async def _latest_per_metric(
    db: aiosqlite.Connection, greenhouse_id: str, area: str
) -> Dict[str, dict]:
    """La alerta más reciente de cada medición (activa o cerrada)."""
    cursor = await db.execute(
        """
        SELECT * FROM alerts a
        WHERE greenhouse_id = ? AND area = ? AND id = (
            SELECT MAX(id) FROM alerts b
            WHERE b.greenhouse_id = a.greenhouse_id AND b.area = a.area AND b.metric = a.metric
        )
        """,
        (greenhouse_id, area),
    )
    return {row["metric"]: dict(row) for row in await cursor.fetchall()}


async def _create(
    db: aiosqlite.Connection,
    greenhouse_id: str,
    area: str,
    device_id: str,
    hit: LimitAlert,
    value: float,
    ts: str,
    now_iso: str,
) -> dict:
    row = {
        "greenhouse_id": greenhouse_id,
        "area": area,
        "metric": hit.metric,
        "device_id": device_id,
        "code": hit.code,
        "direction": hit.direction,
        "threshold": hit.threshold,
        "threshold_name": hit.constant,
        "value": value,
        "reading_ts": ts,
        "created_at": now_iso,
        "last_value": value,
        "last_ts": ts,
        "worst_value": value,
        "evaluated_ts": ts,
        "clear_since": None,
        "status": "active",
    }
    cursor = await db.execute(
        f"INSERT INTO alerts ({', '.join(row)}) VALUES ({', '.join(':' + k for k in row)})",
        row,
    )
    row["id"] = cursor.lastrowid
    return row


async def _save(db: aiosqlite.Connection, alert: dict) -> None:
    await db.execute(
        """
        UPDATE alerts SET
            last_value = :last_value, last_ts = :last_ts, worst_value = :worst_value,
            evaluated_ts = :evaluated_ts, clear_since = :clear_since, status = :status,
            cleared_at = :cleared_at, cleared_ts = :cleared_ts, cleared_value = :cleared_value
        WHERE id = :id
        """,
        {
            "cleared_at": None, "cleared_ts": None, "cleared_value": None,
            **{k: alert.get(k) for k in (
                "id", "last_value", "last_ts", "worst_value", "evaluated_ts", "clear_since",
                "status", "cleared_at", "cleared_ts", "cleared_value",
            )},
        },
    )


def _close(alert: dict, value: float, ts: str, now_iso: str) -> None:
    alert.update(status="cleared", cleared_at=now_iso, cleared_ts=ts,
                 cleared_value=value, evaluated_ts=ts)


async def evaluate(
    db: aiosqlite.Connection,
    device_id: str,
    greenhouse_id: str,
    readings: Sequence[Tuple[str, Reading]],
    now_iso: str,
) -> List[int]:
    """Evalúa las lecturas NUEVAS (ordenadas por hora). Devuelve los ids de
    las alertas creadas. No hace commit."""
    area = settings.default_area
    clear_after = settings.alert_clear_seconds
    latest = await _latest_per_metric(db, greenhouse_id, area)
    created: List[int] = []

    for ts, reading in readings:
        if reading.alertas is None:
            continue
        codes = {code.value for code in reading.alertas}
        for metric in ALERT_METRICS:
            value = getattr(reading, metric)
            if value is None or _sensor_failed(metric, codes):
                continue
            last = latest.get(metric)
            if last is not None and ts <= last["evaluated_ts"]:
                continue   # lectura atrasada: ya se evaluó algo más nuevo
            active = last if last is not None and last["status"] == "active" else None
            hit = _limit_hit(metric, codes)

            if hit is not None and active is not None and active["direction"] == hit.direction:
                active.update(last_value=value, last_ts=ts, evaluated_ts=ts, clear_since=None,
                              worst_value=_worse(hit.direction, active["worst_value"], value))
                await _save(db, active)
            elif hit is not None:
                if active is not None:   # cambió de lado (de muy seco a encharcado)
                    _close(active, value, ts, now_iso)
                    await _save(db, active)
                latest[metric] = await _create(
                    db, greenhouse_id, area, device_id, hit, value, ts, now_iso)
                created.append(latest[metric]["id"])
            elif active is not None:
                active["evaluated_ts"] = ts
                if active["clear_since"] is None:
                    active["clear_since"] = ts
                if _seconds_between(active["clear_since"], ts) >= clear_after:
                    _close(active, value, ts, now_iso)
                await _save(db, active)
    return created
