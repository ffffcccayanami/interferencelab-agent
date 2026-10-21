#!/usr/bin/env python
"""安装版本化 Git 钩子（.githooks/ -> core.hooksPath）。

钩子本身随仓库提交，任何 clone 之后执行一次本脚本即可生效：

    python scripts/install_hooks.py
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / ".githooks"

PRE_COMMIT = """#!/bin/sh
# 由 scripts/install_hooks.py 安装：拦截真实 API Key 进入提交。
set -eu
PY=python
if [ -x "backend/.venv/Scripts/python.exe" ]; then
  PY="backend/.venv/Scripts/python.exe"
elif [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"
elif [ -x "backend/.venv/bin/python" ]; then
  PY="backend/.venv/bin/python"
fi
exec "$PY" scripts/secret_scan.py --staged
"""

PRE_PUSH = """#!/bin/sh
# 由 scripts/install_hooks.py 安装：推送前对整个工作区做一次兜底扫描。
set -eu
PY=python
if [ -x "backend/.venv/Scripts/python.exe" ]; then
  PY="backend/.venv/Scripts/python.exe"
elif [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"
elif [ -x "backend/.venv/bin/python" ]; then
  PY="backend/.venv/bin/python"
fi
exec "$PY" scripts/secret_scan.py --worktree
"""


def write_hook(name: str, content: str) -> Path:
    path = HOOKS_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    try:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    return path


def main() -> int:
    if not (ROOT / ".git").exists():
        print("[hooks] 当前目录还不是 Git 仓库，先执行 git init。", file=sys.stderr)
        return 1

    write_hook("pre-commit", PRE_COMMIT)
    write_hook("pre-push", PRE_PUSH)

    subprocess.run(["git", "-C", str(ROOT), "config", "core.hooksPath", ".githooks"], check=True)
    print("[hooks] 已安装 .githooks/pre-commit 与 pre-push，core.hooksPath=.githooks")
    print("[hooks] 提示：Windows 需要 Git for Windows 自带的 sh 解释钩子脚本。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
