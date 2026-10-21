"""模型厂商/部署预设：一套任务代码接不同 API 与本地服务。

设计原则
--------
* 预设只提供**默认值**，显式环境变量永远覆盖；
* 传输层只有三种：``openai``（OpenAI 兼容）、``anthropic``、``gemini``；
* 部署形态（local / tunnel / cloud）只用于展示与文档，不改变协议。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    label: str
    transport: str
    base_url: str
    auth_mode: str
    health_path: str
    default_model: str | None = None
    deployment: str = "cloud"
    notes: str = ""

    def public(self) -> dict[str, str]:
        return {
            "name": self.name,
            "label": self.label,
            "transport": self.transport,
            "base_url": self.base_url,
            "auth_mode": self.auth_mode,
            "deployment": self.deployment,
            "default_model": self.default_model or "",
        }


PROFILES: dict[str, ProviderProfile] = {
    "deepseek": ProviderProfile(
        name="deepseek",
        label="DeepSeek",
        transport="openai",
        base_url="https://api.deepseek.com",
        auth_mode="bearer",
        health_path="/models",
        default_model="deepseek-chat",
        notes="OpenAI 兼容；chat 与 reasoner 都可用",
    ),
    "openai": ProviderProfile(
        name="openai",
        label="OpenAI",
        transport="openai",
        base_url="https://api.openai.com/v1",
        auth_mode="bearer",
        health_path="/models",
        deployment="cloud",
    ),
    "qwen": ProviderProfile(
        name="qwen",
        label="通义千问（兼容模式）",
        transport="openai",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        auth_mode="bearer",
        health_path="/models",
        notes="DashScope 的 OpenAI 兼容入口",
    ),
    "vllm": ProviderProfile(
        name="vllm",
        label="vLLM（本地/隧道）",
        transport="openai",
        base_url="http://127.0.0.1:8000/v1",
        auth_mode="none",
        health_path="/models",
        deployment="local",
        notes="SSH 隧道时同样是 127.0.0.1:8000",
    ),
    "ollama": ProviderProfile(
        name="ollama",
        label="Ollama",
        transport="openai",
        base_url="http://127.0.0.1:11434/v1",
        auth_mode="none",
        health_path="/models",
        deployment="local",
    ),
    "lmstudio": ProviderProfile(
        name="lmstudio",
        label="LM Studio",
        transport="openai",
        base_url="http://127.0.0.1:1234/v1",
        auth_mode="none",
        health_path="/models",
        deployment="local",
    ),
    "anthropic": ProviderProfile(
        name="anthropic",
        label="Anthropic Claude",
        transport="anthropic",
        base_url="https://api.anthropic.com",
        auth_mode="x-api-key",
        health_path="/v1/models",
        deployment="cloud",
        notes="Messages API；需要 anthropic-version 头",
    ),
    "gemini": ProviderProfile(
        name="gemini",
        label="Google Gemini",
        transport="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        auth_mode="query",
        health_path="/models",
        deployment="cloud",
        notes="generateContent；当前仅非流式（TTFT 记 null）",
    ),
}


def get_profile(name: str | None) -> ProviderProfile | None:
    if not name:
        return None
    return PROFILES.get(name.strip().lower())


def detect_deployment(base_url: str) -> str:
    """按 base_url 推导部署形态：本地回环 → local，其余 → cloud。"""

    try:
        host = urlparse(base_url).hostname or ""
    except ValueError:
        return "unknown"
    if host in LOCAL_HOSTS:
        return "local"
    return "cloud"


def list_profiles() -> list[dict[str, str]]:
    return [profile.public() for profile in PROFILES.values()]
