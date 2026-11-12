"""Опциональный LLM-слой.

По умолчанию выключен: сервис обязан работать и без внешних вызовов —
именно это обеспечивает воспроизводимость и гарантию «ответ только из
документации». Если ключ задан, LLM используется как переформулировщик уже
найденных фрагментов; ответ проходит проверку чисел по контексту и при
расхождении отбрасывается в пользу дословной цитаты.

Ключ никогда не хранится в репозитории: только переменные окружения
SILANT_LLM_API_KEY, SILANT_LLM_BASE_URL, SILANT_LLM_MODEL.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

SYSTEM_PROMPT = """Ты — сервисный ассистент по руководствам по эксплуатации вилочных погрузчиков СИЛАНТ.
Правила, которые нельзя нарушать:
1. Отвечай ТОЛЬКО по фрагментам документации, переданным в сообщении пользователя.
2. Не добавляй ни одного числа, марки, момента затяжки или интервала, которого нет во фрагментах.
3. Всегда указывай модель техники, к которой относится ответ. Если фрагменты относятся к другой модели — прямо скажи об этом.
4. Если во фрагментах ответа нет, ответь ровно: "В предоставленных руководствах ответа нет." — без пересказа смежных разделов.
5. Предупреждения ВНИМАНИЕ / ВАЖНО / запреты, относящиеся к операции, приводи полностью.
6. Не используй внешние знания и не додумывай значения.
Ответ должен быть кратким: 1-4 предложения или таблица, затем строка "Источник: раздел ...".
"""


class LlmClient:
    def __init__(self, config) -> None:
        cfg = config.llm
        self.api_key = (
            os.environ.get("SILANT_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        self.base_url = (
            os.environ.get("SILANT_LLM_BASE_URL") or cfg.get("base_url", "")
        ).rstrip("/")
        self.model = os.environ.get("SILANT_LLM_MODEL") or cfg.get("model", "")
        self.temperature = float(cfg.get("temperature", 0.0))
        self.timeout = float(cfg.get("timeout_seconds", 40))
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def generate(self, question: str, contexts: list[str]) -> str | None:
        if not self.enabled:
            return None
        context_block = "\n\n---\n\n".join(contexts[:6])
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"ВОПРОС:\n{question}\n\nФРАГМЕНТЫ ДОКУМЕНТАЦИИ:\n{context_block}",
                },
            ],
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as exc:
            self.last_error = str(exc)
            return None
