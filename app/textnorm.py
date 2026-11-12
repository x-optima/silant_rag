"""Нормализация русского текста: модели, интервалы ТО, единицы, токены.

Зачем это нужно:
* в руководствах модель пишется как «ПД1,5», «ПД 1,5», «ПД-1,5», а в вопросе
  механик может написать «пд 1.5» — всё приводится к одному токену `пд15`,
  иначе FTS не найдёт совпадение (unicode61 разрежет «ПД1,5» на «пд1» и «5»);
* «м/ч» в руководстве по электропогрузчикам и «моточасы» в руководстве по
  автопогрузчикам — одна и та же единица, поэтому обе формы приводятся
  к токену `моточас`;
* «ТО-2», «ТО2», «то 2» -> `то2`.
"""

from __future__ import annotations

import re

MODEL_RE = re.compile(
    r"(?<![а-яёa-z0-9])([пП][дДгГэЭ])[\s\-–—]*(\d{1,2})\s*[.,]\s*(\d)(?!\d)"
)
TO_RE = re.compile(r"(?<![а-яёa-z])(то|ТО)\s*[-–—]?\s*([1-3])(?!\d)", re.IGNORECASE)
UNIT_REPLACEMENTS = [
    (re.compile(r"(?<![а-яa-z])м\s*/\s*ч", re.IGNORECASE), " моточас "),
    (re.compile(r"(?<![а-яa-z])м\s*/\s*час", re.IGNORECASE), " моточас "),
    (re.compile(r"мот\s*[-–]?\s*час", re.IGNORECASE), " моточас "),
    (re.compile(r"мото-час", re.IGNORECASE), " моточас "),
]
TOKEN_RE = re.compile(r"[а-яёa-z0-9]+(?:[.,]\d+)?")

STOPWORDS = {
    "а", "без", "более", "бы", "был", "была", "были", "было", "быть", "в", "вам",
    "вас", "весь", "во", "вот", "все", "всего", "всех", "вы", "где", "да", "даже",
    "для", "до", "его", "ее", "если", "есть", "еще", "же", "за", "зачем", "здесь",
    "и", "из", "или", "им", "их", "к", "как", "какая", "какие", "каких", "какой",
    "каком", "когда", "который", "которая", "которые", "которых", "кто", "ли",
    "мне", "может", "можно", "мой", "моя", "мы", "на", "над", "надо", "надо",
    "наиболее", "например", "не", "него", "нее", "нет", "ни", "них", "но", "ну",
    "о", "об", "однако", "он", "она", "они", "оно", "от", "очень", "по", "под",
    "после", "при", "про", "раз", "разве", "с", "сам", "свое", "себе", "себя",
    "сейчас", "со", "так", "также", "такие", "там", "те", "тем", "того",
    "тоже", "той", "только", "том", "ты", "у", "уже", "хотя", "чего", "чей",
    "чем", "что", "чтобы", "чье", "эта", "эти", "это", "этого", "этой", "этом",
    "этот", "я", "нужно", "нужен", "нужна", "стоит", "скажите", "подскажите",
    "пожалуйста", "вообще", "касается", "относится", "перед", "через", "между",
    "около", "кроме", "согласно", "вместо", "ради", "сквозь", "среди", "вне",
    "мимо", "после", "напротив",
}

# Расширение запроса: сокращения и разговорные формулировки, встречающиеся
# в вопросах пользователей.
SYNONYMS = {
    "акб": ["аккумулятор", "батарея"],
    "аккумулятор": ["акб", "батарея"],
    "батарея": ["аккумулятор", "акб"],
    "домкрат": ["подъемник"],
    "подъемник": ["домкрат"],
    "масло": ["масла", "масел", "смазка"],
    "то": ["обслуживание"],
    "обслуживание": ["то"],
    "лампа": ["индикатор", "сигнализатор"],
    "неисправность": ["причина", "отказ"],
    "буксировка": ["буксировать", "транспортировать"],
    "буксировать": ["буксировка", "транспортировать"],
    "колесо": ["колес", "колеса", "шин"],
    "тормоз": ["тормозной", "тормоза"],
    "гидравлика": ["гидросистема", "гидро"],
    "охлаждающий": ["антифриз", "тосол"],
}


def model_key_from_match(prefix: str, whole: str, frac: str) -> str:
    return f"{prefix.lower()}{int(whole)}{frac}"


def detect_models(text: str) -> list[str]:
    """Возвращает ключи моделей в порядке первого упоминания."""
    if not text:
        return []
    found: list[str] = []
    for match in MODEL_RE.finditer(text):
        key = model_key_from_match(match.group(1), match.group(2), match.group(3))
        if key not in found:
            found.append(key)
    return found


def normalize_text(text: str) -> str:
    """Нижний регистр, ё->е, унификация моделей, ТО и единиц наработки.

    Пунктуация сохраняется: так в FTS остаются фразовые запросы.
    """
    if not text:
        return ""
    result = text.lower().replace("ё", "е")
    for pattern, replacement in UNIT_REPLACEMENTS:
        result = pattern.sub(replacement, result)
    result = MODEL_RE.sub(
        lambda m: f" {model_key_from_match(m.group(1), m.group(2), m.group(3))} ", result
    )
    result = TO_RE.sub(lambda m: f" то{m.group(2)} ", result)
    return result


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    raw = TOKEN_RE.findall(normalize_text(text))
    tokens: list[str] = []
    for token in raw:
        token = token.strip(".,")
        if token:
            tokens.append(token)
    return tokens


FAMILY_HINTS = {
    "electro": re.compile(
        r"электропогрузчик|электродвигател|тягов\w*\s+аккумулятор|заряд\w*\s+батаре",
        re.IGNORECASE,
    ),
    "auto": re.compile(
        r"автопогрузчик|дизельн|газобензин|двигател\w*\s+внутреннего", re.IGNORECASE
    ),
}


def detect_family(text: str) -> set[str]:
    return {name for name, pattern in FAMILY_HINTS.items() if pattern.search(text or "")}


def is_service_token(token: str) -> bool:
    """Модели и номера ТО — значимые токены, их нельзя отбрасывать как шум."""
    return bool(re.fullmatch(r"п[дгэ]\d+|то[123]|ео", token))


def content_tokens(text: str) -> list[str]:
    result: list[str] = []
    for token in tokenize(text):
        if token in STOPWORDS:
            continue
        if len(token) < 2 and not token.isdigit():
            continue
        if token not in result:
            result.append(token)
    return result


def expand_tokens(tokens: list[str]) -> list[str]:
    expanded = list(tokens)
    for token in tokens:
        for synonym in SYNONYMS.get(token, []):
            if synonym not in expanded:
                expanded.append(synonym)
    return expanded


def fts_prefix(token: str) -> str:
    """Префиксный запрос для русской морфологии: «моточас*» -> моточасов, моточасы."""
    if token.isdigit() or is_service_token(token):
        return token
    stem = stem_token(token)
    if len(stem) >= 4:
        return f"{stem}*"
    if len(token) >= 4:
        return f"{token}*"
    return token


def build_fts_query(tokens: list[str]) -> str:
    """OR-запрос: кандидатов отбирает FTS, точный порядок задаёт Python-реранкер."""
    reserved = {"and", "or", "not", "near"}
    parts = []
    seen = set()
    for token in tokens:
        if token in seen or token in reserved:
            continue
        seen.add(token)
        parts.append(fts_prefix(token))
    return " OR ".join(parts)


def split_sentences(text: str) -> list[str]:
    """Аккуратный разрез на предложения для извлечения цитат."""
    if not text:
        return []
    prepared = re.sub(r"[ \t]+", " ", text)
    prepared = prepared.replace("\n", " \n ")
    chunks = re.split(r"(?<=[.!?;])\s+(?=[А-ЯЁA-Z0-9«(])|\s*\n\s*", prepared)
    sentences = []
    for chunk in chunks:
        cleaned = " ".join(chunk.split())
        if len(cleaned) >= 12:
            sentences.append(cleaned)
    return sentences


def extract_numbers(text: str) -> set[str]:
    """Числа для проверки: каждое число в ответе должно быть в источнике."""
    numbers: set[str] = set()
    for token in re.findall(r"\d+(?:[.,]\d+)?", text or ""):
        numbers.add(token.replace(",", "."))
    return numbers


def singular_forms(token: str) -> list[str]:
    """Грубые основы для подсветки совпадений (без полноценной лемматизации)."""
    forms = {token}
    for suffix in ("ами", "ями", "ого", "ему", "ыми", "ими", "ах", "ях", "ов",
                   "ев", "ий", "ый", "ая", "ое", "ые", "ам", "ям", "ом", "ем",
                   "ой", "ей", "ую", "юю", "ия", "ии", "ть", "ся", "а", "я",
                   "ы", "и", "у", "ю", "е", "о", "ь"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            forms.add(token[: -len(suffix)])
    return list(forms)


_ENDINGS = sorted(
    [
        "ировать", "овать", "евать", "ание", "ение", "ания", "ения", "ями", "ами",
        "ыми", "ими", "ого", "его", "ому", "ему", "ать", "ять", "ить", "еть",
        "уть", "ыть", "ться", "тся", "ов", "ев", "ам", "ям", "ах", "ях", "ом",
        "ем", "ой", "ей", "ую", "юю", "ия", "ии", "ый", "ий", "ая", "ое", "ые",
        "ет", "ут", "ют", "ит", "ат", "ят", "ся", "а", "я", "ы", "и", "у", "ю",
        "е", "о", "ь",
    ],
    key=len,
    reverse=True,
)


def stem_token(token: str) -> str:
    """Грубая основа слова: «обслуживание»/«обслуживать» -> «обслужив».

    Полноценная лемматизация (pymorphy) дала бы больше, но потребовала бы
    зависимость и словарь. Для сопоставления запроса с регламентом отсечения
    окончаний достаточно, а ошибки ограничены длиной основы (>= 3 символов).
    """
    if not token or is_service_token(token) or token.isdigit():
        return token
    for ending in _ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= 3:
            return token[: -len(ending)]
    return token


def token_matches(query_token: str, text_token: str) -> bool:
    """Совпадение словоформ: общая основа либо общий префикс основы."""
    if query_token == text_token:
        return True
    if is_service_token(query_token) or is_service_token(text_token):
        return False
    left, right = stem_token(query_token), stem_token(text_token)
    if left == right:
        return True
    if min(len(left), len(right)) < 4:
        return False
    prefix = min(len(left), len(right)) - 1
    return left[:prefix] == right[:prefix]
