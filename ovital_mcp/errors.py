"""统一错误类型。

约定：
- 工具层把领域错误转换成显式 ``status="error"`` 返回，并带上稳定的 ``code``。
- 禁止把错误伪装为空数据或成功。
- 异常消息里不得出现凭据（PSK）等敏感信息。
"""

from __future__ import annotations

from typing import Any


class OmapError(Exception):
    """所有领域错误的基类。"""

    code = "OMAP_ERROR"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if retryable is not None:
            self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.code}] {self.message}"


class ConfigError(OmapError):
    code = "CONFIG_ERROR"


class AuthError(OmapError):
    code = "OVITAL_AUTH_ERROR"


class OvitalConnectionError(OmapError):
    code = "OVITAL_CONNECTION_ERROR"
    retryable = True


class OvitalProtocolError(OmapError):
    """请求/响应不符合官方协议（ret<0、字段缺失、帧格式错误等）。"""

    code = "OVITAL_PROTOCOL_ERROR"


class OvitalPrereqError(OmapError):
    """会员权限/额度/点数等前置条件不满足（例如地图图片需要 SVIP）。"""

    code = "OVITAL_PREREQ_ERROR"


class OvitalNotFoundError(OmapError):
    code = "OVITAL_NOT_FOUND"


class CoordinateSemanticError(OmapError):
    """坐标语义无法安全解释（未知枚举、禁止的 GCJ-02 自动转换等）。"""

    code = "COORDINATE_SEMANTIC_ERROR"


class DataValidationError(OmapError):
    code = "DATA_VALIDATION_ERROR"


class GeometryError(OmapError):
    code = "GEOMETRY_ERROR"


class EngineUnavailableError(OmapError):
    code = "ENGINE_UNAVAILABLE"


class EngineError(OmapError):
    code = "ENGINE_ERROR"


class ReviewFigureError(OmapError):
    code = "REVIEW_FIGURE_ERROR"


class JobNotFoundError(OmapError):
    code = "JOB_NOT_FOUND"


class JobCancelledError(OmapError):
    code = "JOB_CANCELLED"


class JobStateError(OmapError):
    code = "JOB_STATE_ERROR"


class StoreError(OmapError):
    code = "STORE_ERROR"


class IdempotencyConflictError(OmapError):
    """同一幂等键已存在但参数不同，或写后状态与预期不符。"""

    code = "IDEMPOTENCY_CONFLICT"


class WriteVerificationError(OmapError):
    """写入后回读与预期不一致。"""

    code = "WRITE_VERIFICATION_ERROR"


class ExportError(OmapError):
    code = "EXPORT_ERROR"
