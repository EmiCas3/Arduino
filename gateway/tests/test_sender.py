"""Envío por lotes: 202, rechazos, 401/404, 5xx, sin red y espera creciente."""

from gateway.sender import (
    BATCH_REFUSED, CONFIG_ERROR, OK, RETRY, Backoff, Dispatcher, post_readings,
)
from gateway.tests.conftest import FakeClock, fill


class Ticker:
    """Reloj monotónico manual para el Dispatcher."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make_dispatcher(cfg, buffer, clock=None):
    ticker = Ticker()
    return Dispatcher(cfg, buffer, clock or FakeClock(), monotonic=ticker), ticker


# ── El request ─────────────────────────────────────────────────────────

def test_request_has_api_key_ts_and_original_fields(cfg, buffer, backend_stub):
    fill(buffer, 2, bomba_activa=True)
    rows = buffer.next_batch(10)
    result = post_readings(cfg, rows)
    assert result.kind == OK
    req = backend_stub.requests[0]
    assert req["path"] == "/api/v1/devices/pi-test-01/readings"
    assert req["headers"]["x-api-key"] == "clave-de-prueba"
    assert req["headers"]["content-type"] == "application/json"
    sent = req["body"]["readings"]
    assert [r["ts"] for r in sent] == [row[1] for row in rows]
    assert sent[0]["suelo_pct"] == 62 and sent[0]["sol_min_prev"] is None
    assert sent[0]["bomba_activa"] is True     # se reenvía sin tocar; el backend lo ignora


# ── 202 ────────────────────────────────────────────────────────────────

def test_202_marks_sent(cfg, buffer, backend_stub):
    fill(buffer, 3)
    dispatcher, _ = make_dispatcher(cfg, buffer)
    result = dispatcher.tick()
    assert result.kind == OK and result.accepted == 3
    stats = buffer.stats()["counts"]
    assert stats["enviada"] == 3 and stats["pendiente"] == 0
    assert buffer.get_meta()["ultimo_envio_ok"]


def test_rejected_readings_are_kept_with_reason(cfg, buffer, backend_stub):
    fill(buffer, 3)
    backend_stub.responses.append((202, {
        "accepted": 2, "duplicates": 0,
        "rejected": [{"index": 1, "reason": "suelo_pct: ensure this value is less than or equal to 100"}],
    }))
    dispatcher, _ = make_dispatcher(cfg, buffer)
    dispatcher.tick()
    stats = buffer.stats()
    assert stats["counts"]["enviada"] == 2
    assert stats["counts"]["rechazada"] == 1
    assert stats["last_rejected"][0][1].startswith("suelo_pct:")


def test_duplicates_count_as_sent(cfg, buffer, backend_stub):
    """Si el POST llegó pero la respuesta se perdió, el reintento da duplicadas."""
    fill(buffer, 2)
    backend_stub.responses.append((202, {"accepted": 0, "duplicates": 2, "rejected": []}))
    dispatcher, _ = make_dispatcher(cfg, buffer)
    dispatcher.tick()
    assert buffer.stats()["counts"]["enviada"] == 2


def test_full_batch_sends_next_one_right_away(cfg, buffer, backend_stub):
    cfg.batch_size = 2
    fill(buffer, 5)
    dispatcher, ticker = make_dispatcher(cfg, buffer)
    dispatcher.tick()
    assert dispatcher.next_at == ticker.now          # quedan más: sin esperar
    dispatcher.tick()
    dispatcher.tick()
    assert buffer.stats()["counts"]["enviada"] == 5
    assert dispatcher.next_at == ticker.now + cfg.send_interval


# ── Errores: nada se borra ─────────────────────────────────────────────

def test_401_keeps_everything_and_explains(cfg, buffer, backend_stub, caplog):
    fill(buffer, 2)
    backend_stub.responses.append((401, {"detail": {"error": "invalid_api_key",
                                                    "message": "API key inválida"}}))
    dispatcher, _ = make_dispatcher(cfg, buffer)
    result = dispatcher.tick()
    assert result.kind == CONFIG_ERROR
    assert buffer.stats()["counts"]["pendiente"] == 2
    assert "SG_API_KEY" in caplog.text
    assert "SG_API_KEY" in buffer.get_meta()["ultimo_error"]


def test_404_points_to_device_id(cfg, buffer, backend_stub):
    fill(buffer, 1)
    backend_stub.responses.append((404, {"detail": {"error": "device_not_registered",
                                                    "message": "no está registrado"}}))
    result = post_readings(cfg, buffer.next_batch(10))
    assert result.kind == CONFIG_ERROR
    assert "SG_DEVICE_ID" in result.message


def test_5xx_retries_with_growing_wait(cfg, buffer, backend_stub):
    fill(buffer, 2)
    backend_stub.responses.extend([(500, {"detail": "boom"})] * 3)
    dispatcher, ticker = make_dispatcher(cfg, buffer)
    waits = []
    for _ in range(3):
        result = dispatcher.tick()
        assert result.kind == RETRY
        waits.append(dispatcher.next_at - ticker.now)
        ticker.now = dispatcher.next_at
    assert waits == [5, 10, 20]
    assert buffer.stats()["counts"]["pendiente"] == 2
    dispatcher.tick()                                   # el backend ya responde 202
    assert buffer.stats()["counts"]["enviada"] == 2
    assert dispatcher.backoff.current == 0


def test_no_network_keeps_everything(cfg, buffer):
    fill(buffer, 4)
    dispatcher, _ = make_dispatcher(cfg, buffer)       # cfg apunta a un puerto sin nadie
    result = dispatcher.tick()
    assert result.kind == RETRY
    assert buffer.stats()["counts"]["pendiente"] == 4
    rows = buffer.db.execute("SELECT intentos FROM lecturas").fetchall()
    assert rows == [(1,)] * 4


def test_whole_batch_refused_is_retried_one_by_one(cfg, buffer, backend_stub):
    """Si el backend tumbara el lote (422), se aísla la lectura culpable."""
    fill(buffer, 3)
    backend_stub.responses.extend([
        (422, {"detail": [{"msg": "lote inválido"}]}),     # el lote completo
        (202, {"accepted": 1, "duplicates": 0, "rejected": []}),
        (422, {"detail": [{"msg": "esta no"}]}),
        (202, {"accepted": 1, "duplicates": 0, "rejected": []}),
    ])
    dispatcher, _ = make_dispatcher(cfg, buffer)
    result = dispatcher.tick()
    assert result.kind == BATCH_REFUSED
    counts = buffer.stats()["counts"]
    assert counts["enviada"] == 2 and counts["rechazada"] == 1 and counts["pendiente"] == 0


def test_backoff_caps_at_five_minutes():
    backoff = Backoff()
    waits = [backoff.fail() for _ in range(9)]
    assert waits == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    backoff.reset()
    assert backoff.fail() == 5


def test_nothing_is_sent_while_buffer_only_has_untimed(cfg, buffer, backend_stub):
    buffer.add({"ms": 1}, None, "arranque-1", 1000)
    dispatcher, _ = make_dispatcher(cfg, buffer)
    assert dispatcher.tick() is None
    assert backend_stub.requests == []
