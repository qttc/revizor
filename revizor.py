#!/usr/bin/env python3
"""revizor – локальный редактор правок: человек правит документ в браузере, агент забирает правки.

    revizor.py open FILE [--port N] [--no-browser]   сервер до «Завершить ревью», потом сводка в stdout
                                                    FILE – .md/.markdown/.txt или .html/.htm
    revizor.py show FILE [--json]                     правки, которые ждут агента
    revizor.py mark FILE ID... --state applied|rejected [--note TEXT]
    revizor.py wait FILE                              ждать работу из редактора: правки Live Edit и проверку на нейрослоп
    revizor.py suggest FILE FINDINGS.json            записать находки смысловой проверки на нейрослоп
    revizor.py update [--check [--force]]            обновиться из репо (git pull) и показать, что поменялось
    revizor.py style [init]                          какой скилл стиля нашёл Ревизор; init – создать из шаблона
    revizor.py pairs [--ack]                         новые пары «было → стало» для обучения скилла стиля

Правки лежат рядом с документом: report.md → report.review.json. Формат – в docs/technical.md.
Проверка новой версии – раз в сутки при open, выключается REVIZOR_NO_UPDATE_CHECK=1.
Только stdlib, Python 3.9+.
"""
import argparse
import shutil
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

__version__ = "0.9.0"

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
TEMPLATE = ROOT / "templates" / "style-skill"
CACHE = Path.home() / ".cache" / "revizor"
REGISTRY = CACHE / "docs.json"      # какие документы открывались в Ревизоре – отсюда берутся пары для обучения
BATCH = CACHE / "pairs-batch.json"  # последняя выданная агенту порция пар, `pairs --ack` переносит её в журнал скилла
SKILLS_DIR = Path.home() / ".claude" / "skills"
CONFIG = Path.home() / ".config" / "revizor" / "config.json"


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sidecar(doc: Path) -> Path:
    """Файл правок рядом с документом: report.md → report.review.json. Рядом есть документ с тем же именем и другим
    расширением (report.md и его экспорт report.html) – у каждого свой: report.md.review.json. Файл до 1.0, который уже
    принадлежит этому документу, остаётся на месте."""
    short, full = doc.with_name(doc.stem + ".review.json"), doc.with_name(doc.name + ".review.json")
    if full.exists():
        return full
    if short.exists():
        try:
            owner = json.loads(short.read_text(encoding="utf-8")).get("doc")
        except (OSError, ValueError, AttributeError):
            owner = None
        return short if owner in (None, doc.name) else full
    twins = [doc.with_suffix(x) for x in TEXT_SUFFIXES | HTML_SUFFIXES if x != doc.suffix.lower()]
    return full if any(t.exists() for t in twins) else short


def is_sidecar(name: str) -> bool:
    n = name.lower()  # файловая система macOS регистр не различает: REPORT.REVIEW.JSON – тот же файл правок
    return n.endswith(".review.json") or n.endswith(".review.json.tmp")


class BadRequest(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


class ReviewError(Exception):
    """Файл правок не читается. Перезаписывать его нельзя: это история правок владельца."""


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


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
        try:
            review = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ReviewError(f"файл правок {p} не читается ({e}). Почини его или переименуй, тогда начнётся новый.") from e
        if not isinstance(review, dict) or not isinstance(review.get("items"), list):
            raise ReviewError(f"файл правок {p} – не формат Ревизора: нет списка items. Переименуй его, тогда начнётся новый.")
        return review
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
    atomic_write(sidecar(doc), json.dumps(review, ensure_ascii=False, indent=2) + "\n")


def pending(review: dict) -> list:
    return [i for i in review["items"] if i.get("state", "pending") == "pending"]


LOCKS = CACHE / "locks"
TEXTS = CACHE / "text"  # текст страницы для смысловой проверки: у бандла и HTML его видит только страница
REOPEN_WAIT = 4  # с: вкладка опрашивает сервер раз в 1,5 с
WAIT_GRACE = 10  # с: столько wait ждёт, что завершённое ревью откроют снова
CTX = 32
MAX_BODY = 10 * 1024 * 1024  # байт: больше страница не шлёт даже с текстом большого дека


def text_cache(doc: Path) -> Path:
    return TEXTS / (sha(str(doc.resolve())) + ".json")


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
    print(f"Ревизор обновлён: {old} → {new}. Открытые редакторы подхватят новую версию после перезапуска open.")
    # агент держит в чате текст скилла, загруженный до обновления: без этого он продолжит по старым инструкциям
    print(f"Агенту: перечитай {ROOT / 'skill' / 'SKILL.md'} целиком и дальше работай по нему.\n")
    print(changelog_between(old, new))
    return 0


# ---------- скилл стиля ----------
# Настраивать ничего не нужно: Ревизор сам находит скилл, по которому пишет агент. Явный путь – переменная
# REVIZOR_STYLE_SKILL или "style_skill" в ~/.config/revizor/config.json, если автопоиск ошибся.
# Скиллы под разные языки: "style_skill": {"ru": "...", "en": "..."}.

def text_lang(text: str) -> str:
    """Язык текста по преобладающему алфавиту: кириллица – ru, латиница – en."""
    cyr = len(re.findall(r"[а-яё]", text, re.I))
    return "ru" if cyr >= len(re.findall(r"[a-z]", text, re.I)) else "en"


def doc_lang(doc: Path) -> str:
    raw = doc.read_text(encoding="utf-8", errors="replace")
    if kind(doc) == "html":  # скрипты и стили не текст, в бандле там ещё и сжатые слайды латиницей
        raw = re.sub(r"<(script|style)\b.*?</\1>|<[^>]+>", " ", raw, flags=re.S | re.I)
    return text_lang(raw)


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


def config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.is_file() else {}
    except (OSError, ValueError):
        return {}


def find_style_skill(min_score: int = 30, lang=None):
    """Скилл стиля владельца: {"name", "path"} или None. Свой скилл Ревизора в кандидаты не идёт.
    min_score 20 – «слабый» скилл: только SKILL.md про стиль, без каталога и примеров.
    lang – язык документа: style_skill по языкам выбирает свой скилл, языка в нём нет – автопоиск."""
    explicit = os.environ.get("REVIZOR_STYLE_SKILL") or config().get("style_skill")
    taken = set()  # скиллы, заданные под другие языки, автопоиск не берёт
    if isinstance(explicit, dict):
        taken = {Path(v).expanduser().resolve() for k, v in explicit.items() if k != (lang or "ru") and isinstance(v, str)}
        explicit = explicit.get(lang or "ru")
    if explicit:
        d = Path(explicit).expanduser()
        return {"name": d.name, "path": str(d)} if d.is_dir() else None
    best, top = None, 0
    for d in sorted(SKILLS_DIR.glob("*")) if SKILLS_DIR.is_dir() else []:
        real = d.resolve()
        if not real.is_dir() or real == (ROOT / "skill").resolve() or real in taken:
            continue
        sc = _skill_score(real)
        if sc > top:
            best, top = d, sc
    return {"name": best.name, "path": str(best.resolve())} if best is not None and top >= min_score else None


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


def skill_files(skill: dict, lang: str = "ru") -> dict:
    """Роли файлов скилла стиля для языка документа. revizor.json задаёт их явно – путём или по языкам
    ({"ru": "...", "en": "..."}), иначе – раскладка как у канона стиля: references/examples.md, каталог примет
    references/*tells-<lang>*.md (или общий *tells*.md без языка), голос references/voice.md. Файла может ещё не быть –
    его создаст обучение."""
    d = Path(skill["path"])
    manifest = {}
    if (d / "revizor.json").is_file():
        try:
            manifest = json.loads((d / "revizor.json").read_text(encoding="utf-8")).get("files", {})
        except (OSError, ValueError):
            manifest = {}
    tells = sorted(d.glob(f"references/*tells-{lang}*.md")) or \
        [p for p in sorted(d.glob("references/*tells*.md")) if not re.search(r"tells-[a-z]{2}(?![a-z])", p.name)]
    roles = {"examples": "references/examples.md",
             "catalog": str(tells[0].relative_to(d)) if tells else f"references/ai-tells-{lang}.md",
             "voice": "references/voice.md", "rules": "revizor-rules.json", "ledger": "revizor-learned.json"}
    for k, v in manifest.items():
        v = v.get(lang) if isinstance(v, dict) else v
        if isinstance(v, str):
            roles[k] = v
    return {k: str(d / v) for k, v in roles.items()}


def init_style_skill(name=None) -> dict:
    """Скилл стиля из шаблона – когда своего нет. Имя – первое свободное: writing-style, revizor-style."""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    for cand in ([name] if name else ["writing-style", "revizor-style"]):
        d = SKILLS_DIR / cand
        if not d.exists():
            break
    else:
        sys.exit(f"скилл {cand} уже есть: {d}")
    shutil.copytree(TEMPLATE, d)
    shutil.copy(RULES_DIR / "semantic-ru.md", d / "references" / "ai-tells-ru.md")  # каталог стартует с базового, дальше растёт из правок
    md = d / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8").replace("{{name}}", d.name), encoding="utf-8")
    return {"name": d.name, "path": str(d)}


def cmd_style(args) -> int:
    if args.action == "init":
        skill = find_style_skill()
        if skill and not args.force:
            print(f"Скилл стиля уже есть: {skill['name']} ({skill['path']}).")
            return 0
        skill = init_style_skill(args.name)
        print(f"Создан скилл стиля {skill['name']}: {skill['path']}. Ревизор будет учить его на твоих правках.")
        return 0
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


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # как у HTTPServer: TIME_WAIT от прошлого сервера не мешает
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def last_port(doc: Path, port=None):
    """Порт прошлого сервера этого документа. Повторный `open` встаёт на него, и старая вкладка подхватывает новый сервер."""
    f = CACHE / "ports.json"
    try:
        ports = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ports = {}
    key = sha(str(doc))
    if port is None:
        return ports.get(key)
    ports[key] = port
    f.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(f, json.dumps(ports))


def make_handler(doc: Path, done: threading.Event, update=None, skill="auto", seen=None):
    doc = doc.resolve()
    lock = threading.Lock()
    if skill == "auto":
        skill = find_style_skill(lang=doc_lang(doc))

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

        def read_json(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise BadRequest(400, "Content-Length – не число")
            if n < 0 or n > MAX_BODY:
                raise BadRequest(413, f"тело запроса больше {MAX_BODY // 1024 // 1024} МБ")
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except ValueError:
                raise BadRequest(400, "тело запроса – не JSON")
            if not isinstance(body, dict):
                raise BadRequest(400, "тело запроса – не объект JSON")
            return body

        def allowed(self, post=False) -> bool:
            """Только своя вкладка. Host закрывает DNS-rebinding, Origin, Sec-Fetch-Site и тип тела – запросы с чужих сайтов:
            простой POST с text/plain браузер шлёт на 127.0.0.1 без предварительной проверки CORS."""
            port = self.server.server_address[1]
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in hosts:
                return False
            if not post:
                return True
            origin, site = self.headers.get("Origin"), self.headers.get("Sec-Fetch-Site")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            return ctype == "application/json" and (origin is None or origin in {f"http://{h}" for h in hosts}) \
                and site in (None, "same-origin")

        def do_GET(self):
            self.guarded(self.get, post=False)

        def do_POST(self):
            self.guarded(self.post, post=True)

        def guarded(self, fn, post):
            if not self.allowed(post):
                return self.send(403, b"forbidden", "text/plain")
            try:
                fn()
            except BadRequest as e:
                self.send_json({"ok": False, "error": e.msg}, e.code)
            except ReviewError as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
            except (BrokenPipeError, ConnectionResetError):
                pass  # вкладку закрыли посреди ответа
            except Exception as e:  # сервер живёт, пока открыт редактор: одна кривая просьба не должна его ронять
                print(f"revizor: ошибка на {self.command} {self.path}: {e!r}", file=sys.stderr)
                try:
                    self.send_json({"ok": False, "error": "внутренняя ошибка Ревизора, подробности в выводе open"}, 500)
                except OSError:
                    pass

        def get(self):
            if seen:
                seen.set()
            path = unquote(self.path.split("?")[0])
            if "\x00" in path:
                raise BadRequest(400, "в пути нулевой байт")
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
                rel = path[5:]
                if any(part.startswith(".") for part in Path(rel).parts):  # .git, .env и прочее скрытое не отдаётся
                    return self.send(404, b"not found", "text/plain")
                f = (doc.parent / rel).resolve()
                if f == doc:
                    if kind(doc) != "html":  # текст открывается только через обёртку на /
                        self.send_response(302)
                        self.send_header("Location", "/")
                        return self.end_headers()
                    return self.send(200, inject(doc.read_text(encoding="utf-8")).encode("utf-8"), "text/html; charset=utf-8")
                if doc.parent not in f.parents or not f.is_file() or is_sidecar(f.name):
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

        def post(self):
            path = self.path.split("?")[0]
            if path == "/api/scan":
                return self.request_scan(self.read_json())
            if path not in ("/api/review", "/api/done"):
                return self.send(404, b"not found", "text/plain")
            body = self.read_json()
            items = body.get("items", [])
            if not isinstance(items, list) or not all(isinstance(i, dict) and isinstance(i.get("id"), str) for i in items):
                raise BadRequest(400, "items – список правок, у каждой строковый id")
            with lock, locked(doc):
                review = load(doc)
                # Страница присылает только живые правки; применённые и отклонённые хранит файл.
                # Агент мог закрыть правку, пока вкладка открыта: такую страница больше не воскрешает.
                closed = [i for i in review["items"] if i.get("state", "pending") != "pending"]
                closed_ids = {i["id"] for i in closed}
                # Правку, которую агент уже забрал в Live Edit, страница не меняет и не удаляет: он применяет её прямо сейчас.
                taken = {i["id"]: i for i in review["items"] if i.get("state", "pending") == "pending" and i.get("taken")}
                live = [taken.pop(i["id"], None) or dict(i, state="pending") for i in items
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

        def request_scan(self, body):
            """Страница прислала свой текст: агент в цикле `wait` прочитает его и найдёт смысловые приметы нейрослопа."""
            roots = body.get("roots")
            roots = [r for r in roots if isinstance(r, dict) and isinstance(r.get("text"), str)] if isinstance(roots, list) else []
            if not roots:
                return self.send_json({"ok": False, "error": "нет текста"}, 400)
            TEXTS.mkdir(parents=True, exist_ok=True)
            atomic_write(text_cache(doc), json.dumps({"doc": str(doc), "roots": roots}, ensure_ascii=False))
            with lock, locked(doc):
                review = load(doc)
                review["scan"] = {"state": "requested", "requested": now(), "sha": sha(doc.read_text(encoding="utf-8"))}
                save(doc, review)
            self.send_json({"ok": True, "scan": review["scan"]})

    return Handler


def remember_doc(doc: Path) -> None:
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        docs = json.loads(REGISTRY.read_text(encoding="utf-8")) if REGISTRY.is_file() else []
        if str(doc) not in docs:
            atomic_write(REGISTRY, json.dumps(docs + [str(doc)], ensure_ascii=False, indent=1))
    except (OSError, ValueError):
        pass  # реестр – удобство для обучения, редактору он не нужен


def known_sidecars(extra=()) -> list:
    """Файлы правок для обучения: из реестра открытых документов, плюс разовый поиск Spotlight – чтобы подхватить ревью,
    сделанные до появления реестра. Без Spotlight – только реестр."""
    found = set()
    if REGISTRY.is_file():
        try:
            found |= {str(sidecar(Path(d))) for d in json.loads(REGISTRY.read_text(encoding="utf-8"))}
        except (OSError, ValueError):
            pass
    if shutil.which("mdfind"):
        try:
            out = subprocess.run(["mdfind", "-name", ".review.json"], capture_output=True, text=True, timeout=10).stdout
            found |= {l for l in out.splitlines() if l.endswith(".review.json")}
        except (OSError, subprocess.SubprocessError):
            pass
    for d in extra:
        found |= {str(p) for p in Path(d).expanduser().rglob("*.review.json")}
    return sorted(f for f in found if Path(f).is_file())


def collect_pairs(ledger: dict, extra=()) -> dict:
    """Новые сигналы для обучения: правки владельца (пары), отметки «это не нейрослоп», принятые находки проверки."""
    seen = set(ledger.get("seen", []))
    pairs, dismissed, confirmed = [], [], []
    for f in known_sidecars(extra):
        try:
            review = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for i in review.get("items", []):
            key = sha(f"{f}#{i.get('id')}")  # в журнале скилла – хэш: пути к рабочим документам туда не попадают
            if key in seen or i.get("state") != "applied" or i.get("kind") != "edit":
                continue
            before, after = i.get("quote", ""), i.get("replacement", "")
            if re.sub(r"\s+", " ", before).strip() == re.sub(r"\s+", " ", after).strip():
                continue
            entry = {"key": key, "doc": review.get("doc"), "register": review.get("register"), "lang": text_lang(f"{before} {after}"),
                     "before": before, "after": after, "why": i.get("why") or "", "where": i.get("where") or ""}
            if i.get("rule"):
                entry["rule"], entry["source"] = i["rule"], i.get("source", "radar")
            # чистая механика из проверки («Исправить» без ручной правки) – подтверждение правила, а не новый пример
            (confirmed if i.get("source") == "radar" and not i.get("why") else pairs).append(entry)
        for d in review.get("dismissed", []):
            key = sha(f"{f}#x:{d.get('rule')}:{d.get('prefix', '')}{d.get('quote', '')}")
            if key not in seen:
                dismissed.append({"key": key, "doc": review.get("doc"), "rule": d.get("rule"), "quote": d.get("quote"), "before": d.get("prefix"),
                                  "lang": text_lang(f"{d.get('prefix', '')}{d.get('quote', '')}")})
    return {"pairs": pairs, "dismissed": dismissed, "confirmed": confirmed}


def learn_allowed() -> bool:
    """learn_from в конфиге – обучать скилл только из этой папки (у автора – хаб: пространства пишут только в себя).
    Нет настройки – обучение откуда угодно."""
    root = config().get("learn_from")
    if not root:
        return True
    cwd, base = Path.cwd().resolve(), Path(root).expanduser().resolve()
    return cwd == base or base in cwd.parents


LANGS = ("ru", "en")


def lang_skill(lang=None):
    """Скилл стиля для языка и создан ли он сейчас. Нет своего – слабый (только SKILL.md про стиль), нет и его – из шаблона."""
    skill = find_style_skill(lang=lang) or find_style_skill(min_score=20, lang=lang)
    return (skill, False) if skill else (init_style_skill(), True)


def read_ledger(path: Path) -> dict:
    try:
        ledger = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"seen": [], "stats": {}}
    except ValueError as e:
        sys.exit(f"журнал обучения {path} не читается ({e}). Почини его: без журнала разобранные пары придут снова.")
    # журналы до 0.8.0 хранили путь#id – перевести в хэши
    ledger["seen"] = [k if re.fullmatch(r"[0-9a-f]{16}", k) else sha(k) for k in ledger.get("seen", [])]
    return ledger


def cmd_pairs(args) -> int:
    """Порция для обучения скилла стиля. Нет скилла – подключается слабый (только SKILL.md про стиль) или создаётся
    из шаблона: настраивать ничего не нужно."""
    if not learn_allowed():
        where = config().get("learn_from")
        if args.count:
            print(0)
        print(f"Обучение скилла стиля запускается из {where} (learn_from в {CONFIG}). Пары копятся, их заберёт обучение оттуда.",
              file=sys.stderr)
        return 0 if args.count else 3
    skill, created = lang_skill()
    # скиллы по языкам могут быть разными: журнал у каждого свой, разобранное ищется по всем
    skills = {l: find_style_skill(lang=l) or find_style_skill(min_score=20, lang=l) or skill for l in LANGS}
    ledgers = {}
    for l, sk in skills.items():
        path = Path(skill_files(sk, l)["ledger"])
        ledgers.setdefault(str(path), read_ledger(path))
    if args.ack:
        if not BATCH.is_file():
            print("Нечего отмечать: порции нет. Сначала revizor.py pairs.")
            return 1
        batch = json.loads(BATCH.read_text(encoding="utf-8"))
        touched = set()
        for kind, field in (("pairs", "taken"), ("confirmed", "taken"), ("dismissed", "dismissed")):
            for e in batch.get(kind, []):
                path = skill_files(skills.get(e.get("lang"), skill), e.get("lang") or "ru")["ledger"]
                ledger = ledgers.setdefault(path, read_ledger(Path(path)))
                touched.add(path)
                for rule in filter(None, (e.get("rule") or "").split(",")):
                    ledger.setdefault("stats", {}).setdefault(rule, {"taken": 0, "dismissed": 0})[field] += 1
                ledger["seen"] = sorted(set(ledger.get("seen", [])) | {e["key"]})
        for path in touched:
            ledgers[path]["updated"] = now()
            atomic_write(Path(path), json.dumps(ledgers[path], ensure_ascii=False, indent=1) + "\n")
        BATCH.unlink()
        names = ", ".join(sorted({sk["name"] for l, sk in skills.items()
                                  if skill_files(sk, l)["ledger"] in touched})) or skill["name"]
        print(f"В журнал скилла {names}: {sum(len(batch.get(k, [])) for k in ('pairs', 'dismissed', 'confirmed'))} записей.")
        return 0
    got = collect_pairs({"seen": sorted({k for lg in ledgers.values() for k in lg.get("seen", [])})}, args.dir or ())
    total = len(got["pairs"]) + len(got["dismissed"]) + len(got["confirmed"])
    if not args.count and total > args.max:  # первая порция после обновления бывает большой: остальное – в следующий раз
        left = args.max
        for k in ("pairs", "dismissed", "confirmed"):
            got[k], left = got[k][:left], max(0, left - len(got[k][:left]))
    present = [e.get("lang", "ru") for k in ("pairs", "dismissed", "confirmed") for e in got[k]]
    main_lang = max(sorted(set(present)), key=present.count) if present else "ru"
    langs = {}
    for l in sorted(set(present)):
        files = skill_files(skills[l], l)
        langs[l] = {"skill": skills[l], "files": files, "stats": ledgers[files["ledger"]].get("stats", {})}
    files = skill_files(skills[main_lang], main_lang)
    out = {"skill": skills[main_lang], "created": created, "files": files, "langs": langs,
           "playbook": str(ROOT / "skill" / "learn.md"), "stats": ledgers[files["ledger"]].get("stats", {}), **got}
    CACHE.mkdir(parents=True, exist_ok=True)
    atomic_write(BATCH, json.dumps(got, ensure_ascii=False))
    if args.count:
        print(len(got["pairs"]) + len(got["dismissed"]))
    else:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_open(args) -> int:
    doc = Path(args.file).expanduser().resolve()
    if not doc.is_file():
        sys.exit(f"нет файла: {doc}")
    if doc.suffix.lower() not in TEXT_SUFFIXES | HTML_SUFFIXES:
        sys.exit(f"поддерживаются {', '.join(sorted(TEXT_SUFFIXES | HTML_SUFFIXES))}: {doc.name}")
    try:
        doc.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        sys.exit(f"{doc.name} не в UTF-8. Пересохрани его в UTF-8 и открой снова.")
    remember_doc(doc)
    with locked(doc):
        review = load(doc)
        reopen = review["status"] == "done"
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
    done, seen = threading.Event(), threading.Event()
    prev = last_port(doc)
    port = args.port or (prev if prev and port_free(prev) else free_port())
    last_port(doc, port)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(doc, done, update, seen=seen))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/?v={int(datetime.now().timestamp())}"
    print(f"Редактор: {url}\nПравки: {sidecar(doc)}", file=sys.stderr, flush=True)
    # После «Завершить ревью» вкладка ждёт сервер на старом порту и сама перезагружается.
    # Вкладку закрыли – через пару секунд тишины открываем новую.
    if reopen and port == prev and seen.wait(REOPEN_WAIT):
        print("Вкладка с прошлого ревью подхватила редактор.", file=sys.stderr, flush=True)
    elif not args.no_browser:
        subprocess.run(["open", "-a", "Google Chrome", url], check=False)

    try:
        done.wait()
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()
        with locked(doc):
            review = load(doc)
            review["status"] = "stopped"  # `wait` в Live Edit видит это и заканчивает цикл
            save(doc, review)
        print("Остановлено без «Завершить ревью». Правки, что успели сохраниться, в " + str(sidecar(doc)), file=sys.stderr)
        return 130
    server.shutdown()
    server.server_close()  # порт свободен сразу: следующий `open` встанет на него
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
    """Забрать работу: запрос проверки или отправленные правки, пометить взятыми и вернуть. None – ревью закончено."""
    with locked(doc):
        if not sidecar(doc).exists():
            return [], None
        review = load(doc)
        if review.get("status") != "open":
            return None
        scan = review.get("scan") or {}
        if scan.get("state") == "requested":
            scan["state"], scan["taken"] = "taken", now()
            save(doc, review)
            return "scan", review
        fresh = [i for i in pending(review) if i.get("sent") and not i.get("taken")]
        for i in fresh:
            i["taken"] = now()
        if fresh:
            save(doc, review)
        return fresh, review


def scan_task(doc: Path, review: dict) -> str:
    """Задание агенту на смысловую проверку: где текст, по какому каталогу искать, что уже поймали регулярки."""
    try:
        roots = json.loads(text_cache(doc).read_text(encoding="utf-8"))["roots"]
        lang = text_lang(" ".join(r.get("text", "") for r in roots))
    except (OSError, ValueError, KeyError):
        lang = doc_lang(doc)
    skill = find_style_skill(lang=lang)
    catalog = Path(skill_files(skill, lang)["catalog"]) if skill else None
    if catalog and catalog.is_file():
        where = f" (скилл {skill['name']})"
    else:
        catalog = RULES_DIR / f"semantic-{lang}.md"
        where = " (базовый Ревизора)"
        if not catalog.is_file():
            catalog, where = RULES_DIR / "semantic-ru.md", f" (базовый Ревизора, на русском: каталога на {lang} нет, приметы искать по смыслу)"
    base = ", ".join(r["id"] for r in load_rules(skill)["rules"])
    return "\n".join([
        f"Проверка на нейрослоп: {doc}",
        f"Текст страницы: {text_cache(doc)} (roots[].key – корень: \"\" страница, иначе фрейм слайда)",
        f"Язык: {lang}",
        f"Каталог примет: {catalog}{where}",
        f"Регистр: {review.get('register') or 'определить по тексту'}",
        f"Уже ловят регулярки, не повторять: {base}",
        "Найти смысловые приметы по разделу «Проверка на нейрослоп» скилла revizor, записать: "
        f"revizor.py suggest {doc} <findings.json>, потом снова запустить wait в фоне.",
    ])


def cmd_suggest(args) -> int:
    """Находки агента → файл правок. Цитата обязана быть точной подстрокой текста страницы, иначе подсветить нечего."""
    doc = Path(args.file).expanduser().resolve()
    raw = sys.stdin.read() if args.findings == "-" else Path(args.findings).expanduser().read_text(encoding="utf-8")
    found = json.loads(raw)
    found = found.get("findings", []) if isinstance(found, dict) else found
    cache = json.loads(text_cache(doc).read_text(encoding="utf-8")) if text_cache(doc).is_file() else {"roots": []}
    roots = {r.get("key", ""): r["text"] for r in cache["roots"]}
    if not roots and kind(doc) != "html":
        roots = {"": doc.read_text(encoding="utf-8")}
    out, missed, used = [], [], set()
    for n, f in enumerate(found):
        quote, key = (f.get("quote") or "").strip(), f.get("root", "")
        text = roots.get(key)
        pos = -1
        if text and quote:
            pos = text.find(quote)
            while pos != -1 and (key, pos) in used:
                pos = text.find(quote, pos + 1)
        if pos == -1:
            missed.append(quote[:40] or f"#{n}")
            continue
        used.add((key, pos))
        item = {"id": f"f{n}", "quote": quote, "start": pos, "end": pos + len(quote),
                "prefix": text[max(0, pos - CTX):pos], "suffix": text[pos + len(quote):pos + len(quote) + CTX],
                "rule": f.get("rule") or "agent", "title": f.get("title") or "Нейрослоп", "why": f.get("why") or "",
                "rewrite": f["rewrite"] if isinstance(f.get("rewrite"), str) else None}  # "" – удалить целиком, None – варианта нет
        if key:
            item["frame"] = key
        out.append(item)
    with locked(doc):
        review = load(doc)
        review["findings"] = out
        scan = review.setdefault("scan", {})
        scan.update({"state": "done", "done": now()})
        save(doc, review)
    print(f"Находок записано: {len(out)}." + (f" Не нашлись в тексте ({len(missed)}): {'; '.join(missed)}. Цитата – точная подстрока." if missed else ""))
    return 0


def cmd_wait(args) -> int:
    """Live Edit: ждёт, пока владелец отправит правку из редактора, и выходит со сводкой. Агент применяет, отмечает
    и запускает `wait` снова. Ревью закончено или сервер остановлен – выход без правок, цикл заканчивается."""
    doc = Path(args.file).expanduser().resolve()
    # wait стартует вместе с повторным open: пока open не открыл ревью заново, в файле ещё старый «завершено»
    grace = time.monotonic() + WAIT_GRACE
    while True:
        got = take_sent(doc)
        if got is None and time.monotonic() < grace:
            time.sleep(args.interval)
            continue
        if got is None:
            print(f"Ревью {doc.name} завершено, Live Edit остановлен. Оставшиеся правки – в выводе open.")
            return 0
        fresh, review = got
        if fresh == "scan":
            print(scan_task(doc, review), flush=True)
            return 0
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
    p = sub.add_parser("suggest", help="записать находки смысловой проверки на нейрослоп")
    p.add_argument("file")
    p.add_argument("findings", help="JSON: [{quote, rewrite, rule, title, why, root}] или - для stdin")
    p.set_defaults(fn=cmd_suggest)
    p = sub.add_parser("style", help="какой скилл стиля нашёл Ревизор; init – создать из шаблона")
    p.add_argument("action", nargs="?", choices=["init"])
    p.add_argument("--name")
    p.add_argument("--force", action="store_true", help="с init: создать, даже если скилл уже найден")
    p.set_defaults(fn=cmd_style)
    p = sub.add_parser("pairs", help="новые пары «было → стало» для обучения скилла стиля (JSON)")
    p.add_argument("--ack", action="store_true", help="отметить последнюю порцию разобранной")
    p.add_argument("--count", action="store_true", help="только число новых сигналов")
    p.add_argument("--dir", action="append", help="искать файлы правок ещё и здесь")
    p.add_argument("--max", type=int, default=40, help="сколько сигналов в порции, остальное – в следующий раз")
    p.set_defaults(fn=cmd_pairs)
    p = sub.add_parser("update", help="обновить Ревизор из репо (git pull) и показать, что поменялось")
    p.add_argument("--check", action="store_true", help="только проверить, есть ли новая версия (кэш на сутки)")
    p.add_argument("--force", action="store_true", help="с --check: спросить репо сейчас, мимо кэша")
    p.set_defaults(fn=cmd_update)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ReviewError as e:
        print(f"revizor: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
