"""Разбор руководства на структурные блоки: разделы, таблицы, шаги, предупреждения.

Ссылка на источник в ответе должна быть проверяемой, поэтому при разборе
запоминается номер раздела и страница для каждого блока. Таблицы сохраняются
как таблицы (строки + заголовок), а не как «плоский» текст: иначе строка
«ТО-2 | 400 (500)» потеряет привязку к своему заголовку и модель.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .textnorm import detect_models

HEADING_UPPER = re.compile(
    r"^(\d{1,2}(?:\.\s?\d{1,3}){0,3})\.?\s+([А-ЯЁ][А-ЯЁ0-9\s«»\-–—.,/()]{2,100})$"
)
HEADING_TITLE = re.compile(r"^(\d{1,2}(?:\.\s?\d{1,3}){1,3})\.?\s+([А-ЯЁ][а-яё][^.]{2,100})$")
TABLE_CAPTION = re.compile(r"^таблица\s*([\d.]+)\s*[:\-–—]?\s*(.*)$", re.IGNORECASE)
FIGURE_CAPTION = re.compile(r"^рис(?:унок|\.)?\s*([\d.]+)", re.IGNORECASE)
WARNING_START = re.compile(
    r"^(ВНИМАНИЕ|ВАЖНО|ЗАПРЕЩАЕТСЯ|НЕ\s+ДОПУСКАЕТСЯ|ОПАСНО|ПРЕДУПРЕЖДЕНИЕ)\b",
    re.IGNORECASE,
)
STEP_LINE = re.compile(r"^(\d{1,2})[.)]\s+(\S.*)$")
TOC_LINE = re.compile(r"\.{3,}\s*\d+\s*$")
PAGE_NUMBER = re.compile(r"^\d{1,3}$")
DASH_ROW = re.compile(r"^[-—_=+|\s]{4,}$")


@dataclass
class Block:
    kind: str  # heading | paragraph | table | steps | warning | figure
    text: str
    page: int
    section_no: str = ""
    section_title: str = ""
    section_path: str = ""
    table_id: str = ""
    caption: str = ""
    rows: list[list[str]] = field(default_factory=list)
    models: list[str] = field(default_factory=list)


def _split_cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.count("|") >= 2:
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    else:
        cells = [cell.strip() for cell in re.split(r"\s{2,}", stripped)]
    return cells


def _is_heading(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if len(stripped) > 110 or TOC_LINE.search(stripped):
        return None
    match = HEADING_UPPER.match(stripped)
    if match:
        title = match.group(2).strip()
        if len(title) >= 3 and not re.fullmatch(r"[А-ЯЁ\s]{1,3}", title):
            return re.sub(r"\s+", "", match.group(1)), title
    match = HEADING_TITLE.match(stripped)
    if match:
        return re.sub(r"\s+", "", match.group(1)), match.group(2).strip()
    return None


def _table_cells(line: str) -> list[str]:
    return _split_cells(line)


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 4 or TOC_LINE.search(stripped) or DASH_ROW.match(stripped):
        return False
    if _is_heading(stripped):
        return False
    if STEP_LINE.match(stripped) and len(_split_cells(stripped)) < 2:
        return False
    cells = _table_cells(stripped)
    non_empty = [cell for cell in cells if cell]
    return len(cells) >= 2 and len(non_empty) >= 2 and len(stripped) >= 10


def _join_lines(lines: list[str]) -> str:
    """Склейка строк абзаца с переносом дефисов и лишних пробелов."""
    result = ""
    for line in lines:
        piece = line.strip()
        if not piece:
            continue
        if result.endswith("-") and not result.endswith(" -"):
            result = result[:-1] + piece
        elif result:
            result = f"{result} {piece}"
        else:
            result = piece
    return re.sub(r"\s{2,}", " ", result).strip()


def _strip_running_headers(pages: list[str]) -> list[str]:
    """Убирает повторяющиеся колонтитулы и номера страниц.

    Иначе «Руководство по эксплуатации» попадает в каждый чанк и снижает
    точность поиска, а номер страницы может быть принят за техническое значение.
    """
    if len(pages) < 3:
        return pages
    counter: dict[str, int] = {}
    for page in pages:
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        candidates = lines[:2] + lines[-2:]
        for line in set(candidates):
            if 0 < len(line) <= 80:
                counter[line] = counter.get(line, 0) + 1
    threshold = max(3, int(len(pages) * 0.4))
    repeated = {line for line, count in counter.items() if count >= threshold}
    if not repeated:
        return pages
    cleaned = []
    for page in pages:
        kept = [line for line in page.splitlines() if line.strip() not in repeated]
        cleaned.append("\n".join(kept))
    return cleaned


class _SectionState:
    def __init__(self) -> None:
        self.stack: list[tuple[int, str, str]] = []

    def update(self, number: str, title: str) -> None:
        level = number.count(".") + 1
        self.stack = [item for item in self.stack if item[0] < level]
        self.stack.append((level, number, title))

    @property
    def number(self) -> str:
        return self.stack[-1][1] if self.stack else ""

    @property
    def title(self) -> str:
        return self.stack[-1][2] if self.stack else ""

    @property
    def path(self) -> str:
        return " / ".join(f"{num} {name}" for _, num, name in self.stack)


def parse_document(pages: list[str]) -> list[Block]:
    pages = _strip_running_headers(pages)
    blocks: list[Block] = []
    section = _SectionState()
    pending_caption = ""
    pending_table_id = ""

    def make(kind: str, text: str, page: int, **extra) -> Block:
        return Block(
            kind=kind,
            text=text,
            page=page,
            section_no=section.number,
            section_title=section.title,
            section_path=section.path,
            **extra,
        )

    for page_index, page_text in enumerate(pages, start=1):
        lines = page_text.splitlines()
        index = 0
        while index < len(lines):
            stripped = lines[index].strip()
            if not stripped or PAGE_NUMBER.match(stripped):
                index += 1
                continue

            caption_match = TABLE_CAPTION.match(stripped)
            if caption_match and len(stripped) < 160:
                pending_table_id = caption_match.group(1).rstrip(".")
                pending_caption = stripped
                index += 1
                continue

            if FIGURE_CAPTION.match(stripped) and len(stripped) < 140:
                blocks.append(make("figure", stripped, page_index))
                index += 1
                continue

            heading = _is_heading(stripped)
            if heading:
                section.update(*heading)
                blocks.append(make("heading", f"{heading[0]} {heading[1]}", page_index))
                pending_caption = ""
                index += 1
                continue

            if _is_table_line(stripped):
                group: list[str] = []
                cursor = index
                while cursor < len(lines):
                    candidate = lines[cursor].strip()
                    if not candidate:
                        break
                    if not _is_table_line(candidate):
                        break
                    group.append(candidate)
                    cursor += 1
                if len(group) >= 2 or max(
                    (len(_table_cells(row)) for row in group), default=0
                ) >= 3:
                    rows = _normalize_rows([_table_cells(row) for row in group])
                    caption = pending_caption
                    table_id = pending_table_id
                    text = _table_markdown(caption, rows)
                    blocks.append(
                        make(
                            "table",
                            text,
                            page_index,
                            table_id=table_id,
                            caption=caption,
                            rows=rows,
                            models=detect_models(text),
                        )
                    )
                    pending_caption = ""
                    pending_table_id = ""
                    index = cursor
                    continue

                # Одиночная строка с выравниванием пробелами — не таблица,
                # а обычный абзац: «Техническое обслуживание № 2 (ТО-2)
                # ( через каждые 500 часов работы)». Без этой ветки строка
                # выпадала: таблица не собиралась, а абзац отбрасывал её
                # как «табличную».
                blocks.append(make("paragraph", _join_lines([stripped]), page_index))
                index += 1
                continue

            if WARNING_START.match(stripped):
                collected = [stripped]
                cursor = index + 1
                while cursor < len(lines):
                    candidate = lines[cursor].strip()
                    if not candidate or _is_heading(candidate) or _is_table_line(candidate):
                        break
                    if STEP_LINE.match(candidate) or WARNING_START.match(candidate):
                        break
                    collected.append(candidate)
                    cursor += 1
                blocks.append(make("warning", " ".join(collected), page_index))
                index = cursor
                continue

            if STEP_LINE.match(stripped):
                steps: list[str] = []
                cursor = index
                while cursor < len(lines):
                    candidate = lines[cursor].strip()
                    if not candidate:
                        break
                    step_match = STEP_LINE.match(candidate)
                    if step_match:
                        steps.append(f"{step_match.group(1)}. {step_match.group(2).strip()}")
                    elif steps and not _is_heading(candidate) and not _is_table_line(candidate):
                        steps[-1] = f"{steps[-1]} {candidate}"
                    else:
                        break
                    cursor += 1
                if len(steps) >= 2:
                    blocks.append(make("steps", "\n".join(steps), page_index))
                    index = cursor
                    continue

            paragraph: list[str] = []
            cursor = index
            while cursor < len(lines):
                candidate = lines[cursor].strip()
                if not candidate or PAGE_NUMBER.match(candidate):
                    break
                if (
                    TABLE_CAPTION.match(candidate)
                    or FIGURE_CAPTION.match(candidate)
                    or _is_heading(candidate)
                    or _is_table_line(candidate)
                    or WARNING_START.match(candidate)
                    or STEP_LINE.match(candidate)
                    or TOC_LINE.search(candidate)
                ):
                    break
                paragraph.append(candidate)
                cursor += 1
            text = _join_lines(paragraph)
            if text:
                blocks.append(make("paragraph", text, page_index))
            index = max(cursor, index + 1)

    return blocks


def _normalize_rows(rows: list[list[str]]) -> list[list[str]]:
    if not rows:
        return []
    width = max(len(row) for row in rows)
    width = min(width, 12)
    normalized = []
    for row in rows:
        cells = [cell.strip() for cell in row[:width]]
        if len(cells) < width:
            cells.extend([""] * (width - len(cells)))
        normalized.append(cells)
    return normalized


def _table_markdown(caption: str, rows: list[list[str]]) -> str:
    if not rows:
        return caption
    header = rows[0]
    lines = []
    if caption:
        lines.append(f"{caption}")
        lines.append("")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for row in rows[1:]:
        safe = [cell.replace("|", "/") for cell in row]
        lines.append("| " + " | ".join(safe) + " |")
    if len(rows) == 1:
        # Таблица из одной строки: заголовок без данных, оставляем как есть.
        lines = [caption, "", "| " + " | ".join(header) + " |"] if caption else [
            "| " + " | ".join(header) + " |"
        ]
    return "\n".join(lines).strip()


def document_sections(blocks: list[Block]) -> int:
    return len({block.section_no for block in blocks if block.section_no})
