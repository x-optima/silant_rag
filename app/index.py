"""Хранилище: SQLite + FTS5, без внешних сервисов и бинарных зависимостей.

SQLite FTS5 выбран по трём причинам:
* входит в стандартную поставку Python, решение воспроизводится одной командой
  без установки векторной базы;
* BM25 считается ядром, а не самописным кодом;
* индекс — один файл, его просто приложить к сдаче и пересобрать.

Векторный поиск подключается опционально (см. app/llm.py). Для корпуса из двух
руководств лексический поиск с нормализацией моделей и русской морфологией даёт
предсказуемые ссылки на источник и позволяет показать точную цитату.
"""

from __future__ import annotations

import json
import math
import pathlib
import sqlite3
import threading
from datetime import datetime, timezone

from .textnorm import normalize_text, tokenize

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
    doc_id       TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    filename     TEXT NOT NULL,
    version      TEXT NOT NULL DEFAULT '',
    sha256       TEXT NOT NULL DEFAULT '',
    pages        INTEGER NOT NULL DEFAULT 0,
    sections     INTEGER NOT NULL DEFAULT 0,
    chunk_count  INTEGER NOT NULL DEFAULT 0,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    added_at     TEXT NOT NULL,
    notes        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS chunks(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id        TEXT NOT NULL,
    ord           INTEGER NOT NULL,
    page_from     INTEGER NOT NULL,
    page_to       INTEGER NOT NULL,
    section_no    TEXT NOT NULL DEFAULT '',
    section_title TEXT NOT NULL DEFAULT '',
    section_path  TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'text',
    table_id      TEXT NOT NULL DEFAULT '',
    models        TEXT NOT NULL DEFAULT '[]',
    warnings      TEXT NOT NULL DEFAULT '[]',
    body          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    body, models, section, tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS terms(
    term TEXT PRIMARY KEY,
    df   INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._df: dict[str, int] = {}
        self._total_chunks = 0
        self._load_terms()

    # ---------------------------------------------------------------- служебное
    def _load_terms(self) -> None:
        with self._lock:
            self._total_chunks = self.conn.execute(
                "SELECT COUNT(*) AS n FROM chunks"
            ).fetchone()["n"]
            self._df = {
                row["term"]: row["df"]
                for row in self.conn.execute("SELECT term, df FROM terms")
            }

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @property
    def total_chunks(self) -> int:
        return self._total_chunks

    def idf(self, term: str) -> float:
        """BM25-подобный IDF. Для незнакомого термина — максимальный вес."""
        total = self._total_chunks or 1
        df = self._df.get(term, 0)
        return math.log(1.0 + (total - df + 0.5) / (df + 0.5))

    def df(self, term: str) -> int:
        """Сколько фрагментов содержат термин (0 — слова нет в корпусе)."""
        return self._df.get(term, 0)

    # ---------------------------------------------------------------- документы
    def documents(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM documents ORDER BY title"
            ).fetchall()
        return [dict(row) for row in rows]

    def document(self, doc_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict:
        docs = self.documents()
        with self._lock:
            kinds = {
                row["kind"]: row["n"]
                for row in self.conn.execute(
                    "SELECT kind, COUNT(*) AS n FROM chunks GROUP BY kind"
                )
            }
        return {
            "documents": len(docs),
            # Считаем актуальным запросом: документ могли удалить другим
            # процессом (python3 -m app.ingest --drop), и значение в памяти
            # окажется устаревшим.
            "chunks": sum(kinds.values()),
            "tables": kinds.get("table", 0),
            "procedures": kinds.get("procedure", 0),
            "terms": len(self._df),
        }

    def doc_models(self) -> dict[str, set[str]]:
        """Какие модели техники покрывает каждый документ.

        Нужно, чтобы не выдать регламент по электропогрузчику на вопрос
        о дизельной машине: документ, где модель не упоминается вовсе,
        не может быть источником ответа по ней.
        """
        result: dict[str, set[str]] = {}
        with self._lock:
            rows = self.conn.execute("SELECT doc_id, models FROM chunks").fetchall()
        for row in rows:
            models = json.loads(row["models"] or "[]")
            if models:
                result.setdefault(row["doc_id"], set()).update(models)
        return result

    # ---------------------------------------------------------------- запись
    def replace_document(
        self,
        doc_id: str,
        meta: dict,
        chunks: list[dict],
    ) -> int:
        """Полная замена документа: старые чанки и FTS-записи удаляются.

        Вызывается и при первом добавлении, и при выходе новой версии РЭ —
        процедура обновления базы сводится к «положить PDF и выполнить
        ingest --replace».
        """
        with self._lock:
            self.delete_document(doc_id, commit=False)
            self.conn.execute(
                """INSERT INTO documents(
                       doc_id, title, filename, version, sha256, pages, sections,
                       chunk_count, size_bytes, added_at, notes
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    doc_id,
                    meta.get("title", doc_id),
                    meta.get("filename", ""),
                    meta.get("version", ""),
                    meta.get("sha256", ""),
                    int(meta.get("pages", 0)),
                    int(meta.get("sections", 0)),
                    len(chunks),
                    int(meta.get("size_bytes", 0)),
                    meta.get("added_at")
                    or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    meta.get("notes", ""),
                ),
            )
            for order, chunk in enumerate(chunks):
                cursor = self.conn.execute(
                    """INSERT INTO chunks(
                           doc_id, ord, page_from, page_to, section_no,
                           section_title, section_path, kind, table_id,
                           models, warnings, body
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        doc_id,
                        order,
                        int(chunk.get("page_from", 0)),
                        int(chunk.get("page_to", 0)),
                        chunk.get("section_no", ""),
                        chunk.get("section_title", ""),
                        chunk.get("section_path", ""),
                        chunk.get("kind", "text"),
                        chunk.get("table_id", ""),
                        json.dumps(chunk.get("models", []), ensure_ascii=False),
                        json.dumps(chunk.get("warnings", []), ensure_ascii=False),
                        chunk.get("body", ""),
                    ),
                )
                self.conn.execute(
                    "INSERT INTO chunks_fts(rowid, body, models, section) VALUES (?,?,?,?)",
                    (
                        cursor.lastrowid,
                        normalize_text(chunk.get("body", "")),
                        " ".join(chunk.get("models", [])),
                        normalize_text(
                            f"{chunk.get('section_no', '')} {chunk.get('section_title', '')}"
                        ),
                    ),
                )
            self.conn.commit()
        self.rebuild_stats()
        return len(chunks)

    def delete_document(self, doc_id: str, commit: bool = True) -> None:
        with self._lock:
            ids = [
                row["id"]
                for row in self.conn.execute(
                    "SELECT id FROM chunks WHERE doc_id = ?", (doc_id,)
                )
            ]
            for chunk_id in ids:
                self.conn.execute(
                    "DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,)
                )
            self.conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            self.conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            if commit:
                self.conn.commit()

    def rebuild_stats(self) -> None:
        with self._lock:
            df: dict[str, int] = {}
            total = 0
            for row in self.conn.execute(
                "SELECT body, section_title FROM chunks"
            ):
                total += 1
                terms = set(tokenize(row["body"])) | set(tokenize(row["section_title"]))
                for term in terms:
                    df[term] = df.get(term, 0) + 1
            self.conn.execute("DELETE FROM terms")
            self.conn.executemany(
                "INSERT INTO terms(term, df) VALUES (?, ?)",
                list(df.items()),
            )
            self.conn.commit()
            self._df = df
            self._total_chunks = total

    # ---------------------------------------------------------------- поиск
    def search(self, fts_query: str, limit: int = 60) -> list[sqlite3.Row]:
        if not fts_query.strip():
            return []
        sql = (
            "SELECT c.*, bm25(chunks_fts, 1.0, 3.0, 2.0) AS bm "
            "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ? ORDER BY bm LIMIT ?"
        )
        try:
            with self._lock:
                rows = self.conn.execute(sql, (fts_query, limit)).fetchall()
        except sqlite3.OperationalError:
            # Некорректный пользовательский запрос не должен приводить
            # к отказу сервиса.
            return []
        return list(rows)

    def chunk(self, chunk_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
        return dict(row) if row else None
