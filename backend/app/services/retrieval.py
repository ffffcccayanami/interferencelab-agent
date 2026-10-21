"""本地证据检索：分块 + BM25 + 引用校验。

设计取舍
--------
* 不做联网检索：避免 token 消耗与"编造来源"；
* 不引入向量库：单机工作台的文档量在数百个 chunk 级别，BM25 足够且完全可复现；
* 引用必须带 ``chunk_id`` 与字符区间，报告里可以逐条回指原文。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ..schemas import ErrorCode, RunFailure

CHUNK_SIZE = 600
CHUNK_OVERLAP = 100
QUOTE_CHARS = 400

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-z0-9][a-z0-9_\.\-]*")
_MARKER = re.compile(r"\[\[([A-Za-z0-9_:\-]+)\]\]")


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    filename: str
    chunk_id: str
    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class Citation:
    doc_id: str
    filename: str
    chunk_id: str
    char_start: int
    char_end: int
    quote: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "filename": self.filename,
            "chunk_id": self.chunk_id,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "quote": self.quote,
            "score": round(self.score, 4),
        }


def tokenize(text: str) -> list[str]:
    lowered = text.lower()
    tokens = _WORD.findall(lowered)
    cjk = _CJK.findall(lowered)
    tokens.extend(cjk)
    tokens.extend(f"{cjk[index]}{cjk[index + 1]}" for index in range(len(cjk) - 1))
    return tokens


def chunk_text(text: str, *, doc_id: str, filename: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[Chunk]:
    normalized = re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n")).strip()
    if not normalized:
        return []

    chunks: list[Chunk] = []
    step = max(1, size - overlap)
    for index, start in enumerate(range(0, len(normalized), step)):
        end = min(len(normalized), start + size)
        piece = normalized[start:end].strip()
        if len(piece) < 20:
            continue
        chunks.append(
            Chunk(
                doc_id=doc_id,
                filename=filename,
                chunk_id=f"{doc_id}:c{index + 1:04d}",
                text=piece,
                char_start=start,
                char_end=end,
            )
        )
        if end >= len(normalized):
            break
    return chunks


class BM25Index:
    """极简 BM25（k1=1.5, b=0.75），用于本地证据检索。"""

    k1 = 1.5
    b = 0.75

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self._tokens = [tokenize(chunk.text) for chunk in chunks]
        self._lengths = [len(tokens) for tokens in self._tokens]
        self._avg_length = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        document_frequency: Counter[str] = Counter()
        for tokens in self._tokens:
            document_frequency.update(set(tokens))
        total = max(1, len(chunks))
        self._idf = {
            term: math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        }

    def search(self, query: str, *, top_k: int = 4, min_relative: float = 0.15) -> list[Citation]:
        query_terms = tokenize(query)
        if not query_terms or not self.chunks:
            return []

        scores: list[float] = []
        for tokens, length in zip(self._tokens, self._lengths, strict=True):
            frequencies = Counter(tokens)
            score = 0.0
            for term in query_terms:
                frequency = frequencies.get(term)
                if not frequency:
                    continue
                idf = self._idf.get(term, 0.0)
                denominator = frequency + self.k1 * (1 - self.b + self.b * length / (self._avg_length or 1.0))
                score += idf * frequency * (self.k1 + 1) / denominator
            scores.append(score)

        best = max(scores) if scores else 0.0
        if best <= 0:
            return []

        threshold = best * min_relative
        ranked = sorted(
            (
                (score, index)
                for index, score in enumerate(scores)
                if score >= threshold and score > 0
            ),
            key=lambda item: item[0],
            reverse=True,
        )

        citations: list[Citation] = []
        per_doc: Counter[str] = Counter()
        for score, index in ranked:
            chunk = self.chunks[index]
            if per_doc[chunk.doc_id] >= 2:
                continue
            per_doc[chunk.doc_id] += 1
            quote = chunk.text[:QUOTE_CHARS]
            if len(chunk.text) > QUOTE_CHARS:
                quote = quote.rstrip() + " …"
            citations.append(
                Citation(
                    doc_id=chunk.doc_id,
                    filename=chunk.filename,
                    chunk_id=chunk.chunk_id,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    quote=quote,
                    score=score,
                )
            )
            if len(citations) >= top_k:
                break
        return citations

    def evidence_block(self, citations: list[Citation], *, limit: int = 1200) -> str:
        blocks = []
        for citation in citations:
            body = citation.quote[:limit]
            blocks.append(f"[{citation.chunk_id}] {citation.filename} (字符 {citation.char_start}-{citation.char_end})\n{body}")
        return "\n\n".join(blocks)


def build_index(documents: list[tuple[str, str, str]]) -> tuple[BM25Index, list[dict[str, Any]]]:
    """documents: [(doc_id, filename, text)] → (索引, 文档摘要)。"""

    chunks: list[Chunk] = []
    summaries: list[dict[str, Any]] = []
    for doc_id, filename, text in documents:
        doc_chunks = chunk_text(text, doc_id=doc_id, filename=filename)
        chunks.extend(doc_chunks)
        summaries.append(
            {
                "doc_id": doc_id,
                "filename": filename,
                "chars": len(text),
                "chunks": len(doc_chunks),
            }
        )
    return BM25Index(chunks), summaries


def require_evidence(citations: list[Citation]) -> None:
    if not citations:
        raise RunFailure(
            ErrorCode.NO_EVIDENCE_DOCUMENTS,
            "上传文档中没有与该问题相关的片段，拒绝生成无依据的结论",
            {"hint": "补充更贴近问题的文档，或改写问题"},
        )


def verify_citations(text: str, citations: list[Citation]) -> tuple[str, list[str]]:
    """剔除没有有效引用的段落，返回 (过滤后文本, 警告)。

    LLM 必须用 ``[[chunk_id]]`` 标注结论来源；出现不存在的 chunk_id 时
    直接删掉该段并记警告，避免"看似有引用其实是编造"。
    """

    known = {citation.chunk_id for citation in citations}
    warnings: list[str] = []
    kept: list[str] = []

    for paragraph in re.split(r"\n\s*\n", text):
        block = paragraph.strip()
        if not block:
            continue
        markers = _MARKER.findall(block)
        unknown = [marker for marker in markers if marker not in known]
        if unknown:
            warnings.append(f"剔除包含未知引用 {unknown[:3]} 的段落")
            continue
        if not markers:
            warnings.append("剔除没有引用的解释段落")
            continue
        cleaned = block
        for marker in set(markers):
            cleaned = cleaned.replace(f"[[{marker}]]", f"[{marker}]")
        kept.append(cleaned)

    if not kept:
        warnings.append("模型解释没有任何有效引用，已整体省略")
    return "\n\n".join(kept), warnings
