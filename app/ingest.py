"""CLI загрузки и обновления базы документов.

Примеры:
    python3 -m app.ingest                          # всё из data/documents → data/index.sqlite3
    python3 -m app.ingest --replace                # заменить изменённые документы
    python3 -m app.ingest --list                   # что уже в базе
    python3 -m app.ingest --drop pd-pg-ru          # удалить документ из базы
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

from .chunker import build_chunks, summarize
from .config import ROOT
from .extract import ExtractionError, extract
from .index import Store
from .structure import document_sections, parse_document

SUPPORTED = {".pdf", ".txt", ".md"}
DEFAULT_DB = ROOT / "data" / "index.sqlite3"


def slugify(value: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Zа-яА-Я_-]+", "-", value.strip().lower())
    return re.sub(r"-{2,}", "-", slug).strip("-") or "document"


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def guess_title(pages: list[str]) -> str:
    for page in pages[:3]:
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        for index, line in enumerate(lines[:12]):
            if len(line) < 8:
                continue
            is_title = bool(re.search(r"руководств|эксплуатац|паспорт", line, re.IGNORECASE)) or (
                line.isupper() and len(line) > 12
            )
            if not is_title:
                continue
            combined = line
            if index + 1 < len(lines):
                following = lines[index + 1]
                if 8 < len(following) < 120 and not re.fullmatch(r"[\d\s.,/-]+", following):
                    combined = f"{line} {following}"
            return re.sub(r"\s+", " ", combined)[:140]
    for page in pages[:2]:
        for line in page.splitlines():
            if len(line.strip()) > 10:
                return re.sub(r"\s+", " ", line.strip())[:140]
    return ""


def ingest_file(
    store: Store,
    path: pathlib.Path,
    *,
    version: str = "",
    title: str = "",
    replace: bool = False,
    force: bool = False,
    notes: str = "",
) -> dict:
    doc_id = slugify(path.stem)
    digest = file_sha256(path)
    existing = store.document(doc_id)

    # Совпавший sha256 означает «файл тот же», но не «разбор тот же»:
    # после правок парсера индекс нужно пересобрать даже без нового PDF,
    # поэтому --replace и --force отменяют пропуск.
    if existing and existing["sha256"] == digest and not force and not replace:
        return {"doc_id": doc_id, "status": "unchanged", "title": existing["title"]}
    if existing and not replace:
        return {
            "doc_id": doc_id,
            "status": "exists",
            "title": existing["title"],
            "hint": "документ уже в базе; для замены выполните ingest --replace",
        }

    extracted = extract(path)
    blocks = parse_document(extracted.pages)
    chunks = build_chunks(blocks)
    if not chunks:
        return {"doc_id": doc_id, "status": "empty", "title": path.name}

    payload = [chunk.as_dict() for chunk in chunks]
    meta = {
        "title": title or guess_title(extracted.pages) or path.stem,
        "filename": path.name,
        "version": version or (existing["version"] if existing else ""),
        "sha256": digest,
        "pages": len(extracted.pages),
        "sections": document_sections(blocks),
        "size_bytes": path.stat().st_size,
        "notes": notes,
    }
    store.replace_document(doc_id, meta, payload)
    summary = summarize(chunks)
    return {
        "doc_id": doc_id,
        "status": "updated" if existing else "added",
        "title": meta["title"],
        "pages": meta["pages"],
        "sections": meta["sections"],
        "method": extracted.method,
        **summary,
    }


def collect_files(paths: list[str], directory: pathlib.Path) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for raw in paths:
        candidate = pathlib.Path(raw)
        if candidate.is_dir():
            files.extend(sorted(candidate.iterdir()))
        elif candidate.exists():
            files.append(candidate)
        else:
            raise SystemExit(f"Путь не найден: {raw}")
    if not paths:
        if not directory.exists():
            return []
        files.extend(sorted(directory.iterdir()))
    # README.md в каталоге документов — служебная инструкция, а не руководство.
    service_names = {"readme.md", "readme.txt"}
    return [
        item
        for item in files
        if item.suffix.lower() in SUPPORTED
        and not item.name.startswith(".")
        and item.name.lower() not in service_names
    ]


def print_documents(store: Store) -> None:
    documents = store.documents()
    if not documents:
        print("База пуста.")
        return
    print(f"{'doc_id':28} {'версия':8} {'стр':>4} {'чанков':>7}  заголовок")
    for doc in documents:
        print(
            f"{doc['doc_id'][:28]:28} {(doc['version'] or '—')[:8]:8} "
            f"{doc['pages']:>4} {doc['chunk_count']:>7}  {doc['title'][:70]}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Загрузка руководств СИЛАНТ в поисковый индекс"
    )
    parser.add_argument("paths", nargs="*", help="файлы или каталоги (по умолчанию data/documents)")
    parser.add_argument("--dir", default=None, help="каталог с документами")
    parser.add_argument("--db", default=None, help="путь к файлу индекса")
    parser.add_argument("--replace", action="store_true", help="заменять уже загруженные документы")
    parser.add_argument("--force", action="store_true", help="переиндексировать даже при совпадении хеша")
    parser.add_argument("--version", default="", help="версия документа (например, ред. 2026-01)")
    parser.add_argument("--title", default="", help="заголовок документа (для одного файла)")
    parser.add_argument("--notes", default="", help="произвольная заметка о документе")
    parser.add_argument("--list", action="store_true", help="показать документы в базе")
    parser.add_argument("--drop", action="append", default=[], metavar="DOC_ID", help="удалить документ")
    args = parser.parse_args(argv)

    db_path = pathlib.Path(args.db) if args.db else DEFAULT_DB
    directory = pathlib.Path(args.dir) if args.dir else ROOT / "data" / "documents"
    store = Store(db_path)

    if args.drop:
        for doc_id in args.drop:
            store.delete_document(doc_id)
            print(f"удалён: {doc_id}")
        store.rebuild_stats()
        print_documents(store)
        return 0

    if args.list:
        print(f"База: {db_path}")
        print_documents(store)
        return 0

    files = collect_files(args.paths, directory)
    if not files:
        print(f"Не найдено документов в {directory}. Поддерживаются: {', '.join(sorted(SUPPORTED))}.")
        print("Положите PDF руководств в data/documents и повторите запуск.")
        print_documents(store)
        return 0

    if args.title and len(files) > 1:
        print("--title применим только к одному файлу; для остальных заголовок будет определён автоматически.")

    print(f"База: {db_path}")
    failures = 0
    for path in files:
        title = args.title if (args.title and len(files) == 1) else ""
        try:
            result = ingest_file(
                store,
                path,
                version=args.version,
                title=title,
                replace=args.replace,
                force=args.force,
                notes=args.notes,
            )
        except ExtractionError as exc:
            failures += 1
            print(f"[ERROR] {path.name}: {exc}")
            continue
        status = result["status"]
        if status in {"added", "updated"}:
            print(
                f"[{status.upper():7}] {path.name}: стр. {result['pages']}, "
                f"разделов {result['sections']}, чанков {result['chunks']} "
                f"(таблиц {result['tables']}, процедур {result['procedures']}), "
                f"метод: {result['method']}"
            )
        elif status == "unchanged":
            print(f"[SKIP    ] {path.name}: не изменился (совпадает sha256)")
        elif status == "exists":
            print(f"[EXISTS  ] {path.name}: {result['hint']}")
        else:
            print(f"[EMPTY   ] {path.name}: не удалось выделить текст и структуру")

    stats = store.stats()
    print(
        f"\nИтого: документов {stats['documents']}, чанков {stats['chunks']}, "
        f"таблиц {stats['tables']}, процедур {stats['procedures']}, терминов {stats['terms']}"
    )
    if failures:
        print(f"Ошибок: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
