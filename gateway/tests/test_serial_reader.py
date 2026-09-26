"""Lectura del Arduino: líneas cortadas, basura, reconexión y puerto."""

import json
import threading

import pytest

from gateway.serial_reader import SerialReader, find_port, parse_line
from gateway.tests.conftest import arduino_line


def encode(data: dict) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode() + b"\r\n"


# ── parse_line ─────────────────────────────────────────────────────────

def test_valid_line_keeps_field_names_and_nulls():
    line = arduino_line(temp_c=None, suelo_pct=None)
    parsed = parse_line(encode(line))
    assert parsed == line                  # sin renombrar nada
    assert parsed["temp_c"] is None        # null = sensor caído, nunca 0


@pytest.mark.parametrize("raw", [
    b'"suelo_pct":62,"nivel_raw":150}\r\n',          # primera línea cortada
    b'{"ms":1,"temp_c":24.2\r\n',                     # cortada al final
    b"\r\n",
    b"hola\r\n",
    b"[1,2,3]\r\n",
    b'{"temp_c": NaN}\r\n',                            # NaN no es JSON válido
    b'{"temp_c": Infinity}\r\n',
    b"\xff\xfe{}\r\n",                                 # bytes que no son UTF-8
    b"{" + b"x" * 5000 + b"}\r\n",                     # línea absurda
])
def test_invalid_lines_are_ignored(raw):
    assert parse_line(raw) is None


def test_arduino_ts_is_dropped():
    """La hora la pone la Pi: si el Arduino mandara `ts`, se descarta."""
    parsed = parse_line(encode(arduino_line(ts="1970-01-01T00:00:00Z")))
    assert "ts" not in parsed


# ── SerialReader ───────────────────────────────────────────────────────

class FakePort:
    """Puerto que entrega unas líneas y luego detiene al lector (o falla)."""

    def __init__(self, lines, stop, fail_at_end=False):
        self.lines = list(lines)
        self.stop = stop
        self.fail_at_end = fail_at_end
        self.closed = False

    def readline(self):
        if self.lines:
            return self.lines.pop(0)
        if self.fail_at_end:
            raise OSError("el Arduino se desconectó")
        self.stop.set()
        return b""

    def close(self):
        self.closed = True


def test_first_cut_line_is_ignored_and_rest_is_delivered():
    stop = threading.Event()
    got = []
    lines = [b'_pct":62,"nivel_raw":150}\r\n', encode(arduino_line(ms=1)), encode(arduino_line(ms=2))]
    port = FakePort(lines, stop)
    reader = SerialReader(got.append, open_port=lambda p, b: port,
                          find=lambda s: "/dev/fake", stop=stop)
    reader.run()
    assert [r["ms"] for r in got] == [1, 2]
    assert reader.invalid_lines == 1
    assert reader.valid_lines == 2
    assert port.closed


def test_reconnects_after_disconnect():
    stop = threading.Event()
    got = []
    ports = [
        FakePort([encode(arduino_line(ms=1))], stop, fail_at_end=True),
        FakePort([encode(arduino_line(ms=2))], stop),
    ]
    opened = []

    def open_port(port, baud):
        opened.append(port)
        return ports.pop(0)

    reader = SerialReader(got.append, open_port=open_port, find=lambda s: "/dev/fake",
                          stop=stop, reconnect_seconds=0.01)
    reader.run()
    assert [r["ms"] for r in got] == [1, 2]
    assert len(opened) == 2


def test_waits_until_the_arduino_appears():
    stop = threading.Event()
    got = []
    answers = [None, None, "/dev/fake"]
    port = FakePort([encode(arduino_line(ms=7))], stop)
    reader = SerialReader(got.append, open_port=lambda p, b: port,
                          find=lambda s: answers.pop(0), stop=stop, reconnect_seconds=0.01)
    reader.run()
    assert [r["ms"] for r in got] == [7]


def test_open_failure_is_retried():
    stop = threading.Event()
    got = []
    attempts = []
    port = FakePort([encode(arduino_line(ms=3))], stop)

    def open_port(p, b):
        attempts.append(p)
        if len(attempts) == 1:
            raise OSError("Permission denied: /dev/ttyACM0 (¿grupo dialout?)")
        return port

    reader = SerialReader(got.append, open_port=open_port, find=lambda s: "/dev/fake",
                          stop=stop, reconnect_seconds=0.01)
    reader.run()
    assert len(attempts) == 2 and [r["ms"] for r in got] == [3]


def test_find_port_uses_configured_value():
    assert find_port("/dev/ttyACM1") == "/dev/ttyACM1"
    assert find_port("COM4") == "COM4"


def test_find_port_prefers_serial_by_id(monkeypatch):
    fake = ["/dev/serial/by-id/usb-1a86_USB2.0-Serial-if00-port0",
            "/dev/serial/by-id/usb-Arduino__www.arduino.cc__0043_7563-if00"]
    monkeypatch.setattr("gateway.serial_reader.glob.glob", lambda pattern: fake)
    assert find_port("auto").endswith("0043_7563-if00")
