"""Веб-сервис на стандартной библиотеке.

Запуск:
    python3 -m app.server                  # http://127.0.0.1:8000
    python3 -m app.server --port 8080 --open

Сознательно не используется FastAPI/uvicorn: по условию решение должно
воспроизводиться у проверяющего одной командой, без установки пакетов и без
доступа в интернет. Вся логика — чистый Python + SQLite.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import mimetypes
import os
import pathlib
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .answer import Answerer
from .config import (
    MODEL_KEYS,
    MODELS,
    ROOT,
    get_config,
    model_label,
    save_config,
    validate_config,
)
from .index import Store

WEB_ROOT = (ROOT / "web").resolve()
DEFAULT_DB = ROOT / "data" / "index.sqlite3"
MAX_BODY = 128 * 1024


class Api:
    def __init__(self, db_path: pathlib.Path):
        self.config = get_config()
        self.store = Store(db_path)
        self.answerer = Answerer(self.store, self.config)
        self.db_path = db_path

    def meta(self) -> dict:
        documents = [
            {
                "doc_id": doc["doc_id"],
                "title": doc["title"],
                "version": doc["version"],
                "pages": doc["pages"],
                "sections": doc["sections"],
                "chunks": doc["chunk_count"],
                "added_at": doc["added_at"],
            }
            for doc in self.store.documents()
        ]
        return {
            "app_name": self.config.app_name,
            "app_subtitle": self.config.app_subtitle,
            "models": self.answerer.model_options(),
            "model_keys": {key: model_label(key) for key in self.answerer.known_model_keys()},
            "documents": documents,
            "stats": self.store.stats(),
            "contacts": self.config.contacts,
            "mode": "llm" if self.answerer.llm.enabled else "extractive",
            "db": self.db_path.name,
        }

    def chat(self, message: str, model: str | None) -> dict:
        return self.answerer.answer(message, model)

    def search(self, query: str, model: str | None) -> dict:
        result = self.answerer.retriever.retrieve(query, model)
        return {
            "query_tokens": result.tokens,
            "hits": [
                {
                    "chunk_id": hit.chunk_id,
                    "doc_title": hit.doc_title,
                    "section_no": hit.section_no,
                    "section_title": hit.section_title,
                    "page": hit.page_from,
                    "kind": hit.kind,
                    "coverage": round(hit.coverage, 3),
                    "score": round(hit.score, 3),
                    "models": hit.models,
                    "snippet": hit.body[:400],
                }
                for hit in result.hits
            ],
        }

    # ------------------------------------------------------------------ панель настроек
    @staticmethod
    def llm_key_present() -> bool:
        return bool(os.environ.get("SILANT_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"))

    def config_warnings(self, config=None) -> list[str]:
        """Замечания к настройкам, которые показываются после сохранения."""
        config = config or self.config
        warnings: list[str] = []
        contacts = config.contacts
        if not (contacts.get("phone") or contacts.get("email")):
            warnings.append(
                "Контакты сервисной службы не заполнены: в ответе об отсутствии "
                "информации будет заглушка вместо телефона и почты."
            )
        if config.llm.get("enabled") and not self.llm_key_present():
            warnings.append(
                "LLM включён, но переменная окружения SILANT_LLM_API_KEY не задана — "
                "ответы останутся экстрактивными."
            )
        if not self.store.stats()["documents"]:
            warnings.append(
                "База документов пуста: положите руководства в data/documents "
                "и выполните ./deploy.sh --reindex."
            )
        return warnings

    def admin_payload(self) -> dict:
        return {
            "config": self.config.data,
            "path": str(self.config.path),
            "models": self.answerer.model_options(),
            "llm_key_present": self.llm_key_present(),
            "warnings": self.config_warnings(),
            "stats": self.store.stats(),
        }

    def save_admin(self, data: dict) -> dict:
        errors = validate_config(data)
        if errors:
            return {"saved": False, "errors": errors}
        config = save_config(data, self.config.path)
        self.config = config
        self.answerer = Answerer(self.store, config)
        return {
            "saved": True,
            "config": config.data,
            "warnings": self.config_warnings(config),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "SilantRAG/1.0"
    protocol_version = "HTTP/1.1"

    api: Api  # устанавливается фабрикой

    # ------------------------------------------------------------------ служебное
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("text/html"):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
            )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"status": "error", "message": message}, status)

    def _client_address(self):
        host = (self.client_address[0] or "").strip()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return None
        mapped = getattr(address, "ipv4_mapped", None)
        return mapped if mapped is not None else address

    def _is_loopback(self) -> bool:
        address = self._client_address()
        return bool(address and address.is_loopback)

    def _admin_allowed(self) -> bool:
        """Право читать и менять настройки.

        Приоритет у явного токена. Без токена правки разрешены только с самого
        сервера: в Docker внешний запрос приходит с адреса bridge-сети и внешне
        неотличим от обращения из локальной сети завода, поэтому доверять
        «приватному» адресу по умолчанию нельзя. Внутрисетевой доступ без
        токена включается осознанно переменной SILANT_ADMIN_ALLOW_LAN=1.
        """
        token = (os.environ.get("SILANT_ADMIN_TOKEN") or "").strip()
        provided = self.headers.get("X-Admin-Token", "")
        if token:
            return hmac.compare_digest(provided, token)
        if self._is_loopback():
            return True
        if (os.environ.get("SILANT_ADMIN_ALLOW_LAN") or "").strip() == "1":
            address = self._client_address()
            return bool(address and address.is_private)
        return False

    def _read_json(self) -> dict | None:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._error(415, "Ожидается Content-Type: application/json")
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "Некорректный Content-Length")
            return None
        if length > MAX_BODY:
            self._error(413, "Слишком большой запрос")
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "Ожидается корректный JSON")
            return None
        if not isinstance(payload, dict):
            self._error(400, "Ожидается JSON-объект")
            return None
        return payload

    def _static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        if not target.is_relative_to(WEB_ROOT) or not target.is_file():
            self._error(404, "Файл не найден")
            return
        content_type, _ = mimetypes.guess_type(str(target))
        if target.suffix in {".html", ".css", ".js", ".svg"}:
            content_type = f"{content_type or 'text/plain'}; charset=utf-8"
        self._send(200, target.read_bytes(), content_type or "application/octet-stream")

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/health":
            self._json({"status": "ok", "stats": self.api.store.stats()})
            return
        if path == "/api/meta":
            self._json(self.api.meta())
            return
        if path == "/api/documents":
            self._json({"documents": self.api.meta()["documents"]})
            return
        if path == "/api/admin/config":
            if not self._admin_allowed():
                self._error(403, "Настройки доступны только локально или по токену X-Admin-Token")
                return
            self._json(self.api.admin_payload())
            return
        if path == "/api/search":
            from urllib.parse import parse_qs

            params = parse_qs(urlparse(self.path).query)
            query = (params.get("q") or [""])[0]
            model = (params.get("model") or [None])[0]
            self._json(self.api.search(query, model))
            return
        self._static(path)

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/admin/config":
            self._handle_admin_save()
            return
        if path != "/api/chat":
            self._error(404, "Неизвестный метод API")
            return

        payload = self._read_json()
        if payload is None:
            return

        message = str(payload.get("message") or payload.get("question") or "").strip()
        model = payload.get("model")
        model = str(model).strip() if model else None
        if not message:
            self._error(400, "Пустой вопрос")
            return
        try:
            response = self.api.chat(message, model)
        except Exception as exc:  # ошибка одного запроса не должна останавливать сервис
            # Подробности пишем в журнал: клиенту не отдаём текст исключения,
            # чтобы не раскрывать внутреннее устройство и пути сервиса.
            self.log_message("ошибка обработки вопроса: %s", exc)
            self._error(500, "Внутренняя ошибка обработки запроса. Подробности в журнале сервера.")
            return
        self._json(response)

    def _handle_admin_save(self) -> None:
        if not self._admin_allowed():
            self._error(403, "Сохранение настроек запрещено: нужен локальный доступ или X-Admin-Token")
            return
        payload = self._read_json()
        if payload is None:
            return
        data = payload.get("config", payload)
        if not isinstance(data, dict):
            self._error(400, "Ожидается {\"config\": {…}}")
            return
        try:
            result = self.api.save_admin(data)
        except ValueError as exc:
            self._error(422, str(exc))
            return
        except OSError as exc:
            self.log_message("не удалось записать настройки: %s", exc)
            self._error(500, "Не удалось записать файл настроек. Подробности в журнале сервера.")
            return
        self.log_message("настройки сохранены в %s", self.api.config.path)
        self._json(result, 200 if result.get("saved") else 422)


def build_server(host: str, port: int, db_path: pathlib.Path) -> ThreadingHTTPServer:
    api = Api(db_path)
    handler = type("BoundHandler", (Handler,), {"api": api})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сервисный ассистент СИЛАНТ (RAG)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", default=None)
    parser.add_argument("--open", action="store_true", help="открыть браузер")
    args = parser.parse_args(argv)

    db_path = pathlib.Path(args.db) if args.db else DEFAULT_DB
    if not db_path.exists():
        print(f"Индекс {db_path} не найден — создаю пустую базу.", file=sys.stderr)
    server = build_server(args.host, args.port, db_path)
    url = f"http://{args.host}:{args.port}/"

    stats = server.RequestHandlerClass.api.store.stats()  # type: ignore[attr-defined]
    print(f"СИЛАНТ · Сервисный ассистент: {url}")
    print(f"Индекс: {db_path} · документов {stats['documents']} · чанков {stats['chunks']}")
    if stats["documents"] == 0:
        print(
            "Внимание: база пуста. Положите PDF в data/documents и выполните "
            "`./deploy.sh --reindex` (в контейнере) либо `python3 -m app.ingest` "
            "(при локальном запуске).",
            file=sys.stderr,
        )

    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
