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
        sections: list[SourceSection] = []
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
                sections.extend(
                    _chunk_section_text(
                        text,
                        title=f"Page {page_number}",
                        prefix=f"page-{page_number:04d}",
                        page=page_number,
                        max_chars=self.max_section_chars,
                        start_ordinal=len(sections) + 1,
                    )
                )
            elif page_error:
                raise SourceExtractionError(page_error)
        return sections

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
        blocks: list[str] = []
        headings: list[tuple[int, str]] = []
        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            style = paragraph.style.name.lower() if paragraph.style else ""
            if style.startswith("heading"):
                headings.append((len(blocks), text))
            blocks.append(text)
        if not blocks:
            return []
        return _structured_blocks_to_sections(blocks, headings, self.max_section_chars)


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


def _text_sections(text: str, max_chars: int) -> list[SourceSection]:
    lines = text.splitlines()
    headings: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match:
            headings.append((index, match.group(2).strip()))
    if not headings:
        return (
            _chunk_section_text(text.strip(), "Document", "section-0001", None, max_chars, 1)
            if text.strip()
            else []
        )
    blocks: list[str] = []
    for start, end in zip(
        [index for index, _ in headings],
        [index for index, _ in headings[1:]] + [len(lines)],
    ):
        blocks.append("\n".join(lines[start:end]).strip())
    return _structured_blocks_to_sections(
        blocks, [(index, title) for index, title in headings], max_chars
    )


def _structured_blocks_to_sections(
    blocks: list[str], headings: list[tuple[int, str]], max_chars: int
) -> list[SourceSection]:
    sections: list[SourceSection] = []
    for index, block in enumerate(blocks, start=1):
        title = headings[index - 1][1] if index <= len(headings) else f"Section {index}"
        sections.extend(
            _chunk_section_text(
                block,
                title=title,
                prefix=f"section-{index:04d}",
                page=None,
                max_chars=max_chars,
                start_ordinal=len(sections) + 1,
            )
        )
    return sections


def _chunk_section_text(
    text: str,
    title: str,
    prefix: str,
    page: Optional[int],
    max_chars: int,
    start_ordinal: int,
) -> list[SourceSection]:
    clean = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not clean:
        return []
    chunks = [clean[index : index + max_chars] for index in range(0, len(clean), max_chars)]
    return [
        SourceSection(
            section_id=prefix if len(chunks) == 1 else f"{prefix}-chunk-{chunk_index:02d}",
            title=title if len(chunks) == 1 else f"{title} (part {chunk_index})",
            text=chunk.strip(),
            ordinal=start_ordinal + chunk_index - 1,
            page=page,
        )
        for chunk_index, chunk in enumerate(chunks, start=1)
    ]
