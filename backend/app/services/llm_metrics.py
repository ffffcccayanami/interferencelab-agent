"""模型调用记录器：把每次调用写入 SQLite 并推送 SSE 事件。"""

from __future__ import annotations

from ..db.store import Store
from ..events import EventBus, emit
from .llm import LLMCallRecord


class LLMCallRecorder:
    """callable，供 LLMClient 在每次尝试结束时回调。

    只保存数字与枚举，不保存 prompt / 回复正文，避免把用户数据写进指标表。
    """

    def __init__(self, store: Store, bus: EventBus, run_id: str) -> None:
        self.store = store
        self.bus = bus
        self.run_id = run_id

    def __call__(self, record: LLMCallRecord) -> None:
        row = self.store.add_llm_call(self.run_id, record.to_payload())
        payload = {
            "call_id": row.get("id"),
            "node": record.node,
            "attempt": record.attempt,
            "streamed": record.streamed,
            "outcome": record.outcome,
            "ttft_first_token_ms": record.ttft_first_token_ms,
            "ttft_first_content_ms": record.ttft_first_content_ms,
            "total_ms": record.total_ms,
            "decode_tokens_per_s": record.decode_tokens_per_s,
            "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens,
            "reasoning_tokens": record.reasoning_tokens,
        }
        emit(self.store, self.bus, self.run_id, "llm_call", **payload)
