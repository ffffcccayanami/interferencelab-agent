"""应用配置。

配置文件查找顺序（先找到先用）::

    1. backend/.env      —— 后端目录内的本地配置（推荐）
    2. <repo>/.env       —— 仓库根目录配置（从 .env.example 复制后直接可用）

安全约定
--------
* 密钥字段使用 ``SecretStr``，不会出现在 ``repr``、日志或 ``/api/meta``；
* 启动时校验占位符与 base_url，避免"看起来启动了、其实连不上模型"的半残状态；
* ``LLM_AUTH_MODE=none`` 用于无鉴权的自建/隧道端点（例如本机 vLLM）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .services.providers import ProviderProfile, detect_deployment, get_profile

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent

# 候选配置文件：后者是从仓库根 .env.example 复制出来的常见位置
ENV_CANDIDATES: tuple[Path, ...] = (BACKEND_DIR / ".env", REPO_DIR / ".env")

PLACEHOLDER_MARKERS = (
    "your_",
    "your-",
    "changeme",
    "change_me",
    "set_me",
    "placeholder",
    "example",
    "<",
    "***",
    "xxx",
)

# 模型 id 的占位符判断更保守：真实 id 可能包含 example 之类的词
MODEL_PLACEHOLDER_MARKERS = ("your_", "your-", "changeme", "change_me", "set_me", "placeholder", "<", "xxx")


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def resolve_env_file() -> Path | None:
    """返回实际生效的 .env（不存在则返回 None，仅用进程环境变量）。"""

    for candidate in ENV_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


class Settings(BaseSettings):
    """后端配置，全部来自环境变量或 .env。"""

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM（真实值仅存在于本地 .env）---
    llm_enabled: bool = True
    # 部署预设：deepseek/openai/qwen/vllm/ollama/lmstudio/anthropic/gemini
    llm_profile: str = ""
    # 传输层：openai（OpenAI 兼容）/ anthropic / gemini；auto 由 profile 推导
    llm_provider: Literal["auto", "openai", "anthropic", "gemini"] = "auto"
    # 部署形态仅用于展示：auto 按 base_url 推导 local/cloud
    llm_deployment: Literal["auto", "local", "tunnel", "cloud"] = "auto"
    llm_model_id: str = ""
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    # autox：由 profile 决定；bearer / none / x-api-key / query
    llm_auth_mode: Literal["auto", "bearer", "none", "x-api-key", "query"] = "auto"
    llm_timeout: float = 120.0
    # 连通性探测：GET <base_url><health_path>，默认 /models（不消耗 token）
    llm_health_path: str = ""
    llm_health_timeout: float = 5.0
    # 推理模型会把思考 token 计入 max_tokens，输出上限要留足余量
    llm_max_output_tokens: int = 2048
    llm_max_calls_per_run: int = 8
    llm_max_prompt_chars: int = 24000
    # 流式观测：TTFT 依赖流式；网关不支持时自动降级
    llm_streaming: bool = True
    llm_stream_include_usage: bool = True
    # 可选：vLLM 类端点的 Prometheus 指标（仅隧道内可达，不要对公网开放）
    llm_metrics_url: str = ""
    llm_metrics_timeout: float = 5.0

    # --- 监控与告警（非密钥）---
    infl_monitor_window_hours: int = 24
    infl_monitor_max_calls: int = 2000
    infl_alert_ttft_p95_ms: float = 0.0
    infl_alert_run_error_rate: float = 0.2

    # --- P4 评测判据（非密钥）---
    infl_eval_enabled: bool = True
    infl_eval_min_throughput_pct: float = 0.0
    infl_eval_min_ttft_reduction_pct: float = 0.0
    infl_eval_max_error_rate: float = 1.0
    infl_eval_regression_tolerance_pct: float = 5.0

    # --- 运行参数（非密钥，可公开）---
    infl_api_host: str = "127.0.0.1"
    infl_api_port: int = 8010
    infl_data_dir: Path = Field(default=Path("./var"))
    infl_max_upload_mb: int = 20
    infl_queue_size: int = 4
    # 并行执行：worker 总数 + 按任务类型的并发闸门
    # analysis 占用 GPU，默认串行；research / validation 可并行
    infl_workers: int = 3
    infl_gpu_concurrency: int = 1
    infl_cpu_concurrency: int = 2
    infl_validation_timeout_s: int = 300
    infl_min_group_samples: int = 2
    infl_cors_origins: str = "http://127.0.0.1:5175,http://localhost:5175"

    # 记录生效的配置文件（便于排查"改了 .env 却没生效"），不参与序列化输出密钥
    config_source: str = Field(default="", exclude=True)

    def model_post_init(self, __context: object) -> None:
        """按 ``LLM_PROFILE`` 补齐缺省值；显式配置永远优先。"""

        profile = get_profile(self.llm_profile)
        if profile is not None:
            if not self.llm_base_url.strip():
                self.llm_base_url = profile.base_url
            if self.llm_auth_mode == "auto":
                self.llm_auth_mode = profile.auth_mode  # type: ignore[assignment]
            if not self.llm_health_path.strip():
                self.llm_health_path = profile.health_path
            current_model = self.llm_model_id.strip()
            model_is_placeholder = not current_model or current_model == "unset" or any(
                marker in current_model.lower() for marker in MODEL_PLACEHOLDER_MARKERS
            )
            if model_is_placeholder and profile.default_model:
                self.llm_model_id = profile.default_model
            if self.llm_provider == "auto":
                self.llm_provider = profile.transport  # type: ignore[assignment]
        if self.llm_provider == "auto":
            self.llm_provider = "openai"
        if self.llm_auth_mode == "auto":
            self.llm_auth_mode = "bearer"
        if not self.llm_health_path.strip():
            self.llm_health_path = "/models"

    @property
    def profile(self) -> ProviderProfile | None:
        return get_profile(self.llm_profile)

    @property
    def deployment(self) -> str:
        """展示用的部署形态：显式配置 > 预设 > 按 base_url 推导。"""

        if self.llm_deployment != "auto":
            return self.llm_deployment
        profile = self.profile
        if profile is not None:
            return profile.deployment
        if self.llm_base_url_normalized:
            return detect_deployment(self.llm_base_url_normalized)
        return "unknown"

    @property
    def data_dir(self) -> Path:
        """运行期数据根目录（相对路径按 backend/ 解析）。"""

        if self.infl_data_dir.is_absolute():
            return self.infl_data_dir
        return (BACKEND_DIR / self.infl_data_dir).resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.infl_cors_origins.split(",") if item.strip()]

    @property
    def llm_api_key_value(self) -> str:
        return self.llm_api_key.get_secret_value()

    @property
    def llm_base_url_normalized(self) -> str:
        """去掉末尾斜杠，保留 /v1 等前缀。"""

        return self.llm_base_url.strip().rstrip("/")

    @property
    def llm_health_url(self) -> str:
        path = self.llm_health_path if self.llm_health_path.startswith("/") else f"/{self.llm_health_path}"
        return f"{self.llm_base_url_normalized}{path}"

    @property
    def llm_endpoint(self) -> str:
        """可安全展示的 host:port（不含路径、查询串与密钥）。"""

        base = self.llm_base_url_normalized
        if "://" in base:
            base = base.split("://", 1)[1]
        host_port = base.split("/", 1)[0]
        return host_port or "unset"

    @property
    def llm_configured(self) -> bool:
        if not self.llm_enabled or not self.llm_base_url_normalized or not self.llm_model_id.strip():
            return False
        if self.llm_provider in ("anthropic", "gemini"):
            return bool(self.llm_api_key_value.strip())
        return self.llm_auth_mode == "none" or bool(self.llm_api_key_value.strip())

    def llm_key_fingerprint(self) -> str:
        """返回可安全展示的指纹（前 4 位 + 后 4 位），未配置时返回 unset。"""

        value = self.llm_api_key_value.strip()
        if len(value) < 8:
            return "unset"
        return f"{value[:4]}...{value[-4:]}"

    def validate_for_runtime(self) -> None:
        """启动校验：开启 LLM 时必须有可用的 base_url / model，并按鉴权模式检查密钥。"""

        if not self.llm_enabled:
            return

        base = self.llm_base_url_normalized
        if not base or not self.llm_model_id.strip():
            raise ConfigError(
                "LLM_BASE_URL / LLM_MODEL_ID 未配置。请在 backend/.env（或仓库根 .env）中填写，"
                "参考 .env.example 与 docs/LLM_ENDPOINT.md。"
            )
        if not base.startswith(("http://", "https://")):
            raise ConfigError(
                f"LLM_BASE_URL 必须以 http:// 或 https:// 开头，当前为 {base!r}。"
                "本地/隧道端点示例：http://127.0.0.1:8000/v1"
            )
        if any(marker in self.llm_model_id.strip().lower() for marker in MODEL_PLACEHOLDER_MARKERS):
            raise ConfigError(
                "LLM_MODEL_ID 仍是占位符。请填写模型服务端 /models 返回的 id"
                "（vLLM 用 --served-model-name 的值）。"
            )

        profile = self.profile
        if profile is not None and self.llm_provider != profile.transport:
            raise ConfigError(
                f"LLM_PROFILE={profile.name} 的传输类型是 {profile.transport}，"
                f"但 LLM_PROVIDER={self.llm_provider}；请删除其中一个或改成一致。"
            )

        if self.llm_provider in ("anthropic", "gemini"):
            key = self.llm_api_key_value.strip()
            if not key:
                raise ConfigError(
                    f"LLM_PROVIDER={self.llm_provider} 需要 API key（对应厂商的 x-api-key / query key）。"
                )
            if any(marker in key.lower() for marker in PLACEHOLDER_MARKERS):
                raise ConfigError("LLM_API_KEY 仍是占位符，请填写真实值。")
            return

        if self.llm_auth_mode == "none":
            # 自建或隧道端点通常不带鉴权；此时 key 留空是合法的。
            return

        key = self.llm_api_key_value.strip().lower()
        if not key:
            raise ConfigError(
                "LLM_AUTH_MODE=bearer 但 LLM_API_KEY 为空。请填写真实 key；"
                "若端点无需鉴权，设置 LLM_AUTH_MODE=none。"
            )
        if any(marker in key for marker in PLACEHOLDER_MARKERS):
            raise ConfigError(
                "LLM_API_KEY 仍是占位符。请在 backend/.env（或仓库根 .env）中填写真实值；"
                "若端点无需鉴权（本机 vLLM / SSH 隧道），设置 LLM_AUTH_MODE=none；"
                "若只想做离线演示，可设置 LLM_ENABLED=false。"
            )
        if not key.startswith("sk-") and len(key) < 16:
            raise ConfigError("LLM_API_KEY 长度异常，请确认复制完整。")

    def ensure_dirs(self) -> None:
        for directory in (self.data_dir, self.uploads_dir, self.runs_dir, self.data_dir / "tmp"):
            directory.mkdir(parents=True, exist_ok=True)


def load_settings(**overrides: object) -> Settings:
    """构造并校验配置。

    ``load_settings()``                 → 自动查找 backend/.env 或仓库根 .env
    ``load_settings(_env_file=None)``   → 只用进程环境变量（测试用）
    ``load_settings(_env_file="x.env")``→ 指定文件
    """

    marker = object()
    explicit_env_file: object = overrides.pop("_env_file", marker)
    env_file = resolve_env_file() if explicit_env_file is marker else explicit_env_file

    settings = Settings(_env_file=env_file, **overrides)  # type: ignore[arg-type]
    settings.config_source = str(env_file) if env_file else "env vars only"
    settings.validate_for_runtime()
    settings.ensure_dirs()
    return settings
