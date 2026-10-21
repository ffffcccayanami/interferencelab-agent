"""请求/响应模型、错误码与领域异常。"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

TaskType = Literal["analysis", "research", "validation"]
TASK_TYPES: tuple[str, ...] = ("analysis", "research", "validation")


class ErrorCode(str, Enum):
    """对外暴露的稳定错误码（前端按码做提示）。"""

    UNSUPPORTED_FILE = "UNSUPPORTED_FILE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    PARSE_ERROR = "PARSE_ERROR"
    NON_PAIRWISE_INPUT = "NON_PAIRWISE_INPUT"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NO_EVIDENCE_DOCUMENTS = "NO_EVIDENCE_DOCUMENTS"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_FORMAT_ERROR = "LLM_FORMAT_ERROR"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_MISMATCH = "APPROVAL_MISMATCH"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    CANCELLED = "CANCELLED"
    QUEUE_FULL = "QUEUE_FULL"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INTERNAL = "INTERNAL"


class ApiError(Exception):
    """HTTP 层错误，统一序列化为 {"error": {"code", "message", "details"}}。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status_code: int = 400,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class RunFailure(Exception):
    """任务执行失败：由 worker 落库为 failed 并写入 error 事件。"""

    def __init__(self, code: ErrorCode, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class RunCancelled(Exception):
    """任务被用户取消。"""


class ApprovalRequired(Exception):
    """受控验证缺少人工审批（内部使用，正常路径通过状态表达）。"""


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class RunCreate(BaseModel):
    task_type: TaskType
    question: str = Field(min_length=1, max_length=4000)
    upload_ids: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)


class ApproveRequest(BaseModel):
    approval_id: str = Field(min_length=8, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 响应模型（用于 OpenAPI 文档；实现中返回 dict）
# ---------------------------------------------------------------------------
class UploadOut(BaseModel):
    upload_id: str
    filename: str
    size: int
    sha256: str
    detected_type: str


class ArtifactOut(BaseModel):
    artifact_id: str
    kind: str
    filename: str
    size: int
    sha256: str
    download_url: str


class RunOut(BaseModel):
    run_id: str
    task_type: str
    status: str
    stage: str
    question: str
    degraded: bool = False
    error: dict[str, Any] | None = None
    pending_approval: dict[str, Any] | None = None
    artifacts: list[ArtifactOut] = Field(default_factory=list)
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


class MetaOut(BaseModel):
    task_types: list[str]
    llm_configured: bool
    llm_model_id: str
    llm_key_fingerprint: str
    llm_endpoint: str
    llm_auth_mode: str
    llm_provider: str
    llm_profile: str
    llm_deployment: str
    config_source: str
    max_upload_mb: int
    queue_size: int
    workers: int
    gpu_concurrency: int
    cpu_concurrency: int
    validation_entries: list[dict[str, Any]]


class LLMHealthOut(BaseModel):
    """模型端点连通性（GET /models，不消耗 token，不含密钥）。"""

    enabled: bool
    configured: bool
    provider: str = "openai"
    profile: str = ""
    deployment: str = "unknown"
    endpoint: str
    auth_mode: str
    model_id: str
    health_path: str
    reachable: bool
    status_code: int | None = None
    latency_ms: float | None = None
    models: list[str] = Field(default_factory=list)
    model_present: bool | None = None
    streaming_supported: str = "unknown"
    error: str | None = None
