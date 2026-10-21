#!/usr/bin/env python
"""前端环境变量守卫：VITE_* 会被打包进浏览器，绝不能放密钥。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
FORBIDDEN = re.compile(r"^VITE_[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)
ALLOWED = {"VITE_API_BASE_URL"}


def main() -> int:
    problems: list[str] = []
    files = sorted(FRONTEND.glob(".env*"))
    if not files:
        print("[frontend-env] 未发现 frontend/.env*，跳过。")
        return 0

    for path in files:
        if not path.is_file():
            continue
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key in ALLOWED:
                continue
            if FORBIDDEN.search(key):
                problems.append(f"{path.name}:{lineno} {key} 不得出现在前端环境变量中（会被打包公开）")

    if problems:
        print("[frontend-env] 发现违规变量：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("[frontend-env] 通过：前端环境变量不含密钥。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
