"""MCP 配置加载与日志设置。

安全约定
========
- 预共享密钥（PSK）只从本机配置文件或显式 ``psk_file`` 读取；
  不依赖 DSH 子进程继承的环境变量（DSH 会清理常见凭据变量）。
- 凭据绝不进入工具返回、日志、异常或任务文件；``OvitalSettings.safe_dict()``
  用于对外展示。
- 配置文件权限建议：POSIX ``chmod 600``；Windows 用当前用户 ACL。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ConfigError

#: 环境变量：指向 YAML 配置文件（非秘密，DSH patch 可以通过 env 显式传入）。
CONFIG_ENV = "OVITAL_MCP_CONFIG"

#: 未显式配置时，依次尝试的配置路径（相对当前工作目录）。
DEFAULT_CONFIG_CANDIDATES = (
    "config/ovital.yaml",
    "ovital.yaml",
    ".ovital-mcp/config.yaml",
)

SECRET_FIELD_NAMES = frozenset({"psk"})


class OvitalSettings(BaseModel):
    """奥维桌面 WebSocket 适配器配置。"""

    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(
        default="ws://127.0.0.1:7699",
        description="奥维第三方接口 WebSocket 地址；端口必须在奥维客户端中实际配置。",
    )
    psk: str | None = Field(default=None, description="预共享密钥明文（不推荐；优先 psk_file）。")
    psk_file: str | None = Field(
        default=None,
        description="预共享密钥文件路径（推荐；文件首行，允许存在空白，读取后 strip）。",
    )
    connect_timeout_s: float = Field(default=5.0, gt=0, le=60)
    request_timeout_s: float = Field(default=10.0, gt=0, le=120)
    #: 读取/写入对象时请求的坐标语义：1=CGCS2000（手册 2.2.2 LatlngType）。
    latlng_type: int = Field(default=1, ge=0, le=4)
    #: 写 Shape 时 ``ObjectDetail.Gcj02`` 取值；0=CGCS2000（手册 2.4.8）。
    shape_gcj02: int = Field(default=0, ge=0, le=2)
    real_device_verified: bool = Field(
        default=False,
        description=(
            "运维验收标记：只有在真实 Windows 奥维设备上完成读写/权限/坐标往返验收后，"
            "才允许由人显式设为 true。程序不得自行推断为 true。"
        ),
    )
    allow_gcj02_conversion: bool = Field(
        default=False,
        description="是否允许用公开近似算法做 GCJ-02 <-> CGCS2000 自动转换（默认禁止）。",
    )
    draft_root_folder: str = Field(
        default="OMAP-MCP 草稿",
        description="写草稿时使用的收藏夹根文件夹名；仅显式调用写工具时才创建。",
    )
    #: 草稿几何样式（ARGB 整数，0xAARRGGBB 语义由奥维解释；待实测确认后调整）。
    draft_line_color: int = Field(default=0x00FF8000, ge=0)
    draft_line_width: int = Field(default=3, ge=1, le=10)
    draft_area_color: int = Field(default=0x40FF8000, ge=0)
    draft_area_alpha: int = Field(default=60, ge=0, le=100)

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("endpoint 不能为空")
        if not (value.startswith("ws://") or value.startswith("wss://")):
            raise ValueError("endpoint 必须以 ws:// 或 wss:// 开头")
        return value.strip()

    @model_validator(mode="after")
    def _psk_present(self) -> "OvitalSettings":
        if not self.psk and not self.psk_file:
            # 允许启动（MCP 探测进程必须能在无凭据时启动），但连接工具会明确报错。
            return self
        return self

    def resolve_psk(self) -> str:
        """读取 PSK；不存在时抛 ``ConfigError``（错误信息不含密钥内容）。"""

        if self.psk_file:
            path = Path(os.path.expandvars(os.path.expanduser(self.psk_file)))
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(
                    f"无法读取 psk_file（{path}）：{exc.strerror or exc}",
                    details={"psk_file": str(path)},
                ) from exc
            value = raw.strip().splitlines()[0].strip() if raw.strip() else ""
        elif self.psk:
            value = self.psk.strip()
        else:
            raise ConfigError(
                "未配置奥维预共享密钥：请在配置文件中设置 ovital.psk_file（推荐）或 ovital.psk",
                details={"config_section": "ovital"},
            )
        if not value:
            raise ConfigError("奥维预共享密钥为空")
        return value

    def safe_dict(self) -> dict[str, Any]:
        data = self.model_dump()
        data.pop("psk", None)
        data["psk_configured"] = bool(self.psk or self.psk_file)
        if self.psk_file:
            data["psk_file"] = str(Path(os.path.expanduser(self.psk_file)))
        return data


class GisSettings(BaseModel):
    """GIS 集水区计算配置。"""

    model_config = ConfigDict(extra="forbid")

    engine: str = Field(default="auto", description="auto | native | grass")
    grass_cmd: str = Field(default="grass", description="GRASS 可执行文件路径或命令名")
    snap_radius_m: float = Field(default=90.0, ge=0, le=5000)
    target_res_m: float | None = Field(default=None, gt=0, le=1000)
    metric_crs: str | None = None
    min_accum_cells: int = Field(default=3, ge=1, le=1_000_000)
    simplify_tolerance_m: float = Field(default=0.0, ge=0, le=1000)
    max_cells: int = Field(default=36_000_000, ge=10_000)
    job_timeout_s: int = Field(default=1800, ge=10, le=86_400)
    keep_intermediate: bool = True

    @field_validator("engine")
    @classmethod
    def _validate_engine(cls, value: str) -> str:
        if value not in {"auto", "native", "grass"}:
            raise ValueError("gis.engine 只能是 auto / native / grass")
        return value


class StorageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_root: str = Field(default="./.ovital-mcp", description="任务/成果/快照根目录")
    retention_days: int = Field(default=30, ge=1, le=3650)

    def resolved_root(self, *, base_dir: Path | None = None) -> Path:
        raw = os.path.expandvars(os.path.expanduser(self.data_root))
        path = Path(raw)
        if not path.is_absolute():
            path = (base_dir or Path.cwd()) / path
        return path.resolve()


class McpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_max_bytes: int = Field(default=4 * 1024 * 1024, ge=64 * 1024, le=32 * 1024 * 1024)
    max_image_pixels: int = Field(default=1600, ge=256, le=4096)
    job_poll_hint_s: float = Field(default=2.0, gt=0, le=60)
    list_max_items: int = Field(default=200, ge=1, le=10_000)


class MockSettings(BaseModel):
    """模拟奥维服务器配置。

    该开关只影响本地开发/离线验证；MCP 不会因为 ``enabled`` 自动启动模拟服务，
    需要显式运行 ``scripts/run_mock_ovital.py``。``simulated`` 最终以服务端
    握手响应里的 ``msg.Simulated`` 字段为准（真实奥维不会有该字段）。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=0, ge=0, le=65535)
    psk: str | None = Field(default=None, description="仅供本地测试的模拟 PSK")
    psk_file: str | None = None


class LoggingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = "INFO"
    file: str | None = Field(default=None, description="日志文件路径；None 表示只写 stderr")


class AppConfig(BaseModel):
    """整个 MCP 服务配置。"""

    model_config = ConfigDict(extra="forbid")

    ovital: OvitalSettings = Field(default_factory=OvitalSettings)
    gis: GisSettings = Field(default_factory=GisSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    mock: MockSettings = Field(default_factory=MockSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    #: 配置文件实际来源路径（便于 provenance；不影响模型行为）。
    config_path: str | None = None

    def safe_dict(self) -> dict[str, Any]:
        return {
            "ovital": self.ovital.safe_dict(),
            "gis": self.gis.model_dump(),
            "storage": self.storage.model_dump(),
            "mcp": self.mcp.model_dump(),
            "mock": {
                k: v
                for k, v in self.mock.model_dump().items()
                if k not in {"psk"}
            }
            | {"psk_configured": bool(self.mock.psk or self.mock.psk_file)},
            "logging": self.logging.model_dump(),
            "config_path": self.config_path,
        }


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}: {exc.strerror or exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 {path} 不是合法 YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件 {path} 顶层必须是映射对象")
    return data


def discover_config_path(explicit: str | Path | None = None) -> Path | None:
    """定位配置文件。

    优先级：显式参数 > ``OVITAL_MCP_CONFIG`` 环境变量 > 默认候选路径。
    找不到时返回 ``None``（允许零配置启动；需要在用的工具会报具体缺项）。
    """

    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_value = os.environ.get(CONFIG_ENV)
    if env_value:
        candidates.append(Path(env_value))
    for relative in DEFAULT_CONFIG_CANDIDATES:
        candidates.append(Path(relative))
    for candidate in candidates:
        expanded = Path(os.path.expandvars(os.path.expanduser(str(candidate))))
        if expanded.is_file():
            return expanded.resolve()
    return None


def load_config(
    explicit_path: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """加载配置；不存在配置文件时返回默认配置。"""

    path = discover_config_path(explicit_path)
    data: dict[str, Any] = _read_yaml(path) if path else {}
    if overrides:
        data = _deep_merge(data, overrides)
    try:
        config = AppConfig.model_validate(data)
    except Exception as exc:  # pydantic ValidationError 转成领域错误
        raise ConfigError(f"配置校验失败（{path or '默认配置'}）：{exc}") from exc
    config.config_path = str(path) if path else None
    return config


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


class _SecretFilter(logging.Filter):
    """兜底：把日志文本里可能出现的 PSK 替换掉。"""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 4]

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "***REDACTED***")
        if redacted != message:
            record.msg = redacted
            record.args = ()
        if record.exc_text:
            for secret in self._secrets:
                record.exc_text = record.exc_text.replace(secret, "***REDACTED***")
        return True


def configure_logging(
    level: str = "INFO",
    *,
    log_file: str | None = None,
    secrets: list[str] | None = None,
) -> logging.Logger:
    """配置日志：stderr（及可选文件）。**绝不写 stdout**（MCP 协议专用）。"""

    logger = logging.getLogger("ovital_mcp")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    # 清理重复 handler（DSH 探测进程与正式服务进程可能先后启动）。
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    secret_filter = _SecretFilter(secrets or [])

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.addFilter(secret_filter)
    logger.addHandler(stderr_handler)

    if log_file:
        try:
            path = Path(os.path.expandvars(os.path.expanduser(log_file)))
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(path, encoding="utf-8")
            file_handler.setFormatter(formatter)
            file_handler.addFilter(secret_filter)
            logger.addHandler(file_handler)
        except OSError as exc:  # 日志文件不可写不应导致服务崩溃
            logger.warning("日志文件不可写（%s）：%s", log_file, exc)

    logger.debug("logging configured: level=%s file=%s", level, log_file)
    return logger


@dataclass
class RuntimePaths:
    """运行时目录（全部显式创建，不在 import 时落盘）。"""

    data_root: Path
    tasks: Path
    exports: Path
    logs: Path
    temp: Path

    @classmethod
    def build(cls, config: AppConfig, *, base_dir: Path | None = None) -> "RuntimePaths":
        root = config.storage.resolved_root(base_dir=base_dir)
        paths = cls(
            data_root=root,
            tasks=root / "tasks",
            exports=root / "exports",
            logs=root / "logs",
            temp=root / "temp",
        )
        return paths

    def ensure(self) -> "RuntimePaths":
        for path in (self.data_root, self.tasks, self.exports, self.logs, self.temp):
            path.mkdir(parents=True, exist_ok=True)
        return self


def config_summary_json(config: AppConfig) -> str:
    """脱敏配置摘要（用于日志/诊断，不含密钥）。"""

    return json.dumps(config.safe_dict(), ensure_ascii=False, sort_keys=True)
