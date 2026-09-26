"""Reloj: solo NTP, nunca inventar horas."""

from datetime import datetime, timezone

from gateway.clock import Clock, resolve_epoch, timedatectl_synced, utc_ms
from gateway.tests.conftest import SYNCED_EPOCH, FakeClock


def test_utc_ms_format():
    epoch = datetime(2026, 9, 26, 18, 4, 22, 123456, tzinfo=timezone.utc).timestamp()
    assert utc_ms(epoch) == "2026-09-26T18:04:22.123+00:00"


def fake_run(outputs):
    def run(cmd):
        return outputs.get(cmd[1])
    return run


def test_timedatectl_show_yes_and_no():
    assert timedatectl_synced(fake_run({"show": "yes\n"})) is True
    assert timedatectl_synced(fake_run({"show": "no\n"})) is False


def test_timedatectl_old_status_fallback():
    status = "   Local time: vie 2026-09-26\nSystem clock synchronized: yes\n NTP service: active\n"
    assert timedatectl_synced(fake_run({"show": None, "status": status})) is True


def test_timedatectl_missing_means_unknown():
    assert timedatectl_synced(fake_run({})) is None


def test_unknown_answer_is_treated_as_unsynced():
    clock = Clock("timedatectl", sync_fn=lambda: None, time_fn=lambda: SYNCED_EPOCH)
    assert clock.is_synced() is False


def test_1970_is_never_synced_even_if_systemd_says_yes():
    clock = Clock("timedatectl", sync_fn=lambda: True, time_fn=lambda: 5.0)
    assert clock.is_synced() is False


def test_system_mode_trusts_a_plausible_clock():
    assert Clock("system", time_fn=lambda: SYNCED_EPOCH).is_synced() is True


def test_stamp_without_sync_has_no_ts():
    clock = FakeClock(synced=False, boot=12_345)
    assert clock.stamp() == (None, 12_345)


def test_stamp_with_sync_uses_wall_time():
    clock = FakeClock(synced=True, boot=12_345)
    assert clock.stamp() == (utc_ms(SYNCED_EPOCH), 12_345)


def test_resolve_epoch_counts_back_from_now():
    # La lectura se tomó en el ms 10 000 del arranque; ahora vamos en el 70 000.
    assert resolve_epoch(10_000, now_epoch=1000.0, now_boot_ms=70_000) == 940.0


def test_result_is_cached_between_checks():
    calls = []
    clock = Clock("system", time_fn=lambda: SYNCED_EPOCH,
                  sync_fn=lambda: calls.append(1) or True, monotonic_fn=lambda: 100.0)
    for _ in range(5):
        clock.is_synced()
    assert len(calls) == 1
    clock.is_synced(force=True)
    assert len(calls) == 2
