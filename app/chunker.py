"""Сборка чанков: разделы и таблицы режутся по-разному.

Правила, которые влияют на качество ответов:
* шаги процедуры не разрезаются — порядок и полнота критичны;
* таблица режется по строкам с повтором заголовка, иначе значение теряет
  привязку к столбцу (например, к модели ПД5,0);
* предупреждения «ВНИМАНИЕ» / «ВАЖНО» остаются в том же чанке, что и
  операция, к которой они относятся, и дополнительно выносятся в метаданные.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .structure import Block
from .textnorm import detect_models, split_sentences

MAX_CHARS = 1100
OVERLAP_CHARS = 160
MAX_TABLE_ROWS = 22


@dataclass
class Chunk:
    body: str
    kind: str
    page_from: int
    page_to: int
    section_no: str = ""
    section_title: str = ""
    section_path: str = ""
    table_id: str = ""
    models: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "body": self.body,
            "kind": self.kind,
            "page_from": self.page_from,
            "page_to": self.page_to,
            "section_no": self.section_no,
            "section_title": self.section_title,
            "section_path": self.section_path,
            "table_id": self.table_id,
            "models": self.models,
            "warnings": self.warnings,
        }


def _split_long_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    sentences = split_sentences(text) or [text]
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) + 1 > limit:
            parts.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current.strip())
    if len(parts) > 1:
        overlapped = [parts[0]]
        for index in range(1, len(parts)):
            tail = parts[index - 1][-OVERLAP_CHARS:]
            overlapped.append(f"{tail} {parts[index]}".strip())
        parts = overlapped
    return parts


def _split_table(rows: list[list[str]], limit: int = MAX_TABLE_ROWS) -> list[list[list[str]]]:
    if len(rows) <= limit + 1:
        return [rows]
    header, data = rows[0], rows[1:]
    groups = []
    for start in range(0, len(data), limit):
        groups.append([header, *data[start : start + limit]])
    return groups


def _rows_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header = rows[0]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows[1:]:
        lines.append("| " + " | ".join(cell.replace("|", "/") for cell in row) + " |")
    return "\n".join(lines)


def build_chunks(blocks: list[Block]) -> list[Chunk]:
    chunks: list[Chunk] = []

    buffer: list[tuple[str, int]] = []
    warnings_active: list[str] = []

    section_no = ""
    section_title = ""
    section_path = ""

    def emit(body: str, kind: str, page_from: int, page_to: int, **extra) -> None:
        body = body.strip()
        if not body:
            return
        # В ответ попадают только предупреждения, присутствующие в самом
        # фрагменте. Иначе первый чанк главы уносит все «ВНИМАНИЕ» раздела,
        # и вместо одного нужного на экране оказывается два десятка.
        warnings = [
            warning
            for warning in dict.fromkeys(warnings_active)
            if warning.strip()[:40] in body
        ][:3]
        chunks.append(
            Chunk(
                body=body,
                kind=kind,
                page_from=page_from,
                page_to=max(page_to, page_from),
                section_no=section_no,
                section_title=section_title,
                section_path=section_path,
                models=extra.get("models") or detect_models(body),
                warnings=warnings,
                table_id=extra.get("table_id", ""),
            )
        )

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        text = "\n".join(piece for piece, _ in buffer).strip()
        pages = [page for _, page in buffer]
        buffer = []
        if not text:
            return
        for part in _split_long_text(text):
            emit(part, "text", min(pages), max(pages))

    for block in blocks:
        if block.kind == "heading":
            flush()
            section_no = block.section_no or block.text.split(" ")[0]
            section_title = block.section_title or ""
            section_path = block.section_path or block.text
            warnings_active = []
            continue

        if block.kind == "paragraph":
            buffer.append((block.text, block.page))
            size = sum(len(piece) for piece, _ in buffer)
            if size >= MAX_CHARS:
                flush()
            continue

        if block.kind == "steps":
            flush()
            emit(block.text, "procedure", block.page, block.page)
            continue

        if block.kind == "warning":
            warnings_active.append(block.text)
            # Предупреждение, стоящее сразу после таблицы или процедуры,
            # относится к ней, а не к следующему фрагменту текста.
            if chunks and not buffer and chunks[-1].section_no == section_no:
                if block.text not in chunks[-1].warnings:
                    chunks[-1].warnings.append(block.text)
            buffer.append((block.text, block.page))
            continue

        if block.kind == "table":
            flush()
            caption = block.caption
            groups = _split_table(block.rows)
            for position, group in enumerate(groups, start=1):
                table_markdown = _rows_to_markdown(group)
                suffix = f" (часть {position})" if len(groups) > 1 else ""
                body = f"{caption}{suffix}\n\n{table_markdown}" if caption else table_markdown
                emit(
                    body,
                    "table",
                    block.page,
                    block.page,
                    table_id=block.table_id,
                    models=detect_models(body),
                )
            continue

        if block.kind == "figure":
            flush()
            emit(block.text, "figure", block.page, block.page)
            continue

    flush()
    return chunks


def summarize(chunks: list[Chunk]) -> dict:
    kinds: dict[str, int] = {}
    for chunk in chunks:
        kinds[chunk.kind] = kinds.get(chunk.kind, 0) + 1
    return {
        "chunks": len(chunks),
        "tables": kinds.get("table", 0),
        "procedures": kinds.get("procedure", 0),
        "warnings": kinds.get("warning", 0),
        "sections": len({chunk.section_no for chunk in chunks if chunk.section_no}),
    }
