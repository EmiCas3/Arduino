"""Buffer SQLite: nada se pierde ni se borra antes de un 202."""

from datetime import datetime, timedelta, timezone

from gateway.buffer import Buffer
from gateway.clock import utc_ms
from gateway.tests.conftest import FakeClock, arduino_line, fill


def test_reading_with_ts_is_pending(buffer):
    ids = fill(buffer, 2)
    batch = buffer.next_batch(10)
    assert [row[0] for row in batch] == ids
    assert batch[0][2]["suelo_pct"] == 62
    assert buffer.stats()["counts"]["pendiente"] == 2


def test_reading_without_ts_waits_for_the_clock(buffer):
    buffer.add(arduino_line(), None, "arranque-1", 1000)
    assert buffer.next_batch(10) == []
    assert buffer.stats()["counts"]["sin_hora"] == 1


def test_pending_survive_a_restart(tmp_path):
    """Reinicio del servicio o corte de luz: al abrir de nuevo, siguen ahí."""
    first = Buffer(tmp_path / "b.db")
    ids = fill(first, 3)
    first.close()
    again = Buffer(tmp_path / "b.db")
    assert [row[0] for row in again.next_batch(10)] == ids
    again.close()


def test_untimed_readings_get_their_real_time_when_clock_syncs(buffer):
    clock = FakeClock(synced=False, boot=10_000)
    for _ in range(3):
        ts, boot = clock.stamp()
        buffer.add(arduino_line(), ts, clock.boot_id, boot)
        clock.advance(2)          # lecturas en los ms 10 000, 12 000 y 14 000
    clock.advance(60)             # un minuto después sincroniza NTP
    clock.synced = True
    resolved = buffer.resolve_untimed(clock.boot_id, clock.epoch, clock.boot)
    assert resolved == 3
    ts = [row[1] for row in buffer.next_batch(10)]
    now = clock.epoch
    assert ts == [utc_ms(now - 66), utc_ms(now - 64), utc_ms(now - 62)]


def test_untimed_from_another_boot_is_never_invented(buffer):
    buffer.add(arduino_line(), None, "arranque-viejo", 5000)
    assert buffer.resolve_untimed("arranque-nuevo", 1_790_000_000.0, 9000) == 0
    stats = buffer.stats("arranque-nuevo")
    assert stats["counts"]["sin_hora"] == 1
    assert stats["untimed_other_boot"] == 1


def test_oldest_first(buffer):
    buffer.add(arduino_line(ms=2), "2026-09-26T18:00:02.000+00:00", "a", 2)
    buffer.add(arduino_line(ms=1), "2026-09-26T18:00:01.000+00:00", "a", 1)
    assert [row[2]["ms"] for row in buffer.next_batch(10)] == [1, 2]


def test_mark_sent_and_rejected(buffer):
    a, b, c = fill(buffer, 3)
    buffer.mark_sent([a, b], "2026-09-26T18:00:00.000+00:00")
    buffer.mark_rejected(c, "ts: sin zona horaria")
    stats = buffer.stats()
    assert stats["counts"] == {"sin_hora": 0, "pendiente": 0, "enviada": 2, "rechazada": 1}
    assert stats["last_rejected"][0][1] == "ts: sin zona horaria"


def test_purge_only_removes_old_sent(buffer):
    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    old, recent, rejected, pending = fill(buffer, 4)
    buffer.mark_sent([old], utc_ms((now - timedelta(days=8)).timestamp()))
    buffer.mark_sent([recent], utc_ms((now - timedelta(days=1)).timestamp()))
    buffer.mark_rejected(rejected, "motivo")
    assert buffer.purge_sent(7, now=now) == 1
    counts = buffer.stats()["counts"]
    assert counts == {"sin_hora": 0, "pendiente": 1, "enviada": 1, "rechazada": 1}


def test_two_connections_can_open_the_same_buffer(tmp_path):
    """El hilo del Serial y el de envío abren el buffer a la vez."""
    a = Buffer(tmp_path / "b.db")
    b = Buffer(tmp_path / "b.db")
    fill(a, 1)
    assert len(b.next_batch(10)) == 1
    a.close()
    b.close()
