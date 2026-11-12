"""Тесты пайплайна: нормализация, разбор структуры, индекс, поведение ответов.

Запуск:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from app.answer import Answerer
from app.config import DEFAULTS, get_config, model_key, save_config, validate_config
from app.index import Store
from app.ingest import ingest_file
from app.structure import parse_document
from app.textnorm import detect_models, token_matches, tokenize

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


class TestNormalization(unittest.TestCase):
    def test_model_variants(self):
        self.assertEqual(detect_models("ПД 5,0 и ПЭ1,5"), ["пд50", "пэ15"])
        self.assertEqual(detect_models("ПГ-2.0"), ["пг20"])
        self.assertEqual(model_key("ПД1,5"), "пд15")

    def test_motohours_and_speed(self):
        self.assertIn("моточас", tokenize("замена через 200 м/ч"))
        self.assertNotIn("моточас", tokenize("скорость 5 км/ч"))

    def test_to_number(self):
        self.assertIn("то2", tokenize("ТО-2"))
        self.assertIn("то1", tokenize("то 1"))

    def test_word_forms(self):
        self.assertTrue(token_matches("масла", "масло"))
        self.assertTrue(token_matches("обслуживать", "обслуживание"))
        self.assertFalse(token_matches("масла", "мясо"))


class TestStructure(unittest.TestCase):
    def test_sections_table_warning(self):
        pages = [
            "1. ОБЩИЕ СВЕДЕНИЯ\n\n1.1 Назначение\nТекст первого абзаца.",
            "Таблица 1 — Проверки\n\nНаименование работ    ТО-1    ТО-2\n"
            "Заменить масло    200    400\nВНИМАНИЕ! Работа запрещается.",
        ]
        blocks = parse_document(pages)
        kinds = [block.kind for block in blocks]
        self.assertIn("heading", kinds)
        self.assertIn("table", kinds)
        self.assertIn("warning", kinds)

        table = next(block for block in blocks if block.kind == "table")
        self.assertEqual(table.table_id, "1")
        self.assertEqual(table.rows[0][0], "Наименование работ")
        self.assertEqual(table.page, 2)
        self.assertEqual(table.section_no, "1.1")

    def test_steps_are_kept_in_order(self):
        pages = ["6. ТЕХНИЧЕСКОЕ ОБСЛУЖИВАНИЕ\n\n6.1 Замена масла\n\n"
                 "1. Слить масло.\n2. Заменить фильтр.\n3. Залить масло."]
        blocks = parse_document(pages)
        steps = [block for block in blocks if block.kind == "steps"]
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0].text.startswith("1. Слить масло."))
        self.assertIn("3. Залить масло.", steps[0].text)


class TestStore(unittest.TestCase):
    def test_reingest_replaces_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(pathlib.Path(tmp) / "index.sqlite3")
            chunk = {"body": "проверить уровень масла", "page_from": 1, "page_to": 1,
                     "kind": "text", "section_no": "5.1"}
            store.replace_document("doc", {"title": "Первая редакция", "pages": 1}, [chunk])
            self.assertEqual(store.stats()["chunks"], 1)

            store.replace_document("doc", {"title": "Вторая редакция", "pages": 2},
                                   [chunk, dict(chunk)])
            self.assertEqual(store.stats()["documents"], 1)
            self.assertEqual(store.stats()["chunks"], 2)
            self.assertEqual(store.documents()[0]["title"], "Вторая редакция")
            store.close()


class TestAnswers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(pathlib.Path(cls.tmp.name) / "index.sqlite3")
        for name in ("avtopogruzchiki.txt", "electropogruzchiki.txt"):
            ingest_file(cls.store, FIXTURES / name, replace=True)
        cls.answerer = Answerer(cls.store, get_config())

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def test_model_required(self):
        payload = self.answerer.answer("Через сколько моточасов проводится ТО-2?")
        self.assertEqual(payload["status"], "clarify_model")
        self.assertIn("Уточните модель", payload["answer"])
        self.assertTrue(payload["model_options"])

    def test_answer_with_citation_and_model(self):
        payload = self.answerer.answer(
            "Через сколько моточасов проводится ТО-2 на ПД5,0?", "ПД5,0"
        )
        self.assertEqual(payload["status"], "answered")
        self.assertIn("500", payload["answer"])
        self.assertEqual(payload["model"], "ПД5,0")
        citation = payload["citations"][0]
        self.assertTrue(citation["doc_title"])
        self.assertEqual(citation["section_no"], "6.1")
        self.assertTrue(citation["page"])

    def test_procedure_returns_steps(self):
        payload = self.answerer.answer(
            "В каком порядке выполняется замена масла в двигателе?", "ПД2,0"
        )
        self.assertEqual(payload["status"], "answered")
        self.assertIn("сливную пробку", payload["answer"])
        self.assertEqual(payload["citations"][0]["kind"], "procedure")

    def test_warnings_are_attached(self):
        payload = self.answerer.answer(
            "Как поднять погрузчик домкратом?", "ПД3,0"
        )
        self.assertTrue(payload["warnings"])
        self.assertIn("страховочных подставок", " ".join(payload["warnings"]))

    def test_no_answer_for_missing_topic(self):
        payload = self.answerer.answer(
            "Какой момент затяжки болтов головки блока цилиндров?", "ПД2,0"
        )
        self.assertEqual(payload["status"], "no_answer")
        self.assertIn("нет", payload["answer"])
        self.assertFalse(payload["citations"])

    def test_model_mismatch(self):
        payload = self.answerer.answer(
            "Какое напряжение тяговой аккумуляторной батареи у ПГ2,0?", "ПГ2,0"
        )
        self.assertEqual(payload["status"], "model_mismatch")
        self.assertIn("нельзя", payload["answer"])

    def test_out_of_scope_engine(self):
        payload = self.answerer.answer(
            "Как отремонтировать двигатель по документации изготовителя двигателя?", "ПД2,0"
        )
        self.assertEqual(payload["status"], "out_of_scope")

    def test_empty_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(pathlib.Path(tmp) / "empty.sqlite3")
            answerer = Answerer(store, get_config())
            payload = answerer.answer("Что проверить перед началом смены?")
            self.assertEqual(payload["status"], "empty_base")
            store.close()

    def test_unconfirmed_limit(self):
        payload = self.answerer.answer(
            "Какая максимальная скорость передвижения погрузчика?", "ПД2,0"
        )
        self.assertEqual(payload["status"], "no_answer")
        self.assertIn("не приведено", payload["answer"])


class TestConfig(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(validate_config(dict(DEFAULTS)), [])

    def test_invalid_values_are_reported(self):
        broken = {
            "service_contacts": {"phone": "позвоните", "email": "не-почта", "site": "ftp://x"},
            "thresholds": {"min_coverage": 5, "min_matched_terms": 0},
            "llm": {"temperature": 9, "timeout_seconds": 1},
            "model_rules": {"parenthetical_exception_model": "ПД9,9"},
            "clarification": {"model_required_patterns": ["("]},
        }
        errors = " ".join(validate_config(broken))
        for fragment in (
            "phone", "email", "site", "min_coverage", "min_matched_terms",
            "temperature", "timeout_seconds", "parenthetical", "регулярное",
        ):
            self.assertIn(fragment, errors)

    def test_save_creates_backup_and_refuses_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "service.json"
            first = save_config({"app_name": "Первый"}, path)
            self.assertEqual(first.app_name, "Первый")
            self.assertTrue(path.exists())

            save_config({"app_name": "Второй"}, path)
            self.assertTrue(path.with_name("service.json.bak").exists())
            self.assertEqual(get_config(path).app_name, "Второй")

            before = path.read_text(encoding="utf-8")
            with self.assertRaises(ValueError):
                save_config({"thresholds": {"min_score": 42}}, path)
            self.assertEqual(path.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
