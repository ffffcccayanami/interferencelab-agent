#!/usr/bin/env python
"""从本地 .env 生成/校验 .env.example（只保留占位符）。

真实值永远留在本地 .env（已被 .gitignore 忽略）；模板文件可以安全提交。

    python scripts/env_template.py --write    # 用本地 .env 的值刷新模板（保留模板里的说明）
    python scripts/env_template.py --check    # 校验模板里没有真实值
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / ".env.example"
ENV_CANDIDATES = (ROOT / "backend" / ".env", ROOT / ".env")

SECRET_KEY_PATTERN = re.compile(
    r"(API[_-]?KEY|ACCESS[_-]?KEY|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE
)
# `LLM_MAX_OUTPUT_TOKENS` 这类"数量"字段不是密钥；只把结尾是 _TOKEN 的当成凭据
SECRET_TOKEN_SUFFIX = re.compile(r"(?:^|_)TOKEN$", re.IGNORECASE)
ASSIGNMENT_PATTERN = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$")

HEADER = (
    "# ---------------------------------------------------------------------------\n"
    "# 该文件由 scripts/env_template.py 生成，只包含占位符。\n"
    "# 使用方式（二选一，前者优先）：\n"
    "#   cp .env.example backend/.env\n"
    "#   cp .env.example .env\n"
    "# 两个文件都已被 .gitignore 忽略；禁止在本文件中写入真实 API Key。\n"
    "# ---------------------------------------------------------------------------\n"
)

sys.path.insert(0, str(ROOT / "scripts"))
from secret_scan import RULES  # noqa: E402  （复用同一套密钥规则）


def default_env_path() -> Path:
    for candidate in ENV_CANDIDATES:
        if candidate.is_file():
            return candidate
    return ENV_CANDIDATES[0]


def is_secret_key(key: str) -> bool:
    if SECRET_KEY_PATTERN.search(key):
        return True
    return bool(SECRET_TOKEN_SUFFIX.search(key))


def _parse_assignments(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        match = ASSIGNMENT_PATTERN.match(raw.strip())
        if match and not raw.strip().startswith("#"):
            values[match.group("key")] = match.group("value").strip()
    return values


def render_template(source: str) -> str:
    """把一段 .env 文本整体转成模板（所有 key 都用占位符）。"""

    lines: list[str] = []
    for raw in source.splitlines():
        match = ASSIGNMENT_PATTERN.match(raw.strip())
        if not match:
            lines.append(raw)
            continue
        key = match.group("key")
        value = match.group("value").strip()
        if is_secret_key(key):
            inline_comment = "  #" + value.split("#", 1)[1].rstrip() if "#" in value else ""
            lines.append(f"{key}=YOUR_{key}{inline_comment}")
        else:
            lines.append(f"{key}={value}")
    return HEADER + "\n".join(lines).rstrip() + "\n"


def merge_template(template_text: str, env_text: str) -> str:
    """保留模板里的说明与默认值，只把密钥类变量替换成占位符。

    * 模板已声明的 key：保留模板的注释/默认值；密钥类一律占位符；
    * 本地 .env 里有、模板里没有的 key：追加到末尾（密钥类同样占位符）。
    """

    env_values = _parse_assignments(env_text)
    lines: list[str] = []
    seen: set[str] = set()

    for raw in template_text.splitlines():
        match = ASSIGNMENT_PATTERN.match(raw.strip())
        if not match or raw.strip().startswith("#"):
            lines.append(raw)
            continue
        key = match.group("key")
        seen.add(key)
        lines.append(f"{key}=YOUR_{key}" if is_secret_key(key) else raw)

    missing = [key for key in env_values if key not in seen]
    if missing:
        lines += ["", "# 以下变量来自本地 .env，模板中原本没有（请确认是否需要公开）"]
        for key in missing:
            lines.append(f"{key}=YOUR_{key}" if is_secret_key(key) else f"{key}={env_values[key]}")

    return "\n".join(lines).rstrip() + "\n"


def is_placeholder_value(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return True
    return (
        lowered.startswith("your_")
        or lowered.startswith("your-")
        or lowered.startswith("<")
        or "${" in lowered
        or lowered in {"", "unset", "none", "***", "changeme", "change_me", "placeholder", "example"}
    )


def looks_secret(value: str) -> bool:
    if is_placeholder_value(value):
        return False
    return any(pattern.search(value) for _, pattern in RULES)


def check(template: Path = TEMPLATE_PATH, env: Path | None = None) -> list[str]:
    env = env or default_env_path()
    problems: list[str] = []
    if not template.exists():
        return [f"模板文件不存在：{template.relative_to(ROOT)}（先执行 --write）"]

    real_values: set[str] = set()
    if env.exists():
        for value in _parse_assignments(env.read_text(encoding="utf-8")).values():
            cleaned = value.strip().strip('"').strip("'")
            if cleaned:
                real_values.add(cleaned)

    for lineno, raw in enumerate(template.read_text(encoding="utf-8").splitlines(), start=1):
        match = ASSIGNMENT_PATTERN.match(raw.strip())
        if not match or raw.strip().startswith("#"):
            continue
        key, value = match.group("key"), match.group("value").strip().strip('"').strip("'")
        if not value:
            continue
        if value in real_values and is_secret_key(key) and not is_placeholder_value(value):
            problems.append(f".env.example:{lineno} {key} 的值与本地 .env 中的真实值相同")
        elif looks_secret(value):
            problems.append(f".env.example:{lineno} {key} 疑似真实凭据（已掩码 {value[:4]}...）")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成/校验 .env.example 占位符模板")
    parser.add_argument("--write", action="store_true", help="用本地 .env 刷新模板（保留说明）")
    parser.add_argument("--check", action="store_true", help="校验模板不含真实值")
    parser.add_argument("--env-path", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=TEMPLATE_PATH)
    args = parser.parse_args(argv)

    env_path = args.env_path or default_env_path()

    if args.write:
        if not env_path.exists():
            print(f"[env-template] 未找到 {env_path}，请先创建并填写。", file=sys.stderr)
            return 1
        env_text = env_path.read_text(encoding="utf-8")
        if args.out.exists():
            content = merge_template(args.out.read_text(encoding="utf-8"), env_text)
        else:
            content = render_template(env_text)
        args.out.write_text(content, encoding="utf-8", newline="\n")
        print(f"[env-template] 已用 {env_path.relative_to(ROOT)} 刷新 {args.out.relative_to(ROOT)}（占位符模式）。")

    if args.check or not args.write:
        problems = check(args.out, env_path)
        if problems:
            print("[env-template] 模板校验失败：", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        print("[env-template] 模板校验通过：未发现真实凭据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
