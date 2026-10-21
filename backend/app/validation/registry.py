"""受控验证白名单：唯一允许执行的脚本集合。

LLM 只能"选择条目 + 填写参数"，不能提供脚本路径、命令或 URL。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schemas import ErrorCode, RunFailure

SCRIPT_DIR = Path(__file__).resolve().parent / "scripts"


@dataclass(frozen=True)
class ValidationEntry:
    entry_id: str
    title: str
    description: str
    script: str
    params: dict[str, dict[str, Any]] = field(default_factory=dict)
    timeout_s: int = 300
    requires_approval: bool = True

    def public(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "title": self.title,
            "description": self.description,
            "params": self.params,
            "timeout_s": self.timeout_s,
            "requires_approval": self.requires_approval,
        }


REGISTRY: dict[str, ValidationEntry] = {
    "bench_smoke": ValidationEntry(
        entry_id="bench_smoke",
        title="既有 benchmark 结果复算 smoke",
        description=(
            "在隔离目录中对固定参数做一次可复现的小规模复算，用于验证"
            "上传 → 审批 → 执行 → 产物 的完整链路；不代表整模型结论。"
        ),
        script="bench_smoke.py",
        params={
            "input_tokens": {"type": "int", "min": 1, "max": 200000, "default": 2800},
            "output_tokens": {"type": "int", "min": 1, "max": 4096, "default": 128},
            "concurrency": {"type": "int", "min": 1, "max": 8, "default": 4},
            "repeats": {"type": "int", "min": 1, "max": 10, "default": 2},
        },
        timeout_s=120,
    )
}


def list_entries() -> list[dict[str, Any]]:
    return [entry.public() for entry in REGISTRY.values()]


def get_entry(entry_id: str) -> ValidationEntry:
    entry = REGISTRY.get(entry_id)
    if entry is None:
        raise RunFailure(
            ErrorCode.VALIDATION_ERROR,
            f"未登记的验证条目：{entry_id}",
            {"available": sorted(REGISTRY)},
        )
    return entry


def validate_params(entry: ValidationEntry, raw: dict[str, Any] | None) -> dict[str, Any]:
    """按声明式 schema 校验并补默认值；未知参数直接拒绝。"""

    raw = dict(raw or {})
    unknown = set(raw) - set(entry.params)
    if unknown:
        raise RunFailure(
            ErrorCode.VALIDATION_ERROR,
            "验证参数包含未声明字段",
            {"unknown": sorted(unknown), "allowed": sorted(entry.params)},
        )

    resolved: dict[str, Any] = {}
    for name, spec in entry.params.items():
        value = raw.get(name, spec.get("default"))
        if value is None:
            raise RunFailure(ErrorCode.VALIDATION_ERROR, f"验证参数缺少 {name}", {"param": name})
        if spec.get("type") == "int":
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise RunFailure(ErrorCode.VALIDATION_ERROR, f"参数 {name} 必须是整数", {"param": name})
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise RunFailure(ErrorCode.VALIDATION_ERROR, f"参数 {name} 不是整数", {"param": name}) from exc
            if number < spec.get("min", number) or number > spec.get("max", number):
                raise RunFailure(
                    ErrorCode.VALIDATION_ERROR,
                    f"参数 {name} 超出允许范围 [{spec.get('min')}, {spec.get('max')}]",
                    {"param": name, "value": number},
                )
            resolved[name] = number
        elif spec.get("type") == "str":
            resolved[name] = str(value)[: spec.get("max_length", 200)]
        else:  # pragma: no cover - registry 只为 int/str
            resolved[name] = value
    return resolved


def canonical_params(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def approval_id_for(run_id: str, entry: ValidationEntry, params: dict[str, Any]) -> str:
    payload = f"{run_id}|{entry.entry_id}|{canonical_params(params)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_script(entry: ValidationEntry) -> Path:
    candidate = (SCRIPT_DIR / entry.script).resolve()
    if SCRIPT_DIR.resolve() not in candidate.parents:
        raise RunFailure(ErrorCode.PATH_TRAVERSAL, "验证脚本必须位于 app/validation/scripts 内")
    if not candidate.exists():
        raise RunFailure(ErrorCode.VALIDATION_ERROR, f"验证脚本不存在：{entry.script}")
    return candidate
