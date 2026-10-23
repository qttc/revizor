#!/usr/bin/env python3
"""revizor – локальный редактор правок: человек правит документ в браузере, агент забирает правки.

    revizor.py open FILE [--port N] [--no-browser]   сервер до «Завершить ревью», потом сводка в stdout
                                                    FILE – .md/.markdown/.txt или .html/.htm
    revizor.py show FILE [--json]                     правки, которые ждут агента
    revizor.py mark FILE ID... --state applied|rejected [--note TEXT]
    revizor.py wait FILE                              Live Edit: ждать отправленные правки, забрать их и вывести сводку
    revizor.py update [--check [--force]]            обновиться из репо (git pull) и показать, что поменялось
    revizor.py style                                 какой скилл стиля нашёл Ревизор и сколько правил проверки

Правки лежат рядом с документом: report.md → report.review.json. Формат – в docs/technical.md.
Проверка новой версии – раз в сутки при open, выключается REVIZOR_NO_UPDATE_CHECK=1.
Только stdlib, Python 3.9+.
"""
import argparse
import contextlib
import fcntl
import hashlib
import html
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

__version__ = "0.6.0"

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
VENDOR = WEB / "vendor"
JS = "text/javascript; charset=utf-8"
TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
MD_SUFFIXES = {".md", ".markdown"}
HTML_SUFFIXES = {".html", ".htm"}
ASSET_PREFIX = "/_revizor/"
OVERLAY_TAG = f'<script src="{ASSET_PREFIX}overlay.js" defer></script>'
FORMAT_VERSION = 1
RULES_DIR = ROOT / "rules"
SKILLS_DIR = Path.home() / ".claude" / "skills"
CONFIG = Path.home() / ".config" / "revizor" / "config.json"


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sidecar(doc: Path) -> Path:
    return doc.with_name(doc.stem + ".review.json")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def kind(doc: Path) -> str:
    """html – отдаётся как есть, md – Markdown с двумя видами, text – простой текст."""
    suffix = doc.suffix.lower()
    return "html" if suffix in HTML_SUFFIXES else "md" if suffix in MD_SUFFIXES else "text"


def overlay_bundle(with_md: bool = False) -> bytes:
    """Слой правок вместе с Motion. Motion – UMD: в обёртке он не видит AMD и CommonJS страницы
    и не перетирает её window.Motion, а слою достаётся как window.__revizorMotion."""
    motion = (VENDOR / "motion.js").read_text(encoding="utf-8")
    wrapped = (
        "(function(){var define,exports,module;var prev=window.Motion;\n" + motion +
        "\n;window.__revizorMotion=window.Motion;if(prev===undefined){try{delete window.Motion}catch(e){window.Motion=undefined}}"
        "else{window.Motion=prev}}).call(window);\n"
    )
    if with_md:
        # парсер Markdown (mdast + GFM) – в той же обёртке, слою достаётся как window.__revizorMd
        md = (VENDOR / "mdast-gfm.js").read_text(encoding="utf-8")
        wrapped += "(function(){var define,exports,module;\n" + md + "\n;window.__revizorMd=__revizorMd;}).call(window);\n"
    return (wrapped + (WEB / "overlay.js").read_text(encoding="utf-8")).encode("utf-8")


def is_bundle(doc: Path) -> bool:
    """Экспорт-бандл: страница распаковывает себя скриптом, текст слайдов лежит сжатым в __bundler/manifest."""
    return kind(doc) == "html" and "__bundler/manifest" in doc.read_text(encoding="utf-8", errors="replace")


def inject(page: str) -> str:
    m = list(re.finditer(r"</body\s*>", page, re.I))
    return page[:m[-1].start()] + OVERLAY_TAG + page[m[-1].start():] if m else page + OVERLAY_TAG


MD_CSS = """
#revizor-md { max-width: 72ch; margin: 0 auto; padding: 76px 28px 140px; font: 17px/1.65 Charter, "Iowan Old Style", Georgia, serif; }
#revizor-md > :first-child { margin-top: 0; }
#revizor-md h1, #revizor-md h2, #revizor-md h3, #revizor-md h4, #revizor-md h5, #revizor-md h6 {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", sans-serif; line-height: 1.25; margin: 1.7em 0 .5em; text-wrap: balance; }
#revizor-md h1 { font-size: 32px; font-weight: 750; letter-spacing: -.015em; }
#revizor-md h2 { font-size: 24px; font-weight: 700; letter-spacing: -.01em; }
#revizor-md h3 { font-size: 19px; font-weight: 650; }
#revizor-md h4, #revizor-md h5, #revizor-md h6 { font-size: 16px; font-weight: 650; }
#revizor-md p { margin: .8em 0; }
#revizor-md a { color: var(--link); text-decoration: underline; text-decoration-thickness: 1px; text-underline-offset: 3px; }
#revizor-md strong { font-weight: 700; }
#revizor-md del { color: var(--muted); }
#revizor-md code { font: .86em/1.4 ui-monospace, "SF Mono", Menlo, monospace; background: var(--tint); padding: .12em .38em; border-radius: 6px; }
#revizor-md pre { background: var(--tint); border-radius: 14px; padding: 14px 18px; overflow-x: auto; margin: 1.1em 0; }
#revizor-md pre code { background: none; padding: 0; font-size: 13.5px; line-height: 1.6; }
#revizor-md blockquote { margin: 1em 0; padding: 0 0 0 18px; border-left: 2px solid var(--rule); color: var(--muted); }
#revizor-md ul, #revizor-md ol { padding-left: 1.4em; margin: .7em 0; }
#revizor-md li { margin: .12em 0; }
#revizor-md li > ul, #revizor-md li > ol { margin: .15em 0; }
#revizor-md li > p { margin: .3em 0; }
#revizor-md li.task { list-style: none; margin-left: -1.3em; }
#revizor-md li.task input { margin: 0 .55em 0 0; vertical-align: -.1em; accent-color: var(--link); }
#revizor-md .table { overflow-x: auto; margin: 1.2em 0; }
#revizor-md table { border-collapse: collapse; font: 14.5px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; min-width: 60%; }
#revizor-md th, #revizor-md td { padding: 8px 14px 8px 0; border-bottom: 1px solid var(--rule); text-align: left; vertical-align: top; }
#revizor-md th { font-weight: 650; border-bottom-color: var(--ink); }
#revizor-md hr { border: 0; height: 1px; background: var(--rule); margin: 2.2em 0; }
#revizor-md img { max-width: 100%; border-radius: 10px; }
#revizor-md .raw { color: var(--muted); font: 13.5px/1.5 ui-monospace, Menlo, monospace; white-space: pre-wrap; }
#revizor-md .fn { font-size: 14.5px; color: var(--muted); }
html[data-revizor-view="md"] #revizor-root, html[data-revizor-view="plain"] #revizor-md { display: none; }
html[data-revizor-mode="md"] #revizor-root { padding-top: 76px; }
"""


def text_shell(doc: Path) -> str:
    """Текстовый документ как страница. #revizor-root – текст байт в байт (вид «Текст»). У Markdown рядом #revizor-md,
    его заполняет слой правок (вид «Оформление»); <base> нужен, чтобы картинки по относительным путям шли из папки документа."""
    body = html.escape(doc.read_text(encoding="utf-8"), quote=False)
    md = kind(doc) == "md"
    return f"""<!doctype html>
<html lang="ru" data-revizor-mode="{"md" if md else "text"}"{' data-revizor-view="md"' if md else ""}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(doc.name)} · Revizor</title>
{'<base href="/doc/">' if md else ""}
<style>
:root {{ --bg: #f6f5f1; --ink: #1d1c1a; --muted: #5f5c56; --rule: #dcd8ce; --tint: rgba(29, 28, 26, .055); --link: #2457c5; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #161614; --ink: #ecebe6; --muted: #a8a59d; --rule: #3a3833; --tint: rgba(236, 235, 230, .07); --link: #8db0ff; }} }}
html, body {{ margin: 0; background: var(--bg); color: var(--ink); }}
::selection {{ background: color-mix(in srgb, var(--link) 26%, transparent); }}
#revizor-root {{ max-width: 72ch; margin: 0 auto; padding: 40px 24px 120px; white-space: pre-wrap; word-wrap: break-word;
  font: 16px/1.65 Charter, Georgia, serif; }}
{MD_CSS if md else ""}
</style></head><body><main id="revizor-root">{body}</main>{'<article id="revizor-md"></article>' if md else ""}{OVERLAY_TAG}</body></html>
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


LOCKS = Path.home() / ".cache" / "revizor" / "locks"


@contextlib.contextmanager
def locked(doc: Path):
    """Файл правок пишут сервер, `mark` и `wait` – разные процессы. Чтение-изменение-запись идёт под блокировкой,
    иначе Live Edit теряет отметку «агент забрал» между сохранениями страницы. Лок лежит в кэше, а не рядом с документом."""
    LOCKS.mkdir(parents=True, exist_ok=True)
    with open(LOCKS / (sha(str(doc.resolve())) + ".lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def show_text(s: str, doc: Path) -> str:
    """Для сводки: в HTML переносы и отступы вёрстки – шум, в тексте перенос виден как ⏎."""
    if kind(doc) == "html":
        return re.sub(r"\s+", " ", s)
    return s.replace("\n", "⏎")


def line_of(doc: Path, item: dict):
    """Номер строки правки в текстовом документе: агент открывает файл сразу на нужном месте."""
    if kind(doc) == "html" or item.get("frame") or item.get("orphan"):
        return None
    text = doc.read_text(encoding="utf-8")
    start = item.get("start")
    if not isinstance(start, int) or text[start:start + len(item["quote"])] != item["quote"]:
        start = text.find(item["quote"])
    return text.count("\n", 0, start) + 1 if start >= 0 else None


def summary(doc: Path, review: dict, items=None, head=None) -> str:
    if items is None:
        # правки, которые уже забрал `wait`, агент применяет в цикле Live Edit – второй раз их не выдавать
        items = [i for i in pending(review) if not i.get("taken")]
    out = [head or f"Правки к {doc}: {len(items)} ждут агента. Файл: {sidecar(doc)}"]
    busy = [i["id"] for i in pending(review) if i.get("taken")]
    if busy and head is None:
        out.append(f"Ещё применяются в Live Edit: {', '.join(busy)}")
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
            if i.get("lines"):
                a, b = i["lines"]
                out.append(f"  строки: {a}–{b}" if a != b else f"  строка: {a}")
            out.append(f"  блок: {i['selector']}")
            out.append(f"  текст блока: {i['snippet']}")
            out.append(f"  комментарий: {i['comment']}")
            continue
        line = line_of(doc, i)
        out.append(f"[{i['id']}] {'правка' if i['kind'] == 'edit' else 'комментарий'}{where}"
                   + (f" · строка {line}" if line else "")
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
        if i.get("rule"):
            out.append(f"  приём: {i['rule']} – найден проверкой на нейрослоп")
    if items:
        out.append("")
        out.append(f"После применения: revizor.py mark {doc} ID... --state applied (или rejected --note ...)")
    return "\n".join(out)


# ---------- обновления ----------

REPO_URL = "https://github.com/qttc/revizor.git"
UPDATE_CACHE = Path.home() / ".cache" / "revizor" / "update.json"
UPDATE_TTL = 24 * 3600


def parse_version(v: str):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def latest_release(timeout: float = 3.0):
    """Самый свежий тег vX.Y.Z в публичном репо. Ходит только git ls-remote: никаких данных о документах."""
    try:
        out = subprocess.run(["git", "ls-remote", "--tags", "--refs", REPO_URL, "v*"], capture_output=True, text=True,
                             timeout=timeout, env=dict(os.environ, GIT_TERMINAL_PROMPT="0")).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    versions = [v for v in (parse_version(line.rsplit("/", 1)[-1]) for line in out.splitlines()) if v]
    return ".".join(map(str, max(versions))) if versions else None


def check_update(force: bool = False):
    """Есть ли версия новее текущей. Сеть – не чаще раза в сутки, без сети молча None.
    Выключается переменной REVIZOR_NO_UPDATE_CHECK=1."""
    if os.environ.get("REVIZOR_NO_UPDATE_CHECK") == "1":
        return None
    latest = None
    try:
        cached = json.loads(UPDATE_CACHE.read_text(encoding="utf-8"))
        if not force and time.time() - cached.get("checked", 0) < UPDATE_TTL:
            latest = cached.get("latest")
    except (OSError, ValueError):
        cached = None
    if latest is None:
        latest = latest_release()
        if latest:
            try:
                UPDATE_CACHE.parent.mkdir(parents=True, exist_ok=True)
                UPDATE_CACHE.write_text(json.dumps({"checked": time.time(), "latest": latest}), encoding="utf-8")
            except OSError:
                pass
    if latest and parse_version(latest) > parse_version(__version__):
        return {"current": __version__, "latest": latest}
    return None


def changelog_between(old: str, new: str) -> str:
    """Разделы CHANGELOG новее old и не новее new – что поменялось при обновлении."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    parts = re.split(r"(?m)^## \[", text)[1:]
    out = []
    for part in parts:
        ver = parse_version(part.split("]", 1)[0])
        if ver and parse_version(old) < ver <= parse_version(new):
            out.append("## [" + part.rstrip())
    return "\n\n".join(out)


def cmd_update(args) -> int:
    if args.check:
        info = check_update(force=args.force)
        print(f"Доступна {info['latest']}, у тебя {info['current']}." if info else f"Последняя версия: {__version__}.")
        return 0
    git = lambda *a: subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True)
    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        sys.exit(f"Ревизор в {ROOT} поставлен не через git clone. Переставь: git clone {REPO_URL}")
    dirty = [l[3:] for l in git("status", "--porcelain").stdout.splitlines() if l.strip()]
    if dirty:
        sys.exit("Обновление остановлено: в папке Ревизора есть свои изменения – " + ", ".join(dirty))
    old = __version__
    pull = git("pull", "--ff-only")
    if pull.returncode != 0:
        sys.exit("git pull не прошёл:\n" + (pull.stderr or pull.stdout).strip())
    subprocess.run([str(ROOT / "install.sh")], check=False)
    new = re.search(r'__version__ = "([^"]+)"', (ROOT / "revizor.py").read_text(encoding="utf-8")).group(1)
    if new == old:
        print(f"Уже последняя версия: {old}.")
        return 0
    print(f"Ревизор обновлён: {old} → {new}. Открытые редакторы подхватят новую версию после перезапуска open.\n")
    print(changelog_between(old, new))
    return 0


# ---------- скилл стиля ----------
# Настраивать ничего не нужно: Ревизор сам находит скилл, по которому пишет агент. Явный путь – переменная
# REVIZOR_STYLE_SKILL или "style_skill" в ~/.config/revizor/config.json, если автопоиск ошибся.

def _skill_score(d: Path) -> int:
    if (d / "revizor.json").is_file():
        return 100
    score = 0
    refs = d / "references"
    if refs.is_dir() and any(refs.glob("*tells*.md")):
        score += 50
    if (refs / "examples.md").is_file():
        score += 10
    head = (d / "SKILL.md").read_text(encoding="utf-8", errors="ignore")[:1500].lower() if (d / "SKILL.md").is_file() else ""
    if re.search(r"стил|голос|style|voice|writing", head):
        score += 20
    return score


def find_style_skill():
    """Скилл стиля владельца: {"name", "path"} или None. Свой скилл Ревизора в кандидаты не идёт."""
    explicit = os.environ.get("REVIZOR_STYLE_SKILL")
    if not explicit and CONFIG.is_file():
        try:
            explicit = json.loads(CONFIG.read_text(encoding="utf-8")).get("style_skill")
        except (OSError, ValueError):
            explicit = None
    if explicit:
        d = Path(explicit).expanduser()
        return {"name": d.name, "path": str(d)} if d.is_dir() else None
    best, top = None, 0
    for d in sorted(SKILLS_DIR.glob("*")) if SKILLS_DIR.is_dir() else []:
        real = d.resolve()
        if not real.is_dir() or real == (ROOT / "skill").resolve():
            continue
        sc = _skill_score(real)
        if sc > top:
            best, top = d, sc
    return {"name": best.name, "path": str(best.resolve())} if best is not None and top >= 30 else None


def load_rules(skill=None) -> dict:
    """Правила проверки: базовые из rules/ плюс правила скилла (revizor-rules.json). Правило скилла с тем же id
    заменяет базовое, "off": true – выключает его."""
    rules = {r["id"]: dict(r, source="base") for r in json.loads((RULES_DIR / "ru.json").read_text(encoding="utf-8"))["rules"]}
    if skill:
        f = Path(skill["path"]) / "revizor-rules.json"
        if f.is_file():
            try:
                for r in json.loads(f.read_text(encoding="utf-8")).get("rules", []):
                    if r.get("off"):
                        rules.pop(r.get("id"), None)
                    elif r.get("id") and r.get("pattern"):
                        rules[r["id"]] = dict(r, source=skill["name"])
            except (OSError, ValueError):
                pass  # битый файл правил скилла не должен ломать редактор
    return {"skill": skill, "rules": list(rules.values())}


def cmd_style(args) -> int:
    skill = find_style_skill()
    rules = load_rules(skill)["rules"]
    own = sum(1 for r in rules if r["source"] != "base")
    if skill:
        print(f"Скилл стиля: {skill['name']} ({skill['path']}). Правил проверки: {len(rules)}, из них своих в скилле: {own}.")
    else:
        print(f"Скилл стиля не найден, проверка идёт по базовым правилам: {len(rules)}.")
    return 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_handler(doc: Path, done: threading.Event, update=None, skill="auto"):
    doc = doc.resolve()
    lock = threading.Lock()
    if skill == "auto":
        skill = find_style_skill()

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
                if kind(doc) != "html":
                    return self.send(200, text_shell(doc).encode("utf-8"), "text/html; charset=utf-8")
                self.send_response(302)
                self.send_header("Location", "/doc/" + quote(doc.name))
                self.end_headers()
            elif path == ASSET_PREFIX + "overlay.js":
                self.send(200, overlay_bundle(with_md=kind(doc) == "md"), JS)
            elif path.startswith(ASSET_PREFIX + "vendor/") and re.fullmatch(r"[\w.-]+\.woff2", path.rsplit("/", 1)[-1]):
                f = VENDOR / path.rsplit("/", 1)[-1]
                if not f.is_file():
                    return self.send(404, b"not found", "text/plain")
                self.send(200, f.read_bytes(), "font/woff2")
            elif path.startswith("/doc/"):
                # Отчёт и всё, что лежит рядом с ним: картинки, стили, скрипты по относительным путям.
                f = (doc.parent / path[5:]).resolve()
                if f == doc:
                    if kind(doc) != "html":  # текст открывается только через обёртку на /
                        self.send_response(302)
                        self.send_header("Location", "/")
                        return self.end_headers()
                    return self.send(200, inject(doc.read_text(encoding="utf-8")).encode("utf-8"), "text/html; charset=utf-8")
                if doc.parent not in f.parents or not f.is_file() or f.name.endswith(".review.json"):
                    return self.send(404, b"not found", "text/plain")
                self.send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            elif path == "/api/source" and kind(doc) != "html":
                # Live Edit: агент поменял текст – страница перерисовывает его на месте, без перезагрузки
                self.send(200, doc.read_text(encoding="utf-8").encode("utf-8"), "text/plain; charset=utf-8")
            elif path == "/api/rules":
                self.send_json(load_rules(skill))
            elif path == "/api/state":
                text = doc.read_text(encoding="utf-8")
                with lock:
                    review = load(doc)
                self.send_json({"doc": {"name": doc.name, "path": str(doc), "type": kind(doc), "sha": sha(text)},
                                "review": review, "update": update})
            else:
                self.send(404, b"not found", "text/plain")

        def do_POST(self):
            path = self.path.split("?")[0]
            if path not in ("/api/review", "/api/done"):
                return self.send(404, b"not found", "text/plain")
            body = self.read_json()
            with lock, locked(doc):
                review = load(doc)
                # Страница присылает только живые правки; применённые и отклонённые хранит файл.
                # Агент мог закрыть правку, пока вкладка открыта: такую страница больше не воскрешает.
                closed = [i for i in review["items"] if i.get("state", "pending") != "pending"]
                closed_ids = {i["id"] for i in closed}
                # Правку, которую агент уже забрал в Live Edit, страница не меняет и не удаляет: он применяет её прямо сейчас.
                taken = {i["id"]: i for i in review["items"] if i.get("state", "pending") == "pending" and i.get("taken")}
                live = [taken.pop(i["id"], None) or dict(i, state="pending") for i in body.get("items", [])
                        if i.get("state", "pending") == "pending" and i.get("id") not in closed_ids]
                review["items"] = closed + live + list(taken.values())
                if "live" in body:
                    review["live"] = bool(body["live"])
                if isinstance(body.get("dismissed"), list):
                    review["dismissed"] = body["dismissed"]  # «это не нейрослоп» – больше не показывать, и сигнал для обучения скилла
                review["register"] = body.get("register")
                review["register_auto"] = bool(body.get("register_auto"))
                if body.get("view") in ("md", "plain"):
                    review["view"] = body["view"]  # вид Markdown – порт каждый раз новый, localStorage его не удержит
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

    update = check_update()
    if update:
        print(f"Доступна новая версия Ревизора: {update['latest']}, у тебя {update['current']}. "
              f"Обновить: revizor.py update (или агенту – «обнови ревизор»).", file=sys.stderr, flush=True)
    done = threading.Event()
    port = args.port or free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(doc, done, update))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/?v={int(datetime.now().timestamp())}"
    print(f"Редактор: {url}\nПравки: {sidecar(doc)}", file=sys.stderr, flush=True)
    if not args.no_browser:
        subprocess.run(["open", "-a", "Google Chrome", url], check=False)

    try:
        done.wait()
    except KeyboardInterrupt:
        server.shutdown()
        with locked(doc):
            review = load(doc)
            review["status"] = "stopped"  # `wait` в Live Edit видит это и заканчивает цикл
            save(doc, review)
        print("Остановлено без «Завершить ревью». Правки, что успели сохраниться, в " + str(sidecar(doc)), file=sys.stderr)
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
    with locked(doc):
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


def take_sent(doc: Path):
    """Забрать отправленные правки: пометить `taken` и вернуть их. None – ревью закончено, цикл пора остановить."""
    with locked(doc):
        if not sidecar(doc).exists():
            return [], None
        review = load(doc)
        if review.get("status") != "open":
            return None
        fresh = [i for i in pending(review) if i.get("sent") and not i.get("taken")]
        for i in fresh:
            i["taken"] = now()
        if fresh:
            save(doc, review)
        return fresh, review


def cmd_wait(args) -> int:
    """Live Edit: ждёт, пока владелец отправит правку из редактора, и выходит со сводкой. Агент применяет, отмечает
    и запускает `wait` снова. Ревью закончено или сервер остановлен – выход без правок, цикл заканчивается."""
    doc = Path(args.file).expanduser().resolve()
    while True:
        got = take_sent(doc)
        if got is None:
            print(f"Ревью {doc.name} завершено, Live Edit остановлен. Оставшиеся правки – в выводе open.")
            return 0
        fresh, review = got
        if fresh:
            head = (f"Live Edit: {len(fresh)} {'правка' if len(fresh) == 1 else 'правки' if len(fresh) < 5 else 'правок'}"
                    f" к {doc}. Применить, отметить mark и снова запустить wait в фоне.")
            print(summary(doc, review, items=fresh, head=head), flush=True)
            return 0
        time.sleep(args.interval)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"revizor {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("open", help="открыть документ в редакторе и ждать «Завершить ревью»")
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
    p = sub.add_parser("wait", help="Live Edit: ждать правку, отправленную из редактора, и вывести её")
    p.add_argument("file")
    p.add_argument("--interval", type=float, default=1.0)
    p.set_defaults(fn=cmd_wait)
    p = sub.add_parser("style", help="какой скилл стиля нашёл Ревизор")
    p.set_defaults(fn=cmd_style)
    p = sub.add_parser("update", help="обновить Ревизор из репо (git pull) и показать, что поменялось")
    p.add_argument("--check", action="store_true", help="только проверить, есть ли новая версия (кэш на сутки)")
    p.add_argument("--force", action="store_true", help="с --check: спросить репо сейчас, мимо кэша")
    p.set_defaults(fn=cmd_update)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
