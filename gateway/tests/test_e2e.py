"""
Punta a punta: Arduino simulado → gateway → backend REAL (uvicorn) → BD.

Cubre los criterios de MON-03:
  1. Con red, la lectura llega al backend y aparece en /readings/latest.
  2. Con el backend apagado, las lecturas se acumulan y nada se borra.
  3. Al volver, el buffer se vacía y cada lectura conserva su hora original.
Y la regla del reloj: sin NTP no se envía nada; al sincronizar salen con la
hora correcta.

Necesita las dependencias del backend (se salta si no están instaladas).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from gateway.__main__ import run_gateway  # noqa: E402
from gateway.buffer import Buffer  # noqa: E402
from gateway.config import Config  # noqa: E402
from gateway.simulator import SimulatedArduino  # noqa: E402
from gateway.tests.conftest import FakeClock  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
PASSWORD = "Rabano-E2E-2026!"
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Backend:
    """uvicorn en un subproceso, con su propia BD temporal."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.port = free_port()
        self.proc = None
        self.env = dict(os.environ, SMARTGREENAI_DB=str(db_path),
                        SMARTGREENAI_SEED_PASSWORD=PASSWORD,
                        SMARTGREENAI_JWT_SECRET="secreto-de-prueba-e2e",
                        PYTHONDONTWRITEBYTECODE="1")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1"

    def seed(self) -> str:
        out = subprocess.run([sys.executable, "-m", "app.seed"], cwd=BACKEND_DIR, env=self.env,
                             capture_output=True, text=True, check=True).stdout
        lines = out.splitlines()
        marker = next(i for i, line in enumerate(lines) if "API Key" in line)
        return lines[marker + 1].strip()

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(self.port)],
            cwd=BACKEND_DIR, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                NO_PROXY.open(f"http://127.0.0.1:{self.port}/health", timeout=1).read()
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("el backend no arrancó")

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=10)
            self.proc = None

    def stored_ts(self) -> list:
        with sqlite3.connect(self.db_path) as conn:
            return [r[0] for r in conn.execute("SELECT ts FROM readings ORDER BY ts")]

    def latest(self) -> dict:
        def call(path, body=None, token=None):
            req = urllib.request.Request(self.url + path, method="POST" if body else "GET",
                                         data=json.dumps(body).encode() if body else None)
            req.add_header("Content-Type", "application/json")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            return json.loads(NO_PROXY.open(req, timeout=5).read())

        token = call("/auth/login", {"login": "productor", "password": PASSWORD})["access_token"]
        return call("/greenhouses/vivero-rabano-01/readings/latest", token=token)


@pytest.fixture
def backend(tmp_path):
    server = Backend(tmp_path / "backend.db")
    api_key = server.seed()
    server.start()
    server.api_key = api_key
    yield server
    server.stop()


class GatewayThread:
    """`python -m gateway simulate` dentro de un hilo, para poder detenerlo."""

    def __init__(self, cfg: Config, clock: FakeClock) -> None:
        self.stop = threading.Event()
        self.cfg = cfg
        self.clock = clock
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        run_gateway(
            self.cfg,
            open_port=lambda port, baud: SimulatedArduino(
                interval=0.05, sleep=lambda s: self.stop.wait(s)),
            find=lambda setting: "arduino-simulado",
            clock=self.clock,
            stop=self.stop,
        )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=10)


class LiveClock(FakeClock):
    """Reloj real (para que el backend acepte las horas) con NTP controlable."""

    def __init__(self, synced=True):
        super().__init__(synced=synced)
        self.time = time.time
        self.boot_ms = lambda: int(time.monotonic() * 1000)


def gateway_config(tmp_path, backend) -> Config:
    return Config(api_url=backend.url, device_id="pi-vivero-01", api_key=backend.api_key,
                  buffer_db=tmp_path / "buffer.db", batch_size=100, send_interval=0.3,
                  http_timeout=2)


def wait_for(condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


def buffer_counts(cfg):
    buf = Buffer(cfg.buffer_db)
    try:
        return buf.stats()["counts"]
    finally:
        buf.close()


def pending_ts(cfg):
    buf = Buffer(cfg.buffer_db)
    try:
        return [row[1] for row in buf.next_batch(500)]
    finally:
        buf.close()


# ── Criterio 1 ─────────────────────────────────────────────────────────

def test_readings_reach_backend_and_latest(tmp_path, backend):
    cfg = gateway_config(tmp_path, backend)
    with GatewayThread(cfg, LiveClock()):
        assert wait_for(lambda: len(backend.stored_ts()) >= 5)
    latest = backend.latest()
    assert latest["has_data"] is True
    assert latest["last_reading"]["device_id"] == "pi-vivero-01"
    temp = next(m for m in latest["metrics"] if m["key"] == "temp_c")
    assert temp["unit"] == "°C" and temp["value"] is not None
    # La primera línea (cortada) se ignoró; ninguna llegó rechazada.
    assert buffer_counts(cfg)["rechazada"] == 0


# ── Criterios 2 y 3 ────────────────────────────────────────────────────

def test_backend_down_then_up_keeps_original_timestamps(tmp_path, backend):
    cfg = gateway_config(tmp_path, backend)
    backend.stop()                                     # "se cae la red"
    with GatewayThread(cfg, LiveClock()):
        assert wait_for(lambda: buffer_counts(cfg)["pendiente"] >= 10)
        buffered = pending_ts(cfg)                     # horas de LECTURA
        assert buffer_counts(cfg)["enviada"] == 0      # nada se borró ni se marcó
        time.sleep(0.5)
        backend.start()                                # vuelve la red
        # El reintento puede estar esperando hasta 5-10 s (espera creciente).
        assert wait_for(lambda: set(buffered) <= set(backend.stored_ts()), timeout=30)
    stored = backend.stored_ts()
    assert set(buffered) <= set(stored)                # la misma hora, al milisegundo
    assert len(stored) == len(set(stored))             # sin duplicados


# ── Reloj sin sincronizar ──────────────────────────────────────────────

def test_unsynced_clock_sends_nothing_until_ntp(tmp_path, backend):
    cfg = gateway_config(tmp_path, backend)
    clock = LiveClock(synced=False)
    with GatewayThread(cfg, clock):
        assert wait_for(lambda: buffer_counts(cfg)["sin_hora"] >= 5)
        time.sleep(1)
        assert backend.stored_ts() == []               # nunca una hora inventada
        untimed = buffer_counts(cfg)["sin_hora"]
        before_sync = time.time()
        clock.synced = True                            # NTP sincroniza
        assert wait_for(lambda: len(backend.stored_ts()) > untimed)
    stored = backend.stored_ts()
    # Las lecturas "sin hora" quedaron fechadas ANTES de la sincronización.
    first = datetime.fromisoformat(stored[0]).timestamp()
    assert first < before_sync
