"""技术研究：检索命中、引用校验、无证据拒绝。"""

from __future__ import annotations

from typing import Any

from .conftest import FIXTURES, create_run, upload, wait_for_run


def test_research_returns_citations(client: Any) -> None:
    first = upload(client, FIXTURES / "docs" / "apc_notes.md")
    second = upload(client, FIXTURES / "docs" / "chunked_prefill.md")
    run_id = create_run(
        client,
        task_type="research",
        question="前缀缓存为什么可能不命中？",
        upload_ids=[first, second],
    )
    run = wait_for_run(client, run_id)

    assert run["status"] == "succeeded", run.get("error")
    report = client.get(f"/api/runs/{run_id}/report").text
    assert "检索到的证据" in report
    assert ":c000" in report
    # 结论小节只出现一次，不能出现"空标题 + 重复标题"
    assert "## 4. 结论（模型解释" in report
    assert "## 4. 结论\n\n## 模型解释" not in report
    # 证据原文里的 Markdown 标题必须留在引用块内，不能变成报告章节
    assert "> ## 什么情况下前缀会命中" in report

    payload = client.get(f"/api/runs/{run_id}/artifacts/{run['artifacts'][1]['artifact_id']}")
    assert payload.status_code == 200


def test_research_rejects_irrelevant_documents(client: Any) -> None:
    upload_id = upload(client, FIXTURES / "docs" / "unrelated.md")
    run_id = create_run(client, task_type="research", question="前缀缓存块回收机制", upload_ids=[upload_id])
    run = wait_for_run(client, run_id)

    assert run["status"] == "failed"
    assert run["error"]["code"] == "NO_EVIDENCE_DOCUMENTS"


def test_research_degrades_without_model(client: Any) -> None:
    upload_id = upload(client, FIXTURES / "docs" / "apc_notes.md")
    run_id = create_run(client, task_type="research", question="前缀缓存按什么粒度匹配？", upload_ids=[upload_id])
    run = wait_for_run(client, run_id)

    assert run["status"] == "succeeded"
    assert run["degraded"] is True
    assert any("模型" in warning for warning in run["warnings"])

    report = client.get(f"/api/runs/{run_id}/report").text
    assert "未生成模型解释" in report
    assert "结论边界" in report


def test_research_rejects_benchmark_file(client: Any) -> None:
    upload_id = upload(client, FIXTURES / "bench_single_config.csv")
    run_id = create_run(client, task_type="research", question="这是什么", upload_ids=[upload_id])
    run = wait_for_run(client, run_id)

    assert run["status"] == "failed"
    assert run["error"]["code"] == "UNSUPPORTED_FILE"
