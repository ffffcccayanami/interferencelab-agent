"""Utilities for exporting research reports as PDF."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from html import unescape

PAGE_WIDTH = 595.2756
PAGE_HEIGHT = 841.8898
MARGIN_LEFT = 48.0
MARGIN_RIGHT = 48.0
MARGIN_TOP = 54.0
MARGIN_BOTTOM = 54.0

TITLE_FONT = 18.0
TITLE_LEADING = 24.0
BODY_FONT = 11.0
SUBTITLE_FONT = 10.0
SUBTITLE_LEADING = 14.0
FOOTER_FONT = 9.0


@dataclass(frozen=True)
class RenderLine:
    text: str
    font_size: float
    leading: float
    indent: float = 0.0
    blank: bool = False


def build_pdf_bytes(title: str, markdown_text: str) -> bytes:
    """Render markdown-like report text as a simple Chinese-capable PDF."""

    document_title = _normalize_title(title)
    render_lines = _markdown_to_render_lines(document_title, markdown_text or "")
    pages = _paginate_lines(render_lines)
    if not pages:
        pages = [[]]

    return _assemble_pdf(pages)


def _normalize_title(title: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", title or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or "MechResearch-Agent 研究报告"


def _markdown_to_render_lines(document_title: str, markdown_text: str) -> list[RenderLine]:
    render_lines: list[RenderLine] = [
        *_text_to_render_lines(document_title, font_size=TITLE_FONT, leading=TITLE_LEADING),
        RenderLine(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}", SUBTITLE_FONT, SUBTITLE_LEADING),
        RenderLine("导出来源：MechResearch-Agent", SUBTITLE_FONT, SUBTITLE_LEADING),
        RenderLine("", SUBTITLE_FONT, 8.0, blank=True),
    ]

    in_code_block = False
    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()

        if line.startswith("```"):
            in_code_block = not in_code_block
            render_lines.append(RenderLine("", BODY_FONT, 8.0, blank=True))
            continue

        if not line:
            render_lines.append(RenderLine("", BODY_FONT, 8.0, blank=True))
            continue

        if line in {"---", "***", "___"}:
            render_lines.append(RenderLine("", BODY_FONT, 8.0, blank=True))
            continue

        if in_code_block:
            code_line = raw_line.replace("\t", "    ").rstrip()
            render_lines.extend(
                _text_to_render_lines(
                    code_line,
                    font_size=10.0,
                    indent=18.0,
                    leading=13.5,
                    preserve_spacing=True,
                )
            )
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            content = _normalize_inline_text(heading_match.group(2))
            size = {1: 15.5, 2: 14.5, 3: 13.2}.get(level, 12.0)
            render_lines.extend(
                _text_to_render_lines(content, font_size=size, leading=size + 5.5)
            )
            render_lines.append(RenderLine("", BODY_FONT, 6.0, blank=True))
            continue

        bullet_match = re.match(r"^([-*+])\s+(.*)$", line)
        if bullet_match:
            content = _normalize_inline_text(bullet_match.group(2))
            render_lines.extend(
                _text_to_render_lines(
                    content,
                    font_size=11.0,
                    indent=18.0,
                    prefix="• ",
                    leading=15.0,
                )
            )
            continue

        numbered_match = re.match(r"^(\d+[.)])\s+(.*)$", line)
        if numbered_match:
            prefix = f"{numbered_match.group(1)} "
            content = _normalize_inline_text(numbered_match.group(2))
            render_lines.extend(
                _text_to_render_lines(
                    content,
                    font_size=11.0,
                    indent=18.0,
                    prefix=prefix,
                    leading=15.0,
                )
            )
            continue

        blockquote_match = re.match(r"^>+\s*(.*)$", line)
        if blockquote_match:
            content = _normalize_inline_text(blockquote_match.group(1))
            render_lines.extend(
                _text_to_render_lines(
                    content,
                    font_size=11.0,
                    indent=18.0,
                    prefix="› ",
                    leading=15.0,
                )
            )
            continue

        paragraph = _normalize_inline_text(line)
        render_lines.extend(_text_to_render_lines(paragraph, font_size=11.0, leading=15.0))

    return render_lines


def _normalize_inline_text(text: str, *, preserve_spacing: bool = False) -> str:
    normalized = unescape(text)
    normalized = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1 (\2)", normalized)
    normalized = normalized.replace("**", "").replace("__", "").replace("`", "")
    normalized = normalized.replace("\u00a0", " ")
    if preserve_spacing:
        normalized = normalized.replace("\r", "")
        normalized = normalized.rstrip()
    else:
        normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _text_to_render_lines(
    text: str,
    *,
    font_size: float,
    leading: float,
    indent: float = 0.0,
    prefix: str = "",
    preserve_spacing: bool = False,
) -> list[RenderLine]:
    available_width = PAGE_WIDTH - MARGIN_LEFT - MARGIN_RIGHT - indent
    width_factor = max(12.0, (available_width / font_size) * 0.92)

    if preserve_spacing:
        normalized = text.replace("\r", "")
        wrapped = _wrap_text(normalized, width_factor)
    else:
        normalized = _normalize_inline_text(text)
        wrapped = _wrap_text(normalized, width_factor)

    if not wrapped:
        return [RenderLine(prefix.rstrip(), font_size, leading, indent=indent)] if prefix else []

    if prefix:
        prefix_units = _measure_units(prefix)
        cont_prefix = " " * len(prefix)
        cont_units = _measure_units(cont_prefix)
        first_width = max(8.0, width_factor - prefix_units)
        cont_width = max(8.0, width_factor - cont_units)
        first_lines = _wrap_text(wrapped[0], first_width)
        extra_lines = []
        for item in wrapped[1:]:
            extra_lines.extend(_wrap_text(item, cont_width))

        lines: list[RenderLine] = []
        if first_lines:
            lines.append(RenderLine(prefix + first_lines[0], font_size, leading, indent=indent))
            for continuation in first_lines[1:]:
                lines.append(RenderLine(cont_prefix + continuation, font_size, leading, indent=indent))
        else:
            lines.append(RenderLine(prefix.rstrip(), font_size, leading, indent=indent))

        for item in extra_lines:
            lines.append(RenderLine(cont_prefix + item, font_size, leading, indent=indent))
        return lines

    return [RenderLine(item, font_size, leading, indent=indent) for item in wrapped]


def _wrap_text(text: str, max_units: float) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []

    tokens = re.findall(r"\s+|\S+", stripped)
    lines: list[str] = []
    current = ""
    current_units = 0.0

    for token in tokens:
        if token.isspace():
            if not current:
                continue
            token_units = _measure_units(token)
            if current_units + token_units > max_units:
                lines.append(current.rstrip())
                current = ""
                current_units = 0.0
            else:
                current += token
                current_units += token_units
            continue

        token_units = _measure_units(token)
        if current and current_units + token_units <= max_units:
            current += token
            current_units += token_units
            continue

        if not current and token_units <= max_units:
            current = token
            current_units = token_units
            continue

        if current:
            lines.append(current.rstrip())
            current = ""
            current_units = 0.0

        if token_units <= max_units:
            current = token
            current_units = token_units
            continue

        fragment = ""
        fragment_units = 0.0
        for char in token:
            char_units = _char_units(char)
            if fragment and fragment_units + char_units > max_units:
                lines.append(fragment)
                fragment = char
                fragment_units = char_units
            else:
                fragment += char
                fragment_units += char_units
        current = fragment
        current_units = fragment_units

    if current:
        lines.append(current.rstrip())

    return lines


def _measure_units(text: str) -> float:
    return sum(_char_units(char) for char in text)


def _char_units(char: str) -> float:
    if char == "\t":
        return 1.6
    if char == " ":
        return 0.33

    east_asian = unicodedata.east_asian_width(char)
    if east_asian in {"W", "F"}:
        return 1.0
    if char in ",.;:!?，。！？；：、“”‘’（）()[]{}<>《》·—-+/\\|":
        return 0.65
    return 0.58


def _paginate_lines(lines: list[RenderLine]) -> list[list[tuple[RenderLine, float]]]:
    pages: list[list[tuple[RenderLine, float]]] = []
    current_page: list[tuple[RenderLine, float]] = []
    current_y = PAGE_HEIGHT - MARGIN_TOP

    for line in lines:
        leading = line.leading or 12.0
        if line.blank:
            if current_y - leading < MARGIN_BOTTOM:
                pages.append(current_page)
                current_page = []
                current_y = PAGE_HEIGHT - MARGIN_TOP
            else:
                current_y -= leading
            continue

        if current_y - leading < MARGIN_BOTTOM:
            pages.append(current_page)
            current_page = []
            current_y = PAGE_HEIGHT - MARGIN_TOP

        current_y -= leading
        current_page.append((line, current_y))

    if current_page or not pages:
        pages.append(current_page)

    return pages


def _assemble_pdf(pages: list[list[tuple[RenderLine, float]]]) -> bytes:
    page_count = max(1, len(pages))
    object_payloads: list[tuple[int, bytes]] = [
        (1, _catalog_object()),
        (2, _pages_object(page_count)),
        (3, _type0_font_object()),
        (4, _cid_font_object()),
    ]

    for page_index, page in enumerate(pages, start=1):
        content_object_number = 4 + (page_index * 2 - 1)
        page_object_number = 4 + page_index * 2
        object_payloads.append(
            (content_object_number, _page_content_object(page, page_index, page_count))
        )
        object_payloads.append((page_object_number, _page_object(content_object_number)))

    object_payloads.sort(key=lambda item: item[0])

    buffer = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]

    for object_number, payload in object_payloads:
        offsets.append(len(buffer))
        buffer.extend(f"{object_number} 0 obj\n".encode("ascii"))
        buffer.extend(payload)
        if not payload.endswith(b"\n"):
            buffer.extend(b"\n")
        buffer.extend(b"endobj\n")

    startxref = len(buffer)
    buffer.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    buffer.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        buffer.extend(f"{offset:010d} 00000 n \n".encode("ascii"))

    buffer.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(buffer)


def _catalog_object() -> bytes:
    return b"<< /Type /Catalog /Pages 2 0 R >>\n"


def _pages_object(page_count: int) -> bytes:
    kids = " ".join(f"{4 + index * 2} 0 R" for index in range(1, page_count + 1))
    return f"<< /Type /Pages /Kids [ {kids} ] /Count {page_count} >>\n".encode("ascii")


def _type0_font_object() -> bytes:
    return (
        b"<< /Type /Font\n"
        b"   /Subtype /Type0\n"
        b"   /BaseFont /STSong-Light\n"
        b"   /Encoding /UniGB-UCS2-H\n"
        b"   /DescendantFonts [ 4 0 R ]\n"
        b">>\n"
    )


def _cid_font_object() -> bytes:
    return (
        b"<< /Type /Font\n"
        b"   /Subtype /CIDFontType0\n"
        b"   /BaseFont /STSong-Light\n"
        b"   /CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >>\n"
        b"   /DW 1000\n"
        b">>\n"
    )


def _page_object(content_object_number: int) -> bytes:
    return (
        f"<< /Type /Page\n"
        f"   /Parent 2 0 R\n"
        f"   /MediaBox [0 0 {PAGE_WIDTH:.3f} {PAGE_HEIGHT:.3f}]\n"
        f"   /Resources << /Font << /F1 3 0 R >> >>\n"
        f"   /Contents {content_object_number} 0 R\n"
        f">>\n"
    ).encode("ascii")


def _page_content_object(
    page_lines: list[tuple[RenderLine, float]],
    page_number: int,
    total_pages: int,
) -> bytes:
    commands: list[str] = ["BT"]

    for line, y in page_lines:
        if not line.text.strip():
            continue
        x = MARGIN_LEFT + line.indent
        encoded_text = line.text.encode("utf-16-be").hex().upper()
        commands.append(f"/F1 {line.font_size:.2f} Tf")
        commands.append(f"1 0 0 1 {x:.2f} {y:.2f} Tm <{encoded_text}> Tj")

    footer = f"第 {page_number} 页 / 共 {total_pages} 页"
    footer_x = PAGE_WIDTH - MARGIN_RIGHT - 122.0
    commands.append(f"/F1 {FOOTER_FONT:.2f} Tf")
    commands.append(
        f"1 0 0 1 {footer_x:.2f} {MARGIN_BOTTOM - 22.0:.2f} Tm <{footer.encode('utf-16-be').hex().upper()}> Tj"
    )
    commands.append("ET")
    stream = "\n".join(commands).encode("utf-8")
    return f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream\n"