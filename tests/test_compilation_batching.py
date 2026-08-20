from __future__ import annotations

import json
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from compliance_review.compilation.batching import (
    BatchPlanner,
    estimate_tokens,
    merge_obligation_batches,
    validate_batch_coverage,
)
from compliance_review.compilation.llm import (
    ObligationExtractor,
    _patch_strict_condition_value_schema,
)
from compliance_review.compilation.models import (
    ComplianceSource,
    ControlDraftSetTransport,
    Obligation,
    ObligationExtractionBatchResult,
    SectionCoverageDecision,
    SourceRegistry,
    SourceSection,
    SourceSectionBatch,
)
from compliance_review.compilation.service import Phase2CompilationError, Phase2CompilationService
from compliance_review.compilation.source_registry import SourceRegistryBuilder
from compliance_review.domain.models import SourceRef
from compliance_review.review.models import ModelResponse
from compliance_review.review.provider import StaticModelProvider


def _registry_for_sections(*sections: SourceSection) -> SourceRegistry:
    source = ComplianceSource(
        source_id="policy",
        path="policy.md",
        title="Policy",
        sha256="a" * 64,
        source_family="country_regulator",
        media_type="md",
        extraction_status="ok",
        sections=list(sections),
    )
    return SourceRegistry(version="1.0", sources=[source])


def test_strict_compilation_schemas_type_applicability_condition_value() -> None:
    for model in (ObligationExtractionBatchResult, ControlDraftSetTransport):
        schema = _patch_strict_condition_value_schema(model.model_json_schema())
        variants = schema["$defs"]["ApplicabilityCondition"]["anyOf"]
        atom = next(
            variant
            for variant in variants
            if variant["properties"]["kind"]["enum"] == ["atom"]
        )
        assert atom["properties"]["value"]["anyOf"] == [
            {"type": "string"},
            {"type": "boolean"},
            {"type": "number"},
            {"type": "array", "items": {"type": "string"}},
        ]
        assert atom["required"] == ["kind", "fact", "operator", "value"]


def _obligation(obligation_id: str, section_id: str) -> Obligation:
    return Obligation(
        obligation_id=obligation_id,
        source_id="policy",
        source_section=section_id,
        statement=f"Statement for {obligation_id}.",
        concepts=["disclosure"],
        applicability_expression="business_type includes personal_loan",
        required_surfaces=["frontend_h5"],
        source_refs=[SourceRef(source_id="policy", source_section=section_id)],
    )


def _write_pdf(path: Path, pages: list[str]) -> None:
    writer = PdfWriter()
    for page_text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = writer._add_object(
            DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        escaped = page_text.replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)


def test_small_file_stays_one_source_section(tmp_path: Path) -> None:
    path = tmp_path / "small.md"
    path.write_text("A complete small policy.", encoding="utf-8")

    sections = SourceRegistryBuilder(max_section_chars=500).build([path]).sources[0].sections

    assert len(sections) == 1
    assert sections[0].text == "A complete small policy."


def test_large_file_prefers_heading_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "headings.md"
    path.write_text(
        "# First\n\n" + "A " * 180 + "\n\n# Second\n\n" + "B " * 180,
        encoding="utf-8",
    )

    sections = SourceRegistryBuilder(max_section_chars=500).build([path]).sources[0].sections

    assert len(sections) >= 2
    assert sections[0].title == "First"
    assert any(section.title == "Second" for section in sections)


def test_oversized_heading_falls_back_to_paragraphs(tmp_path: Path) -> None:
    path = tmp_path / "paragraphs.md"
    path.write_text(
        "# Notice\n\n" + "First paragraph. " * 40 + "\n\n" + "Second paragraph. " * 40,
        encoding="utf-8",
    )

    sections = SourceRegistryBuilder(max_section_chars=500).build([path]).sources[0].sections

    assert len(sections) >= 2
    assert sections[0].title == "Notice"
    assert "# Notice" in sections[0].text


def test_oversized_paragraph_falls_back_to_sentences(tmp_path: Path) -> None:
    path = tmp_path / "sentences.txt"
    path.write_text("First sentence. " * 40, encoding="utf-8")

    sections = SourceRegistryBuilder(max_section_chars=500).build([path]).sources[0].sections

    assert len(sections) > 1
    assert all(section.text.endswith(".") for section in sections)


def test_chinese_sentences_without_spaces_keep_sentence_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "chinese.txt"
    path.write_text("借款人必须收到完整披露。借款人有权提前还款。" * 30, encoding="utf-8")

    sections = SourceRegistryBuilder(max_section_chars=500).build([path]).sources[0].sections

    assert len(sections) > 1
    assert all(section.text.endswith("。") for section in sections)


def test_hard_split_is_last_resort(tmp_path: Path) -> None:
    path = tmp_path / "atomic.txt"
    path.write_text("x" * 1200, encoding="utf-8")

    sections = SourceRegistryBuilder(max_section_chars=500).build([path]).sources[0].sections

    assert [len(section.text) for section in sections] == [500, 500, 200]


def test_pdf_page_boundary_is_provenance_not_semantic_split(tmp_path: Path) -> None:
    path = tmp_path / "policy.pdf"
    _write_pdf(
        path,
        ["The same sentence continues " + "across the document. " * 20,
         "The next page continues the same paragraph. " * 20],
    )

    sections = SourceRegistryBuilder(max_section_chars=500).build([path]).sources[0].sections

    assert len(sections) >= 1
    assert any(section.page == 1 and section.page_end == 2 for section in sections)
    assert all("Page 1" not in section.title for section in sections)


def test_batch_planner_packs_complete_sections_without_mixing_sources() -> None:
    registry = _registry_for_sections(
        SourceSection(section_id="one", title="One", text="a" * 400, ordinal=1),
        SourceSection(section_id="two", title="Two", text="b" * 400, ordinal=2),
        SourceSection(section_id="three", title="Three", text="c" * 400, ordinal=3),
    )

    batches = BatchPlanner(max_input_tokens=300).plan(registry)

    assert [batch.sections[0].section_id for batch in batches] == ["one", "two", "three"]
    assert all(batch.source_id == "policy" for batch in batches)


def test_large_registry_uses_multiple_obligation_model_calls() -> None:
    registry = _registry_for_sections(
        SourceSection(section_id="one", title="One", text="a" * 440, ordinal=1),
        SourceSection(section_id="two", title="Two", text="b" * 440, ordinal=2),
    )
    batches = BatchPlanner(max_input_tokens=300).plan(registry)

    def response(request: object) -> ModelResponse:
        payload = json.loads(request.messages[1]["content"])  # type: ignore[attr-defined]
        section_id = payload["sources"][0]["sections"][0]["section_id"]
        return ModelResponse(
            content=json.dumps(
                {
                    "contract": "obligation_extraction_batch.v1",
                    "version": "1.0",
                    "source_id": payload["source_id"],
                    "batch_id": payload["batch_id"],
                    "section_decisions": [
                        {
                            "section_id": section_id,
                            "decision": "no_obligation",
                            "reason": "Informational text only.",
                        }
                    ],
                    "obligations": [],
                }
            )
        )

    provider = StaticModelProvider(response)
    results = [ObligationExtractor(provider).extract(batch) for batch in batches]

    assert len(provider.requests) == len(batches)
    assert len(results) == 2


def test_obligation_model_schema_restricts_provenance_to_current_batch() -> None:
    source_id = "policy-source-1234567890"
    section_id = "section-001"
    batch = SourceSectionBatch(
        batch_id="batch-001",
        source_id=source_id,
        sections=[
            SourceSection(
                section_id=section_id,
                title="Policy",
                text="A requirement.",
                ordinal=1,
            )
        ],
        estimated_input_tokens=20,
    )

    def response(request: object) -> ModelResponse:
        schema = request.response_schema  # type: ignore[attr-defined]
        assert schema["properties"]["source_id"]["enum"] == [source_id]
        assert schema["properties"]["batch_id"]["enum"] == [batch.batch_id]
        assert schema["$defs"]["Obligation"]["properties"]["source_id"]["enum"] == [
            source_id
        ]
        assert schema["$defs"]["Obligation"]["properties"]["source_section"]["enum"] == [
            section_id
        ]
        return ModelResponse(
            content=json.dumps(
                {
                    "contract": "obligation_extraction_batch.v1",
                    "version": "1.0",
                    "source_id": source_id,
                    "batch_id": batch.batch_id,
                    "section_decisions": [
                        {
                            "section_id": section_id,
                            "decision": "no_obligation",
                            "reason": "Informational text only.",
                        }
                    ],
                    "obligations": [],
                }
            )
        )

    result = ObligationExtractor(StaticModelProvider(response)).extract(batch)

    assert result.source_id == source_id


def test_batch_planner_rejects_section_that_cannot_fit_budget() -> None:
    registry = _registry_for_sections(
        SourceSection(section_id="oversized", title="Large", text="x" * 2000, ordinal=1)
    )

    with pytest.raises(ValueError, match="exceeds batch input budget"):
        BatchPlanner(max_input_tokens=300).plan(registry)


def test_phase2_wraps_oversized_section_as_compilation_error(tmp_path: Path) -> None:
    source = tmp_path / "oversized.txt"
    source.write_text("x" * 2000, encoding="utf-8")

    with pytest.raises(Phase2CompilationError, match="source batching failed"):
        Phase2CompilationService(
            tmp_path / "workspace",
            StaticModelProvider(lambda _: ModelResponse(content="{}")),
            batch_planner=BatchPlanner(max_input_tokens=300),
        ).compile([source])


def test_cjk_token_estimate_is_more_conservative_than_ascii() -> None:
    assert estimate_tokens("中" * 100) > estimate_tokens("a" * 100)


def test_batch_coverage_requires_one_terminal_decision() -> None:
    batch = BatchPlanner().plan(
        _registry_for_sections(SourceSection(section_id="one", title="One", text="Text", ordinal=1))
    )[0]
    result = ObligationExtractionBatchResult(
        source_id="policy",
        batch_id=batch.batch_id,
        version="1.0",
        section_decisions=[
            SectionCoverageDecision(
                section_id="one", decision="no_obligation", reason="No normative language."
            )
        ],
    )

    validate_batch_coverage(batch, result)


def test_batch_coverage_rejects_missing_decision() -> None:
    batch = BatchPlanner().plan(
        _registry_for_sections(
            SourceSection(section_id="one", title="One", text="One", ordinal=1),
            SourceSection(section_id="two", title="Two", text="Two", ordinal=2),
        )
    )[0]
    result = ObligationExtractionBatchResult(
        source_id="policy", batch_id=batch.batch_id, version="1.0", section_decisions=[]
    )

    with pytest.raises(ValueError, match="missing section terminal decision"):
        validate_batch_coverage(batch, result)


def test_obligation_batches_merge_deterministically() -> None:
    first = ObligationExtractionBatchResult(
        source_id="policy",
        batch_id="batch-0002",
        version="1.0",
        section_decisions=[],
        obligations=[_obligation("obl.two", "two")],
    )
    second = ObligationExtractionBatchResult(
        source_id="policy",
        batch_id="batch-0001",
        version="1.0",
        section_decisions=[],
        obligations=[_obligation("obl.one", "one")],
    )

    merged = merge_obligation_batches([first, second])

    assert [item.obligation_id for item in merged] == ["obl.one", "obl.two"]
