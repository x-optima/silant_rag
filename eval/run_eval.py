"""Прогон тестового набора и расчёт метрик.

Запуск:
    python3 -m eval.run_eval --db data/index.sqlite3 --questions eval/questions_real.jsonl --corpus real
    python3 -m eval.run_eval                    # реальная база data/index.sqlite3, поведенческие вопросы

Метрики:
* status — совпал ли тип ответа (ответ / отказ / уточнение модели / вне базы);
* facts — присутствуют ли в ответе ожидаемые значения и формулировки;
* citation — есть ли ссылка на документ и раздел;
* safety — приведены ли предупреждения, когда это обязательно;
* forbidden — не появилось ли в ответе того, чего быть не должно.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from app.answer import Answerer
from app.config import ROOT, get_config
from app.index import Store

QUESTIONS = ROOT / "eval" / "questions.jsonl"
RESULTS_MD = ROOT / "eval" / "results.md"


def normalize(text: str) -> str:
    return " ".join((text or "").lower().replace("ё", "е").split())


def load_questions(path: pathlib.Path) -> list[dict]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        items.append(json.loads(line))
    return items


def check(item: dict, payload: dict) -> dict:
    answer_text = normalize(payload.get("answer", ""))
    citations = payload.get("citations") or []
    quotes = normalize(" ".join(citation.get("quote", "") for citation in citations))
    haystack = f"{answer_text} {quotes}"

    status_ok = payload.get("status") in item.get("expect_status", [])
    must = item.get("must_contain") or []
    facts_ok = all(normalize(value) in haystack for value in must)
    forbidden = item.get("must_not_contain") or []
    forbidden_ok = not any(normalize(value) in haystack for value in forbidden)

    if item.get("must_cite"):
        citation_ok = bool(citations) and all(
            citation.get("doc_title") and (citation.get("section_no") or citation.get("section_title"))
            for citation in citations
        )
    else:
        citation_ok = True

    safety_ok = True
    if item.get("must_warn"):
        safety_ok = bool(payload.get("warnings"))

    kind_ok = True
    if item.get("must_kind"):
        kind_ok = any(citation.get("kind") == item["must_kind"] for citation in citations)

    section_ok = True
    if item.get("expect_section"):
        expected = normalize(item["expect_section"])
        section_ok = any(
            expected in normalize(citation.get("section_no", ""))
            or expected in normalize(citation.get("section_title", ""))
            for citation in citations
        )

    checks = {
        "status": status_ok,
        "facts": facts_ok,
        "citation": citation_ok,
        "safety": safety_ok,
        "kind": kind_ok,
        "section": section_ok,
        "forbidden": forbidden_ok,
    }
    return {"checks": checks, "ok": all(checks.values())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка качества ответов")
    parser.add_argument("--db", default=str(ROOT / "data" / "index.sqlite3"))
    parser.add_argument("--questions", default=str(QUESTIONS))
    parser.add_argument(
        "--corpus",
        choices=["all", "real", "any"],
        default="all",
        help=(
            "какие вопросы прогонять: привязанные к демо-корпусу, "
            "привязанные к заводским руководствам, универсальные или все"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-write", action="store_true", help="не перезаписывать eval/results.md")
    args = parser.parse_args(argv)

    store = Store(args.db)
    if store.total_chunks == 0:
        print(
            f"База {args.db} пуста. Сначала загрузите документы: "
            "./deploy.sh --reindex (в контейнере) или python3 -m app.ingest (локально).",
            file=sys.stderr,
        )
        return 2

    config = get_config()
    answerer = Answerer(store, config)
    questions = load_questions(pathlib.Path(args.questions))
    if args.corpus != "all":
        questions = [item for item in questions if item.get("corpus") == args.corpus]

    rows = []
    counters = {"passed": 0, "total": 0}
    per_check = {key: [0, 0] for key in ("status", "facts", "citation", "safety", "kind", "section", "forbidden")}

    for item in questions:
        payload = answerer.answer(item["question"], item.get("model"))
        verdict = check(item, payload)
        counters["total"] += 1
        if verdict["ok"]:
            counters["passed"] += 1
        for key, value in verdict["checks"].items():
            per_check[key][1] += 1
            if value:
                per_check[key][0] += 1
        failed = [key for key, value in verdict["checks"].items() if not value]
        rows.append(
            {
                "id": item["id"],
                "category": item["category"],
                "question": item["question"],
                "status": payload.get("status"),
                "ok": verdict["ok"],
                "failed": failed,
                "answer": payload.get("answer", ""),
                "citations": payload.get("citations", []),
                "reference": item.get("reference", ""),
                "debug": payload.get("debug", {}),
            }
        )
        if args.verbose or not verdict["ok"]:
            marker = "OK  " if verdict["ok"] else "FAIL"
            print(f"[{marker}] {item['id']}: {item['question']}")
            print(f"        статус: {payload.get('status')}; провалы: {', '.join(failed) or '—'}")
            if not verdict["ok"]:
                print(f"        ответ: {payload.get('answer','')[:400].replace(chr(10), ' ')}")
                print(f"        debug: {payload.get('debug')}")

    accuracy = counters["passed"] / counters["total"] * 100 if counters["total"] else 0.0
    print("\n" + "=" * 72)
    print(f"Вопросов: {counters['total']}, полностью пройдено: {counters['passed']} ({accuracy:.1f}%)")
    for key, (good, total) in per_check.items():
        if total:
            print(f"  {key:10}: {good}/{total} ({good / total * 100:.0f}%)")

    if not args.no_write:
        write_report(args.db, rows, counters, per_check, accuracy)
        print(f"\nОтчёт: {RESULTS_MD}")
    return 0 if counters["passed"] == counters["total"] else 1


def write_report(db_path: str, rows: list[dict], counters: dict, per_check: dict, accuracy: float) -> None:
    lines = [
        "# Результаты проверки на тестовом наборе",
        "",
        f"База: `{pathlib.Path(db_path).name}` · вопросов: {counters['total']} · "
        f"пройдено полностью: {counters['passed']} ({accuracy:.1f}%)",
        "",
        "| Проверка | Пройдено | Всего | Доля |",
        "|---|---|---|---|",
    ]
    titles = {
        "status": "тип ответа",
        "facts": "ожидаемые факты в ответе",
        "citation": "ссылка на документ и раздел",
        "safety": "предупреждения приведены",
        "kind": "тип фрагмента (таблица/процедура)",
        "section": "ожидаемый раздел",
        "forbidden": "нет запрещённых формулировок",
    }
    for key, (good, total) in per_check.items():
        if total:
            lines.append(f"| {titles[key]} | {good} | {total} | {good / total * 100:.0f}% |")

    lines.extend(
        [
            "",
            "| № | Категория | Вопрос | Результат | Тип ответа | Что не сошлось |",
            "|---|---|---|---|---|---|",
        ]
    )
    for row in rows:
        mark = "OK" if row["ok"] else "FAIL"
        failed = ", ".join(row["failed"]) or "—"
        question = row["question"].replace("|", "/")
        lines.append(
            f"| {row['id']} | {row['category']} | {question} | {mark} | {row['status']} | {failed} |"
        )

    failures = [row for row in rows if not row["ok"]]
    if failures:
        lines.extend(["", "## Разбор расхождений", ""])
        for row in failures:
            citation = row["citations"][0] if row["citations"] else {}
            location = ""
            if citation:
                location = (
                    f" Источник: {citation.get('doc_title')}, "
                    f"раздел {citation.get('section_no') or '—'}, стр. {citation.get('page')}."
                )
            lines.extend(
                [
                    f"### {row['id']} — {row['question']}",
                    "",
                    f"Эталон: {row['reference']}{location}",
                    "",
                    "Получено:",
                    "",
                    "```",
                    row["answer"][:900],
                    "```",
                    "",
                ]
            )

    RESULTS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
