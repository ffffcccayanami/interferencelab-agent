"""产物登记：写文件 + 计算相对路径/大小/哈希 + 落库。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..db.store import Store
from .files import ensure_within, sha256_file


def register_artifact(
    store: Store,
    *,
    run_id: str,
    run_dir: Path,
    path: Path,
    kind: str,
    filename: str | None = None,
) -> dict[str, Any]:
    """把 run 目录内的文件登记为可下载产物。"""

    absolute = ensure_within(run_dir, path)
    rel_path = absolute.relative_to(run_dir.resolve()).as_posix()
    return store.add_artifact(
        run_id,
        kind=kind,
        filename=filename or absolute.name,
        rel_path=rel_path,
        size=absolute.stat().st_size,
        sha256=sha256_file(absolute),
    )


def write_text_artifact(
    store: Store,
    *,
    run_id: str,
    run_dir: Path,
    name: str,
    text: str,
    kind: str,
) -> dict[str, Any]:
    path = run_dir / "artifacts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return register_artifact(store, run_id=run_id, run_dir=run_dir, path=path, kind=kind)
