"""Конфигурация сервиса: модели техники, пороги релевантности, контакты.

Все значения, которые могут меняться при внедрении (контакты сервисной
службы, пороги отказа, список моделей), вынесены в config/service.json.
Модуль не бросает исключений при отсутствии файла — используются значения
по умолчанию, чтобы сервис запускался «из коробки».
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import re
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "service.json"

# Модели из задания. Список используется для уточняющего вопроса и для
# нормализации упоминаний в тексте руководств.
AUTOLOADER_MODELS = [
    "ПД1,5", "ПД1,8", "ПД2,0", "ПД2,5", "ПД3,0", "ПД3,5", "ПД5,0",
    "ПГ1,5", "ПГ1,8", "ПГ2,0", "ПГ2,5", "ПГ3,0", "ПГ3,5",
]
ELECTRO_MODELS = ["ПЭ1,5", "ПЭ1,8", "ПЭ2,0", "ПЭ2,5"]
MODELS = AUTOLOADER_MODELS + ELECTRO_MODELS

FAMILY_LABEL = {"ПД": "дизельный", "ПГ": "газобензиновый", "ПЭ": "электрический"}


def model_key(label: str) -> str:
    """'ПД5,0' -> 'пд50'. Ключ используется как токен и как метка в индексе."""
    m = re.match(r"\s*([Пп][ДдГгЭэ])\s*(\d{1,2})\s*[,.]\s*(\d)\s*$", label)
    if not m:
        return label.strip().lower().replace(",", "").replace(".", "")
    return f"{m.group(1).lower()}{m.group(2)}{m.group(3)}"


MODEL_KEYS = {model_key(m): m for m in MODELS}
MODEL_FAMILY = {model_key(m): m[:2] for m in MODELS}


def model_label(key: str) -> str:
    """Ключ модели в читаемую подпись: 'пд50' -> 'ПД5,0'.

    Ключа может не быть в справочнике: завод вправе загрузить руководство по
    модели, которой не было в исходном списке, и она появится в индексе.
    """
    known = MODEL_KEYS.get(key)
    if known:
        return known
    lowered = key.lower()
    # Ключ вида «пд40» означает модель ПД4,0: последняя цифра — дробная часть.
    if len(lowered) >= 4 and lowered[0] == "п" and lowered[1] in "дгэ" and lowered[2:].isdigit():
        return f"{lowered[:2].upper()}{lowered[2:-1]},{lowered[-1]}"
    return key.upper()

DEFAULTS: dict[str, Any] = {
    "app_name": "СИЛАНТ · Сервисный ассистент",
    "app_subtitle": "Ответы только по руководствам по эксплуатации ЧЗСА",
    "service_contacts": {"phone": "", "email": "", "site": "", "note": ""},
    "thresholds": {
        "min_coverage": 0.42,
        "min_matched_terms": 2,
        "min_score": 0.30,
        "top_k": 8,
        "candidate_limit": 60,
    },
    "clarification": {"model_required_patterns": []},
    "model_rules": {"parenthetical_exception_model": "ПД5,0"},
    "out_of_scope": {"groups": []},
    "llm": {
        "enabled": False,
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "temperature": 0.0,
        "timeout_seconds": 40,
    },
}


def _merge(base: dict, extra: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    def __init__(self, data: dict, path: pathlib.Path):
        self.data = data
        self.path = path

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        target = pathlib.Path(path) if path else _default_path()
        raw: dict = {}
        if target.exists():
            try:
                raw = json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:  # конфиг правится руками — предупредим
                raise SystemExit(f"config: файл {target} содержит некорректный JSON: {exc}") from exc
        return cls(_merge(DEFAULTS, raw), target)

    # --- доступ ---
    @property
    def thresholds(self) -> dict:
        return self.data["thresholds"]

    @property
    def contacts(self) -> dict:
        return self.data.get("service_contacts", {})

    @property
    def llm(self) -> dict:
        return self.data.get("llm", {})

    @property
    def app_name(self) -> str:
        return self.data.get("app_name", DEFAULTS["app_name"])

    @property
    def app_subtitle(self) -> str:
        return self.data.get("app_subtitle", DEFAULTS["app_subtitle"])

    def model_required_patterns(self) -> list[re.Pattern]:
        raw = self.data.get("clarification", {}).get("model_required_patterns", [])
        compiled = []
        for pattern in raw:
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error:
                continue
        return compiled

    def out_of_scope_groups(self) -> list[dict]:
        return self.data.get("out_of_scope", {}).get("groups", [])


_CACHE: Config | None = None

CONTACT_FIELDS = ("phone", "email", "site", "note")
PHONE_RE = re.compile(r"[0-9+()\-\s]{5,25}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}")
URL_RE = re.compile(r"https?://\S+")

THRESHOLD_LIMITS = {
    "min_coverage": (0.0, 1.0, "число от 0 до 1"),
    "min_matched_terms": (1, 10, "целое от 1 до 10"),
    "min_score": (0.0, 1.0, "число от 0 до 1"),
    "top_k": (1, 50, "целое от 1 до 50"),
    "candidate_limit": (10, 500, "целое от 10 до 500"),
}


def _default_path() -> pathlib.Path:
    """Путь к конфигу: SILANT_CONFIG перекрывает значение по умолчанию (Docker)."""
    override = os.environ.get("SILANT_CONFIG")
    return pathlib.Path(override) if override else DEFAULT_CONFIG_PATH


def get_config(path: str | os.PathLike | None = None) -> Config:
    global _CACHE
    if path is not None:
        return Config.load(path)
    if _CACHE is None:
        _CACHE = Config.load()
    return _CACHE


def reload_config() -> Config:
    """Перечитать конфиг с диска; вызывается после сохранения настроек."""
    global _CACHE
    _CACHE = Config.load()
    return _CACHE


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_config(data: Any) -> list[str]:
    """Проверить конфиг перед записью.

    Возвращает список понятных человеку ошибок. Пустой список означает, что
    конфиг корректен. Проверка намеренно строгая: настройки изменяются в
    браузере, поэтому администратору следует показать ошибку, а не записывать
    файл, с которым сервис не запустится.
    """
    if not isinstance(data, dict):
        return ["Ожидается JSON-объект с настройками"]

    errors: list[str] = []

    for key in ("app_name", "app_subtitle"):
        value = data.get(key, "")
        if not isinstance(value, str):
            errors.append(f"{key}: ожидается строка")
        elif len(value.strip()) > 200:
            errors.append(f"{key}: не длиннее 200 символов")

    contacts = data.get("service_contacts", {})
    if not isinstance(contacts, dict):
        errors.append("service_contacts: ожидается объект")
        contacts = {}
    for field in CONTACT_FIELDS:
        value = contacts.get(field, "")
        if not isinstance(value, str):
            errors.append(f"service_contacts.{field}: ожидается строка")
        elif len(value) > 300:
            errors.append(f"service_contacts.{field}: не длиннее 300 символов")
    phone = str(contacts.get("phone") or "").strip()
    if phone and not PHONE_RE.fullmatch(phone):
        errors.append("service_contacts.phone: цифры, +, -, скобки и пробелы (5–25 знаков)")
    email = str(contacts.get("email") or "").strip()
    if email and not EMAIL_RE.fullmatch(email):
        errors.append("service_contacts.email: некорректный адрес")
    site = str(contacts.get("site") or "").strip()
    if site and not URL_RE.fullmatch(site):
        errors.append("service_contacts.site: ожидается адрес вида https://…")

    thresholds = data.get("thresholds", {})
    if not isinstance(thresholds, dict):
        errors.append("thresholds: ожидается объект")
        thresholds = {}
    for key, (low, high, human) in THRESHOLD_LIMITS.items():
        if key not in thresholds:
            continue
        value = thresholds[key]
        if not _is_number(value) or not (low <= float(value) <= high):
            errors.append(f"thresholds.{key}: {human}")
    if thresholds.get("min_coverage", 0) and thresholds.get("min_score", 0):
        pass  # пороги независимы, дополнительных связей нет

    llm = data.get("llm", {})
    if not isinstance(llm, dict):
        errors.append("llm: ожидается объект")
        llm = {}
    if "enabled" in llm and not isinstance(llm["enabled"], bool):
        errors.append("llm.enabled: true или false")
    base_url = str(llm.get("base_url") or "").strip()
    if base_url and not URL_RE.fullmatch(base_url):
        errors.append("llm.base_url: ожидается http(s)-адрес")
    temperature = llm.get("temperature", 0)
    if not _is_number(temperature) or not (0 <= float(temperature) <= 2):
        errors.append("llm.temperature: число от 0 до 2")
    timeout = llm.get("timeout_seconds", 40)
    if not _is_number(timeout) or not (5 <= float(timeout) <= 300):
        errors.append("llm.timeout_seconds: число от 5 до 300")

    model_rules = data.get("model_rules", {})
    if isinstance(model_rules, dict):
        model = str(model_rules.get("parenthetical_exception_model") or "").strip()
        if model and model not in MODELS:
            errors.append(
                "model_rules.parenthetical_exception_model: модель из списка или пустая строка"
            )

    scope = data.get("out_of_scope", {})
    if scope and not isinstance(scope, dict):
        errors.append("out_of_scope: ожидается объект")
    elif isinstance(scope, dict):
        groups = scope.get("groups", [])
        if not isinstance(groups, list):
            errors.append("out_of_scope.groups: ожидается массив")
        else:
            for index, group in enumerate(groups, start=1):
                if not isinstance(group, dict):
                    errors.append(f"out_of_scope.groups[{index}]: ожидается объект")
                    continue
                for field in ("any_of", "also_of"):
                    value = group.get(field, [])
                    if not isinstance(value, list) or not all(
                        isinstance(item, str) for item in value
                    ):
                        errors.append(f"out_of_scope.groups[{index}].{field}: массив строк")

    clarification = data.get("clarification", {})
    if clarification and not isinstance(clarification, dict):
        errors.append("clarification: ожидается объект")
    elif isinstance(clarification, dict):
        patterns = clarification.get("model_required_patterns", [])
        if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
            errors.append("clarification.model_required_patterns: массив строк")
        else:
            for index, pattern in enumerate(patterns, start=1):
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append(
                        f"clarification.model_required_patterns[{index}]: "
                        f"некорректное регулярное выражение ({exc})"
                    )

    return errors


def _normalize(data: dict) -> dict:
    """Убрать случайные пробелы по краям — иначе они попадают в ответы."""
    result = copy.deepcopy(data)
    for key in ("app_name", "app_subtitle"):
        if isinstance(result.get(key), str):
            result[key] = result[key].strip()
    contacts = result.get("service_contacts")
    if isinstance(contacts, dict):
        for field in CONTACT_FIELDS:
            if isinstance(contacts.get(field), str):
                contacts[field] = contacts[field].strip()
    llm = result.get("llm")
    if isinstance(llm, dict):
        for field in ("base_url", "model"):
            if isinstance(llm.get(field), str):
                llm[field] = llm[field].strip()
    return result


def save_config(data: dict, path: str | os.PathLike | None = None) -> Config:
    """Проверить и атомарно записать конфиг, сохранив предыдущую версию в .bak.

    Атомарная запись (tmp + os.replace) нужна, чтобы оборванное сохранение не
    оставило сервис без конфига: файл либо старый, либо новый целиком.
    """
    errors = validate_config(data)
    if errors:
        raise ValueError("; ".join(errors))

    target = pathlib.Path(path) if path else _default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        # Запись через символическую ссылку увела бы файл за пределы каталога
        # настроек: разрешаем только обычный файл или его отсутствие.
        raise ValueError("config/service.json: запись через символическую ссылку запрещена")
    merged = _normalize(_merge(DEFAULTS, data))

    if target.exists():
        backup = target.with_name(target.name + ".bak")
        backup.write_bytes(target.read_bytes())

    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, target)
    return Config.load(target)
