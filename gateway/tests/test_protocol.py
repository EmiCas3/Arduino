"""
Pruebas de las órdenes al Arduino (ACT-02): formato y validación de la
línea, el camino backend → gateway → Serial, y el Arduino simulado
comportándose como el firmware (modo, objetivo, tiempo máximo, descanso).
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from gateway import protocol
from gateway.protocol import CommandError, to_line
from gateway.sender import OK, Dispatcher, post_readings
from gateway.serial_reader import SerialReader
from gateway.simulator import BOMBA_DESCANSO_MS, BOMBA_MAX_MS, BOMBA_MIN_MS, SimulatedArduino
from gateway.tests.conftest import FakeClock, arduino_line, fill

# ── Formato de la línea ────────────────────────────────────────────────

@pytest.mark.parametrize("command,line", [
    ({"id": 17, "kind": "mode", "value": "MANUAL"}, b"SG,17,MODO,MANUAL\n"),
    ({"id": 1, "kind": "mode", "value": "AUTO"}, b"SG,1,MODO,AUTO\n"),
    ({"id": 18, "kind": "pump", "value": "ON", "actuator_id": "bomba-riego-01"}, b"SG,18,BOMBA,ON\n"),
    ({"id": 999999999, "kind": "pump", "value": "OFF"}, b"SG,999999999,BOMBA,OFF\n"),
])
def test_valid_commands_become_short_lines(command, line):
    assert to_line(command) == line
    assert len(line) < 32       # cabe en el búfer de 64 bytes del Uno y en orden[32]


@pytest.mark.parametrize("command", [
    {"id": 0, "kind": "pump", "value": "ON"},
    {"id": -3, "kind": "pump", "value": "ON"},
    {"id": 1_000_000_000, "kind": "pump", "value": "ON"},
    {"id": "17", "kind": "pump", "value": "ON"},
    {"id": True, "kind": "pump", "value": "ON"},
    {"id": 5, "kind": "fan", "value": "ON"},
    {"id": 5, "kind": "pump", "value": "on"},
    {"id": 5, "kind": "pump", "value": "ON\nSG,6,BOMBA,ON"},
    {"id": 5, "kind": "mode", "value": "ON"},
    {"id": 5, "kind": "pump"},
    "SG,5,BOMBA,ON",
])
def test_anything_else_is_refused(command):
    with pytest.raises(CommandError):
        to_line(command)


@pytest.mark.parametrize("line,expected", [
    ("SG,17,BOMBA,ON", (17, "BOMBA", "ON")),
    ("SG,9,BOMBA,ON,EXTRA", (9, None, None)),     # FORMATO
    ("SG,7,BOMBA,ON,", (7, "BOMBA", "ON")),       # strtok: la coma final no cuenta
    ("SG,,6,BOMBA,ON", (6, "BOMBA", "ON")),       # strtok se salta campos vacíos
    ("=hola", None),
    (" SG,5,BOMBA,ON", None),                     # el prefijo debe ser "SG" exacto
    ("SG,abc,BOMBA,ON", None),
    ("SG,7x,BOMBA,ON", None),                     # esNumeroDeOrden(): solo dígitos
    ("SG,-1,BOMBA,ON", None),
    ("SG,0,BOMBA,ON", None),
    ("SG,1234567890,BOMBA,ON", None),             # más de 9 dígitos
])
def test_parse_like_the_firmware(line, expected):
    assert protocol.parse(line) == expected


# ── Backend → gateway ──────────────────────────────────────────────────

def test_commands_in_the_202_are_read(cfg, buffer, backend_stub):
    backend_stub.responses.append((202, {"accepted": 1, "duplicates": 0, "rejected": [], "commands": [
        {"id": 7, "kind": "pump", "value": "ON", "actuator_id": "bomba-riego-01"}]}))
    fill(buffer, 1)
    result = post_readings(cfg, buffer.next_batch(10))
    assert result.kind == OK
    assert result.commands == [{"id": 7, "kind": "pump", "value": "ON", "actuator_id": "bomba-riego-01"}]


def test_old_backend_without_commands_still_works(cfg, buffer, backend_stub):
    backend_stub.responses.append((202, {"accepted": 1, "duplicates": 0, "rejected": []}))
    fill(buffer, 1)
    assert post_readings(cfg, buffer.next_batch(10)).commands == []


def test_garbage_commands_field_is_ignored(cfg, buffer, backend_stub):
    backend_stub.responses.append((202, {"accepted": 1, "duplicates": 0, "rejected": [],
                                         "commands": "SG,1,BOMBA,ON"}))
    fill(buffer, 1)
    assert post_readings(cfg, buffer.next_batch(10)).commands == []


def test_dispatcher_hands_commands_over(cfg, buffer, backend_stub):
    backend_stub.responses.append((202, {"accepted": 1, "duplicates": 0, "rejected": [],
                                         "commands": [{"id": 3, "kind": "mode", "value": "MANUAL"}]}))
    got = []
    dispatcher = Dispatcher(cfg, buffer, FakeClock(), on_commands=got.extend)
    fill(buffer, 1)
    dispatcher.tick()
    assert got == [{"id": 3, "kind": "mode", "value": "MANUAL"}]


# ── Gateway → Serial ───────────────────────────────────────────────────

class WritablePort:
    def __init__(self, lines, stop):
        self.lines = list(lines)
        self.stop = stop
        self.written = []

    def readline(self):
        if self.lines:
            return self.lines.pop(0)
        self.stop.set()
        return b""

    def write(self, data):
        self.written.append(data)
        return len(data)

    def close(self):
        pass


def encode(line: dict) -> bytes:
    return json.dumps(line).encode() + b"\r\n"


def test_reader_writes_queued_commands_between_lines():
    stop = threading.Event()
    port = WritablePort([encode(arduino_line(ms=1)), encode(arduino_line(ms=2))], stop)
    got = []
    reader = SerialReader(got.append, open_port=lambda p, b: port, find=lambda s: "/dev/fake", stop=stop)
    reader.send(b"SG,17,BOMBA,ON\n")
    reader.run()
    assert port.written == [b"SG,17,BOMBA,ON\n"]
    assert [r["ms"] for r in got] == [1, 2]     # la lectura sigue igual
    assert reader.written == 1


def test_nothing_is_written_before_the_first_valid_line():
    """El Uno se reinicia al abrir el puerto: se espera a que mande una lectura."""
    stop = threading.Event()
    events = []

    class Port(WritablePort):
        def readline(self):
            line = super().readline()
            events.append(("read", line[:8]))
            return line

        def write(self, data):
            events.append(("write", data))
            return super().write(data)

    port = Port([b"", b'_pct":62}\r\n', encode(arduino_line(ms=1))], stop)
    reader = SerialReader(lambda r: None, open_port=lambda p, b: port, find=lambda s: "/dev/fake", stop=stop)
    reader.send(b"SG,1,MODO,MANUAL\n")
    reader.run()
    first_write = next(i for i, e in enumerate(events) if e[0] == "write")
    assert [e[0] for e in events[:first_write]] == ["read", "read", "read"]   # timeout, cortada, válida
    assert port.written == [b"SG,1,MODO,MANUAL\n"]


def test_stale_command_is_dropped(monkeypatch):
    stop = threading.Event()
    port = WritablePort([encode(arduino_line(ms=1))], stop)
    reader = SerialReader(lambda r: None, open_port=lambda p, b: port, find=lambda s: "/dev/fake", stop=stop)
    reader.outbox.put((time.monotonic() - protocol.MAX_COMMAND_AGE_SECONDS - 1, b"SG,1,BOMBA,ON\n"))
    reader.run()
    assert port.written == []


def test_port_without_write_does_not_crash(caplog):
    stop = threading.Event()

    class ReadOnly:
        def __init__(self):
            self.lines = [encode(arduino_line(ms=1))]

        def readline(self):
            if self.lines:
                return self.lines.pop(0)
            stop.set()
            return b""

        def close(self):
            pass

    reader = SerialReader(lambda r: None, open_port=lambda p, b: ReadOnly(),
                          find=lambda s: "/dev/fake", stop=stop)
    reader.send(b"SG,1,BOMBA,ON\n")
    reader.run()
    assert "no acepta escritura" in caplog.text


# ── El Arduino simulado se comporta como el firmware ───────────────────

def arduino(**kwargs) -> SimulatedArduino:
    sim = SimulatedArduino(interval=2.0, seed=1, sleep=lambda s: None, **kwargs)
    sim.readline()   # la primera línea llega cortada
    return sim


def read(sim) -> dict:
    return json.loads(sim.readline())


def order(sim, text: str) -> dict:
    sim.write(text.encode() + b"\n")
    return read(sim)


def test_starts_in_auto_and_reports_protocol_fields():
    line = read(arduino())
    assert (line["modo"], line["cmd_id"], line["cmd_err"], line["bomba_paro"]) == ("AUTO", 0, None, None)


def test_auto_irrigates_until_target_then_rests():
    sim = arduino(dry=True)
    line = read(sim)
    assert line["suelo_pct"] >= 50 and line["bomba_activa"] is False
    for _ in range(40):                 # la tierra se va secando sola
        line = read(sim)
        if line["bomba_activa"]:
            break
    assert line["suelo_pct"] < 50 and line["bomba_paro"] is None
    started_at = sim.ms
    for _ in range(20):
        line = read(sim)
        if not line["bomba_activa"]:
            break
    assert line["bomba_paro"] == "OBJETIVO" and line["suelo_pct"] >= 50
    assert sim.ms - started_at >= BOMBA_MIN_MS    # no para antes del mínimo
    stopped_at = sim.ms
    sim.suelo_raw = 470.0               # se vuelve a secar enseguida
    while sim.ms - stopped_at < BOMBA_DESCANSO_MS - 2000:
        assert read(sim)["bomba_activa"] is False
    read(sim)
    assert read(sim)["bomba_activa"] is True


def test_max_run_time_stops_the_pump():
    sim = arduino(dry=True)
    sim.suelo_raw = 555.0
    read(sim)
    sim.suelo_raw = 555.0
    steps = 0
    while read(sim)["bomba_activa"]:
        sim.suelo_raw = 555.0           # la tierra nunca llega al objetivo
        steps += 1
    assert steps * 2000 <= BOMBA_MAX_MS
    assert sim.bomba_paro == "TIEMPO"


def test_manual_mode_skips_the_rule_and_obeys_orders():
    sim = arduino()
    sim.suelo_raw = 470.0               # tierra seca
    line = order(sim, "SG,5,MODO,MANUAL")
    assert (line["modo"], line["cmd_id"], line["bomba_activa"]) == ("MANUAL", 5, False)
    for _ in range(5):
        assert read(sim)["bomba_activa"] is False       # tierra seca, pero no riega solo
    assert order(sim, "SG,6,BOMBA,ON")["bomba_activa"] is True
    line = order(sim, "SG,7,BOMBA,OFF")
    assert (line["bomba_activa"], line["bomba_paro"], line["cmd_id"]) == (False, "ORDEN", 7)


def test_switch_to_manual_stops_auto_irrigation():
    sim = arduino()
    sim.suelo_raw = 470.0
    assert read(sim)["bomba_activa"] is True
    line = order(sim, "SG,9,MODO,MANUAL")
    assert (line["bomba_activa"], line["bomba_paro"]) == (False, "MODO")


def test_orders_refused_like_the_firmware():
    sim = arduino()
    assert order(sim, "SG,3,BOMBA,ON")["cmd_err"] == "MODO"      # en AUTO
    order(sim, "SG,4,MODO,MANUAL")
    sim.nivel_raw = 10.0
    read(sim)
    line = order(sim, "SG,5,BOMBA,ON")
    assert (line["cmd_id"], line["cmd_err"], line["bomba_activa"]) == (5, "TANQUE", False)
    assert order(sim, "SG,6,BOMBA,PRENDE")["cmd_err"] == "FORMATO"
    assert order(sim, "=eco de la terminal")["cmd_id"] == 6


def test_legacy_firmware_ignores_orders_and_sends_no_mode():
    sim = arduino(legacy=True)
    sim.suelo_raw = 470.0
    line = order(sim, "SG,5,MODO,MANUAL")
    assert "modo" not in line and "cmd_id" not in line
    assert line["bomba_activa"] is True       # sigue regando solo, como antes
    assert sim.written == []


def test_tank_is_150_raw_like_the_firmware():
    sim = arduino()
    sim.nivel_raw = 150.0
    assert read(sim)["nivel_pct"] >= 99


def test_simulator_drops_long_lines_like_the_firmware():
    sim = arduino()
    order(sim, "SG,4,MODO,MANUAL")
    line = order(sim, "SG,5,BOMBA,ON," + "A" * 30)
    assert line["cmd_id"] == 4 and line["bomba_activa"] is False
