"""监控接口：窗口聚合（任务 + 模型调用 + 节点耗时）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ..services import monitor
from .state import AppState, get_state

router = APIRouter(tags=["monitor"])


@router.get("/monitor/summary")
def monitor_summary(
    hours: float = Query(default=0, ge=0, le=24 * 30, description="0 表示使用配置默认窗口"),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    window = hours or float(state.settings.infl_monitor_window_hours)
    return monitor.summary(state.store, state.settings, hours=window)


@router.get("/monitor/trend")
def monitor_trend(
    hours: float = Query(default=24, ge=1, le=24 * 30),
    buckets: int = Query(default=24, ge=4, le=96),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """时间分桶趋势（折线图数据源），不触发任何模型调用。"""

    return monitor.trend(
        state.store,
        hours=hours,
        buckets=buckets,
        max_calls=state.settings.infl_monitor_max_calls,
    )
