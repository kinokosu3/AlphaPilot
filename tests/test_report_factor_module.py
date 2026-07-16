"""Offline tests for the independent report-factor module."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from typing import Any

import pytest

from alphapilot.modules.report_factor.module import ReportFactorModule, _result_dict
from alphapilot.modules.report_factor.readers.azure import (
    AzureDocumentIntelligenceOCRProvider,
    read_pdf_with_azure,
)
from alphapilot.modules.report_factor.readers.base import PageText, PDFReadResult
from alphapilot.modules.report_factor.readers.ocr import (
    OCRProvider,
    OCRProviderRegistry,
    _entry_point_factory,
    _validate_ocr_result,
    discover_ocr_providers,
    normalize_provider_id,
)
from alphapilot.modules.report_factor.service import ReportFactorService
from alphapilot.modules.report_factor.settings import ReportFactorSettings
from alphapilot.systems.factor.types import FactorValidationResult


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.calls = 0

    def count_tokens(self, **_kwargs: Any) -> int:
        return 100

    def chat_completion(self, **_kwargs: Any) -> str:
        self.calls += 1
        return next(self.responses)


class FakeFactorSystem:
    def __init__(self) -> None:
        self.validated: list[str] = []
        self.added: list[str] = []

    def validate_expression(self, expression: str) -> FactorValidationResult:
        self.validated.append(expression)
        return FactorValidationResult(True, "ok", "valid", {"expression": expression})

    def add_factor(self, factor_name: str, factor_expression: str, **_kwargs: Any) -> FactorValidationResult:
        self.added.append(factor_name)
        return FactorValidationResult(bool(factor_expression), "ok", "added")


def settings(tmp_path: Path, **overrides: Any) -> ReportFactorSettings:
    values: dict[str, Any] = {
        "upload_root": tmp_path / "important_data" / "report_factor" / "uploads",
        "max_upload_bytes": 50 * 1024 * 1024,
        "min_text_chars": 500,
        "chunk_token_limit": 12_000,
        "max_evidence_chars": 500,
        "max_evidence_items": 3,
        "llm_json_retries": 2,
        "default_ocr_provider": "azure",
        "azure_key": "",
        "azure_endpoint": "",
    }
    values.update(overrides)
    return ReportFactorSettings(**values)


def test_extract_returns_page_aware_review_drafts_without_writing_zoo(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.7 fake report")
    report_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    draft_id = hashlib.sha256(f"{report_hash}:momentum_5d".encode()).hexdigest()[:20]
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "summary": "Momentum research",
                    "classification": {
                        "relevant": True,
                        "label": "quant_factor_research",
                        "reason": "factor study",
                    },
                }
            ),
            json.dumps(
                {
                    "factors": [
                        {
                            "factor_name": "Momentum 5D",
                            "description": "five-day momentum",
                            "formulation": "P_t/P_{t-5}-1",
                            "variables": {"P": "close price"},
                            "source_pages": [1],
                            "evidence": ["five-day price momentum"],
                            "viability": {"status": "viable", "reason": "daily prices exist"},
                        },
                        {
                            "factor_name": "momentum_5d",
                            "description": "duplicate mention with a longer description",
                            "formulation": "P_t/P_{t-5}-1",
                            "variables": {},
                            "source_pages": [2],
                            "evidence": ["momentum is evaluated over five days"],
                            "viability": {"status": "viable", "reason": "daily prices exist"},
                        },
                    ]
                }
            ),
            json.dumps(
                {
                    "expressions": [
                        {
                            "draft_id": draft_id,
                            "factor_expression": "$close/Ref($close,5)-1",
                        }
                    ]
                }
            ),
        ]
    )
    factor = FakeFactorSystem()
    service = ReportFactorService(llm=llm, factor_system=factor, settings=settings(tmp_path))
    monkeypatch.setattr(
        service,
        "_read",
        lambda *_args, **_kwargs: PDFReadResult(
            pages=[PageText(1, "momentum page one"), PageText(2, "momentum page two")],
            parser="fake",
        ),
    )

    result = service.extract(str(source), ocr_mode="local")

    assert result.schema_version == "1.0"
    assert result.report.page_count == 2
    assert len(result.factors) == 1
    draft = result.factors[0]
    assert draft.draft_id == draft_id
    assert draft.source_pages == [1, 2]
    assert len(draft.evidence) == 2
    assert draft.factor_expression == "$close/Ref($close,5)-1"
    assert draft.validation.acceptable is True
    assert factor.validated == ["$close/Ref($close,5)-1"]
    assert factor.added == []


def test_llm_json_is_retried_twice(tmp_path) -> None:
    llm = FakeLLM(["not json", "```json\nnope\n```", '{"ok": true}'])
    service = ReportFactorService(
        llm=llm,
        factor_system=FakeFactorSystem(),
        settings=settings(tmp_path, llm_json_retries=2),
    )

    assert service._chat_json("system", "user") == {"ok": True}
    assert llm.calls == 3


def test_module_result_conversion_delegation_and_commit_rejections(
    tmp_path,
    monkeypatch,
) -> None:  # noqa: ANN001
    from alphapilot.modules.report_factor import service as service_module

    @dataclass
    class DataclassResult:
        acceptable: bool

    class UnknownResult:
        def __repr__(self) -> str:
            return "unknown-value"

    assert _result_dict(DataclassResult(True)) == {"acceptable": True}
    assert _result_dict({"acceptable": True}) == {"acceptable": True}
    assert _result_dict(UnknownResult()) == {
        "acceptable": False,
        "code": "unknown_result",
        "message": "unknown-value",
    }

    calls: dict[str, Any] = {}

    class FakeExtraction:
        def to_dict(self) -> dict[str, Any]:
            return {"schema_version": "test"}

    class FakeService:
        def __init__(self, **kwargs: Any) -> None:
            calls["init"] = kwargs

        def extract(self, source: str, **kwargs: Any) -> FakeExtraction:
            calls["extract"] = {"source": source, **kwargs}
            return FakeExtraction()

    monkeypatch.setattr(service_module, "ReportFactorService", FakeService)
    monkeypatch.setenv("ALPHAPILOT_IMPORTANT_DATA_DIR", str(tmp_path / "important_data"))
    factor = FakeFactorSystem()
    llm = object()
    module = ReportFactorModule()
    module.setup(SimpleNamespace(get_llm=lambda: llm, factor=lambda: factor))

    assert module.validate_ocr_mode(" LOCAL ") == "local"
    with pytest.raises(ValueError, match="Unknown OCR mode/provider"):
        module.validate_ocr_mode("missing")
    result = module.extract_pdf(
        "report.pdf",
        market="A-share",
        frequency="week",
        available_data=["close"],
        ocr_mode="local",
        progress_callback=lambda *_args, **_kwargs: None,
    )
    assert result == {"schema_version": "test"}
    assert calls["init"]["llm"] is llm
    assert calls["init"]["factor_system"] is factor
    assert calls["extract"]["frequency"] == "week"

    commit = module.commit_factors(
        "job-edge",
        [
            {},
            {"draft_id": "same", "factor_name": "one", "factor_expression": "$close"},
            {"draft_id": "same", "factor_name": "two", "factor_expression": "$open"},
        ],
    )
    assert commit["n_committed"] == 1
    assert [item["code"] for item in commit["rejected"]] == [
        "invalid_commit_item",
        "duplicate_draft_id",
    ]


def test_local_pdf_reader_handles_encryption_page_failures_and_empty_documents(
    tmp_path,
    monkeypatch,
) -> None:  # noqa: ANN001
    import pypdf

    from alphapilot.modules.report_factor.readers.local import read_pdf_locally

    class RaisingEncryptedReader:
        is_encrypted = True
        pages: list[Any] = []

        def decrypt(self, _password: str) -> int:
            raise RuntimeError("locked")

    monkeypatch.setattr(pypdf, "PdfReader", lambda _path: RaisingEncryptedReader())
    with pytest.raises(ValueError, match="Encrypted PDF"):
        read_pdf_locally(tmp_path / "locked.pdf")

    class LockedReader(RaisingEncryptedReader):
        def decrypt(self, _password: str) -> int:
            return 0

    monkeypatch.setattr(pypdf, "PdfReader", lambda _path: LockedReader())
    with pytest.raises(ValueError, match="Encrypted PDF"):
        read_pdf_locally(tmp_path / "locked.pdf")

    class BrokenPage:
        def extract_text(self) -> str:
            raise RuntimeError("bad page")

    class PartialReader:
        is_encrypted = False
        pages = [BrokenPage()]

    monkeypatch.setattr(pypdf, "PdfReader", lambda _path: PartialReader())
    partial = read_pdf_locally(tmp_path / "partial.pdf")
    assert partial.pages[0].text == ""
    assert partial.warnings == ["Page 1 text extraction failed: RuntimeError"]

    class EmptyReader:
        is_encrypted = False
        pages: list[Any] = []

    monkeypatch.setattr(pypdf, "PdfReader", lambda _path: EmptyReader())
    with pytest.raises(ValueError, match="no pages"):
        read_pdf_locally(tmp_path / "empty.pdf")


def test_ocr_registry_rejects_invalid_registration_factory_and_result_shapes(tmp_path) -> None:
    assert normalize_provider_id(" Vendor-1 ") == "vendor-1"
    for invalid in ("", "1vendor", "local", "auto", "bad space"):
        with pytest.raises(ValueError):
            normalize_provider_id(invalid)

    registry = OCRProviderRegistry()
    with pytest.raises(TypeError, match="factory must be callable"):
        registry.register("vendor", object())  # type: ignore[arg-type]
    registry.register("vendor", lambda: object())  # type: ignore[arg-type]
    assert registry.has("bad space") is False
    with pytest.raises(ValueError, match="already registered"):
        registry.register("vendor", lambda: object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unknown OCR provider"):
        registry.create("missing")
    with pytest.raises(TypeError, match="expected OCRProvider"):
        registry.create("vendor")

    class DifferentProvider(OCRProvider):
        provider_id = "different"

        def extract_pdf(self, _source: Path) -> PDFReadResult:
            raise AssertionError("not reached")

    registry.register("vendor", DifferentProvider, replace=True)
    with pytest.raises(ValueError, match="does not match"):
        registry.create("vendor")

    invalid_results = [
        (object(), TypeError, "expected PDFReadResult"),
        (PDFReadResult([PageText(1, "text")], "", True), ValueError, "empty parser"),
        (PDFReadResult([], "parser", True), ValueError, "no pages"),
        (PDFReadResult(["bad"], "parser", True), TypeError, "non-PageText"),  # type: ignore[list-item]
        (PDFReadResult([PageText(True, "text")], "parser", True), TypeError, "non-integer"),
        (PDFReadResult([PageText(0, "text")], "parser", True), ValueError, "invalid page"),
        (
            PDFReadResult([PageText(1, "one"), PageText(1, "two")], "parser", True),
            ValueError,
            "duplicate page",
        ),
        (PDFReadResult([PageText(1, 42)], "parser", True), TypeError, "text must be str"),  # type: ignore[arg-type]
        (PDFReadResult([PageText(1, "  ")], "parser", True), ValueError, "no readable text"),
    ]
    for payload, error, match in invalid_results:
        with pytest.raises(error, match=match):
            _validate_ocr_result("vendor", payload)


def test_azure_ocr_adapter_maps_pages_content_fallback_and_empty_result(
    tmp_path,
    monkeypatch,
) -> None:  # noqa: ANN001
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-test")
    results = iter(
        [
            SimpleNamespace(
                pages=[
                    SimpleNamespace(
                        page_number=3,
                        lines=[SimpleNamespace(content="line one"), SimpleNamespace(content="line two")],
                    )
                ],
                content="",
            ),
            SimpleNamespace(pages=[], content="fallback text"),
            SimpleNamespace(pages=[], content=""),
        ]
    )

    class Poller:
        def result(self) -> Any:
            return next(results)

    class Client:
        def __init__(self, *, endpoint: str, credential: Any) -> None:
            assert endpoint == "https://azure.example"
            assert credential.key == "secret"

        def begin_analyze_document(self, model: str, handle: Any) -> Poller:
            assert model == "prebuilt-layout"
            assert handle.read().startswith(b"%PDF")
            return Poller()

    class Credential:
        def __init__(self, key: str) -> None:
            self.key = key

    formrecognizer = ModuleType("azure.ai.formrecognizer")
    formrecognizer.DocumentAnalysisClient = Client  # type: ignore[attr-defined]
    credentials = ModuleType("azure.core.credentials")
    credentials.AzureKeyCredential = Credential  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "azure", ModuleType("azure"))
    monkeypatch.setitem(sys.modules, "azure.ai", ModuleType("azure.ai"))
    monkeypatch.setitem(sys.modules, "azure.ai.formrecognizer", formrecognizer)
    monkeypatch.setitem(sys.modules, "azure.core", ModuleType("azure.core"))
    monkeypatch.setitem(sys.modules, "azure.core.credentials", credentials)

    provider = AzureDocumentIntelligenceOCRProvider(
        endpoint="https://azure.example",
        key="secret",
    )
    page_result = provider.extract_pdf(source)
    assert [(page.page_number, page.text) for page in page_result.pages] == [
        (3, "line one\nline two")
    ]
    fallback = read_pdf_with_azure(
        source,
        endpoint="https://azure.example",
        key="secret",
    )
    assert fallback.pages == [PageText(1, "fallback text")]
    with pytest.raises(ValueError, match="no readable pages"):
        provider.extract_pdf(source)
    with pytest.raises(RuntimeError, match="credentials are not configured"):
        AzureDocumentIntelligenceOCRProvider(endpoint="", key="").extract_pdf(source)


def test_auto_ocr_requires_credentials_for_sparse_pdf(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from alphapilot.modules.report_factor.readers import local

    monkeypatch.setattr(
        local,
        "read_pdf_locally",
        lambda _path: PDFReadResult([PageText(1, "short")], parser="pypdf"),
    )
    service = ReportFactorService(
        llm=FakeLLM([]),
        factor_system=FakeFactorSystem(),
        settings=settings(tmp_path, min_text_chars=500),
    )

    with pytest.raises(RuntimeError, match="Azure OCR is required"):
        service._read(tmp_path / "scan.pdf", ocr_mode="auto", progress_callback=None)


def test_local_reader_preserves_page_numbers(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    import pypdf

    from alphapilot.modules.report_factor.readers.local import read_pdf_locally

    class FakePage:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class FakeReader:
        is_encrypted = False
        pages = [FakePage("first"), FakePage("second")]

    monkeypatch.setattr(pypdf, "PdfReader", lambda _path: FakeReader())
    result = read_pdf_locally(tmp_path / "report.pdf")

    assert [(page.page_number, page.text) for page in result.pages] == [(1, "first"), (2, "second")]


def test_custom_ocr_provider_can_replace_auto_fallback_without_service_changes(
    tmp_path,
    monkeypatch,
) -> None:  # noqa: ANN001
    from alphapilot.modules.report_factor.readers import local

    calls: list[Path] = []

    class CustomOCRProvider(OCRProvider):
        provider_id = "custom"
        display_name = "Custom OCR"

        def extract_pdf(self, source: Path) -> PDFReadResult:
            calls.append(source)
            return PDFReadResult(
                pages=[PageText(1, "custom OCR text")],
                parser="custom_ocr_v1",
                ocr_used=True,
            )

    registry = OCRProviderRegistry()
    registry.register("custom", CustomOCRProvider, display_name="Custom OCR")
    monkeypatch.setattr(
        local,
        "read_pdf_locally",
        lambda _path: PDFReadResult([PageText(1, "short")], parser="pypdf"),
    )
    service = ReportFactorService(
        llm=FakeLLM([]),
        factor_system=FakeFactorSystem(),
        settings=settings(
            tmp_path,
            min_text_chars=500,
            default_ocr_provider="custom",
        ),
        ocr_registry=registry,
    )
    source = tmp_path / "scan.pdf"

    result = service._read(source, ocr_mode="auto", progress_callback=None)

    assert calls == [source]
    assert result.parser == "custom_ocr_v1"
    assert result.ocr_used is True
    assert "custom" in " ".join(result.warnings)
    assert service.validate_ocr_mode("CUSTOM") == "custom"


def test_ocr_registry_enforces_provider_result_contract(tmp_path) -> None:
    class InvalidOCRProvider(OCRProvider):
        provider_id = "invalid"

        def extract_pdf(self, _source: Path) -> PDFReadResult:
            return PDFReadResult(
                pages=[PageText(1, "text")],
                parser="invalid",
                ocr_used=False,
            )

    registry = OCRProviderRegistry()
    registry.register("invalid", InvalidOCRProvider)

    with pytest.raises(ValueError, match="ocr_used=True"):
        registry.extract_pdf("invalid", tmp_path / "report.pdf")


def test_report_factor_module_exposes_lazy_provider_registration(
    tmp_path,
    monkeypatch,
) -> None:  # noqa: ANN001
    monkeypatch.setenv("ALPHAPILOT_IMPORTANT_DATA_DIR", str(tmp_path / "important_data"))
    constructed: list[str] = []

    class ManualOCRProvider(OCRProvider):
        provider_id = "manual_ocr"

        def __init__(self) -> None:
            constructed.append("manual_ocr")

        def extract_pdf(self, _source: Path) -> PDFReadResult:
            return PDFReadResult(
                pages=[PageText(1, "manual OCR text")],
                parser="manual_ocr",
                ocr_used=True,
            )

    module = ReportFactorModule()
    module.setup(SimpleNamespace())
    module.register_ocr_provider(
        "manual_ocr",
        ManualOCRProvider,
        display_name="Manual OCR",
    )

    catalog = module.ocr_providers()

    assert "manual_ocr" in catalog["modes"]
    assert next(
        item for item in catalog["providers"] if item["provider_id"] == "manual_ocr"
    )["source"] == "manual"
    assert constructed == []


def test_ocr_entry_point_is_discovered_but_loaded_only_when_selected(
    tmp_path,
    monkeypatch,
) -> None:  # noqa: ANN001
    from alphapilot.modules.report_factor.readers import ocr

    loaded: list[str] = []

    class PluginOCRProvider(OCRProvider):
        provider_id = "vendor"

        def extract_pdf(self, _source: Path) -> PDFReadResult:
            return PDFReadResult(
                pages=[PageText(1, "plugin OCR text")],
                parser="vendor_api",
                ocr_used=True,
            )

    class EntryPoint:
        name = "vendor"

        def load(self):  # noqa: ANN201
            loaded.append("vendor")
            return PluginOCRProvider

    monkeypatch.setattr(ocr, "_entry_points", lambda: [EntryPoint()])
    registry = OCRProviderRegistry()

    assert discover_ocr_providers(registry) == []
    assert registry.provider_ids() == ["vendor"]
    assert loaded == []

    result = registry.extract_pdf("vendor", tmp_path / "report.pdf")

    assert loaded == ["vendor"]
    assert result.pages[0].page_number == 1


def test_ocr_entry_point_factory_and_discovery_isolate_all_invalid_plugins(
    monkeypatch,
) -> None:  # noqa: ANN001
    from alphapilot.modules.report_factor.readers import ocr

    class Provider(OCRProvider):
        provider_id = "vendor"

        def extract_pdf(self, _source: Path) -> PDFReadResult:
            return PDFReadResult([PageText(1, "text")], "vendor", True)

    class EntryPoint:
        def __init__(self, name: str, loaded: Any) -> None:
            self.name = name
            self.loaded = loaded

        def load(self) -> Any:
            return self.loaded

    provider = Provider()
    assert _entry_point_factory(EntryPoint("vendor", provider)) is provider
    with pytest.raises(TypeError, match="returned object"):
        _entry_point_factory(EntryPoint("bad-factory", lambda: object()))
    with pytest.raises(TypeError, match="expected an OCRProvider"):
        _entry_point_factory(EntryPoint("bad-value", object()))

    registry = OCRProviderRegistry()
    registry.register("vendor", Provider)
    monkeypatch.setattr(
        ocr,
        "_entry_points",
        lambda: [
            EntryPoint("vendor", Provider),
            EntryPoint("bad name", Provider),
        ],
    )
    skipped = discover_ocr_providers(registry)
    assert skipped[0] == "vendor: duplicate provider id"
    assert "bad name: ValueError" in skipped[1]


def test_commit_is_explicit_and_returns_partial_failures(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("ALPHAPILOT_IMPORTANT_DATA_DIR", str(tmp_path / "important_data"))

    class Factor(FakeFactorSystem):
        def add_factor(self, factor_name: str, factor_expression: str, **_kwargs: Any) -> FactorValidationResult:
            self.added.append(factor_name)
            if factor_name == "bad":
                return FactorValidationResult(False, "parse_error", "invalid")
            return FactorValidationResult(True, "ok", "added")

    factor = Factor()
    context = SimpleNamespace(factor=lambda: factor)
    module = ReportFactorModule()
    module.setup(context)

    result = module.commit_factors(
        "job-1",
        [
            {"draft_id": "one", "factor_name": "good", "factor_expression": "$close"},
            {"draft_id": "two", "factor_name": "bad", "factor_expression": "bad"},
        ],
    )

    assert result["n_committed"] == 1
    assert result["n_rejected"] == 1
    assert factor.added == ["good", "bad"]


def test_report_factor_module_has_no_forbidden_imports() -> None:
    root = Path(__file__).resolve().parents[1] / "alphapilot" / "modules" / "report_factor"
    forbidden = (
        "alphapilot.modules.alpha_mining",
        "alphapilot.systems.backtest",
        "alphapilot.systems.strategy",
        "alphapilot.systems.trading",
        "alphapilot.systems.live",
    )
    imported: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
    assert not [name for name in imported if name.startswith(forbidden)]


@pytest.mark.real_llm
def test_real_report_factor_smoke() -> None:
    source = os.getenv("ALPHAPILOT_REPORT_FACTOR_REAL_PDF")
    if not source:
        pytest.skip("ALPHAPILOT_REPORT_FACTOR_REAL_PDF is not configured")
    from alphapilot.kernel import build_engine

    result = build_engine().get_module("report_factor").extract_pdf(source, ocr_mode="local")
    assert result["schema_version"] == "1.0"
    assert isinstance(result["factors"], list)
