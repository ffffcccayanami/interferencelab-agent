"""API 包。"""

from fastapi import APIRouter

from . import meta, runs, uploads
from . import monitor as monitor_api

router = APIRouter(prefix="/api")
router.include_router(meta.router)
router.include_router(uploads.router)
router.include_router(runs.router)
router.include_router(monitor_api.router)

__all__ = ["router"]
