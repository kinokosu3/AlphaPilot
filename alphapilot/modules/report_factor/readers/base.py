"""Reader-neutral page contracts."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PageText:
    page_number: int
    text: str


@dataclass
class PDFReadResult:
    pages: list[PageText]
    parser: str
    ocr_used: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def text_chars(self) -> int:
        return sum(len(page.text.strip()) for page in self.pages)
