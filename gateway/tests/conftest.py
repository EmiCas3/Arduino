"""Piezas compartidas por las pruebas del gateway (MON-03)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional

import pytest

from gateway.buffer import Buffer
from gateway.clock import Clock
from gateway.config import Config

SYNCED_EPOCH = 1_790_000_000.0      # 2026-09-21: una hora "real" posterior a 2026-01-01


class FakeClock(Clock):
    """Reloj controlable: hora, ms desde el arranque y si está sincronizado."""

    RECHECK_UNSYNCED = 0.0
    RECHECK_SYNCED = 0.0

    def __init__(self, synced: bool = True, epoch: float = SYNCED_EPOCH,
                 boot: int = 50_000, boot_id: str = "arranque-1") -> None:
        self.synced = synced
        self.epoch = epoch
        self.boot = boot
        super().__init__(
            "system",
            time_fn=lambda: self.epoch,
            boot_ms_fn=lambda: self.boot,
            boot_id_fn=lambda: boot_id,
            sync_fn=lambda: self.synced,
        )

    def advance(self, seconds: float) -> None:
        """Pasa el tiempo en los dos relojes a la vez."""
        self.epoch += seconds
        self.boot += int(seconds * 1000)


def arduino_line(**overrides) -> dict:
    """Una línea como la que imprime vivero_sensoresV2.ino."""
    base = {
        "ms": 148022, "temp_c": 24.2, "hum_aire_pct": 72.0, "suelo_raw": 380,
        "suelo_pct": 62, "nivel_raw": 150, "nivel_pct": 75, "luz_raw": 306,
        "luz_nivel": 717, "es_dia": True, "sol_min_hoy": 42, "sol_min_prev": None,
        "riego_sugerido": False, "bomba_habilitada": True, "bomba_activa": False,
        "vent": "BASE", "vent_activo": False, "alertas": [], "estado": "OK",
    }
    base.update(overrides)
    return base


@pytest.fixture
def buffer(tmp_path: Path):
    buf = Buffer(tmp_path / "buffer.db")
    yield buf
    buf.close()


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        api_url="http://127.0.0.1:9/api/v1",   # puerto 9: nadie escucha
        device_id="pi-test-01",
        api_key="clave-de-prueba",
        buffer_db=tmp_path / "buffer.db",
        batch_size=100,
        send_interval=10,
        http_timeout=2,
    )


class StubBackend:
    """Servidor HTTP mínimo que responde lo que la prueba le pida."""

    def __init__(self) -> None:
        self.responses: List[tuple] = []     # (status, body dict)
        self.requests: List[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                headers = {k.lower(): v for k, v in self.headers.items()}   # HTTP no distingue mayúsculas
                stub.requests.append({"path": self.path, "headers": headers, "body": body})
                if stub.responses:
                    status, payload = stub.responses.pop(0)
                else:
                    status, payload = 202, {"accepted": len(body.get("readings", [])),
                                            "duplicates": 0, "rejected": []}
                if callable(payload):
                    payload = payload(body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/api/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def backend_stub(cfg: Config):
    stub = StubBackend()
    cfg.api_url = stub.url
    yield stub
    stub.close()


def fill(buffer: Buffer, n: int, clock: Optional[FakeClock] = None, **overrides) -> List[int]:
    """Mete n lecturas con hora al buffer, separadas 2 s."""
    clock = clock or FakeClock()
    ids = []
    for i in range(n):
        ts, boot = clock.stamp()
        ids.append(buffer.add(arduino_line(ms=1000 + i, **overrides), ts, clock.boot_id, boot))
        clock.advance(2)
    return ids
