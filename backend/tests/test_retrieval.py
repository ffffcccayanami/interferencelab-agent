"""检索与引用校验。"""

from __future__ import annotations

import pytest

from app.schemas import ErrorCode, RunFailure
from app.services.retrieval import (
    build_index,
    chunk_text,
    require_evidence,
    verify_citations,
)

APC = (
    "前缀缓存按块匹配。块大小由服务端配置决定，实际值必须从启动日志中读取。"
    "如果两次请求在块边界内前缀一致，并且该块仍被保留，才会命中。"
)
PREFILL = (
    "Chunked prefill 把长提示词拆成多个小块，与 decode 交错调度。"
    "它主要改善尾延迟与并发吞吐，不必然降低单请求 TTFT。"
)


def _index():
    return build_index([("upl_a", "apc.md", APC), ("upl_b", "prefill.md", PREFILL)])


def test_chunk_ids_are_stable_and_ordered() -> None:
    chunks = chunk_text(APC, doc_id="upl_a", filename="apc.md")
    assert chunks
    assert chunks[0].chunk_id == "upl_a:c0001"
    assert chunks[0].char_start == 0


def test_search_prefers_relevant_document() -> None:
    index, summaries = _index()
    assert len(summaries) == 2

    citations = index.search("前缀缓存块不命中的原因")
    assert citations
    assert citations[0].doc_id == "upl_a"
    assert 0 <= citations[0].score


def test_search_returns_empty_for_unrelated_query() -> None:
    index, _ = _index()
    assert index.search("红烧肉的做法") == []


def test_require_evidence_raises() -> None:
    with pytest.raises(RunFailure) as excinfo:
        require_evidence([])
    assert excinfo.value.code is ErrorCode.NO_EVIDENCE_DOCUMENTS


def test_verify_citations_drops_uncited_and_unknown_paragraphs() -> None:
    index, _ = _index()
    citations = index.search("前缀缓存块大小")
    assert citations
    known = citations[0].chunk_id

    text = (
        f"前缀命中受块大小影响 [[{known}]]。\n\n"
        "这一段没有任何引用。\n\n"
        "这一段引用了不存在的片段 [[upl_a:c9999]]。"
    )
    verified, warnings = verify_citations(text, citations)

    assert known in verified
    assert "没有引用" not in verified
    assert "不存在" not in verified
    assert any("未知引用" in warning for warning in warnings)
    assert any("没有引用" in warning for warning in warnings)
