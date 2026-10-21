"""受控验证执行器：固定 argv、无 shell、环境变量白名单、超时与输出落盘。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .registry import ValidationEntry, resolve_script

ENV_WHITELIST = ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "PYTHONIOENCODING", "PYTHONUTF8", "HOME")


def _child_env(workdir: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in ENV_WHITELIST}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env["HOME"] = str(workdir)
    return env


def run_entry(
    entry: ValidationEntry,
    params: dict[str, Any],
    *,
    run_dir: Path,
    timeout_s: int | None = None,
) -> dict[str, Any]:
    """执行 registry 里的脚本，返回退出码与输出路径（不含任何模型生成的命令）。"""

    script = resolve_script(entry)
    workdir = run_dir / "work"
    workdir.mkdir(parents=True, exist_ok=True)

    stdout_path = workdir / "validation_stdout.txt"
    stderr_path = workdir / "validation_stderr.txt"
    command = [sys.executable, str(script), "--params", json.dumps(params, ensure_ascii=False)]

    started = time.perf_counter()
    timed_out = False
    exit_code: int | None = None

    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        try:
            completed = subprocess.run(
                command,
                cwd=str(workdir),
                env=_child_env(workdir),
                stdout=stdout_handle,
                stderr=stderr_handle,
                timeout=timeout_s or entry.timeout_s,
                shell=False,
                check=False,
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = -1
        except OSError as exc:
            stderr_handle.write(f"启动脚本失败：{exc}".encode())

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")[-8000:]
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")[-8000:]

    return {
        "entry_id": entry.entry_id,
        "title": entry.title,
        "script": entry.script,
        "params": params,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }
