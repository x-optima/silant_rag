"""Поиск: FTS5 отбирает кандидатов, Python-реранкер расставляет их по смыслу.

Зачем два этапа:
* FTS5 BM25 хорошо находит лексические совпадения, но не знает про модель
  техники и про то, что таблица важнее абзаца;
* реранкер добавляет покрытие запроса по IDF, совпадение модели, приоритет
  таблиц и процедур — именно это отделяет «нашли нужную строку регламента»
  от «нашли раздел, где слово встречается».
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import MODEL_KEYS, Config
from .index import Store
from .textnorm import (
    SYNONYMS,
    build_fts_query,
    content_tokens,
    detect_family,
    expand_tokens,
    token_matches,
    tokenize,
)

KIND_PRIOR = {
    "table": 0.85,
    "procedure": 0.75,
    "warning": 0.5,
    "text": 0.35,
    "figure": 0.1,
}


@dataclass
class Hit:
    chunk_id: int
    doc_id: str
    doc_title: str
    body: str
    kind: str
    page_from: int
    page_to: int
    section_no: str
    section_title: str
    section_path: str
    table_id: str
    models: list[str]
    warnings: list[str]
    coverage: float
    matched: list[str]
    bm25: float
    score: float


@dataclass
class RetrievalResult:
    question: str
    tokens: list[str]
    topic_tokens: list[str]
    model_tokens: list[str]
    idf: dict[str, float]
    hits: list[Hit] = field(default_factory=list)
    doc_models: dict[str, set[str]] = field(default_factory=dict)

    @property
    def best(self) -> Hit | None:
        return self.hits[0] if self.hits else None


def _matched_tokens(query_tokens: list[str], text_tokens: set[str]) -> list[str]:
    matched = []
    for token in query_tokens:
        candidates = [token, *SYNONYMS.get(token, [])]
        if any(
            token_matches(candidate, text_token)
            for candidate in candidates
            for text_token in text_tokens
        ):
            matched.append(token)
    return matched


class Retriever:
    def __init__(self, store: Store, config: Config):
        self.store = store
        self.config = config
        self._titles: dict[str, str] = {}

    def _doc_title(self, doc_id: str) -> str:
        if not self._titles:
            self._titles = {doc["doc_id"]: doc["title"] for doc in self.store.documents()}
        return self._titles.get(doc_id, doc_id)

    def retrieve(
        self, question: str, model: str | None = None, top_k: int | None = None
    ) -> RetrievalResult:
        tokens = content_tokens(question)
        model_tokens = [token for token in tokens if token in MODEL_KEYS]
        # Слова, отсутствующие в корпусе («проводится», «максимальная»), не
        # могут быть найдены, поэтому не должны учитываться при расчёте
        # покрытия запроса.
        topic = [token for token in tokens if token not in model_tokens and self.store.df(token) > 0]
        if len(topic) < 2:
            topic = [token for token in tokens if token not in model_tokens] or tokens
        thresholds = self.config.thresholds
        idf_map = {token: self.store.idf(token) for token in tokens}
        result = RetrievalResult(
            question=question,
            tokens=tokens,
            topic_tokens=topic,
            model_tokens=model_tokens,
            idf=idf_map,
        )
        if not tokens or self.store.total_chunks == 0:
            return result

        query = build_fts_query(expand_tokens(tokens))
        rows = self.store.search(query, limit=int(thresholds.get("candidate_limit", 60)))
        if not rows:
            return result

        total_weight = sum(idf_map[token] for token in topic) or 1.0
        question_family = detect_family(question) if not model else set()
        doc_models = self.store.doc_models()
        result.doc_models = doc_models
        hits: list[Hit] = []
        row_count = max(1, len(rows) - 1)

        for position, row in enumerate(rows):
            body = row["body"]
            text_tokens = set(tokenize(body)) | set(tokenize(row["section_title"]))
            matched = _matched_tokens(tokens, text_tokens)
            if not matched:
                continue
            matched_topic = [token for token in matched if token in topic]
            coverage = sum(idf_map[token] for token in matched_topic) / total_weight

            row_models = json.loads(row["models"] or "[]")
            section_title = row["section_title"] or ""
            section_tokens = set(tokenize(section_title))

            model_score = 0.0
            if model:
                if model in row_models or model in text_tokens:
                    model_score = 1.0
                elif row_models:
                    model_score = -1.0

            section_bonus = 1.0 if _matched_tokens(tokens, section_tokens) else 0.0
            rank_score = 1.0 - position / row_count
            kind_prior = KIND_PRIOR.get(row["kind"], 0.3)
            family_penalty = (
                0.05
                if question_family == set() and detect_family(section_title)
                else 0.0
            )
            doc_penalty = 0.0
            if model:
                known_models = doc_models.get(row["doc_id"])
                if known_models and model not in known_models:
                    doc_penalty = 0.15

            score = (
                0.55 * coverage
                + 0.22 * rank_score
                + 0.13 * max(model_score, 0.0)
                + 0.06 * section_bonus
                + 0.04 * kind_prior
                - 0.12 * max(-model_score, 0.0)
                - family_penalty
                - doc_penalty
            )

            hits.append(
                Hit(
                    chunk_id=row["id"],
                    doc_id=row["doc_id"],
                    doc_title=self._doc_title(row["doc_id"]),
                    body=body,
                    kind=row["kind"],
                    page_from=row["page_from"],
                    page_to=row["page_to"],
                    section_no=row["section_no"],
                    section_title=section_title,
                    section_path=row["section_path"] or "",
                    table_id=row["table_id"] or "",
                    models=row_models,
                    warnings=json.loads(row["warnings"] or "[]"),
                    coverage=coverage,
                    matched=matched,
                    bm25=-float(row["bm"]),
                    score=score,
                )
            )

        hits.sort(key=lambda hit: hit.score, reverse=True)
        limit = int(top_k) if top_k else int(thresholds.get("top_k", 8))
        result.hits = hits[:limit]
        return result

    def invalidate(self) -> None:
        self._titles = {}
