"""Извлечение текста из руководств: PDF (pdftotext / pypdf) и plain text.

Требование воспроизводимости: сервис не должен зависеть от одного
инструмента. Порядок попыток для PDF:
1. `pdftotext -layout` (poppler) — сохраняет колонки таблиц, критично для
   контрольных листов ТО и таблиц заправочных объёмов;
2. `pdftotext` без -layout — если первый режим завершается ошибкой на файле;
3. `pypdf` — если poppler не установлен.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from dataclasses import dataclass


class ExtractionError(RuntimeError):
    """Документ не удалось превратить в текст."""


@dataclass
class Extracted:
    pages: list[str]
    method: str


def extract(path: str | pathlib.Path) -> Extracted:
    file_path = pathlib.Path(path)
    if not file_path.exists():
        raise ExtractionError(f"Файл не найден: {file_path}")
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(file_path)
    if suffix in {".txt", ".md"}:
        return _extract_plain(file_path)
    raise ExtractionError(
        f"Формат {suffix or '<без расширения>'} не поддерживается. "
        "Поддерживаются .pdf, .txt, .md."
    )


def _extract_pdf(path: pathlib.Path) -> Extracted:
    errors: list[str] = []
    exe = shutil.which("pdftotext")
    if exe:
        modes = {
            "pdftotext -layout": [exe, "-layout", "-enc", "UTF-8"],
            "pdftotext": [exe, "-enc", "UTF-8"],
        }
        for name, command in modes.items():
            try:
                proc = subprocess.run(
                    [*command, str(path), "-"],
                    capture_output=True,
                    timeout=300,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"{name}: {exc}")
                continue
            if proc.returncode != 0:
                errors.append(f"{name}: код {proc.returncode}, {proc.stderr[:200]!r}")
                continue
            text = proc.stdout.decode("utf-8", "replace")
            pages = text.split("\f")
            if pages and not pages[-1].strip():
                pages.pop()
            if any(page.strip() for page in pages):
                return Extracted(pages=pages, method=name)
            errors.append(f"{name}: пустой текстовый слой")

    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        raise ExtractionError(
            "Не удалось извлечь текст PDF: pdftotext недоступен или не справился, "
            "а модуль pypdf не установлен.\n"
            "Варианты: установить poppler-utils (даёт pdftotext -layout, лучший "
            "результат для таблиц) либо выполнить `pip install pypdf`.\n"
            f"Детали: {'; '.join(errors) if errors else 'нет'}"
        ) from None

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    if not any(page.strip() for page in pages):
        raise ExtractionError(
            "В PDF нет текстового слоя — документ, вероятно, отсканирован. "
            "Нужен OCR (например, `ocrmypdf --skip-text file.pdf out.pdf`), "
            "иначе таблицы и регламенты будут недоступны."
        )
    return Extracted(pages=pages, method="pypdf")


def _extract_plain(path: pathlib.Path) -> Extracted:
    text = path.read_text(encoding="utf-8", errors="replace")
    if "\f" in text:
        pages = [page for page in text.split("\f")]
    else:
        # Демо-корпус и выгрузки из текстовых редакторов: страницы помечаются
        # строкой вида "--- страница 3 ---".
        pages = []
        current: list[str] = []
        for line in text.splitlines():
            stripped = line.strip().lower()
            if stripped.startswith("---") and "страниц" in stripped:
                if current:
                    pages.append("\n".join(current))
                    current = []
                continue
            current.append(line)
        if current:
            pages.append("\n".join(current))
    if pages and not pages[-1].strip():
        pages.pop()
    return Extracted(pages=pages or [""], method="plain text")
