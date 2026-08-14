"""Phase 1/2 report scaffolding. No alpha claims."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResearchReport:
    status: str
    title: str
    alpha_claim: str | None
    notes: tuple[str, ...]


class ReportBuilder:
    def build(self) -> ResearchReport:
        return ResearchReport(
            status="insufficient_data",
            title="Weather-market research report (scaffold)",
            alpha_claim=None,
            notes=(
                "Phase 1/2 does not compute or claim alpha.",
                "See docs/RESEARCH_INTEGRITY.md before any future evaluation.",
            ),
        )
