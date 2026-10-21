"""文件上传接口。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile

from ..schemas import ApiError, ErrorCode, UploadOut
from ..services.files import safe_filename, save_upload
from ..services.parsing import ParseError, detect_type
from .state import AppState, get_state

router = APIRouter(tags=["uploads"])


@router.post("/uploads", response_model=UploadOut, status_code=201)
async def upload_file(
    file: UploadFile = File(...),
    state: AppState = Depends(get_state),
) -> UploadOut:
    filename = safe_filename(file.filename or "upload.bin")
    # 先按扩展名做一次快速校验，避免把不支持的文件写盘
    try:
        preliminary = detect_type(filename)
    except ParseError as exc:
        raise ApiError(
            ErrorCode.UNSUPPORTED_FILE,
            exc.message,
            status_code=415,
            details=exc.details,
        ) from exc

    info = await save_upload(
        file,
        uploads_dir=state.settings.uploads_dir,
        max_bytes=state.settings.infl_max_upload_mb * 1024 * 1024,
        detected_type=preliminary,
    )
    # .txt 需要读内容才能区分 Prometheus / 技术文档
    info["detected_type"] = detect_type(info["original_name"], Path(info["stored_path"]))
    row = state.store.create_upload(**info)

    return UploadOut(
        upload_id=row["id"],
        filename=row["original_name"],
        size=row["size"],
        sha256=row["sha256"],
        detected_type=row["detected_type"],
    )
