"""
Envío por lotes al endpoint de MON-04: POST {SG_API_URL}/devices/{id}/readings.

Qué pasa con cada respuesta del backend:

    202              enviadas; las que vengan en `rejected` quedan como
                     rechazadas con su motivo (no se borran)
    401 / 403 / 404  API key o device_id mal configurados: se dice claro en el
                     log, se conserva todo y se reintenta con espera creciente
    400 / 413 / 422  el backend rechazó el lote completo: se reenvía de una en
                     una para aislar la lectura culpable
    5xx, sin red,    se conserva todo y se reintenta con espera creciente
    timeout          (5 s, 10 s, 20 s ... hasta 5 min)

El backend es idempotente por (device_id, ts): reenviar un lote que sí llegó
(pero cuya respuesta se perdió) no duplica nada.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from gateway import __version__
from gateway.buffer import Row
from gateway.clock import utc_ms
from gateway.config import Config

log = logging.getLogger("gateway.sender")

OK = "ok"                 # 202
CONFIG_ERROR = "config"   # 401 / 403 / 404
RETRY = "retry"           # sin red, timeout, 5xx
BATCH_REFUSED = "batch"   # 400 / 413 / 422


@dataclass
class SendResult:
    kind: str
    status: Optional[int] = None
    accepted: int = 0
    duplicates: int = 0
    rejected: List[Tuple[int, str]] = field(default_factory=list)  # (índice, motivo)
    message: str = ""


class Backoff:
    """Espera creciente entre reintentos: 5, 10, 20, 40 ... hasta 300 s."""

    def __init__(self, initial: float = 5.0, maximum: float = 300.0) -> None:
        self.initial = initial
        self.maximum = maximum
        self.current = 0.0

    def fail(self) -> float:
        self.current = self.initial if self.current == 0 else min(self.current * 2, self.maximum)
        return self.current

    def reset(self) -> None:
        self.current = 0.0


def _opener(url: str):
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        # El backend está en la misma Pi: nunca pasar por un proxy del sistema.
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _error_message(body: bytes) -> str:
    """Saca el mensaje de {"detail": {"error", "message"}} o de {"error", "message"}."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return body[:200].decode("utf-8", "replace")
    detail = data.get("detail", data) if isinstance(data, dict) else data
    if isinstance(detail, dict) and "message" in detail:
        return f"{detail.get('error', '')}: {detail['message']}".strip(": ")
    return json.dumps(detail, ensure_ascii=False)[:300]


def post_readings(cfg: Config, rows: List[Row]) -> SendResult:
    """Manda un lote. No toca el buffer: solo dice qué pasó."""
    body = json.dumps(
        {"readings": [dict(payload, ts=ts) for _, ts, payload in rows]},
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        cfg.ingest_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": cfg.api_key,
            "User-Agent": f"smartgreenai-gateway/{__version__}",
        },
    )
    try:
        with _opener(cfg.ingest_url).open(request, timeout=cfg.http_timeout) as resp:
            status, payload = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return SendResult(RETRY, message=f"sin conexión con {cfg.api_url}: {reason}")

    if status == 202:
        result = SendResult(OK, status=status)
        try:
            data = json.loads(payload.decode("utf-8"))
            result.accepted = int(data.get("accepted", 0))
            result.duplicates = int(data.get("duplicates", 0))
            result.rejected = [
                (int(item["index"]), str(item.get("reason", "")))
                for item in data.get("rejected", [])
            ]
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
            result.message = "202 con una respuesta que no se pudo leer"
        return result

    message = _error_message(payload)
    if status in (401, 403):
        return SendResult(CONFIG_ERROR, status, message=(
            f"El backend rechazó la API key (HTTP {status} {message}). "
            "Revisa SG_API_KEY y SG_DEVICE_ID en gateway/.env."
        ))
    if status == 404:
        return SendResult(CONFIG_ERROR, status, message=(
            f"El backend no conoce el device '{cfg.device_id}' o la URL está mal "
            f"(HTTP 404 {message}). Revisa SG_DEVICE_ID y SG_API_URL ({cfg.api_url}); "
            "el gateway se registra con `python -m app.seed`."
        ))
    if status in (400, 413, 422):
        return SendResult(BATCH_REFUSED, status, message=f"HTTP {status}: {message}")
    return SendResult(RETRY, status, message=f"el backend respondió HTTP {status}: {message}")


class Dispatcher:
    """Saca lotes del buffer, los manda y decide qué hacer con la respuesta.

    `tick()` se llama seguido desde el bucle principal; solo envía cuando ya
    toca (cada SG_SEND_INTERVAL, o de inmediato si quedan más pendientes).
    """

    def __init__(self, cfg: Config, buffer, clock, *, post=post_readings, monotonic=None) -> None:
        self.cfg = cfg
        self.buffer = buffer
        self.clock = clock
        self.post = post
        self.monotonic = monotonic or time.monotonic
        self.backoff = Backoff()
        self.next_at = 0.0
        self.failing = False

    def _now_utc(self) -> Optional[str]:
        return utc_ms(self.clock.time()) if self.clock.is_synced() else None

    def tick(self) -> Optional[SendResult]:
        now = self.monotonic()
        if now < self.next_at:
            return None
        rows = self.buffer.next_batch(self.cfg.batch_size)
        if not rows:
            self.next_at = now + self.cfg.send_interval
            return None
        result, delay = self.send(rows)
        self.next_at = self.monotonic() + delay
        return result

    def send(self, rows: List[Row]) -> Tuple[SendResult, float]:
        """Manda `rows` y actualiza el buffer. Devuelve (resultado, espera)."""
        self.buffer.count_attempt(row_id for row_id, _, _ in rows)
        result = self.post(self.cfg, rows)

        if result.kind == OK:
            self._apply_ok(rows, result)
            full = len(rows) >= self.cfg.batch_size
            return result, (0.0 if full else self.cfg.send_interval)

        if result.kind == BATCH_REFUSED:
            if len(rows) == 1:
                self.buffer.mark_rejected(rows[0][0], result.message)
                log.error("Lectura del %s rechazada por el backend: %s", rows[0][1], result.message)
                return result, 0.0
            log.warning("El backend rechazó el lote completo (%s). Se reenvía de una en una.", result.message)
            for row in rows:
                single, _ = self.send([row])
                if single.kind in (RETRY, CONFIG_ERROR):
                    return single, self.backoff.current or self.backoff.fail()
            return result, 0.0

        # RETRY o CONFIG_ERROR: no se toca nada, todo sigue pendiente.
        delay = self.backoff.fail()
        self.failing = True
        pending = self.buffer.stats()["counts"]["pendiente"]
        level = logging.ERROR if result.kind == CONFIG_ERROR else logging.WARNING
        log.log(level, "No se pudo enviar: %s. %d lecturas pendientes a salvo en el buffer. "
                "Reintento en %.0f s.", result.message, pending, delay)
        self.buffer.set_meta(ultimo_error=result.message, ultimo_error_en=self._now_utc())
        return result, delay

    def _apply_ok(self, rows: List[Row], result: SendResult) -> None:
        rejected = {}
        for index, reason in result.rejected:
            if 0 <= index < len(rows):
                rejected[index] = reason
        sent_ids = []
        for index, (row_id, ts, _) in enumerate(rows):
            if index in rejected:
                self.buffer.mark_rejected(row_id, rejected[index])
                log.warning("El backend rechazó la lectura del %s: %s", ts, rejected[index])
            else:
                sent_ids.append(row_id)
        sent_at = self._now_utc()
        self.buffer.mark_sent(sent_ids, sent_at)

        recovered = self.failing
        if recovered:
            log.info("Conexión con el backend recuperada.")
        self.failing = False
        self.backoff.reset()
        summary = (f"aceptadas {result.accepted}, duplicadas {result.duplicates}, "
                   f"rechazadas {len(rejected)}")
        pending = self.buffer.stats()["counts"]["pendiente"]
        # Los envíos de rutina (cada 10 s) van a DEBUG para no llenar el journal;
        # vaciar el buffer, recuperarse o tener rechazos sí se ven en INFO.
        notable = recovered or rejected or pending or len(rows) >= self.cfg.batch_size
        log.log(logging.INFO if notable else logging.DEBUG,
                "Lote enviado: %d lecturas (%s). Pendientes: %d.", len(rows), summary, pending)
        self.buffer.set_meta(ultimo_envio_ok=sent_at, ultimo_envio_resumen=summary)
