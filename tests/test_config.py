"""配置加载、凭据与日志脱敏测试。"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from ovital_mcp.config import (
    CONFIG_ENV,
    AppConfig,
    _SecretFilter,
    configure_logging,
    discover_config_path,
    load_config,
)
from ovital_mcp.errors import ConfigError


def _write_config(path: Path, *, psk: str | None = None, psk_file: str | None = None) -> Path:
    lines = [
        "ovital:",
        "  endpoint: ws://127.0.0.1:7699",
    ]
    if psk:
        lines.append(f"  psk: {json.dumps(psk)}")
    if psk_file:
        # Windows 路径含反斜杠：用 JSON 转义（JSON 字符串是合法的 YAML 双引号标量）。
        lines.append(f"  psk_file: {json.dumps(psk_file)}")
    lines += [
        "gis:",
        "  engine: native",
        "  snap_radius_m: 60",
        "storage:",
        "  data_root: ./data",
        "logging:",
        "  level: DEBUG",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_config_from_yaml(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path / "ovital.yaml", psk="secret-psk-value")
    config = load_config(cfg)
    assert config.ovital.endpoint == "ws://127.0.0.1:7699"
    assert config.gis.snap_radius_m == 60
    assert config.logging.level == "DEBUG"
    assert config.config_path == str(cfg.resolve())
    assert config.ovital.resolve_psk() == "secret-psk-value"


def test_default_config_is_valid() -> None:
    config = AppConfig()
    assert config.gis.engine == "auto"
    assert config.ovital.endpoint.startswith("ws://")
    with pytest.raises(ConfigError):
        config.ovital.resolve_psk()  # 未配置密钥时显式报错


def test_psk_file_takes_precedence_and_is_stripped(tmp_path: Path) -> None:
    secret = tmp_path / "psk.txt"
    secret.write_text("  file-secret  \nsecond-line\n", encoding="utf-8")
    cfg = _write_config(tmp_path / "ovital.yaml", psk="inline-secret", psk_file=str(secret))
    config = load_config(cfg)
    assert config.ovital.resolve_psk() == "file-secret"


def test_psk_file_missing_raises_without_leaking(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path / "ovital.yaml", psk_file=str(tmp_path / "nope.txt"))
    config = load_config(cfg)
    with pytest.raises(ConfigError) as exc_info:
        config.ovital.resolve_psk()
    message = str(exc_info.value)
    assert "nope.txt" in message
    assert "secret" not in message.lower()


def test_safe_dict_never_contains_psk(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path / "ovital.yaml", psk="super-secret-psk")
    config = load_config(cfg)
    safe = config.safe_dict()
    rendered = str(safe)
    assert "super-secret-psk" not in rendered
    assert safe["ovital"]["psk_configured"] is True
    assert "psk" not in safe["ovital"]


def test_invalid_endpoint_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("ovital:\n  endpoint: http://127.0.0.1:7699\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_discover_config_path_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write_config(tmp_path / "custom.yaml", psk="x")
    monkeypatch.setenv(CONFIG_ENV, str(cfg))
    assert discover_config_path() == cfg.resolve()


def test_secret_filter_redacts_log_text() -> None:
    stream = io.StringIO()
    logger = logging.getLogger("test.secret.filter")
    logger.handlers = []
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.addFilter(_SecretFilter(["super-secret-psk", "md5pskvalue"]))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info("connect with psk=%s md5psk=%s", "super-secret-psk", "md5pskvalue")
    out = stream.getvalue()
    assert "super-secret-psk" not in out
    assert "md5pskvalue" not in out
    assert "***REDACTED***" in out


def test_configure_logging_never_writes_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    logger = configure_logging("INFO", secrets=["secret-psk"], log_file=str(tmp_path / "log.txt"))
    logger.info("hello %s", "secret-psk")
    captured = capsys.readouterr()
    assert captured.out == ""  # 关键：stdout 只能留给 MCP 协议
    assert "hello" in captured.err
    assert "secret-psk" not in captured.err
    assert "hello" in (tmp_path / "log.txt").read_text(encoding="utf-8")
