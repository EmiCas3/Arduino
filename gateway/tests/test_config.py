"""Configuración: gateway/.env + variables de entorno."""

import pytest

from gateway.config import ConfigError, load_config


def write_env(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_file(tmp_path):
    cfg = load_config(tmp_path / "no-existe.env", environ={})
    assert cfg.api_url == "http://localhost:8000/api/v1"
    assert cfg.device_id == "pi-vivero-01"
    assert cfg.serial_port == "auto"
    assert (cfg.batch_size, cfg.send_interval, cfg.baud) == (100, 10.0, 9600)
    assert cfg.ingest_url == "http://localhost:8000/api/v1/devices/pi-vivero-01/readings"


def test_file_values_and_env_override(tmp_path):
    env = write_env(tmp_path, "# comentario\nSG_API_KEY='abc'\nSG_BATCH_SIZE=50\nexport SG_DEVICE_ID=pi-x-01\n")
    cfg = load_config(env, environ={"SG_BATCH_SIZE": "20"})
    assert cfg.api_key == "abc"
    assert cfg.device_id == "pi-x-01"
    assert cfg.batch_size == 20


def test_api_key_is_not_printed(tmp_path):
    cfg = load_config(write_env(tmp_path, "SG_API_KEY=super-secreta\n"), environ={})
    assert "super-secreta" not in repr(cfg)


def test_run_requires_api_key(tmp_path):
    cfg = load_config(tmp_path / "x.env", environ={})
    with pytest.raises(ConfigError, match="SG_API_KEY"):
        cfg.require_api_key()


@pytest.mark.parametrize("key,value", [
    ("SG_BATCH_SIZE", "0"), ("SG_BATCH_SIZE", "501"), ("SG_BATCH_SIZE", "muchos"),
    ("SG_SEND_INTERVAL", "0"), ("SG_CLOCK", "rtc"), ("SG_API_URL", "localhost:8000"),
])
def test_invalid_values(tmp_path, key, value):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "x.env", environ={key: value})


def test_relative_buffer_path_is_inside_gateway(tmp_path):
    cfg = load_config(tmp_path / "x.env", environ={"SG_BUFFER_DB": "datos/buf.db"})
    assert cfg.buffer_db.parts[-3:] == ("gateway", "datos", "buf.db")
