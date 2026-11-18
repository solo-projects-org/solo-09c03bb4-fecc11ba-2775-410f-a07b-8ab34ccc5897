"""Pure carryover decision logic.

The caller supplies injections in run order: the order in which the batch CSV
was first uploaded (later new injection numbers are appended).  A sample whose
peak area is at or above the batch high threshold starts a carryover episode.
Following samples are flagged until a blank at or below the blank limit is
seen.  A blank above the limit, or one with a missing peak area, cannot clear
the episode.  A later high sample becomes the new trigger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional


SAMPLE = "sample"
BLANK = "blank"

HIGH = "high"
CARRYOVER_RISK = "carryover_risk"
CLEAR = "clear"
BLANK_CLEAR = "blank_clear"
BLANK_EXCEEDED = "blank_exceeded"
BLANK_MISSING = "blank_missing"

STATUS_LABELS = {
    HIGH: "高值样品",
    CARRYOVER_RISK: "残留风险",
    CLEAR: "未标记",
    BLANK_CLEAR: "空白未超限",
    BLANK_EXCEEDED: "空白超限",
    BLANK_MISSING: "空白缺失",
}

KIND_LABELS = {SAMPLE: "样品", BLANK: "冲洗空白"}


@dataclass(frozen=True)
class Injection:
    injection_no: str
    kind: str
    peak_area: Optional[float]
    revision: int
    position: int = 0


@dataclass(frozen=True)
class ResultRow:
    injection_no: str
    kind: str
    peak_area: Optional[float]
    revision: int
    status: str
    trigger_injection: Optional[str]
    note: str


def natural_key(value: str) -> tuple:
    """Sort injection numbers naturally ("2" before "10"); informational only."""
    parts = re.split(r"(\d+)", value.casefold())
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)


def sort_run(rows: Iterable[Injection]) -> list[Injection]:
    """Stable sort by stored run position.

    Equal positions keep the incoming order, so callers that already hold the
    run sequence (and leave position at its default) are unaffected.
    """
    return sorted(rows, key=lambda injection: injection.position)


def compute_results(
    injections: Iterable[Injection],
    high_threshold: float,
    blank_limit: float,
) -> list[ResultRow]:
    ordered = sort_run(injections)
    results: list[ResultRow] = []
    trigger: Optional[str] = None

    for injection in ordered:
        status: str
        note = ""
        trigger_for_row = None

        if injection.kind == BLANK:
            if injection.peak_area is None:
                status = BLANK_MISSING
                note = "冲洗空白峰面积缺失，不能解除残留风险"
                if trigger is not None:
                    note += f"（仍由进样 {trigger} 触发）"
                    trigger_for_row = trigger
            elif injection.peak_area <= blank_limit:
                status = BLANK_CLEAR
                if trigger is not None:
                    note = f"冲洗空白未超限，解除进样 {trigger} 触发的残留风险"
                else:
                    note = "冲洗空白未超限"
                trigger = None
            else:
                status = BLANK_EXCEEDED
                note = "冲洗空白超出空白上限，不能解除残留风险"
                if trigger is not None:
                    note += f"（仍由进样 {trigger} 触发）"
                    trigger_for_row = trigger
        else:
            # A sample without an area is rejected during CSV validation, but
            # keep this defensive branch so malformed database rows cannot
            # silently clear a risk.
            if injection.peak_area is None:
                status = CARRYOVER_RISK if trigger is not None else CLEAR
                note = "样品峰面积缺失"
                trigger_for_row = trigger
            elif injection.peak_area >= high_threshold:
                status = HIGH
                trigger = injection.injection_no
                note = "达到高值阈值，作为后续样品的残留触发进样"
            elif trigger is not None:
                status = CARRYOVER_RISK
                trigger_for_row = trigger
                note = f"高值进样 {trigger} 之后尚未出现有效冲洗空白"
            else:
                status = CLEAR

        results.append(
            ResultRow(
                injection_no=injection.injection_no,
                kind=injection.kind,
                peak_area=injection.peak_area,
                revision=injection.revision,
                status=status,
                trigger_injection=trigger_for_row,
                note=note,
            )
        )

    return results
