"""
Buffer local en SQLite (MON-03, criterios 2 y 3).

Toda lectura se guarda AQUÍ antes de intentar enviarla, y solo se marca como
enviada cuando el backend responde 202. Sobrevive a reinicios del servicio y
a cortes de luz (journal WAL + synchronous=FULL).

Estados de una lectura:
    sin_hora   llegó con el reloj sin sincronizar; espera su hora
    pendiente  tiene `ts` y espera a ser enviada
    enviada    el backend la guardó (202). Se purga tras SG_PURGE_DAYS
    rechazada  el backend la rechazó; se conserva con su motivo
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from gateway.clock import resolve_epoch, utc_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS lecturas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT,                 -- UTC con ms; NULL mientras no hay hora
    boot_id    TEXT    NOT NULL,     -- arranque de la Pi en que se leyó
    boot_ms    INTEGER NOT NULL,     -- ms desde ese arranque al leer la línea
    payload    TEXT    NOT NULL,     -- la línea del Arduino tal cual, sin ts
    estado     TEXT    NOT NULL
               CHECK(estado IN ('sin_hora','pendiente','enviada','rechazada')),
    intentos   INTEGER NOT NULL DEFAULT 0,
    motivo     TEXT,                 -- por qué se rechazó
    enviada_en TEXT
);
CREATE INDEX IF NOT EXISTS ix_lecturas_estado ON lecturas(estado, ts, id);

-- Datos sueltos para `status`: último envío, último error, última línea...
CREATE TABLE IF NOT EXISTS meta (
    clave TEXT PRIMARY KEY,
    valor TEXT
);
"""

Row = Tuple[int, str, dict]   # (id, ts, payload)


class Buffer:
    """Cola persistente. Cada hilo debe abrir su propio Buffer."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self._enable_wal()
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def _enable_wal(self, attempts: int = 50) -> None:
        """Activa WAL (lectores y escritor a la vez) solo si hace falta.

        Cambiar el modo pide un candado exclusivo y SQLite NO espera por él:
        si dos procesos crean el buffer al mismo tiempo (el servicio arrancando
        y un `status`), uno recibe "database is locked". Se reintenta.
        """
        for attempt in range(attempts):
            try:
                mode = self.db.execute("PRAGMA journal_mode").fetchone()[0]
                if mode.lower() != "wal":
                    self.db.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.1)

    def close(self) -> None:
        self.db.close()

    # ── Escritura ──────────────────────────────────────────────────────

    def add(self, payload: dict, ts: Optional[str], boot_id: str, boot_ms: int) -> int:
        """Guarda una lectura. Con `ts` queda pendiente; sin él, sin_hora."""
        estado = "pendiente" if ts else "sin_hora"
        cur = self.db.execute(
            "INSERT INTO lecturas (ts, boot_id, boot_ms, payload, estado) VALUES (?, ?, ?, ?, ?)",
            (ts, boot_id, boot_ms, json.dumps(payload, separators=(",", ":")), estado),
        )
        self.db.commit()
        return cur.lastrowid

    def resolve_untimed(self, boot_id: str, now_epoch: float, now_boot_ms: int) -> int:
        """Pone hora a las lecturas sin_hora de ESTE arranque. Devuelve cuántas."""
        rows = self.db.execute(
            "SELECT id, boot_ms FROM lecturas WHERE estado = 'sin_hora' AND boot_id = ?",
            (boot_id,),
        ).fetchall()
        for row_id, reading_boot_ms in rows:
            ts = utc_ms(resolve_epoch(reading_boot_ms, now_epoch, now_boot_ms))
            self.db.execute(
                "UPDATE lecturas SET ts = ?, estado = 'pendiente' WHERE id = ? AND estado = 'sin_hora'",
                (ts, row_id),
            )
        self.db.commit()
        return len(rows)

    def next_batch(self, limit: int) -> List[Row]:
        """Las pendientes más viejas primero."""
        rows = self.db.execute(
            "SELECT id, ts, payload FROM lecturas WHERE estado = 'pendiente' "
            "ORDER BY ts, id LIMIT ?",
            (limit,),
        ).fetchall()
        return [(row_id, ts, json.loads(payload)) for row_id, ts, payload in rows]

    def count_attempt(self, ids: Iterable[int]) -> None:
        self.db.executemany(
            "UPDATE lecturas SET intentos = intentos + 1 WHERE id = ?", [(i,) for i in ids]
        )
        self.db.commit()

    def mark_sent(self, ids: Iterable[int], sent_at: str) -> None:
        self.db.executemany(
            "UPDATE lecturas SET estado = 'enviada', enviada_en = ? WHERE id = ? AND estado = 'pendiente'",
            [(sent_at, i) for i in ids],
        )
        self.db.commit()

    def mark_rejected(self, row_id: int, reason: str) -> None:
        self.db.execute(
            "UPDATE lecturas SET estado = 'rechazada', motivo = ? WHERE id = ?",
            (reason[:500], row_id),
        )
        self.db.commit()

    def purge_sent(self, days: int, now: Optional[datetime] = None) -> int:
        """Borra las ENVIADAS de hace más de `days` días. Nunca otras."""
        now = now or datetime.now(timezone.utc)
        limit = utc_ms((now - timedelta(days=days)).timestamp())
        cur = self.db.execute(
            "DELETE FROM lecturas WHERE estado = 'enviada' AND enviada_en < ?", (limit,)
        )
        self.db.commit()
        return cur.rowcount

    # ── Meta ───────────────────────────────────────────────────────────

    def set_meta(self, **values: Optional[str]) -> None:
        self.db.executemany(
            "INSERT INTO meta (clave, valor) VALUES (?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            list(values.items()),
        )
        self.db.commit()

    def get_meta(self) -> Dict[str, Optional[str]]:
        return dict(self.db.execute("SELECT clave, valor FROM meta").fetchall())

    # ── Consulta ───────────────────────────────────────────────────────

    def stats(self, current_boot_id: Optional[str] = None) -> dict:
        counts = {"sin_hora": 0, "pendiente": 0, "enviada": 0, "rechazada": 0}
        for estado, total in self.db.execute(
            "SELECT estado, COUNT(*) FROM lecturas GROUP BY estado"
        ):
            counts[estado] = total
        oldest = self.db.execute(
            "SELECT MIN(ts) FROM lecturas WHERE estado = 'pendiente'"
        ).fetchone()[0]
        orphan = 0
        if current_boot_id is not None:
            orphan = self.db.execute(
                "SELECT COUNT(*) FROM lecturas WHERE estado = 'sin_hora' AND boot_id != ?",
                (current_boot_id,),
            ).fetchone()[0]
        reasons = [
            (ts, motivo)
            for ts, motivo in self.db.execute(
                "SELECT ts, motivo FROM lecturas WHERE estado = 'rechazada' ORDER BY id DESC LIMIT 3"
            )
        ]
        return {
            "counts": counts,
            "oldest_pending": oldest,
            "untimed_other_boot": orphan,
            "last_rejected": reasons,
        }
