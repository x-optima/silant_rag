"""Формирование ответа: цитата, ссылка на раздел, модель, предупреждения, отказ.

По умолчанию сервис работает экстрактивно: найденный текст приводится дословно
со ссылкой на раздел и страницу вместо свободного пересказа. Пересказ без
генеративной модели создаёт риск исказить регламент, а искажённый регламент
для механика опаснее отсутствия ответа. Поэтому ответ состоит из цитаты,
атрибуции и предупреждений.
"""

from __future__ import annotations

import re

from .config import MODELS, Config, model_key, model_label
from .index import Store
from .llm import LlmClient
from .retriever import Hit, RetrievalResult, Retriever, _matched_tokens
from .textnorm import (
    detect_models,
    extract_numbers,
    normalize_text,
    split_sentences,
    stem_token,
    token_matches,
    tokenize,
)

PROCEDURE_INTENT = re.compile(
    r"(порядок|последовательност|в каком порядке|как\s+(выполняется|проводится|"
    r"заменить|отрегулировать|проверить|снять|установить|подтянуть)|шаги|инструкц)",
    re.IGNORECASE,
)
NUMERIC_INTENT = re.compile(
    r"(через сколько|сколько|периодичност|интервал|момент|объ[её]м|норм[аы]|"
    r"давлен|температур|напряжен|заправочн|наработк)",
    re.IGNORECASE,
)
WARNING_IN_TEXT = re.compile(r"(ВНИМАНИЕ|ВАЖНО|ЗАПРЕЩАЕТСЯ|НЕ ДОПУСКАЕТСЯ|ОПАСНО)", re.IGNORECASE)

# Вопрос о периодичности: «через сколько», «как часто», «интервал».
INTERVAL_INTENT = re.compile(
    r"(через\s+сколько|периодичн|интервал|как\s+часто|через\s+какие)", re.IGNORECASE
)
# Готовая формулировка регламента: «через каждые 500 часов работы».
INTERVAL_VALUE = re.compile(
    r"(через\s+каждые\s+\d+|каждые\s+\d+\s*(?:мот\w*)?\s*(?:часов|моточасов))",
    re.IGNORECASE,
)
# Область действия строки: «Для погрузчиков ПД 5,0».
MODEL_SCOPE = re.compile(r"(для\s+погрузчиков|для\s+модел|для\s+П[ДГЭ])", re.IGNORECASE)
STRICT_LIMIT_TERMS = re.compile(
    r"(максимальн|минимальн|предельн|номинальн|наибольш|наименьш)", re.IGNORECASE
)


def _parse_markdown_table(body: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        core = stripped.strip("|")
        if not core.strip():
            continue
        if set(core.replace("|", "").strip()) <= set("-: "):
            continue
        rows.append([cell.strip() for cell in core.split("|")])
    return rows


def _plain(text: str) -> str:
    """Текст без markdown-разметки — для карточки источника."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            lines.append(stripped.lstrip("> "))
            continue
        core = stripped.strip("|")
        if set(core.replace("|", "").strip()) <= set("-: "):
            continue
        lines.append(" · ".join(cell.strip() for cell in core.split("|") if cell.strip()))
    return " ".join(" ".join(lines).split())


class Answerer:
    def __init__(self, store: Store, config: Config):
        self.store = store
        self.config = config
        self.retriever = Retriever(store, config)
        self.llm = LlmClient(config)

    # ------------------------------------------------------------------ модели
    def known_model_keys(self) -> list[str]:
        """Справочник моделей плюс модели, найденные в загруженных руководствах.

        Завод вправе добавить документ по модели, которой не было в исходном
        перечне. Такая модель попадает в индекс вместе с документом, поэтому
        список пополняется из данных, а не только из конфигурации.
        """
        keys = [model_key(label) for label in MODELS]
        for models in self.store.doc_models().values():
            for key in sorted(models):
                if key not in keys:
                    keys.append(key)
        return keys

    def model_options(self) -> list[str]:
        return [model_label(key) for key in self.known_model_keys()]

    # ------------------------------------------------------------------ публичное
    def answer(self, question: str, model_hint: str | None = None) -> dict:
        question = re.sub(r"\s+", " ", question or "").strip()
        if len(question) < 3:
            return self._payload(
                "clarify_question",
                "Сформулируйте вопрос по эксплуатации, техническому обслуживанию "
                "или диагностике техники СИЛАНТ.",
            )

        scope = self._out_of_scope(question)
        if scope:
            return self._payload("out_of_scope", scope["message"], reason=scope.get("reason"))

        if self.store.total_chunks == 0:
            return self._payload(
                "empty_base",
                "База документов пуста: руководства по эксплуатации ещё не загружены. "
                "Положите PDF в каталог `data/documents` и выполните `./deploy.sh --reindex` — "
                "сервис разберёт документы сам.",
            )

        requested, clarification = self._resolve_model(question, model_hint)
        if clarification:
            return self._payload(
                "clarify_model",
                clarification,
                model_options=self.model_options(),
                models_found=detect_models(question),
            )

        if requested and not self.store.search(requested, limit=1):
            label = model_label(requested)
            return self._payload(
                "no_source",
                f"В базе нет руководства, в котором упоминается модель {label}. "
                "Ответить по документации завода невозможно: нужный документ не загружен.",
                model=label,
                contacts=self._contacts(),
            )

        # Для вопроса о периодичности нужен более широкий список кандидатов:
        # регламент лежит в разделе, который по BM25 может оказаться ниже.
        top_k = 25 if INTERVAL_INTENT.search(question) else None
        result = self.retriever.retrieve(question, model=requested, top_k=top_k)
        if not result.hits:
            return self._refuse(question, requested, result, reason="no_match")

        selected, status = self._select_evidence(result, requested, question)
        if selected is None:
            return self._refuse(question, requested, result, reason="weak_evidence")

        hit, evidence = selected
        doc_models = result.doc_models.get(hit.doc_id) or set()
        if status == "answered" and requested and doc_models and requested not in doc_models:
            # Руководство не покрывает запрошенную модель: найденная
            # цитата относится к другим машинам, переносить её нельзя.
            status = "model_mismatch"
            if not evidence.get("models"):
                evidence["models"] = sorted(doc_models)[:4]
        if status == "answered" and self._unconfirmed_limit(question, evidence):
            return self._refuse(question, requested, result, reason="unconfirmed_limit")

        supporting = (
            self._supporting_evidence(result, hit, evidence, requested, question)
            if status == "answered"
            else []
        )
        answer_text = self._render(status, hit, evidence, requested, supporting)

        warnings = self._warnings(hit, evidence)
        for extra_hit, extra_evidence in supporting:
            for warning in self._warnings(extra_hit, extra_evidence):
                if warning not in warnings:
                    warnings.append(warning)
        citations = [self._citation(hit, evidence)] + [
            self._citation(extra_hit, extra_evidence) for extra_hit, extra_evidence in supporting
        ]
        related = [self._related(item) for item in result.hits if item.chunk_id != hit.chunk_id][:3]

        confidence = (
            "high" if hit.coverage >= 0.68 else "medium" if hit.coverage >= 0.52 else "low"
        )
        mode = "extractive"

        if status == "answered" and self.llm.enabled:
            context = "\n\n".join(
                f"[{item.doc_title}, раздел {item.section_no or '—'}, стр. {item.page_from}]\n{item.body}"
                for item in result.hits[:4]
            )
            generated = self.llm.generate(question, [context])
            if generated and self._numbers_are_supported(generated, context):
                answer_text = generated
                mode = "llm"

        return self._payload(
            status,
            answer_text,
            model=model_label(requested) if requested else None,
            citations=citations,
            warnings=warnings,
            related=related,
            confidence=confidence if status == "answered" else "low",
            mode=mode,
            debug={
                "query_tokens": result.tokens,
                "coverage": round(hit.coverage, 3),
                "score": round(hit.score, 3),
                "chunk_id": hit.chunk_id,
                "doc_id": hit.doc_id,
                "kind": hit.kind,
                "matches": hit.matched,
            },
        )

    # ------------------------------------------------------------------ разбор вопроса
    def _resolve_model(
        self, question: str, model_hint: str | None
    ) -> tuple[str | None, str | None]:
        hint = model_key(model_hint) if model_hint else None
        if hint not in self.known_model_keys():
            hint = None
        found = detect_models(question)

        if hint and found and hint not in found and len(found) > 0:
            labels = ", ".join(model_label(item) for item in found)
            return None, (
                f"В вопросе указаны разные модели ({labels}). "
                f"В интерфейсе выбрана {model_label(hint)}. Уточните, для какой модели нужен ответ: "
                "регламенты и параметры у моделей различаются."
            )

        if hint:
            return hint, None

        if len(found) == 1:
            return found[0], None

        if len(found) > 1:
            labels = ", ".join(model_label(item) for item in found)
            return None, (
                f"В вопросе упомянуто несколько моделей ({labels}). "
                "Уточните одну модель: параметры и регламенты нельзя смешивать."
            )

        if self._needs_model(question):
            return None, (
                "Уточните модель техники — от неё зависят регламенты, интервалы "
                "обслуживания и заправочные объёмы. Например: ПД1,5; ПД5,0; ПГ2,0; ПЭ2,5."
            )
        return None, None

    def _needs_model(self, question: str) -> bool:
        text = normalize_text(question)
        return any(pattern.search(text) for pattern in self.config.model_required_patterns())

    def _out_of_scope(self, question: str) -> dict | None:
        low = question.lower().replace("ё", "е")
        for group in self.config.out_of_scope_groups():
            primary = [word for word in group.get("any_of", []) if word in low]
            if not primary:
                continue
            secondary = group.get("also_of") or []
            if secondary and not any(word in low for word in secondary):
                continue
            return group
        return None

    def _unconfirmed_limit(self, question: str, evidence: dict) -> bool:
        """Предельные значения нельзя вывести из смежного фрагмента.

        Если механик спрашивает «максимальная скорость», а в найденном
        фрагменте слова «максимальная» нет, речь о другом ограничении
        (например, о скорости буксировки). Такой ответ опаснее отказа.
        """
        if not STRICT_LIMIT_TERMS.search(question):
            return False
        return not STRICT_LIMIT_TERMS.search(evidence.get("quote", ""))

    # ------------------------------------------------------------------ доказательства
    def _has_evidence(self, result: RetrievalResult, hit: Hit) -> bool:
        thresholds = self.config.thresholds
        matched_topic = [token for token in hit.matched if token in result.topic_tokens]
        required = min(int(thresholds.get("min_matched_terms", 2)), len(result.topic_tokens))
        if len(matched_topic) < required:
            return False
        if hit.coverage < float(thresholds.get("min_coverage", 0.42)):
            return False
        if hit.score < float(thresholds.get("min_score", 0.30)):
            return False
        # Самый редкий термин вопроса (наиболее специфичный) обязан найтись:
        # иначе вопрос «какое напряжение батареи» будет сопоставлен с любым
        # фрагментом, где батарея лишь упомянута. Слова-контекст («погрузчик»,
        # «техника») из проверки исключены: они присутствуют в любом разделе.
        generic = {
            "погрузчик", "автопогрузчик", "электропогрузчик", "машин",
            "техник", "силант", "оборудован",
        }
        specific = [
            token for token in result.topic_tokens if stem_token(token) not in generic
        ] or result.topic_tokens
        rare = sorted(specific, key=lambda token: result.idf.get(token, 0.0), reverse=True)[:1]
        if rare and not any(token in hit.matched for token in rare):
            return False
        return True

    def _select_evidence(
        self, result: RetrievalResult, requested: str | None, question: str
    ) -> tuple[tuple[Hit, dict] | None, str]:
        mismatch: tuple[Hit, dict] | None = None

        # Вопрос о периодичности ТО. В руководстве на каждый вид обслуживания
        # отведён отдельный раздел, и его заголовок содержит и номер ТО, и
        # интервал: «Техническое обслуживание № 2 (ТО-2) ( через каждые 400
        # часов работы)». Такой раздел точнее фрагмента, где слова просто
        # упомянуты, поэтому он проверяется первым.
        if INTERVAL_INTENT.search(question):
            number = re.search(r"ТО\s*[-–]?\s*([123])", question, re.IGNORECASE)
            if number:
                pattern = re.compile(
                    rf"(?:ТО\s*[-–]?\s*{number.group(1)}\b|№\s*{number.group(1)}\b)",
                    re.IGNORECASE,
                )
                for hit in result.hits[:25]:
                    title = hit.section_title or ""
                    if not pattern.search(title):
                        continue
                    evidence = self._text_evidence(hit, result, question)
                    if not evidence or not INTERVAL_VALUE.search(evidence["quote"]):
                        continue
                    if requested and not detect_models(evidence["quote"]):
                        continue
                    return (hit, evidence), "answered"

        candidates = result.hits[:5]
        if PROCEDURE_INTENT.search(question):
            candidates = [hit for hit in candidates if hit.kind == "procedure"] + [
                hit for hit in candidates if hit.kind != "procedure"
            ]
        else:
            candidates = [hit for hit in candidates if hit.kind != "procedure"] + [
                hit for hit in candidates if hit.kind == "procedure"
            ]
        for hit in candidates:
            if not self._has_evidence(result, hit):
                continue
            evidence = self._evidence(hit, result, requested, question)
            if not evidence:
                continue
            models = evidence.get("models") or []
            if requested and evidence["kind"] == "table" and models and requested not in models:
                if mismatch is None:
                    mismatch = (hit, evidence)
                continue
            return (hit, evidence), "answered"
        if mismatch is not None:
            return mismatch, "model_mismatch"
        return None, "weak_evidence"

    def _evidence(
        self, hit: Hit, result: RetrievalResult, requested: str | None, question: str
    ) -> dict | None:
        if hit.kind == "table":
            return self._table_evidence(hit, result, requested, question)
        return self._text_evidence(hit, result, question)

    def _table_evidence(
        self, hit: Hit, result: RetrievalResult, requested: str | None, question: str
    ) -> dict | None:
        rows = _parse_markdown_table(hit.body)
        if len(rows) < 2:
            return None
        header, data = rows[0], rows[1:]
        caption = " ".join(
            line.strip()
            for line in hit.body.splitlines()
            if line.strip() and not line.strip().startswith("|")
        )

        columns = self._columns_of_interest(header, result)
        targeted = len(columns) < len(header)
        model_rows = [
            index
            for index, row in enumerate(data)
            if requested and requested in detect_models(" ".join(row))
        ]
        if model_rows:
            indexes = model_rows[:5]
        else:
            scored = []
            for index, row in enumerate(data):
                row_text = " ".join(row)
                matched = _matched_tokens(result.tokens, set(tokenize(row_text)))
                weight = sum(result.idf.get(token, 0.0) for token in matched)
                weight += 0.15 * len(matched)
                if weight <= 0:
                    continue
                scored.append((weight, len(matched), index))
            if not scored:
                if not targeted:
                    # Ни строки, ни столбца: таблица не отвечает на вопрос.
                    return None
                # Вопрос относится к столбцу таблицы («через сколько моточасов
                # ТО-2»): в строках нет слов запроса, а ответ содержится в
                # столбце, поэтому таблица показывается целиком.
                indexes = list(range(min(len(data), 6)))
            else:
                scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
                indexes = sorted(item[2] for item in scored[:3])

        chosen = [data[index] for index in indexes]

        models: list[str] = []
        for row in chosen:
            for model in detect_models(" ".join(row)):
                if model not in models:
                    models.append(model)
        for model in detect_models(caption):
            if model not in models:
                models.append(model)

        projected_header = [header[index] for index in columns]
        projected_rows = [
            [row[index] if index < len(row) else "" for index in columns] for row in chosen
        ]

        lines = [
            "| " + " | ".join(projected_header) + " |",
            "|" + "|".join(["---"] * len(projected_header)) + "|",
        ]
        for row in projected_rows:
            lines.append("| " + " | ".join(cell.replace("|", "/") for cell in row) + " |")
        table_markdown = "\n".join(lines)
        quote = f"{caption}\n\n{table_markdown}" if caption else table_markdown

        return {
            "kind": "table",
            "quote": quote,
            "models": models,
            "caption": caption,
        }

    @staticmethod
    def _columns_of_interest(header: list[str], result: RetrievalResult) -> list[int]:
        """Оставить столбец-название и столбцы, о которых спрашивают.

        Без этого на вопрос «через сколько моточасов ТО-2» ответ показывал бы
        всю строку с четырьмя интервалами, и выбор нужного столбца оставался бы
        за пользователем. Ошибка при выборе столбца приводит к неверному
        регламенту, поэтому столбец определяется автоматически.
        """
        if len(header) < 3:
            return list(range(len(header)))
        header_sets = [set(tokenize(cell)) for cell in header]
        matched: list[int] = []
        for token in result.tokens:
            if len(token) < 3:
                continue
            for index in range(1, len(header_sets)):
                if any(
                    token_matches(token, header_token)
                    for header_token in header_sets[index]
                ):
                    if index not in matched:
                        matched.append(index)
                    break
        if not matched:
            return list(range(len(header)))
        return ([0] + sorted(matched))[:4]

    def _text_evidence(
        self, hit: Hit, result: RetrievalResult, question: str
    ) -> dict | None:
        sentences = split_sentences(hit.body)
        if not sentences:
            return None
        numeric = bool(NUMERIC_INTENT.search(question))
        procedure = bool(PROCEDURE_INTENT.search(question))
        interval = bool(INTERVAL_INTENT.search(question))

        scored = []
        for index, sentence in enumerate(sentences):
            sentence_tokens = set(tokenize(sentence))
            matched = _matched_tokens(result.tokens, sentence_tokens)
            if not matched:
                continue
            weight = sum(result.idf.get(token, 0.0) for token in matched)
            weight += 0.15 * len(matched)
            if numeric and re.search(r"\d", sentence):
                weight += 1.2
            if interval and INTERVAL_VALUE.search(sentence):
                # «через каждые 500 часов работы» — это и есть регламент;
                # такая строка весомее любого упоминания по теме.
                weight += 2.4
                if MODEL_SCOPE.search(sentence) or detect_models(sentence):
                    weight += 1.0
            if WARNING_IN_TEXT.search(sentence):
                weight += 0.4
            scored.append((weight, index, sentence))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[:3]
        indexes = {item[1] for item in top}
        # Строка «Для погрузчиков ПД 5,0» уточняет, к какой модели относится
        # интервал, и обычно стоит следующей строкой за регламентной.
        for index in list(indexes):
            for neighbour in (index + 1, index - 1):
                if 0 <= neighbour < len(sentences) and neighbour not in indexes:
                    nearby = sentences[neighbour]
                    if MODEL_SCOPE.search(nearby) or detect_models(nearby):
                        indexes.add(neighbour)
        quote = " ".join(sentences[index] for index in sorted(indexes))
        # Интервал бывает указан в самом заголовке раздела:
        # «11.4.2 Техническое обслуживание № 1 (ТО-1) ( через каждые 200
        # часов работы)». Тогда заголовок и есть ответ, его нужно привести.
        if (
            interval
            and not INTERVAL_VALUE.search(quote)
            and hit.section_title
            and INTERVAL_VALUE.search(hit.section_title)
        ):
            quote = f"{hit.section_title}. {quote}"

        if procedure and hit.kind == "procedure":
            quote = hit.body.strip()

        return {"kind": "procedure" if hit.kind == "procedure" else "text", "quote": quote, "models": hit.models}

    def _supporting_evidence(
        self,
        result: RetrievalResult,
        primary_hit: Hit,
        primary_evidence: dict,
        requested: str | None,
        question: str,
    ) -> list[tuple[Hit, dict]]:
        """Второй фрагмент того же раздела: предупреждение или уточнение.

        Ответ «как обслуживать батарею» без строки о том, где её разрешено
        заряжать, формально верен, но неполон — а неполный регламент опасен.
        """
        collected: list[tuple[Hit, dict]] = []
        seen = {primary_evidence["quote"].strip()}
        for candidate in result.hits:
            if candidate.chunk_id == primary_hit.chunk_id:
                continue
            if not candidate.section_no or candidate.section_no != primary_hit.section_no:
                continue
            if candidate.coverage < primary_hit.coverage - 0.2:
                continue
            if not self._has_evidence(result, candidate):
                continue
            evidence = self._evidence(candidate, result, requested, question)
            if not evidence:
                continue
            quote = evidence["quote"].strip()
            if quote in seen:
                continue
            seen.add(quote)
            collected.append((candidate, evidence))
            if len(collected) == 2:
                break
        return collected

    # ------------------------------------------------------------------ отрисовка
    def _render(
        self,
        status: str,
        hit: Hit,
        evidence: dict,
        requested: str | None,
        supporting: list[tuple[Hit, dict]] | None = None,
    ) -> str:
        label = model_label(requested) if requested else None
        doc_part = f"«{hit.doc_title}»"
        location = []
        if hit.section_no:
            location.append(f"раздел {hit.section_no}")
        if hit.section_title:
            location.append(f"«{hit.section_title}»")
        if hit.page_from:
            location.append(f"стр. {hit.page_from}")
        source = ", ".join([doc_part, *location])

        if status == "model_mismatch":
            found = [model_label(model) for model in evidence.get("models", [])]
            head = f"Для модели **{label}** в руководстве прямого указания нет."
            if found:
                head += f" Найденные данные относятся к модели **{', '.join(found)}**:"
            parts = [head, self._quote_block(evidence), f"Источник: {source}."]
            parts.append(
                f"Переносить это значение на **{label}** нельзя: регламенты и параметры "
                "у моделей различаются. Уточните у сервисной службы завода."
            )
            return "\n\n".join(parts)

        prefix = f"**Модель: {label}.** " if label else ""
        parts = [f"{prefix}По руководству {source}:", self._quote_block(evidence)]
        for extra_hit, extra_evidence in supporting or []:
            parts.append("Дополнительно в том же разделе:")
            parts.append(self._quote_block(extra_evidence))
            if extra_hit.page_from and extra_hit.page_from != hit.page_from:
                parts.append(f"Источник: «{extra_hit.doc_title}», стр. {extra_hit.page_from}.")
        if evidence["kind"] == "procedure":
            parts.append("Порядок приведён дословно из указанного раздела.")
        else:
            parts.append("Фрагмент приведён дословно из указанного раздела.")
        note = self._parenthetical_note(requested, evidence)
        if note:
            parts.append(note)
        return "\n\n".join(parts)

    def _parenthetical_note(self, requested: str | None, evidence: dict) -> str:
        """Для модели с отдельным рядом интервалов значения стоят в скобках.

        Это структурная особенность руководства, а не наше толкование:
        в цитируемой строке скобки видны, поэтому ответ не добавляет фактов,
        которых нет в источнике.
        """
        rule = (self.config.data.get("model_rules") or {}).get(
            "parenthetical_exception_model", ""
        )
        if not requested or not rule or model_key(rule) != requested:
            return ""
        if not re.search(r"\(\s*\d", evidence.get("quote", "")):
            return ""
        label = model_label(requested)
        return (
            f"Для модели {label} действует отдельный ряд интервалов: в таблице "
            f"руководства значения для неё указаны в скобках."
        )

    def _quote_block(self, evidence: dict) -> str:
        quote = evidence["quote"].strip()
        if evidence["kind"] == "table":
            return quote
        if evidence["kind"] == "procedure":
            return quote
        return "\n".join(f"> {line}" for line in quote.splitlines() if line.strip())

    def _warnings(self, hit: Hit, evidence: dict) -> list[str]:
        collected: list[str] = []
        for warning in hit.warnings:
            cleaned = _plain(warning)
            if cleaned and cleaned not in collected:
                collected.append(cleaned)
        for line in evidence["quote"].splitlines():
            if WARNING_IN_TEXT.search(line):
                cleaned = _plain(line)
                if cleaned and cleaned not in collected:
                    collected.append(cleaned)
        return collected

    def _citation(self, hit: Hit, evidence: dict) -> dict:
        return {
            "doc_id": hit.doc_id,
            "doc_title": hit.doc_title,
            "section_no": hit.section_no,
            "section_title": hit.section_title,
            "section_path": hit.section_path,
            "page": hit.page_from,
            "kind": hit.kind,
            "table_id": hit.table_id,
            "quote": _plain(evidence["quote"])[:700],
        }

    def _related(self, hit: Hit) -> dict:
        return {
            "doc_title": hit.doc_title,
            "section_no": hit.section_no,
            "section_title": hit.section_title,
            "page": hit.page_from,
            "snippet": _plain(hit.body)[:240],
            "score": round(hit.score, 3),
        }

    @staticmethod
    def _related_line(item: dict) -> str:
        """Строка «документ, раздел 6.1 «Название», стр. 4» без пустых мест."""
        parts = [item["doc_title"]]
        if item["section_no"]:
            parts.append(f"раздел {item['section_no']}")
        if item["section_title"]:
            parts.append(f"«{item['section_title']}»")
        if item["page"]:
            parts.append(f"стр. {item['page']}")
        return ", ".join(parts)

    # ------------------------------------------------------------------ отказ
    def _refuse(
        self,
        question: str,
        requested: str | None,
        result: RetrievalResult,
        reason: str,
    ) -> dict:
        label = model_label(requested) if requested else None
        if reason == "unconfirmed_limit":
            head = (
                f"В руководстве для модели {label} предельное значение по этому вопросу не приведено."
                if label
                else "В предоставленных руководствах нет предельного значения, которое можно "
                "подтвердить по этому вопросу."
            )
        elif reason == "no_match":
            head = "В предоставленных руководствах нет раздела, отвечающего на этот вопрос."
        else:
            head = "В предоставленных руководствах ответа на этот вопрос нет."
        if label and reason != "unconfirmed_limit":
            head = (
                f"В руководстве для модели {label} ответа на этот вопрос нет."
                if reason != "no_match"
                else f"В руководстве для модели {label} нет раздела по этому вопросу."
            )

        related = [self._related(hit) for hit in result.hits[:3]]
        parts = [head]
        if related:
            listed = "\n".join(f"- {self._related_line(item)}" for item in related)
            parts.append("Рядом по теме в документации найдено (оцените сами, отвечает ли это на вопрос):")
            parts.append(listed)
        parts.append("За уточнением регламента обратитесь в сервисную службу завода." + self._contact_line())

        return self._payload(
            "no_answer",
            "\n\n".join(parts),
            model=label,
            related=related,
            contacts=self._contacts(),
            confidence="low",
            debug={
                "query_tokens": result.tokens,
                "best_coverage": round(result.hits[0].coverage, 3) if result.hits else 0.0,
                "reason": reason,
            },
        )

    # ------------------------------------------------------------------ утилиты
    def _contacts(self) -> dict:
        return dict(self.config.contacts)

    def _contact_line(self) -> str:
        contacts = self.config.contacts
        items = []
        if contacts.get("phone"):
            items.append(f"тел. {contacts['phone']}")
        if contacts.get("email"):
            items.append(f"e-mail {contacts['email']}")
        if contacts.get("site"):
            items.append(str(contacts["site"]))
        if items:
            return " " + ", ".join(items) + "."
        note = contacts.get("note") or (
            "Контакты сервисной службы не настроены — заполните config/service.json."
        )
        return " " + note

    def _numbers_are_supported(self, generated: str, context: str) -> bool:
        allowed = extract_numbers(context)
        for number in extract_numbers(generated):
            if len(number.replace(".", "")) <= 1:
                continue
            if number not in allowed:
                return False
        return True

    def _payload(self, status: str, answer: str, **extra) -> dict:
        payload = {
            "status": status,
            "answer": answer,
            "model": None,
            "citations": [],
            "warnings": [],
            "related": [],
            "contacts": self._contacts(),
            "mode": "extractive",
            "confidence": "low",
            "debug": {},
        }
        payload.update(extra)
        return payload
