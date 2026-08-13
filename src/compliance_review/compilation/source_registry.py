from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable, Literal, Optional

from compliance_review.compilation.models import (
    ComplianceSource,
    SourceMediaType,
    SourceRegistry,
    SourceSection,
)

SUPPORTED_SUFFIXES = {".md", ".txt", ".pdf", ".docx"}
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+")


class SourceExtractionError(ValueError):
    """Raised when a supported source cannot be extracted safely."""


class SourceRegistryBuilder:
    """Register source files and extract bounded, provenance-preserving sections."""

    def __init__(self, max_section_chars: int = 12000) -> None:
        if max_section_chars < 500:
            raise ValueError("max_section_chars must be at least 500")
        self.max_section_chars = max_section_chars

    def build(
        self,
        paths: Iterable[Path],
        source_families: Optional[dict[str, str]] = None,
        versions: Optional[dict[str, str]] = None,
    ) -> SourceRegistry:
        expanded = _expand_paths(paths)
        if not expanded:
            raise ValueError("no supported source files were found")
        source_families = source_families or {}
        versions = versions or {}
        sources = [
            self._register_file(path, _mapping_for_path(path, source_families, "other"), versions)
            for path in expanded
        ]
        return SourceRegistry(version="1.0", sources=sources)

    def _register_file(
        self, path: Path, source_family: str, versions: dict[str, str]
    ) -> ComplianceSource:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        media_type = _media_type(path)
        source_id = _source_id(path, digest)
        title = path.stem.replace("_", " ").replace("-", " ").strip() or source_id
        limitations: list[str] = []
        extraction_status: Literal["ok", "partial", "failed"] = "failed"
        try:
            sections = self._extract(path, media_type)
            if sections and sections[0].title != title:
                title = sections[0].title if media_type in {"md", "txt"} else title
            extraction_status = "ok" if sections else "failed"
            if not sections:
                limitations.append("no text could be extracted from the source")
        except SourceExtractionError as exc:
            sections = []
            extraction_status = "failed"
            limitations.append(str(exc))
        return ComplianceSource(
            source_id=source_id,
            path=path.as_posix(),
            title=title,
            version=versions.get(path.as_posix()),
            sha256=digest,
            source_family=source_family,
            media_type=media_type,
            extraction_status=extraction_status,
            sections=sections,
            limitations=limitations,
        )

    def _extract(self, path: Path, media_type: SourceMediaType) -> list[SourceSection]:
        if media_type in {"md", "txt"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            return _text_sections(text, self.max_section_chars)
        if media_type == "pdf":
            return self._pdf_sections(path)
        return self._docx_sections(path)

    def _pdf_sections(self, path: Path) -> list[SourceSection]:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise SourceExtractionError("PDF extraction requires the pypdf dependency") from exc
        try:
            pages = PdfReader(str(path)).pages
        except Exception as exc:
            raise SourceExtractionError(f"PDF reader failed: {exc}") from exc
        page_texts: list[tuple[int, str]] = []
        for page_number, page in enumerate(pages, start=1):
            page_error: Optional[str] = None
            try:
                text = (page.extract_text() or "").strip()
            except Exception as exc:
                text = ""
                page_error = f"page {page_number} extraction failed: {exc}"
            else:
                page_error = None
            if text:
                page_texts.append((page_number, text))
            elif page_error:
                raise SourceExtractionError(page_error)
        # A page boundary is provenance, not a paragraph boundary.
        combined = "\n".join(text for _, text in page_texts)
        sections = _text_sections(combined, self.max_section_chars, prefix="pdf-section")
        return _attach_page_metadata(sections, combined, page_texts)

    def _docx_sections(self, path: Path) -> list[SourceSection]:
        try:
            from docx import Document
        except ImportError as exc:
            raise SourceExtractionError(
                "DOCX extraction requires the python-docx dependency"
            ) from exc
        try:
            document = Document(str(path))
        except Exception as exc:
            raise SourceExtractionError(f"DOCX reader failed: {exc}") from exc
        blocks: list[tuple[str, bool]] = []
        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            style = paragraph.style.name.lower() if paragraph.style else ""
            blocks.append((text, style.startswith("heading")))
        if not blocks:
            return []
        return _structured_blocks_to_sections(blocks, self.max_section_chars)


def _expand_paths(paths: Iterable[Path]) -> list[Path]:
    expanded: list[Path] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        candidates = sorted(path.rglob("*") if path.is_dir() else [path])
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            key = candidate.as_posix()
            if key not in seen:
                seen.add(key)
                expanded.append(candidate)
    return expanded


def _media_type(path: Path) -> SourceMediaType:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return "md"
    if suffix == ".txt":
        return "txt"
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    raise ValueError(f"unsupported source type: {path}")


def _source_id(path: Path, digest: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", path.stem).strip("-") or "source"
    path_digest = hashlib.sha256(path.as_posix().encode("utf-8")).hexdigest()[:12]
    return f"{stem}-{digest[:12]}-{path_digest}"


def _mapping_for_path(path: Path, mappings: dict[str, str], default: str) -> str:
    matches = [
        (Path(raw_path).expanduser().resolve(), family) for raw_path, family in mappings.items()
    ]
    containing = [
        (candidate, family)
        for candidate, family in matches
        if candidate == path or candidate in path.parents
    ]
    if not containing:
        return default
    return max(containing, key=lambda item: len(item[0].parts))[1]


def _text_sections(
    text: str, max_chars: int, prefix: str = "section"
) -> list[SourceSection]:
    clean = _normalize_text(text)
    if not clean:
        return []
    return _attach_line_metadata(_semantic_sections(clean, max_chars, prefix=prefix), clean)


def _structured_blocks_to_sections(
    blocks: list[tuple[str, bool]], max_chars: int
) -> list[SourceSection]:
    text = "\n\n".join(item[0] for item in blocks)
    heading_lines: set[int] = set()
    line_cursor = 0
    for block, is_heading in blocks:
        if is_heading:
            heading_lines.add(line_cursor)
        line_cursor += len(block.splitlines()) + 2
    sections = _semantic_sections(
        _normalize_text(text),
        max_chars,
        prefix="section",
        explicit_heading_lines=heading_lines,
    )
    return _attach_line_metadata(sections, _normalize_text(text))


def _normalize_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()


def _semantic_sections(
    text: str,
    max_chars: int,
    *,
    prefix: str,
    explicit_heading_lines: set[int] | None = None,
) -> list[SourceSection]:
    """Split from document to heading, paragraph, sentence, then hard boundaries."""
    if len(text) <= max_chars:
        return [_make_section(text, "Document", f"{prefix}-0001", 1)]

    heading_blocks = _heading_blocks(text, explicit_heading_lines)
    lines = text.splitlines()
    starts_with_heading = bool(_HEADING_RE.match(lines[0])) if lines else False
    starts_with_heading = starts_with_heading or bool(
        explicit_heading_lines and 0 in explicit_heading_lines
    )
    if len(heading_blocks) > 1 or starts_with_heading:
        parts: list[tuple[str, str]] = []
        for index, block in enumerate(heading_blocks, start=1):
            title = _heading_title(block) or f"Section {index}"
            parts.extend(_split_block(block, title, max_chars))
    else:
        parts = _split_block(text, "Document", max_chars)

    sections: list[SourceSection] = []
    for index, (part, title) in enumerate(parts, start=1):
        sections.append(_make_section(part, title, f"{prefix}-{index:04d}", index))
    return sections


def _heading_blocks(text: str, explicit_heading_lines: set[int] | None) -> list[str]:
    lines = text.splitlines()
    heading_positions: list[int] = []
    for index, line in enumerate(lines):
        if _HEADING_RE.match(line):
            heading_positions.append(index)
    if explicit_heading_lines:
        heading_positions = sorted(set(heading_positions) | set(explicit_heading_lines))
    if not heading_positions:
        return [text]
    blocks: list[str] = []
    if heading_positions[0] > 0:
        blocks.append("\n".join(lines[: heading_positions[0]]).strip())
    for start, end in zip(heading_positions, heading_positions[1:] + [len(lines)]):
        blocks.append("\n".join(lines[start:end]).strip())
    return [block for block in blocks if block]


def _heading_title(block: str) -> str | None:
    first = block.splitlines()[0].strip() if block.splitlines() else ""
    match = _HEADING_RE.match(first)
    return match.group(2).strip() if match else None


def _split_block(text: str, title: str, max_chars: int) -> list[tuple[str, str]]:
    if len(text) <= max_chars:
        return [(text, title)]
    lines = text.splitlines()
    heading_prefix = ""
    body = text
    if lines and _HEADING_RE.match(lines[0]):
        heading_prefix = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    if len(paragraphs) <= 1:
        return [(chunk, title) for chunk in _hard_split_semantically(text, max_chars)]
    parts: list[tuple[str, str]] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            parts.append((paragraph, title))
        else:
            parts.extend((chunk, title) for chunk in _hard_split_semantically(paragraph, max_chars))
    if heading_prefix and parts:
        first_text, first_title = parts[0]
        combined = f"{heading_prefix}\n\n{first_text}"
        if len(combined) <= max_chars:
            parts[0] = (combined, first_title)
        else:
            parts.insert(0, (heading_prefix, title))
    return parts


def _hard_split_semantically(text: str, max_chars: int) -> list[str]:
    sentences = _sentences(text)
    if len(sentences) <= 1:
        return _hard_split(text, max_chars)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [piece for chunk in chunks for piece in _hard_split(chunk, max_chars)]


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_RE.split(text) if part.strip()]


def _hard_split(text: str, max_chars: int) -> list[str]:
    return [text[index : index + max_chars].strip() for index in range(0, len(text), max_chars)]


def _make_section(text: str, title: str, section_id: str, ordinal: int) -> SourceSection:
    return SourceSection(
        section_id=section_id,
        title=title or "Document",
        text=text.strip(),
        ordinal=ordinal,
    )


def _attach_page_metadata(
    sections: list[SourceSection], combined: str, page_texts: list[tuple[int, str]]
) -> list[SourceSection]:
    offsets: list[tuple[int, int, int]] = []
    cursor = 0
    for page_number, text in page_texts:
        start = cursor
        cursor += len(text)
        offsets.append((page_number, start, cursor))
        cursor += 1
    search_from = 0
    updated: list[SourceSection] = []
    for section in sections:
        start = combined.find(section.text, search_from)
        if start < 0:
            start = search_from
        end = start + len(section.text)
        pages = [
            page
            for page, page_start, page_end in offsets
            if start < page_end and end > page_start
        ]
        updated.append(
            section.model_copy(
                update={
                    "page": min(pages) if pages else None,
                    "page_end": max(pages) if pages else None,
                    "location": (
                        f"pages:{min(pages)}-{max(pages)}" if pages else "pages:unknown"
                    ),
                }
            )
        )
        search_from = end
    return updated


def _attach_line_metadata(
    sections: list[SourceSection], combined: str
) -> list[SourceSection]:
    search_from = 0
    updated: list[SourceSection] = []
    for section in sections:
        start = combined.find(section.text, search_from)
        if start < 0:
            start = search_from
        end = start + len(section.text)
        line_start = combined.count("\n", 0, start) + 1
        line_end = combined.count("\n", 0, end) + 1
        updated.append(
            section.model_copy(update={"location": f"lines:{line_start}-{line_end}"})
        )
        search_from = end
    return updated
