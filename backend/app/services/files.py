"""文件保存、哈希与路径越界防护。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from ..schemas import ApiError, ErrorCode

_FILENAME_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_filename(name: str, *, fallback: str = "upload.bin", limit: int = 80) -> str:
    """把用户提供的文件名压缩成安全的单段文件名。"""

    normalized = unicodedata.normalize("NFKC", name or "")
    base = Path(normalized.replace("\\", "/")).name
    cleaned = _FILENAME_UNSAFE.sub("_", base).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)[:limit]
    return cleaned or fallback


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def ensure_within(root: Path, candidate: Path) -> Path:
    """拒绝路径越界（``..``、符号链接逃逸等）。"""

    root_resolved = root.resolve()
    try:
        candidate_resolved = candidate.resolve()
    except OSError as exc:  # pragma: no cover
        raise ApiError(ErrorCode.PATH_TRAVERSAL, "路径不可解析", status_code=403) from exc

    if candidate_resolved == root_resolved:
        return candidate_resolved
    if root_resolved not in candidate_resolved.parents:
        raise ApiError(ErrorCode.PATH_TRAVERSAL, "禁止访问任务目录之外的路径", status_code=403)
    return candidate_resolved


async def save_upload(
    upload: UploadFile,
    *,
    uploads_dir: Path,
    max_bytes: int,
    detected_type: str,
) -> dict[str, Any]:
    """流式保存上传文件，超限即删除并报错。"""

    original_name = safe_filename(upload.filename or "upload.bin")
    upload_dir = uploads_dir / hashlib.sha256(original_name.encode("utf-8")).hexdigest()[:16]
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / original_name

    size = 0
    digest = hashlib.sha256()
    try:
        with target.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ApiError(
                        ErrorCode.FILE_TOO_LARGE,
                        f"文件超过上限 {max_bytes // (1024 * 1024)} MB",
                        status_code=413,
                    )
                digest.update(chunk)
                handle.write(chunk)
    except ApiError:
        target.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    if size == 0:
        target.unlink(missing_ok=True)
        raise ApiError(ErrorCode.VALIDATION_ERROR, "文件为空", status_code=400)

    return {
        "original_name": original_name,
        "stored_path": str(target),
        "size": size,
        "sha256": digest.hexdigest(),
        "content_type": upload.content_type or "",
        "detected_type": detected_type,
    }


def remove_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
