#!/usr/bin/env python
"""仓库密钥扫描器（预提交 / 预推送 / CI 共用）。

设计目标
--------
1. 真实 API Key 永不进入 Git 对象库：默认在工作区与暂存区拦截；
2. 只输出掩码（前 4 位），不把密钥回显到终端、日志或 CI；
3. 明确区分"密钥"与"占位符"，避免把 ``YOUR_LLM_API_KEY`` 误报。

用法
----
    python scripts/secret_scan.py --staged      # 预提交：只扫暂存内容
    python scripts/secret_scan.py --worktree    # 工作区（含未跟踪文件）
    python scripts/secret_scan.py --worktree --include-fixtures
    python scripts/secret_scan.py --staged --fix

``--fix`` 只允许改写模板/示例类文件（``*.example``、``docs/**``），把命中的
真实值替换为 ``YOUR_SECRET_HERE``；它不会静默改写 ``backend/.env``。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 规则：名称 -> 正则。命中任意一条即视为疑似真实凭据。
# 赋值类规则排除常见的"代码引用"（self./settings./os.getenv 等），避免把形参当密钥。
_CODE_REFERENCE = r"(?!self\.|cls\.|settings\.|config\.|os\.|env\.|request\.|payload\.|getenv|environ)"

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("deepseek_openai_style_key", re.compile(r"sk-[A-Za-z0-9]{16,}")),
    ("tavily_key", re.compile(r"tvly-[A-Za-z0-9\-]{16,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "assigned_secret",
        re.compile(
            r"(?i)(?:api[_-]?key|access[_-]?key|token|secret|password|credential)"
            r"\s*[:=]\s*[\"']?"
            + _CODE_REFERENCE
            + r"([A-Za-z0-9_\-\.]{20,})"
        ),
    ),
    (
        "long_hex_secret",
        re.compile(
            r"(?i)(?:api[_-]?key|token|secret)\s*[:=]\s*[\"']?"
            + _CODE_REFERENCE
            + r"([0-9a-f]{32,})"
        ),
    ),
)

# 占位符白名单：出现这些片段的值一律放行（大小写不敏感）。
PLACEHOLDER_TOKENS: tuple[str, ...] = (
    "your_",
    "your-",
    "changeme",
    "change_me",
    "set_me",
    "placeholder",
    "example",
    "sample",
    "${",
    "<",
    "***",
    "xxx",
    "dummy",
    "test-key",
)

ALLOW_COMMENT = "allow-secret:"

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    "var",
    ".uv-cache",
    ".pytest_cache",
    ".ruff_cache",
}

SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".7z",
    ".woff",
    ".woff2",
    ".ttf",
    ".vsdx",
    ".pyc",
    ".sqlite3",
    ".db",
}

MAX_BYTES = 2 * 1024 * 1024

# --fix 允许触碰的路径模式（只改模板/文档，不改真实 .env）
FIXABLE_PATTERNS = ("*.example", "*.example.*", "docs/*.md", "docs/**/*.md", "README.md", "*.md")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    masked: str
    snippet: str


def _mask(value: str) -> str:
    value = value.strip()
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:4]}...{'*' * min(8, len(value) - 4)}"


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in PLACEHOLDER_TOKENS)


def _looks_like_secret(value: str) -> bool:
    if not value or _is_placeholder(value):
        return False
    return any(pattern.search(value) for _, pattern in RULES)


def scan_text(text: str, rel_path: str) -> list[Finding]:
    """扫描一段文本，返回掩码后的命中项。"""

    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or ALLOW_COMMENT in line:
            continue
        for rule, pattern in RULES:
            match = pattern.search(line)
            if not match:
                continue
            candidate = match.group(1) if match.groups() else match.group(0)
            if _is_placeholder(candidate):
                continue
            findings.append(
                Finding(
                    path=rel_path,
                    line=lineno,
                    rule=rule,
                    masked=_mask(candidate),
                    snippet="<redacted>",
                )
            )
            break
    return findings


def _git(*args: str) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [item for item in completed.stdout.decode("utf-8", "replace").split("\0") if item]


def _files_from_git(staged: bool) -> list[Path]:
    if staged:
        names = _git("diff", "--cached", "--name-only", "--diff-filter=ACM", "-z")
    else:
        names = _git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return [ROOT / name for name in names]


def _walk_worktree() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def _readable(path: Path) -> bool:
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    try:
        return path.stat().st_size <= MAX_BYTES
    except OSError:
        return False


# 本机凭据的"合法存放位置"：这些文件本来就要放真实值，工作区扫描应跳过。
# （它们已被 .gitignore 忽略，提交扫描 --staged 仍然会拦截误加的情况。）
_LOCAL_SECRET_NAMES = re.compile(r"^(\.env(\..+)?|id_rsa.*|id_ed25519.*|.*\.(pem|key|p12|pfx))$", re.IGNORECASE)


def is_local_secret_file(path: Path) -> bool:
    name = path.name
    if name.endswith(".example"):
        return False
    return bool(_LOCAL_SECRET_NAMES.match(name))


def _is_fixable(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    return any(path.match(pattern) or Path(rel).match(pattern) for pattern in FIXABLE_PATTERNS)


def _fix_file(path: Path) -> int:
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    fixed = original
    for _, pattern in RULES:
        fixed = pattern.sub(lambda m: m.group(0).replace(m.group(1), "YOUR_SECRET_HERE"), fixed)
    if fixed == original:
        return 0
    path.write_text(fixed, encoding="utf-8")
    return 1


def collect(staged: bool, include_fixtures: bool, worktree: bool) -> list[Finding]:
    paths = _walk_worktree() if worktree else _files_from_git(staged)
    findings: list[Finding] = []
    for path in paths:
        if not path.is_file() or not _readable(path):
            continue
        if is_local_secret_file(path):
            continue
        if "fixtures/" in path.as_posix() and not include_fixtures:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(scan_text(text, path.relative_to(ROOT).as_posix()))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描并拦截真实 API Key")
    parser.add_argument("--staged", action="store_true", help="只扫描暂存区新增/修改内容")
    parser.add_argument("--worktree", action="store_true", help="扫描整个工作区（含未跟踪文件）")
    parser.add_argument("--include-fixtures", action="store_true", help="连 fixtures/ 一起扫描")
    parser.add_argument("--fix", action="store_true", help="把模板/文档中的真实值替换为占位符")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    worktree = args.worktree or not args.staged
    findings = collect(staged=args.staged, include_fixtures=args.include_fixtures, worktree=worktree)

    if args.fix:
        fixed = 0
        for rel in sorted({item.path for item in findings}):
            path = ROOT / rel
            if _is_fixable(path):
                fixed += _fix_file(path)
        if fixed and not args.quiet:
            print(f"[secret-scan] 已把 {fixed} 个模板文件中的真实值替换为占位符。")
        findings = [item for item in findings if not _is_fixable(ROOT / item.path)]

    if not findings:
        if not args.quiet:
            print("[secret-scan] 未发现真实凭据。")
        return 0

    print("[secret-scan] 检测到疑似真实凭据（已掩码）：", file=sys.stderr)
    for item in findings:
        print(f"  - {item.path}:{item.line}  [{item.rule}]  {item.masked}", file=sys.stderr)
    print(
        "\n处理方式：\n"
        "  1) 若该值真实有效：立即在服务商后台轮换/吊销，然后执行 "
        "`python scripts/secret_scan.py --staged --fix`（仅模板）或手工改为占位符；\n"
        "  2) 如为误报：在该行加注释 `# allow-secret: <原因>` 并走人工 review。\n"
        "  3) 提交被拦截时不要使用 `git commit --no-verify` 绕过。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
