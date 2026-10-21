"""技术研究子图：文档入库 → BM25 检索 → 证据引用。"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph import END, StateGraph

from ...deps import Deps
from ...schemas import ErrorCode, RunFailure
from ...services.parsing import DOCUMENT, parse_input
from ...services.retrieval import build_index, require_evidence
from ..state import RunState

NODE_INGEST = "research_ingest"
NODE_RETRIEVE = "research_retrieve"


def build_research_subgraph(deps: Deps):
    def ingest(state: RunState) -> RunState:
        run_id = state["run_id"]
        with deps.node(run_id, NODE_INGEST):
            deps.check_cancel(run_id)
            warnings = list(state.get("warnings") or [])
            documents: list[tuple[str, str, str]] = []

            if not state.get("uploads"):
                raise RunFailure(ErrorCode.VALIDATION_ERROR, "技术研究至少需要一个上传文档")

            for item in state["uploads"]:
                result = parse_input(Path(item["stored_path"]), item["filename"])
                if result.kind != DOCUMENT:
                    raise RunFailure(
                        ErrorCode.UNSUPPORTED_FILE,
                        "技术研究只接受 .md / .txt / .pdf 文档",
                        {"file": item["filename"], "detected_type": result.kind},
                    )
                if not result.text.strip():
                    warnings.append(f"{item['filename']} 没有可用文本，已跳过")
                    continue
                documents.append((item["upload_id"], item["filename"], result.text))

            if not documents:
                raise RunFailure(ErrorCode.NO_EVIDENCE_DOCUMENTS, "上传文档中没有可用文本")

            index, summaries = build_index(documents)
            deps.emit(
                run_id,
                "tool_call",
                node=NODE_INGEST,
                tool="document_index",
                result={"documents": len(summaries), "chunks": len(index.chunks)},
            )
            return {"documents": summaries, "document_texts": documents, "warnings": warnings}

    def retrieve(state: RunState) -> RunState:
        run_id = state["run_id"]
        with deps.node(run_id, NODE_RETRIEVE):
            deps.check_cancel(run_id)
            documents = state.get("document_texts") or []
            if not documents:
                raise RunFailure(ErrorCode.INTERNAL, "检索文档缺失")
            index, _ = build_index(documents)
            citations = index.search(state["question"], top_k=4)
            require_evidence(citations)
            payload = [citation.to_dict() for citation in citations]
            deps.emit(run_id, "citation", node=NODE_RETRIEVE, citations=payload)
            return {
                "citations": payload,
                "evidence_block": index.evidence_block(citations),
            }

    builder = StateGraph(RunState)
    builder.add_node(NODE_INGEST, ingest)
    builder.add_node(NODE_RETRIEVE, retrieve)
    builder.set_entry_point(NODE_INGEST)
    builder.add_edge(NODE_INGEST, NODE_RETRIEVE)
    builder.add_edge(NODE_RETRIEVE, END)
    return builder.compile()
