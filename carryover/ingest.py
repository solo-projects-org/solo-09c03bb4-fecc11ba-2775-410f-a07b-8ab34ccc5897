"""CSV parsing and row-level validation for uploads."""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from typing import Iterable, Optional

from .engine import BLANK, SAMPLE


MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ERRORS = 25


class CsvError(ValueError):
    def __init__(self, messages: Iterable[str] | str):
        if isinstance(messages, str):
            messages = [messages]
        self.messages = list(messages)
        super().__init__("; ".join(self.messages))


@dataclass(frozen=True)
class ParsedRow:
    injection_no: str
    kind: str
    peak_area: Optional[float]
    revision: int


HEADER_ALIASES = {
    "injection_no": {
        "injection_no", "injection no", "injection", "injection_number",
        "injection number", "vial", "vial_no", "vial no", "sample_no",
        "sample no", "进样号", "进样编号", "序号", "样品号", "瓶号",
    },
    "kind": {
        "kind", "type", "injection_type", "injection type", "sample_type",
        "sample type", "类型", "进样类型", "样品类型",
    },
    "peak_area": {
        "peak_area", "peak area", "area", "峰面积", "面积",
    },
    "revision": {
        "revision", "rev", "revision_no", "revision no", "修订号", "修订版本",
        "版本号", "修订",
    },
}

SAMPLE_ALIASES = {
    "sample", "samples", "sample(s)", "s", "样品", "试样", "样本",
}
BLANK_ALIASES = {
    "blank", "blanks", "flush blank", "flushblank", "flush_blank",
    "rinse blank", "rinseblank", "rinse_blank", "carryover blank",
    "空白", "冲洗空白", "洗针空白", "冲洗液空白", "空白样品",
}


def decode_csv(data: bytes) -> str:
    if not data:
        raise CsvError("CSV 文件为空")
    if b"\x00" in data:
        raise CsvError("CSV 文件包含二进制内容，请另存为 UTF-8 或 GB18030 文本")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvError("无法解码 CSV，请使用 UTF-8 或 GB18030 编码")


def _normalise_header(value: str) -> str:
    return value.strip().lstrip("﻿").casefold()


def _header_map(fieldnames: list[str] | None) -> dict[str, str]:
    if not fieldnames:
        raise CsvError("CSV 缺少表头")
    normalised: dict[str, str] = {}
    for index, raw in enumerate(fieldnames):
        name = _normalise_header(raw or "")
        if not name:
            continue
        for canonical, aliases in HEADER_ALIASES.items():
            if name in aliases:
                if canonical in normalised:
                    raise CsvError(f"CSV 表头重复定义了 {canonical} 列")
                normalised[canonical] = index
                break
    missing = [name for name in HEADER_ALIASES if name not in normalised]
    if missing:
        labels = {
            "injection_no": "进样号",
            "kind": "类型",
            "peak_area": "峰面积",
            "revision": "修订号",
        }
        raise CsvError(
            "CSV 缺少必需列：" + "、".join(labels[name] for name in missing)
        )
    return normalised


def _parse_kind(raw: str, line: int) -> str:
    value = raw.strip().casefold()
    if value in SAMPLE_ALIASES:
        return SAMPLE
    if value in BLANK_ALIASES:
        return BLANK
    raise ValueError(f"第 {line} 行类型“{raw.strip()}”无法识别（应为样品或冲洗空白）")


def _parse_area(raw: str, kind: str, line: int) -> Optional[float]:
    text = raw.strip().replace(" ", " ")
    if not text:
        if kind == SAMPLE:
            raise ValueError(f"第 {line} 行样品缺少峰面积")
        return None
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"第 {line} 行峰面积“{raw.strip()}”不是数字") from None
    if not math.isfinite(value):
        raise ValueError(f"第 {line} 行峰面积必须是有限数值")
    if value < 0:
        raise ValueError(f"第 {line} 行峰面积不能为负数")
    return value


def _parse_revision(raw: str, line: int) -> int:
    text = raw.strip()
    if not text:
        raise ValueError(f"第 {line} 行缺少修订号")
    try:
        revision = int(text)
    except ValueError:
        raise ValueError(f"第 {line} 行修订号“{raw.strip()}”必须是整数") from None
    if revision < 1:
        raise ValueError(f"第 {line} 行修订号必须不小于 1")
    return revision


def parse_csv(data: bytes) -> list[ParsedRow]:
    text = decode_csv(data)
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise CsvError("CSV 文件为空") from None

    positions = _header_map(header)
    parsed: list[ParsedRow] = []
    errors: list[str] = []

    for line, cells in enumerate(reader, start=2):
        if not cells or all(not cell.strip() for cell in cells):
            continue

        def cell(name: str) -> str:
            index = positions[name]
            return cells[index] if index < len(cells) else ""

        try:
            injection_no = cell("injection_no").strip()
            if not injection_no:
                raise ValueError(f"第 {line} 行缺少进样号")
            if len(injection_no) > 64:
                raise ValueError(f"第 {line} 行进样号超过 64 个字符")
            if any(ord(ch) < 32 for ch in injection_no):
                raise ValueError(f"第 {line} 行进样号包含控制字符")
            kind = _parse_kind(cell("kind"), line)
            peak_area = _parse_area(cell("peak_area"), kind, line)
            revision = _parse_revision(cell("revision"), line)
            parsed.append(ParsedRow(injection_no, kind, peak_area, revision))
        except ValueError as exc:
            errors.append(str(exc))
            if len(errors) >= MAX_ERRORS:
                errors.append(f"错误过多，仅显示前 {MAX_ERRORS} 条")
                break

    if errors:
        raise CsvError(errors)
    if not parsed:
        raise CsvError("CSV 没有数据行")

    # A single file may contain both an old and a corrected row for the same
    # injection.  Keep the highest revision; reject conflicting content at the
    # same revision instead of guessing which one the operator meant.
    highest: dict[str, ParsedRow] = {}
    order: list[str] = []
    for row in parsed:
        previous = highest.get(row.injection_no)
        if previous is None:
            highest[row.injection_no] = row
            order.append(row.injection_no)
        elif row.revision > previous.revision:
            highest[row.injection_no] = row
        elif row.revision == previous.revision:
            if (row.kind, row.peak_area) != (previous.kind, previous.peak_area):
                raise CsvError(
                    f"同一文件内进样号 {row.injection_no} 的修订号 "
                    f"{row.revision} 存在冲突数据"
                )
        # A lower revision in the same file is superseded; ignore it.

    return [highest[no] for no in order]
