#!/usr/bin/env python3
"""revizor – локальный редактор правок: человек правит документ в браузере, агент забирает правки.

    revizor.py open FILE [--port N] [--no-browser]   сервер до кнопки «Готово», потом сводка в stdout
                                                    FILE – .md/.markdown/.txt или .html/.htm
    revizor.py show FILE [--json]                     правки, которые ждут агента
    revizor.py mark FILE ID... --state applied|rejected [--note TEXT]

Правки лежат рядом с документом: report.md → report.review.json. Формат – в README.md.
Только stdlib, Python 3.9+.
"""
import argparse
import hashlib
import html
import json
import mimetypes
import re
import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

__version__ = "0.2.0"

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
VENDOR = WEB / "vendor"
JS = "text/javascript; charset=utf-8"
TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
HTML_SUFFIXES = {".html", ".htm"}
ASSET_PREFIX = "/_revizor/"
OVERLAY_TAG = f'<script src="{ASSET_PREFIX}overlay.js" defer></script>'
FORMAT_VERSION = 1


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sidecar(doc: Path) -> Path:
    return doc.with_name(doc.stem + ".review.json")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def kind(doc: Path) -> str:
    return "html" if doc.suffix.lower() in HTML_SUFFIXES else "text"


def overlay_bundle() -> bytes:
    """Слой правок вместе с Motion. Motion – UMD: в обёртке он не видит AMD и CommonJS страницы
    и не перетирает её window.Motion, а слою достаётся как window.__revizorMotion."""
    motion = (VENDOR / "motion.js").read_text(encoding="utf-8")
    wrapped = (
        "(function(){var define,exports,module;var prev=window.Motion;\n" + motion +
        "\n;window.__revizorMotion=window.Motion;if(prev===undefined){try{delete window.Motion}catch(e){window.Motion=undefined}}"
        "else{window.Motion=prev}}).call(window);\n"
    )
    return (wrapped + (WEB / "overlay.js").read_text(encoding="utf-8")).encode("utf-8")


def is_bundle(doc: Path) -> bool:
    """Экспорт-бандл: страница распаковывает себя скриптом, текст слайдов лежит сжатым в __bundler/manifest."""
    return kind(doc) == "html" and "__bundler/manifest" in doc.read_text(encoding="utf-8", errors="replace")


def inject(page: str) -> str:
    m = list(re.finditer(r"</body\s*>", page, re.I))
    return page[:m[-1].start()] + OVERLAY_TAG + page[m[-1].start():] if m else page + OVERLAY_TAG


def text_shell(doc: Path) -> str:
    """Текстовый документ как страница: текст в #revizor-root байт в байт, слой правок поверх."""
    body = html.escape(doc.read_text(encoding="utf-8"), quote=False)
    return f"""<!doctype html>
<html lang="ru" data-revizor-mode="text"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(doc.name)} · Revizor</title>
<style>
:root {{ --bg: #f6f5f1; --ink: #1d1c1a; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #161614; --ink: #ecebe6; }} }}
html, body {{ margin: 0; background: var(--bg); color: var(--ink); }}
#revizor-root {{ max-width: 72ch; margin: 0 auto; padding: 40px 24px 120px; white-space: pre-wrap; word-wrap: break-word;
  font: 16px/1.65 Charter, Georgia, serif; }}
</style></head><body><main id="revizor-root">{body}</main>{OVERLAY_TAG}</body></html>
"""


def load(doc: Path) -> dict:
    p = sidecar(doc)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {
        "version": FORMAT_VERSION,
        "doc": doc.name,
        "doc_sha": None,
        "register": None,
        "status": "open",
        "created": now(),
        "updated": None,
        "items": [],
    }


def save(doc: Path, review: dict) -> None:
    review["updated"] = now()
    p = sidecar(doc)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def pending(review: dict) -> list:
    return [i for i in review["items"] if i.get("state", "pending") == "pending"]


def show_text(s: str, doc: Path) -> str:
    """Для сводки: в HTML переносы и отступы вёрстки – шум, в тексте перенос виден как ⏎."""
    if kind(doc) == "html":
        return re.sub(r"\s+", " ", s)
    return s.replace("\n", "⏎")


def summary(doc: Path, review: dict) -> str:
    items = pending(review)
    out = [f"Правки к {doc}: {len(items)} ждут агента. Файл: {sidecar(doc)}"]
    if review.get("register"):
        out.append(f"Регистр: {review['register']}")
    if items and is_bundle(doc):
        out.append("Документ – самораспаковывающийся бандл: текст слайдов сжат внутри файла. Правки применять в исходнике"
                   " дека, слайд – в поле раздела (первая часть до « · »).")
    for i in items:
        out.append("")
        where = f" · {i['where']}" if i.get("where") else ""
        if i["scope"] == "doc":
            out.append(f"[{i['id']}] общий комментарий")
            out.append(f"  {i['comment']}")
            continue
        if i["scope"] == "block":
            out.append(f"[{i['id']}] комментарий к блоку{where}")
            out.append(f"  блок: {i['selector']}")
            out.append(f"  текст блока: {i['snippet']}")
            out.append(f"  комментарий: {i['comment']}")
            continue
        out.append(f"[{i['id']}] {'правка' if i['kind'] == 'edit' else 'комментарий'}{where}"
                   + (" (место не найдено, документ менялся)" if i.get("orphan") else ""))
        ctx = show_text(f"…{i.get('prefix', '')}⟦{i['quote']}⟧{i.get('suffix', '')}…", doc)
        out.append(f"  контекст: {ctx}")
        if i["kind"] == "edit":
            out.append(f"  было:  {show_text(i['quote'], doc)}")
            out.append(f"  стало: {i['replacement']}")
            if i.get("why"):
                out.append(f"  почему: {i['why']}")
        else:
            out.append(f"  комментарий: {i['comment']}")
    if items:
        out.append("")
        out.append(f"После применения: revizor.py mark {doc} ID... --state applied (или rejected --note ...)")
    return "\n".join(out)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_handler(doc: Path, done: threading.Event):
    doc = doc.resolve()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, data, code=200):
            self.send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                      "application/json; charset=utf-8")

        def read_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

        def do_GET(self):
            path = unquote(self.path.split("?")[0])
            if path == "/":
                if kind(doc) == "text":
                    return self.send(200, text_shell(doc).encode("utf-8"), "text/html; charset=utf-8")
                self.send_response(302)
                self.send_header("Location", "/doc/" + quote(doc.name))
                self.end_headers()
            elif path == ASSET_PREFIX + "overlay.js":
                self.send(200, overlay_bundle(), JS)
            elif path.startswith(ASSET_PREFIX + "vendor/") and re.fullmatch(r"[\w.-]+\.woff2", path.rsplit("/", 1)[-1]):
                f = VENDOR / path.rsplit("/", 1)[-1]
                if not f.is_file():
                    return self.send(404, b"not found", "text/plain")
                self.send(200, f.read_bytes(), "font/woff2")
            elif path.startswith("/doc/"):
                # Отчёт и всё, что лежит рядом с ним: картинки, стили, скрипты по относительным путям.
                f = (doc.parent / path[5:]).resolve()
                if f == doc:
                    return self.send(200, inject(doc.read_text(encoding="utf-8")).encode("utf-8"), "text/html; charset=utf-8")
                if doc.parent not in f.parents or not f.is_file() or f.name.endswith(".review.json"):
                    return self.send(404, b"not found", "text/plain")
                self.send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            elif path == "/api/state":
                text = doc.read_text(encoding="utf-8")
                with lock:
                    review = load(doc)
                self.send_json({"doc": {"name": doc.name, "path": str(doc), "type": kind(doc), "sha": sha(text)},
                                "review": review})
            else:
                self.send(404, b"not found", "text/plain")

        def do_POST(self):
            path = self.path.split("?")[0]
            if path not in ("/api/review", "/api/done"):
                return self.send(404, b"not found", "text/plain")
            body = self.read_json()
            with lock:
                review = load(doc)
                # Страница присылает только живые правки; применённые и отклонённые хранит файл.
                # Агент мог закрыть правку, пока вкладка открыта: такую страница больше не воскрешает.
                closed = [i for i in review["items"] if i.get("state", "pending") != "pending"]
                closed_ids = {i["id"] for i in closed}
                live = [dict(i, state="pending") for i in body.get("items", [])
                        if i.get("state", "pending") == "pending" and i.get("id") not in closed_ids]
                review["items"] = closed + live
                review["register"] = body.get("register")
                review["register_auto"] = bool(body.get("register_auto"))
                review["doc_sha"] = sha(doc.read_text(encoding="utf-8"))
                review["status"] = "done" if path == "/api/done" else "open"
                save(doc, review)
            self.send_json({"ok": True, "updated": review["updated"], "closed": closed})
            if path == "/api/done":
                done.set()

    return Handler


def cmd_open(args) -> int:
    doc = Path(args.file).expanduser().resolve()
    if not doc.is_file():
        sys.exit(f"нет файла: {doc}")
    if doc.suffix.lower() not in TEXT_SUFFIXES | HTML_SUFFIXES:
        sys.exit(f"поддерживаются {', '.join(sorted(TEXT_SUFFIXES | HTML_SUFFIXES))}: {doc.name}")
    review = load(doc)
    if review["status"] == "done" and pending(review):
        print(f"В {sidecar(doc).name} есть {len(pending(review))} неприменённых правок, они откроются в редакторе.",
              file=sys.stderr)
    if review["status"] != "open":
        review["status"] = "open"
        save(doc, review)

    done = threading.Event()
    port = args.port or free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(doc, done))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/?v={int(datetime.now().timestamp())}"
    print(f"Редактор: {url}\nПравки: {sidecar(doc)}", file=sys.stderr, flush=True)
    if not args.no_browser:
        subprocess.run(["open", "-a", "Google Chrome", url], check=False)

    try:
        done.wait()
    except KeyboardInterrupt:
        server.shutdown()
        print("Остановлено без «Готово». Правки, что успели сохраниться, в " + str(sidecar(doc)), file=sys.stderr)
        return 130
    server.shutdown()
    print(summary(doc, load(doc)))
    return 0


def cmd_show(args) -> int:
    doc = Path(args.file).expanduser().resolve()
    review = load(doc)
    if args.json:
        print(json.dumps(pending(review), ensure_ascii=False, indent=2))
    else:
        print(summary(doc, review))
    return 0


def cmd_mark(args) -> int:
    doc = Path(args.file).expanduser().resolve()
    review = load(doc)
    known = {i["id"]: i for i in review["items"]}
    missing = [i for i in args.ids if i not in known]
    if missing:
        sys.exit(f"нет правок с id: {', '.join(missing)}")
    for i in args.ids:
        known[i]["state"] = args.state
        known[i]["resolved"] = now()
        if args.note:
            known[i]["note"] = args.note
    save(doc, review)
    print(f"{len(args.ids)} → {args.state}. Ждут агента: {len(pending(review))}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"revizor {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("open", help="открыть документ в редакторе и ждать «Готово»")
    p.add_argument("file")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_open)
    p = sub.add_parser("show", help="правки, которые ждут агента")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("mark", help="отметить правки применёнными или отклонёнными")
    p.add_argument("file")
    p.add_argument("ids", nargs="+")
    p.add_argument("--state", choices=["applied", "rejected"], required=True)
    p.add_argument("--note")
    p.set_defaults(fn=cmd_mark)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
