"""前端静态挂载：/ 跳转 /app/，资源路径带 /app 前缀（需要先构建 dist）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

pytestmark = pytest.mark.skipif(
    not (DIST / "index.html").is_file(),
    reason="前端未构建（npm run build 后才会挂载）",
)


def test_root_redirects_to_workbench(client) -> None:  # type: ignore[no-untyped-def]
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/app/"


def test_app_index_references_prefixed_assets(client) -> None:  # type: ignore[no-untyped-def]
    index = client.get("/app/")
    assert index.status_code == 200
    assert 'src="/app/assets/' in index.text
    assert index.headers.get("cache-control") == "no-cache"


def test_app_assets_are_served(client) -> None:  # type: ignore[no-untyped-def]
    index = client.get("/app/")
    match = re.search(r'src="(/app/assets/[^"]+)"', index.text)
    assert match, "index.html 里没有找到脚本资源"
    asset = client.get(match.group(1))
    assert asset.status_code == 200
    assert len(asset.content) > 1000
