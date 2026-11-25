#!/usr/bin/env python3
"""revizor – локальный редактор правок: человек правит документ в браузере, агент забирает правки.

    revizor.py open FILE [--port N] [--no-browser]   сервер до «Завершить раунд» или «Завершить работу», потом сводка
                                                    FILE – .md/.markdown/.txt или .html/.htm
    revizor.py show FILE [--json]                     правки, которые ждут агента
    revizor.py mark FILE ID... --state applied|rejected [--note TEXT | --notes JSON]
                                                    комментарию и отклонённой правке пометка обязательна
    revizor.py wait FILE                              ждать работу из редактора: правки Live Edit и проверку на нейрослоп
    revizor.py check FILE [--self] [--no-layout]     проверка до показа: независимый читатель на нейрослоп, вёрстка HTML
    revizor.py ps                                    какие документы сейчас открыты
    revizor.py stop [FILE | --all]                   остановить сервер документа или все
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
import signal
import contextlib
import hashlib
import html
import html.parser
import gzip
import json
import mimetypes
import os
import re
import socket
import ssl
import subprocess
import tempfile
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

try:
    import fcntl
except ImportError:  # Windows: блокировки файла правок на fcntl, без них два процесса потеряют правки
    fcntl = None

__version__ = "1.9.0"

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
VENDOR = WEB / "vendor"
JS = "text/javascript; charset=utf-8"
TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
MD_SUFFIXES = {".md", ".markdown"}
HTML_SUFFIXES = {".html", ".htm"}
ASSET_PREFIX = "/_revizor/"
OVERLAY_TAG = f'<script src="{ASSET_PREFIX}overlay.js" defer></script>'
API_PREFIX = ASSET_PREFIX + "api/"  # API под префиксом Ревизора: у страницы дев-сервера свой /api/
URL_SUFFIX = ".url"  # якорь страницы дев-сервера в кэше: адрес и папка проекта, файл правок лежит рядом с ним
FORMAT_VERSION = 1
RULES_DIR = ROOT / "rules"
BASE_RULES = ("ru.json", "en.json")  # правило с полем lang работает только на строках своего языка
TEMPLATE = ROOT / "templates" / "style-skill"
CACHE = Path.home() / ".cache" / "revizor"
REGISTRY = CACHE / "docs.json"      # какие документы открывались в Ревизоре – отсюда берутся пары для обучения
BATCH = CACHE / "pairs-batch.json"  # последняя выданная агенту порция пар, `pairs --ack` переносит её в журнал скилла
NOTES_BATCH = CACHE / "notes-batch.json"  # то же для комментариев: `notes --ack` переносит порцию в журнал плейбука документов
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
        owner = _owner(short)
        # регистр не сверяем: на macOS Report.md и report.md – один файл, а resolve() оставляет регистр из команды
        return short if owner is None or owner.casefold() == doc.name.casefold() else full
    twins = [doc.with_suffix(x) for x in TEXT_SUFFIXES | HTML_SUFFIXES if x != doc.suffix.lower()]
    return full if any(t.exists() for t in twins) else short


_OWNERS = {}  # файл правок → (mtime, doc): sidecar() зовут на каждый опрос вкладки, разбирать JSON каждый раз незачем


def _owner(short: Path):
    try:
        mtime = short.stat().st_mtime_ns
        cached = _OWNERS.get(short)
        if cached and cached[0] == mtime:
            return cached[1]
        owner = json.loads(short.read_text(encoding="utf-8")).get("doc")
    except (OSError, ValueError, AttributeError):
        return None
    owner = owner if isinstance(owner, str) else None
    _OWNERS[short] = (mtime, owner)
    return owner


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
    """Через временный файл со своим именем: два процесса пишут ports.json одновременно и не мешают друг другу.
    Имя начинается с точки – скрытый файл сервер не отдаёт."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def kind(doc: Path) -> str:
    """html – отдаётся как есть, md – Markdown с двумя видами, text – простой текст, url – страница дев-сервера."""
    suffix = doc.suffix.lower()
    return "html" if suffix in HTML_SUFFIXES else "md" if suffix in MD_SUFFIXES else "url" if suffix == URL_SUFFIX else "text"


def is_markup(doc: Path) -> bool:
    """HTML-отчёт или страница дев-сервера: текст – это видимый текст страницы, строк исходника нет."""
    return kind(doc) in ("html", "url")


# ---------- страница дев-сервера ----------

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def is_url(s) -> bool:
    return isinstance(s, str) and bool(re.match(r"https?://", s, re.I))


def url_anchor(url: str, root=None) -> Path:
    """Страница дев-сервера как документ: якорь в кэше по адресу. open пишет в него папку проекта, остальные
    команды берут готовый. Адрес – только локальный: Ревизор не ходит в сеть за чужими страницами."""
    u = urlsplit(url)
    if u.scheme.lower() not in ("http", "https") or (u.hostname or "").lower() not in LOCAL_HOSTS:
        sys.exit(f"по адресу Ревизор открывает только локальный дев-сервер (http://localhost:<порт>/...): {url}")
    path = u.path or "/"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{u.hostname}-{u.port or ''}{path}").strip("-") or "page"
    anchor = PAGES / f"{slug}{URL_SUFFIX}"
    data = {"url": urlunsplit((u.scheme.lower(), u.netloc, path, u.query, "")), "root": str(root) if root else None}
    if root is None:
        if not anchor.is_file():
            sys.exit(f"страница {url} ещё не открывалась в Ревизоре: сначала revizor.py open {url}")
        return anchor
    try:
        old = json.loads(anchor.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = None
    if old != data:
        PAGES.mkdir(parents=True, exist_ok=True)
        atomic_write(anchor, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return anchor


def page_info(doc: Path) -> dict:
    return json.loads(doc.read_text(encoding="utf-8"))


def display(doc: Path) -> str:
    """Как назвать документ агенту: страницу – адресом, по нему же работают mark, show и wait."""
    return page_info(doc)["url"] if kind(doc) == "url" else str(doc)


def _local_ssl():
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # самоподписанный сертификат дев-сервера на localhost
    return ctx


def fetch_page(doc: Path, timeout: float = 5.0) -> str:
    url = page_info(doc)["url"]
    req = urllib.request.Request(url, headers={"Accept": "text/html", "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_local_ssl()) as res:
            raw = res.read()
            if res.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode(res.headers.get_content_charset() or "utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ReviewError(f"дев-сервер не отвечает на {url} ({e}). Запусти его (npm run dev или как принято в проекте).") from e


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект дев-сервера уходит вкладке как есть: она сама перейдёт, и адрес в ней останется адресом Ревизора."""
    def redirect_request(self, *args, **kwargs):
        return None


# заголовки одного соединения: прокси их не пересылает (RFC 9110, 7.6.1)
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}


def doc_text(doc: Path) -> str:
    """Текст документа для отпечатка и проверок: файл с диска или HTML страницы дев-сервера."""
    return fetch_page(doc) if kind(doc) == "url" else doc.read_text(encoding="utf-8")


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




def text_shell(doc: Path) -> str:
    """Текстовый документ как страница. #revizor-root – текст байт в байт (вид «Текст»). У Markdown рядом #revizor-md,
    его заполняет слой правок (вид «Оформление»); <base> нужен, чтобы картинки по относительным путям шли из папки документа."""
    body = html.escape(doc.read_text(encoding="utf-8"), quote=False)
    md = kind(doc) == "md"
    css = (WEB / "shell.css").read_text(encoding="utf-8") + ((WEB / "markdown.css").read_text(encoding="utf-8") if md else "")
    return f"""<!doctype html>
<html lang="{doc_lang(doc)}" data-revizor-mode="{"md" if md else "text"}"{' data-revizor-view="md"' if md else ""}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(doc.name)} · Revizor</title>
{'<base href="/doc/">' if md else ""}
<style>
{css}</style></head><body><main id="revizor-root">{body}</main>{'<article id="revizor-md"></article>' if md else ""}{OVERLAY_TAG}</body></html>
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
PAGES = CACHE / "pages"  # якоря страниц дев-сервера и их файлы правок
TEXTS = CACHE / "text"  # текст страницы для смысловой проверки: у бандла и HTML его видит только страница
REOPEN_WAIT = 4  # с: вкладка опрашивает сервер раз в 1,5 с
WAIT_GRACE = 10  # с: столько wait ждёт, что завершённое ревью откроют снова
OWNER_CHECK = 5  # с: как часто open и wait проверяют, жив ли запустивший их процесс
SERVERS = CACHE / "servers"  # запущенные `open`: pid, порт, документ – для `ps`, `stop` и перехвата повторным `open`
TAKEOVER_WAIT = 6  # с: вкладка прошлого сервера опрашивает его раз в 3 с и подхватывает новый на том же порту
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
    if is_markup(doc):
        return re.sub(r"\s+", " ", s)
    return s.replace("\n", "⏎")


def line_of(doc: Path, item: dict):
    """Номер строки правки в текстовом документе: агент открывает файл сразу на нужном месте."""
    if is_markup(doc) or item.get("frame") or item.get("orphan"):
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
    out = [head or f"Правки к {display(doc)}: {len(items)} ждут агента. Файл: {sidecar(doc)}"]
    busy = [i["id"] for i in pending(review) if i.get("taken")]
    if busy and head is None:
        out.append(f"Ещё применяются в Live Edit: {', '.join(busy)}")
    if review.get("register"):
        out.append(f"Регистр: {review['register']}")
    if items and kind(doc) == "url":
        root = page_info(doc).get("root")
        out.append(f"Страница дев-сервера, проект {root}. Текст живёт в исходниках проекта: цитату искать там (rg -F, без "
                   "node_modules и сборки), в компонентах, разметке, JSON с текстами и переводами, и править в исходнике. "
                   "Нашлась в нескольких местах – выбрать по «где» и блоку, не уверен – спросить владельца. "
                   "Дев-сервер перезагрузит страницу сам.")
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
        out.append(f"После применения: revizor.py mark {display(doc)} ID... --state applied (или rejected). "
                   "Комментарию и отклонённой правке – пометка: --notes '{\"ID\": \"что сделано или почему нет\"}'")
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
                atomic_write(UPDATE_CACHE, json.dumps({"checked": time.time(), "latest": latest}))
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
    # плагином Claude Code обновляет сам Claude Code. Проверка до git: кэш может лежать внутри чужого репо (~/.claude в dotfiles)
    parts = ROOT.resolve().parts
    if any(parts[i:i + 2] == ("plugins", "cache") for i in range(len(parts) - 1)):
        sys.exit("Ревизор стоит плагином Claude Code. Обновление – в Claude Code: "
                 "/plugin marketplace update revizor, затем /plugin update revizor@revizor")
    top = git("rev-parse", "--show-toplevel")
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != ROOT.resolve():
        sys.exit(f"Ревизор в {ROOT} поставлен не через git clone. Переставь: git clone {REPO_URL}")
    dirty = [l[3:] for l in git("status", "--porcelain").stdout.splitlines() if l.strip()]
    if dirty:
        sys.exit("Обновление остановлено: в папке Ревизора есть свои изменения – " + ", ".join(dirty))
    old = __version__
    # ставится последний релиз: тег vX.Y.Z. Ветка между релизами может уйти вперёд невыпущенным кодом
    fetch = git("fetch", "--tags", "--quiet", "origin")
    if fetch.returncode != 0:
        sys.exit("git fetch не прошёл:\n" + (fetch.stderr or fetch.stdout).strip())
    tags = [t for t in git("tag", "--list", "v*").stdout.split() if parse_version(t)]
    if not tags:
        sys.exit("В репо нет релизных тегов vX.Y.Z.")
    tag = max(tags, key=parse_version)
    if parse_version(tag) <= parse_version(old):
        print(f"Уже последняя версия: {old}.")
        return 0
    move = git("merge", "--ff-only", "--quiet", tag)
    if move.returncode != 0:
        sys.exit(f"Не получилось перейти на {tag}: в папке Ревизора свои коммиты или не та ветка.\n"
                 + (move.stderr or move.stdout).strip())
    subprocess.run(["bash", str(ROOT / "install.sh")], check=False)
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
    try:
        raw = fetch_page(doc) if kind(doc) == "url" else doc.read_text(encoding="utf-8", errors="replace")
    except ReviewError:
        return "ru"  # дев-сервер ещё не поднялся: язык уточнит смысловая проверка по тексту со страницы
    if is_markup(doc):  # скрипты и стили не текст, в бандле там ещё и сжатые слайды латиницей
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


def ui_lang():
    """Язык панели: REVIZOR_UI_LANG главнее ui_lang из настроек. Не задан – None, страница выберет по языку браузера."""
    v = str(os.environ.get("REVIZOR_UI_LANG") or config().get("ui_lang") or "").strip().lower()
    return v if v in ("ru", "en") else None


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
    """Правила проверки: базовые из rules/ (русские и английские) плюс правила скилла (revizor-rules.json). Правило скилла с тем же id
    заменяет базовое, "off": true – выключает его."""
    rules = {r["id"]: dict(r, source="base") for f in BASE_RULES
             for r in json.loads((RULES_DIR / f).read_text(encoding="utf-8"))["rules"]}
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
             "voice": "references/voice.md", "rules": "revizor-rules.json", "ledger": "revizor-learned.json",
             "documents": "references/documents.md", "notes_ledger": "revizor-notes.json"}
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
    for lang in ("ru", "en"):  # каталоги стартуют с базовых, дальше растут из правок
        shutil.copy(RULES_DIR / f"semantic-{lang}.md", d / "references" / f"ai-tells-{lang}.md")
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


def pick_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def is_port_free(port: int) -> bool:
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


def merge_items(review: dict, items: list) -> list:
    """Правки со страницы → файл. Страница присылает только живые правки; применённые и отклонённые хранит файл.
    Агент мог закрыть правку, пока вкладка открыта: такую страница больше не воскрешает. Правку, которую агент уже
    забрал в Live Edit, страница не меняет и не удаляет: он применяет её прямо сейчас. Возвращает закрытые правки."""
    closed = [i for i in review["items"] if i.get("state", "pending") != "pending"]
    closed_ids = {i["id"] for i in closed}
    taken = {i["id"]: i for i in review["items"] if i.get("state", "pending") == "pending" and i.get("taken")}
    live = [taken.pop(i["id"], None) or dict(i, state="pending") for i in items
            if i.get("state", "pending") == "pending" and i.get("id") not in closed_ids]
    review["items"] = closed + live + list(taken.values())
    return closed


def make_handler(doc: Path, done: threading.Event, update=None, skill="auto", tab_seen=None):
    doc = doc.resolve()
    lock = threading.Lock()
    if skill == "auto":
        skill = find_style_skill(lang=doc_lang(doc))
    lang = ui_lang()
    page = kind(doc) == "url"
    up = urlsplit(page_info(doc)["url"]) if page else None
    up_origin = f"{up.scheme}://{up.netloc}" if page else None
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_local_ssl())) if page else None

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
            if n < 0:
                raise BadRequest(400, "Content-Length отрицательный")
            if n > MAX_BODY:
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

        def other(self):
            """HEAD, PUT, DELETE и прочее – только страница дев-сервера, у самого Ревизора таких запросов нет."""
            if not page or self.ours():
                return self.send(405, b"method not allowed", "text/plain")
            self.guarded(self.proxy, post=False)
        do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = other

        def ours(self) -> bool:
            return self.path.startswith(ASSET_PREFIX)

        def guarded(self, fn, post):
            # запросы самой страницы дев-сервера идут к нему как есть: строгая проверка POST – только для API Ревизора
            if not self.allowed(post and not (page and not self.ours())):
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
            if tab_seen:
                tab_seen.set()
            if page and not self.ours():
                return self.proxy()
            path = unquote(self.path.split("?")[0])
            if "\x00" in path:
                raise BadRequest(400, "в пути нулевой байт")
            if path.startswith(API_PREFIX):
                path = "/api/" + path[len(API_PREFIX):]  # слой ходит в API под префиксом Ревизора, старые вкладки – в /api/
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
                f = (doc.parent / rel).resolve()
                if f == doc:
                    if kind(doc) != "html":  # текст открывается только через обёртку на /
                        self.send_response(302)
                        self.send_header("Location", "/")
                        return self.end_headers()
                    return self.send(200, inject(doc.read_text(encoding="utf-8")).encode("utf-8"), "text/html; charset=utf-8")
                if doc.parent not in f.parents or not f.is_file() or is_sidecar(f.name):
                    return self.send(404, b"not found", "text/plain")
                # .git, .env и прочее скрытое не отдаётся – ни по пути из запроса, ни через симлинк внутри папки
                if any(part.startswith(".") for part in (*Path(rel).parts, *f.relative_to(doc.parent).parts)):
                    return self.send(404, b"not found", "text/plain")
                self.send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            elif path == "/api/source" and not is_markup(doc):
                # Live Edit: агент поменял текст – страница перерисовывает его на месте, без перезагрузки
                self.send(200, doc.read_text(encoding="utf-8").encode("utf-8"), "text/plain; charset=utf-8")
            elif path == "/api/rules":
                self.send_json(load_rules(skill))
            elif path == "/api/state":
                text = doc_text(doc)
                with lock:
                    review = load(doc)
                name = up.netloc + (up.path or "/") if page else doc.name
                self.send_json({"doc": {"name": name, "path": display(doc), "type": kind(doc), "sha": sha(text)},
                                "review": review, "update": update, "ui_lang": lang})
            else:
                self.send(404, b"not found", "text/plain")

        def post(self):
            if page and not self.ours():
                return self.proxy()
            path = self.path.split("?")[0]
            if path.startswith(API_PREFIX):
                path = "/api/" + path[len(API_PREFIX):]
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
                closed = merge_items(review, items)
                if "live" in body:
                    review["live"] = bool(body["live"])
                if isinstance(body.get("dismissed"), list):
                    review["dismissed"] = body["dismissed"]  # «это не нейрослоп» – больше не показывать, и сигнал для обучения скилла
                review["register"] = body.get("register")
                review["register_auto"] = bool(body.get("register_auto"))
                if body.get("view") in ("md", "plain"):
                    review["view"] = body["view"]  # вид Markdown – порт каждый раз новый, localStorage его не удержит
                review["doc_sha"] = sha(doc_text(doc))
                review["status"] = "done" if path == "/api/done" else "open"
                if path == "/api/done":
                    review["final"] = body.get("final") is True  # «Завершить работу» – последний круг, иначе раунд
                save(doc, review)
            self.send_json({"ok": True, "updated": review["updated"], "closed": closed})
            if path == "/api/done":
                done.set()

        # ---------- страница дев-сервера: Ревизор между вкладкой и дев-сервером ----------

        def upstream_headers(self) -> dict:
            own = {f"http://{h}" for h in (f"127.0.0.1:{self.server.server_address[1]}", f"localhost:{self.server.server_address[1]}")}
            out = {}
            for k, v in self.headers.items():
                kl = k.lower()
                if kl in HOP or kl in ("host", "accept-encoding", "content-length"):
                    continue
                if kl in ("origin", "referer"):  # для дев-сервера запрос пришёл с его же адреса: Vite и Next проверяют Origin
                    v = next((up_origin + v[len(o):] for o in own if v.startswith(o)), v)
                out[k] = v
            out["Host"] = up.netloc
            out["Accept-Encoding"] = "identity"  # HTML нужен несжатым, чтобы вставить слой
            return out

        def proxy(self):
            if self.headers.get("Upgrade", "").lower() == "websocket":
                return self.tunnel()  # горячая перезагрузка Vite, Astro и Next идёт по websocket
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise BadRequest(400, "Content-Length – не число")
            body = self.rfile.read(n) if n > 0 else None
            req = urllib.request.Request(up_origin + self.path, data=body, method=self.command, headers=self.upstream_headers())
            try:
                res = opener.open(req, timeout=60)
            except urllib.error.HTTPError as e:
                res = e  # 3xx, 404, 500 дев-сервера – ответ страницы, вкладка должна его увидеть
            except (urllib.error.URLError, OSError) as e:
                msg = f"Дев-сервер {up_origin} не отвечает ({e}). Запусти его и обнови вкладку."
                return self.send(502, msg.encode("utf-8"), "text/plain; charset=utf-8")
            with res:
                status = getattr(res, "status", None) or res.code
                ctype = res.headers.get("Content-Type", "")
                html_page = self.command == "GET" and ctype.lower().startswith("text/html")
                self.send_response(status)
                for k, v in res.headers.items():
                    kl = k.lower()
                    if kl in HOP or kl in ("content-length", "server", "date") or (html_page and kl in (
                            "content-encoding", "content-security-policy", "content-security-policy-report-only")):
                        continue  # CSP страницы могла бы не пустить скрипт слоя
                    if kl == "location" and v.startswith(up_origin):
                        v = v[len(up_origin):] or "/"
                    self.send_header(k, v)
                if html_page:
                    raw = res.read()
                    if res.headers.get("Content-Encoding", "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                    data = inject(raw.decode(res.headers.get_content_charset() or "utf-8", errors="replace")).encode("utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if res.headers.get("Content-Length"):
                    self.send_header("Content-Length", res.headers["Content-Length"])
                self.end_headers()
                if self.command == "HEAD":
                    return
                # остальное – потоком: скрипты, картинки и поток событий (EventSource) без конца
                while True:
                    chunk = res.read1(65536) if hasattr(res, "read1") else res.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()

        def tunnel(self):
            """Websocket – трубой: запрос с заголовками уходит дев-серверу, дальше байты в обе стороны до закрытия."""
            try:
                sock = socket.create_connection((up.hostname, up.port or (443 if up.scheme == "https" else 80)), timeout=10)
                if up.scheme == "https":
                    sock = _local_ssl().wrap_socket(sock, server_hostname=up.hostname)
            except OSError:
                return self.send(502, b"dev server is not responding", "text/plain")
            head = [f"{self.command} {self.path} HTTP/1.1", f"Host: {up.netloc}"]
            head += [f"{k}: {v}" for k, v in self.upstream_headers().items() if k.lower() not in ("host", "accept-encoding")]
            head += ["Connection: Upgrade", "Upgrade: websocket"]
            sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
            client = self.connection
            sock.settimeout(None)
            client.settimeout(None)

            def pump(src, dst):
                try:
                    while True:
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except OSError:
                    pass
                finally:
                    for x in (src, dst):
                        try:
                            x.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

            back = threading.Thread(target=pump, args=(sock, client), daemon=True)
            back.start()
            pump(client, sock)
            back.join(5)
            sock.close()
            self.close_connection = True

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
                review["scan"] = {"state": "requested", "requested": now(), "sha": sha(doc_text(doc))}
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


def known_sidecars(extra=(), discover=False) -> list:
    """Файлы правок для обучения: из реестра открытых документов и папок --dir. discover – ещё поиск Spotlight по всему
    диску, чтобы подхватить ревью до появления реестра. По умолчанию выключен: на диске лежат и чужие файлы правок."""
    found = set()
    if REGISTRY.is_file():
        try:
            found |= {str(sidecar(Path(d))) for d in json.loads(REGISTRY.read_text(encoding="utf-8"))}
        except (OSError, ValueError):
            pass
    if discover and shutil.which("mdfind"):
        try:
            out = subprocess.run(["mdfind", "-name", ".review.json"], capture_output=True, text=True, timeout=10).stdout
            found |= {l for l in out.splitlines() if l.endswith(".review.json")}
        except (OSError, subprocess.SubprocessError):
            pass
    for d in extra:
        found |= {str(p) for p in Path(d).expanduser().rglob("*.review.json")}
    return sorted(f for f in found if Path(f).is_file())


def collect_pairs(ledger: dict, extra=(), discover=False) -> dict:
    """Новые сигналы для обучения: правки владельца (пары), отметки «это не нейрослоп», принятые находки проверки."""
    seen = set(ledger.get("seen", []))
    pairs, dismissed, confirmed = [], [], []
    for f in known_sidecars(extra, discover):
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


def collect_notes(seen: set, extra=(), discover=False) -> list:
    """Комментарии владельца, которые агент уже разобрал (applied или rejected): к фрагменту, блоку и всему документу.
    Из них растёт плейбук документов – как строить документ целиком. Ждущие агента не берутся: по ним ещё нет итога."""
    notes = []
    for f in known_sidecars(extra, discover):
        try:
            review = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for i in review.get("items", []):
            key = sha(f"{f}#n:{i.get('id')}")
            if key in seen or i.get("kind") != "comment" or i.get("state") not in ("applied", "rejected"):
                continue
            if not (i.get("comment") or "").strip():
                continue
            target = i.get("quote") or i.get("snippet") or i.get("text") or ""
            notes.append({"key": key, "doc": review.get("doc"), "register": review.get("register"), "scope": i.get("scope", "text"),
                          "where": i.get("where") or i.get("frame") or "", "target": re.sub(r"\s+", " ", target).strip()[:300],
                          "comment": i["comment"].strip(), "state": i["state"], "note": i.get("note") or "",
                          "lang": text_lang(f"{target} {i['comment']}")})
    return notes


def cmd_notes(args) -> int:
    """Порция комментариев для плейбука документов. Плейбук живёт в скилле стиля (references/documents.md), журнал разобранного
    – рядом (revizor-notes.json). Скилла нет – создаётся из шаблона, как для пар."""
    if not learn_allowed():
        if args.count:
            print(0)
        print(f"Обучение запускается из {config().get('learn_from')} (learn_from в {CONFIG}). Комментарии копятся, их заберёт обучение оттуда.",
              file=sys.stderr)
        return 0 if args.count else 3
    skill, created = lang_skill()
    files = skill_files(skill)
    ledger_path = Path(files["notes_ledger"])
    ledger = read_ledger(ledger_path)
    if args.ack:
        if not NOTES_BATCH.is_file():
            print("Нечего отмечать: порции нет. Сначала revizor.py notes.")
            return 1
        batch = json.loads(NOTES_BATCH.read_text(encoding="utf-8"))
        ledger["seen"] = sorted(set(ledger.get("seen", [])) | {n["key"] for n in batch})
        ledger["updated"] = now()
        atomic_write(ledger_path, json.dumps(ledger, ensure_ascii=False, indent=1) + "\n")
        NOTES_BATCH.unlink()
        print(f"В журнал плейбука документов {skill['name']}: {len(batch)} комментариев.")
        return 0
    notes = collect_notes(set(ledger.get("seen", [])), args.dir or (), args.discover)
    if args.count:  # только число: порцию не трогает
        print(len(notes))
        return 0
    notes = notes[:args.max]
    atomic_write(NOTES_BATCH, json.dumps(notes, ensure_ascii=False))
    docs = {}
    for n in notes:  # по документам: правило видно, когда комментарии одного документа стоят рядом
        docs.setdefault((n["doc"], n["register"]), []).append({k: v for k, v in n.items() if k not in ("doc", "register")})
    out = {"skill": skill, "created": created, "files": {k: files[k] for k in ("documents", "notes_ledger")},
           "playbook": str(ROOT / "skill" / "learn-docs.md"), "total": len(notes),
           "docs": [{"doc": d, "register": r, "notes": ns} for (d, r), ns in docs.items()]}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


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


def pairs_ack(skill: dict, skills: dict, ledgers: dict) -> int:
    """Порция разобрана: её записи – в журнал скилла своего языка, статистика правил – туда же."""
    if not BATCH.is_file():
        print("Нечего отмечать: порции нет. Сначала revizor.py pairs.")
        return 1
    batch = json.loads(BATCH.read_text(encoding="utf-8"))
    touched = set()
    for group, field in (("pairs", "taken"), ("confirmed", "taken"), ("dismissed", "dismissed")):
        for e in batch.get(group, []):
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
        return pairs_ack(skill, skills, ledgers)
    got = collect_pairs({"seen": sorted({k for lg in ledgers.values() for k in lg.get("seen", [])})}, args.dir or (), args.discover)
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
    if args.count:  # только число: порцию не трогает, иначе --ack отметит то, чего агент не видел
        print(len(got["pairs"]) + len(got["dismissed"]))
        return 0
    atomic_write(BATCH, json.dumps(got, ensure_ascii=False))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def open_browser(url: str) -> None:
    """Chrome на macOS, иначе браузер по умолчанию. Не открылся – ссылка уже напечатана, её откроют руками."""
    if sys.platform == "darwin":
        try:
            if subprocess.run(["open", "-a", "Google Chrome", url], capture_output=True).returncode == 0:
                return
        except OSError:
            pass
    try:
        if webbrowser.open(url):
            return
    except webbrowser.Error:
        pass
    print(f"Браузер не открылся сам. Открой ссылку: {url}", file=sys.stderr, flush=True)


def resolve_doc(args, opening=False) -> Path:
    if is_url(args.file):
        # open запоминает папку проекта: агент правит исходники оттуда, а не HTML страницы
        return url_anchor(args.file, Path.cwd().resolve() if opening else None)
    doc = Path(args.file).expanduser().resolve()
    if not doc.is_file():
        sys.exit(f"нет файла: {doc}")
    return doc


# Агенты путали конец круга с концом работы: первой строкой сводки – что нажал владелец и что делать дальше
FINISH_ROUND = ("Владелец завершил раунд правок, работа над документом продолжается. Применить правки, отметить mark "
                "и сразу снова открыть документ (шаги 2 и 3 «Запуска» скилла): вкладка ждёт новую версию.")
FINISH_WORK = ("Владелец завершил работу с документом: это последний круг. Применить правки, отметить mark, "
               "редактор больше не открывать. Дальше – обучение (разделы «Обучение» скилла) и итог владельцу.")


def owner_chain(depth: int = 2) -> list:
    """Родитель и дед: у агента это обёртка-shell и сама сессия, в терминале – shell и login."""
    chain, pid = [], os.getppid()
    while pid > 1 and len(chain) < depth:
        chain.append(pid)
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
            pid = int(out.strip() or 1)
        except (ValueError, OSError, subprocess.SubprocessError):
            break
    return chain


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def watch_owner(orphaned: threading.Event, wake=None) -> None:
    """Сессию агента закрыли – open и wait выходят сами. Иначе сервер и опрос висят сиротами днями.
    Запущен уже сиротой (nohup, launchd) – не следим: хозяина нет с самого начала."""
    chain = owner_chain()
    if not chain:
        return

    def loop():
        while not orphaned.is_set():
            time.sleep(OWNER_CHECK)
            if os.getppid() == 1 or not all(pid_alive(p) for p in chain):
                orphaned.set()
                if wake:
                    wake.set()
    threading.Thread(target=loop, daemon=True).start()


def server_file(doc: Path) -> Path:
    return SERVERS / f"{sha(str(doc))}.json"


def live_servers() -> list:
    """Записи живых `open`. Запись мёртвого процесса удаляется: сервер упал или машину перезагрузили."""
    out = []
    for f in sorted(SERVERS.glob("*.json")) if SERVERS.is_dir() else []:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            f.unlink(missing_ok=True)
            continue
        if isinstance(rec.get("pid"), int) and pid_alive(rec["pid"]):
            out.append(rec)
        else:
            f.unlink(missing_ok=True)
    return out


def stop_server(rec: dict, timeout: float = 5.0) -> bool:
    """SIGTERM процессу `open`: он сохраняет статус «stopped» и освобождает порт. True – процесс вышел."""
    try:
        os.kill(rec["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not pid_alive(rec["pid"]):
            return True
        time.sleep(0.1)
    return False


def cmd_ps(args) -> int:
    recs = live_servers()
    if not recs:
        print("Ревизор сейчас ничего не держит открытым.")
        return 0
    for r in recs:
        print(f"{r['pid']:>7}  http://127.0.0.1:{r['port']}/  {r['doc']}  (с {r.get('started', '?')})")
    return 0


def cmd_stop(args) -> int:
    recs = live_servers()
    if not args.all:
        if not args.file:
            sys.exit("укажи документ или --all")
        doc = str(resolve_doc(args))
        recs = [r for r in recs if r["doc"] == doc]
    if not recs:
        print("Нечего останавливать.")
        return 0
    for r in recs:
        ok = stop_server(r)
        print(f"{'остановлен' if ok else 'не остановился'}: {r['doc']} (pid {r['pid']}, порт {r['port']})")
    return 0


def cmd_open(args) -> int:
    doc = resolve_doc(args, opening=True)
    if kind(doc) == "url":
        try:
            fetch_page(doc)
        except ReviewError as e:
            sys.exit(str(e))
    elif doc.suffix.lower() not in TEXT_SUFFIXES | HTML_SUFFIXES:
        sys.exit(f"поддерживаются {', '.join(sorted(TEXT_SUFFIXES | HTML_SUFFIXES))} и адрес дев-сервера: {doc.name}")
    else:
        try:
            doc.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            sys.exit(f"{doc.name} не в UTF-8. Пересохрани его в UTF-8 и открой снова.")
    # Документ уже открыт другим `open` (прошлая сессия, «открой заново») – гасим его и встаём на его порт:
    # вкладка подхватит новый сервер сама, вторая вкладка и сирота-сервер не появляются.
    took = None
    for rec in live_servers():
        if rec["doc"] == str(doc) and rec["pid"] != os.getpid():
            if stop_server(rec):
                took = rec
            else:
                print(f"Прошлый сервер этого документа (pid {rec['pid']}) не остановился, открываю на другом порту.",
                      file=sys.stderr, flush=True)
    remember_doc(doc)
    with locked(doc):
        review = load(doc)
        reopen = review["status"] == "done"
        if review["status"] == "done" and pending(review):
            print(f"В {sidecar(doc).name} есть {len(pending(review))} неприменённых правок, они откроются в редакторе.",
                  file=sys.stderr)
        review["status"] = "open"
        review.pop("final", None)  # документ снова открыт – значит, работа продолжается
        review["opened"] = now()  # `wait` прошлого запуска видит новую метку и выходит
        save(doc, review)

    update = check_update()
    if update:
        print(f"Доступна новая версия Ревизора: {update['latest']}, у тебя {update['current']}. "
              f"Обновить: revizor.py update (или агенту – «обнови ревизор»).", file=sys.stderr, flush=True)
    done, tab_seen = threading.Event(), threading.Event()
    prev = last_port(doc)
    if took and took.get("port"):
        prev = took["port"]
    port = args.port or (prev if prev and is_port_free(prev) else pick_free_port())
    last_port(doc, port)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(doc, done, update, tab_seen=tab_seen))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if kind(doc) == "url":  # та же страница, только через Ревизор: путь и параметры адреса сохраняются
        up = urlsplit(page_info(doc)["url"])
        url = f"http://127.0.0.1:{port}{up.path or '/'}" + (f"?{up.query}" if up.query else "")
    else:
        url = f"http://127.0.0.1:{port}/?v={int(datetime.now().timestamp())}"
    print(f"Редактор: {url}\nПравки: {sidecar(doc)}", file=sys.stderr, flush=True)
    SERVERS.mkdir(parents=True, exist_ok=True)
    atomic_write(server_file(doc), json.dumps({"pid": os.getpid(), "port": port, "doc": str(doc), "started": now()},
                                              ensure_ascii=False))
    replaced = threading.Event()

    def on_term(signum, frame):  # повторный `open` или `stop`: выйти так же, как по Ctrl+C
        replaced.set()
        raise KeyboardInterrupt
    main_thread = threading.current_thread() is threading.main_thread()  # в тестах open крутится в потоке
    if main_thread:
        signal.signal(signal.SIGTERM, on_term)
    # После «Завершить раунд» вкладка ждёт сервер на старом порту и сама перезагружается.
    # Вкладку закрыли – через пару секунд тишины открываем новую.
    if reopen and port == prev and tab_seen.wait(REOPEN_WAIT):
        print("Вкладка с прошлого ревью подхватила редактор.", file=sys.stderr, flush=True)
    elif took and port == prev and tab_seen.wait(TAKEOVER_WAIT):
        print("Документ был открыт прошлым запуском: его сервер остановлен, вкладка подхватила новый.", file=sys.stderr, flush=True)
    elif not args.no_browser:
        open_browser(url)

    orphaned = threading.Event()
    watch_owner(orphaned, wake=done)
    try:
        done.wait()
        if orphaned.is_set():
            raise KeyboardInterrupt
    except KeyboardInterrupt:
        if main_thread:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
        with contextlib.suppress(OSError):
            rec = json.loads(server_file(doc).read_text(encoding="utf-8"))
            if rec.get("pid") == os.getpid():
                server_file(doc).unlink()
        server.shutdown()
        server.server_close()
        with locked(doc):
            review = load(doc)
            review["status"] = "stopped"  # `wait` в Live Edit видит это и заканчивает цикл
            save(doc, review)
        why = ("Документ открыт заново другим запуском или остановлен через stop: этот сервер закрыт" if replaced.is_set()
               else "Сессия, запустившая редактор, закрыта: сервер остановлен" if orphaned.is_set()
               else "Остановлено без «Завершить раунд»")
        print(f"{why}. Правки, что успели сохраниться, в {sidecar(doc)}", file=sys.stderr)
        return 130
    server.shutdown()
    server.server_close()  # порт свободен сразу: следующий `open` встанет на него
    if main_thread:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    server_file(doc).unlink(missing_ok=True)
    review = load(doc)
    print(FINISH_WORK if review.get("final") else FINISH_ROUND)
    print(summary(doc, review))
    return 0


def cmd_show(args) -> int:
    doc = resolve_doc(args)
    review = load(doc)
    if args.json:
        print(json.dumps(pending(review), ensure_ascii=False, indent=2))
    else:
        print(summary(doc, review))
    return 0


def parse_notes(raw):
    """--notes: JSON {id: пометка} строкой, путём к файлу или «-» для stdin."""
    if not raw:
        return {}
    try:
        text = sys.stdin.read() if raw == "-" else raw if raw.lstrip().startswith("{") else Path(raw).read_text(encoding="utf-8")
        notes = json.loads(text)
    except (OSError, ValueError) as e:
        sys.exit(f"--notes: нужен JSON {{\"ID\": \"пометка\"}}: {e}")
    if not isinstance(notes, dict):
        sys.exit("--notes: нужен объект {\"ID\": \"пометка\"}")
    return {str(k): str(v).strip() for k, v in notes.items() if str(v).strip()}


def cmd_mark(args) -> int:
    doc = resolve_doc(args)
    notes = parse_notes(args.notes)
    with locked(doc):
        review = load(doc)
        known = {i["id"]: i for i in review["items"]}
        missing = [i for i in args.ids if i not in known]
        if missing:
            sys.exit(f"нет правок с id: {', '.join(missing)}")
        # пометка – вход обучения плейбука документов (`notes`): без неё не видно, что сделано по комментарию
        bare = [i for i in args.ids if not (notes.get(i) or args.note)
                and (known[i].get("kind") == "comment" or args.state == "rejected")]
        if bare:
            sys.exit(f"нужна пометка, что сделано или почему отклонено: {', '.join(bare)}. "
                     f"--notes '{{\"{bare[0]}\": \"...\"}}' – своя на каждый id, --note – одна на все")
        for i in args.ids:
            known[i]["state"] = args.state
            known[i]["resolved"] = now()
            if notes.get(i) or args.note:
                known[i]["note"] = notes.get(i) or args.note
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


def catalog_for(lang: str):
    """Каталог смысловых примет под язык: из скилла стиля владельца, иначе базовый Ревизора."""
    skill = find_style_skill(lang=lang)
    catalog = Path(skill_files(skill, lang)["catalog"]) if skill else None
    if catalog and catalog.is_file():
        where = f" (скилл {skill['name']})"
    else:
        catalog = RULES_DIR / f"semantic-{lang}.md"
        where = " (базовый Ревизора)"
        if not catalog.is_file():
            catalog, where = RULES_DIR / "semantic-ru.md", f" (базовый Ревизора, на русском: каталога на {lang} нет, приметы искать по смыслу)"
    return skill, catalog, where


def rule_ids(skill, lang: str) -> str:
    """id правил, которые сработают на тексте этого языка: агенту незачем знать про русские правила в английском тексте."""
    return ", ".join(r["id"] for r in load_rules(skill)["rules"] if r.get("lang", "any") in ("any", lang))


def scan_task(doc: Path, review: dict) -> str:
    """Задание агенту на смысловую проверку: где текст, по какому каталогу искать, что уже поймали регулярки."""
    try:
        roots = json.loads(text_cache(doc).read_text(encoding="utf-8"))["roots"]
        lang = text_lang(" ".join(r.get("text", "") for r in roots))
    except (OSError, ValueError, KeyError):
        lang = doc_lang(doc)
    skill, catalog, where = catalog_for(lang)
    base = rule_ids(skill, lang)
    return "\n".join([
        f"Проверка на нейрослоп: {display(doc)}",
        f"Текст страницы: {text_cache(doc)} (roots[].key – корень: \"\" страница, иначе фрейм слайда)",
        f"Язык: {lang}",
        f"Каталог примет: {catalog}{where}",
        f"Регистр: {review.get('register') or 'определить по тексту'}",
        f"Уже ловят регулярки, не повторять: {base}",
        "Найти смысловые приметы по разделу «Проверка на нейрослоп» скилла revizor, записать: "
        f"revizor.py suggest {display(doc)} <findings.json>, потом снова запустить wait в фоне.",
    ])


class _Visible(html.parser.HTMLParser):
    """Видимый текст HTML без браузера: CSS, JS и шаблоны пропускаются, блоки – с новой строки."""
    SKIP = {"style", "script", "template", "noscript", "title"}
    BLOCK = {"p", "div", "section", "article", "header", "footer", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "br", "td", "th", "figcaption", "blockquote", "aside", "main", "nav", "dt", "dd"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.out).split("\n"))
        return "\n".join(line for line in lines if line)


def visible_text(doc: Path) -> str:
    src = fetch_page(doc) if kind(doc) == "url" else doc.read_text(encoding="utf-8", errors="replace")
    if not is_markup(doc):
        return src
    p = _Visible()
    p.feed(src)
    return p.text()


def repeated_sentences(text: str, min_len: int = 40) -> list:
    """Предложения, которые стоят в тексте дважды и больше: строка про одно и то же кочует по разделам."""
    seen = {}
    for s in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = " ".join(s.split())
        if len(s) >= min_len:
            seen[s] = seen.get(s, 0) + 1
    return [(s, n) for s, n in seen.items() if n > 1]


READER_MODEL = os.environ.get("REVIZOR_READER_MODEL", "sonnet")
READER_TIMEOUT = 300  # с на кусок текста
READER_CHUNK = 40000  # знаков: длинный документ читается кусками по абзацам
READER_SYSTEM = {
    "ru": ("Ты редактор, который ищет нейрослоп в чужом тексте. Ты не автор и не знаешь, зачем текст писали. "
           "Ищешь только смысловые приметы из каталога. Отвечаешь только JSON-массивом, без пояснений вокруг."),
    "en": ("You are an editor hunting for AI-slop in someone else's text. You are not the author. "
           "Look only for the semantic tells from the catalog. Reply with a JSON array only, nothing around it."),
}
READER_TASK = {
    "ru": """Каталог примет:
<<<КАТАЛОГ
{catalog}
КАТАЛОГ>>>

Механику уже ловят регулярки, её не искать: {base}.

Правила отбора: только сильные и средние приметы, слабые – если рядом есть ещё одна; 1–2 на абзац, не больше {limit} на этот кусок; чистый текст – пустой массив []. Факты и цифры не трогать и не добавлять. Содержание, полноту, методику и выводы не оценивать: только приметы из каталога.

Формат ответа: [{{"quote": "точная подстрока текста, символ в символ, до 300 знаков", "rule": "id приметы из каталога", "why": "одна строка про это место", "rewrite": "как переписать; пустая строка – удалить целиком"}}]

Текст:
<<<ТЕКСТ
{text}
ТЕКСТ>>>""",
    "en": """Catalog of tells:
<<<CATALOG
{catalog}
CATALOG>>>

Regexes already catch the mechanics, skip them: {base}.

Selection: strong and medium tells only, a weak one only next to another; 1–2 per paragraph, at most {limit} in this chunk; a clean text is an empty array []. Do not change or add facts and numbers. Do not judge content, completeness, method or conclusions: catalog tells only.

Reply format: [{{"quote": "exact substring of the text, character for character, up to 300 chars", "rule": "tell id from the catalog", "why": "one line about this spot", "rewrite": "how to rewrite; empty string means delete"}}]

Text:
<<<TEXT
{text}
TEXT>>>""",
}


def chunks(text: str, size: int = READER_CHUNK) -> list:
    out, cur = [], ""
    for para in text.split("\n"):
        if cur and len(cur) + len(para) + 1 > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{para}" if cur else para
    return out + ([cur] if cur else [])


def parse_findings(raw: str) -> list:
    """JSON-массив из ответа модели: она иногда оборачивает его в ```json или добавляет строку перед ним."""
    a, b = raw.find("["), raw.rfind("]")
    if a == -1 or b < a:
        raise ValueError("в ответе нет JSON-массива")
    data = json.loads(raw[a:b + 1])
    return [f for f in data if isinstance(f, dict) and isinstance(f.get("quote"), str)]


def reader_pass(text: str, catalog: Path, base: str, lang: str):
    """Независимый читатель: отдельный `claude -p` без контекста автора, хуков и MCP видит только текст и каталог.
    Автор, проверяя себя, пропускает примерно половину примет: он помнит, что хотел сказать.
    None – читателя нет (нет `claude` в PATH, REVIZOR_NO_READER=1 или он упал), тогда самопроверка автором."""
    exe = shutil.which("claude")
    if not exe or os.environ.get("REVIZOR_NO_READER") == "1":
        return None
    key = "ru" if lang == "ru" else "en"
    try:
        cat = catalog.read_text(encoding="utf-8")
    except OSError:
        return None
    found = []
    parts = chunks(text)
    for part in parts:
        prompt = READER_TASK[key].format(catalog=cat, base=base, text=part, limit=max(5, 15 // len(parts)))
        try:
            proc = subprocess.run(
                [exe, "-p", "--model", READER_MODEL, "--setting-sources", "", "--strict-mcp-config", "--tools", "",
                 "--no-session-persistence", "--system-prompt", READER_SYSTEM[key]],
                input=prompt, capture_output=True, text=True, timeout=READER_TIMEOUT, cwd=tempfile.gettempdir())
            if proc.returncode != 0:
                print(f"revizor: читатель упал ({proc.returncode}): {proc.stderr.strip()[:200]}", file=sys.stderr)
                return None
            items = parse_findings(proc.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            print(f"revizor: читатель не ответил: {e}", file=sys.stderr)
            return None
        found += [f for f in items if f["quote"].strip() and f["quote"].strip() in part]
    return found


# ---------- вёрстка: headless Chrome ----------

LAYOUT_WIDTHS = (1440, 390)
LAYOUT_PROBE = r"""<script>(function(){
function txt(e){return (e.innerText||e.textContent||'').trim().replace(/\s+/g,' ');}
function short(e){var t=txt(e);return t.length>70?t.slice(0,70)+'…':t;}
function num(t){t=t.replace(/[\s  ]/g,'');return t!==''&&/^[−\-+–]?[$€£₽]?[\d.,]+[%KkMмлнтыс×x₽$€]*$/.test(t);}
function align(e){var a=getComputedStyle(e).textAlign;return a==='end'||a==='right'?'right':a==='center'?'center':'left';}
function docs(){var out=[document];document.querySelectorAll('iframe').forEach(function(f){try{if(f.contentDocument&&f.contentDocument.body)out.push(f.contentDocument);}catch(e){}});return out;}
function run(){var w=[];var W=innerWidth;
docs().forEach(function(d){
 d.querySelectorAll('table').forEach(function(t){
  var rows=[].slice.call(t.rows);if(rows.length<2)return;
  var head=rows[0];var hcells=[].slice.call(head.cells);
  if(!hcells.some(function(c){return c.tagName==='TH';}))return;
  var span=hcells.reduce(function(s,c){return s+(c.colSpan||1);},0);
  var body=rows.slice(1).filter(function(r){return ![].slice.call(r.cells).every(function(c){return c.tagName==='TH';});});
  var widest=body.reduce(function(m,r){return Math.max(m,[].slice.call(r.cells).reduce(function(s,c){return s+(c.colSpan||1);},0));},0);
  if(span!==widest&&widest)w.push({kind:'table-columns',where:short(head),msg:'заголовков на '+span+' колонок, а в строках '+widest});
  var col=0;hcells.forEach(function(h){var c0=col;col+=h.colSpan||1;if((h.colSpan||1)>1)return;
   var cells=body.map(function(r){var k=0;for(var i=0;i<r.cells.length;i++){if(k===c0)return r.cells[i];k+=r.cells[i].colSpan||1;}return null;}).filter(Boolean);
   var nums=cells.filter(function(c){return num(txt(c));});
   if(cells.length&&nums.length/cells.length>=0.8){var ca=align(nums[0]),ha=align(h);
    if(ca!==ha)w.push({kind:'table-header',where:txt(h)||'(пустой заголовок)',msg:'заголовок выровнен '+ha+', числа под ним '+ca});}
  });
 });
 d.querySelectorAll('img').forEach(function(i){if(i.complete&&i.naturalWidth===0&&i.getAttribute('src'))w.push({kind:'image-broken',where:i.getAttribute('src').slice(0,80),msg:'картинка не загрузилась'});});
 d.querySelectorAll('details:not([open])').forEach(function(x){var s=x.querySelector('summary');var t=s?txt(s):'';
  if(/вывод|итог|главн|резюме|summary|conclusion|takeaway|key finding|tl;?dr/i.test(t))w.push({kind:'hidden-conclusion',where:t.slice(0,70),msg:'вывод спрятан в свёрнутый блок'});});
 d.querySelectorAll('section,.slide,[data-slide]').forEach(function(x){if(txt(x).length<3&&!x.querySelector('img,svg,canvas,video,iframe,picture'))w.push({kind:'empty-block',where:(x.id?'#'+x.id:x.className?'.'+String(x.className).split(' ')[0]:x.tagName.toLowerCase()),msg:'пустой слайд или раздел'});});
 if(d===document){
  if(document.documentElement.scrollWidth>W+2)w.push({kind:'page-scroll',where:'страница',msg:'горизонтальная прокрутка: ширина '+document.documentElement.scrollWidth+' при окне '+W});
  var seen=0;document.querySelectorAll('body *').forEach(function(e){if(seen>=5)return;var r=e.getBoundingClientRect();if(!r.width||r.right<=W+2)return;
   for(var p=e.parentElement;p&&p!==document.body;p=p.parentElement){var o=getComputedStyle(p);if(/auto|scroll|hidden|clip/.test(o.overflowX))return;}
   var pr=e.parentElement&&e.parentElement.getBoundingClientRect();if(pr&&pr.right>W+2)return;
   if(!txt(e)&&!/IMG|SVG|CANVAS|TABLE|VIDEO/.test(e.tagName))return;seen++;
   w.push({kind:'overflow',where:short(e)||e.tagName.toLowerCase(),msg:'вылезает за правый край на '+Math.round(r.right-W)+' px'});});
 }
 d.querySelectorAll('h1,h2,h3,h4,th,td').forEach(function(e){var o=getComputedStyle(e);
  if(/hidden|clip/.test(o.overflowX)&&e.scrollWidth>e.clientWidth+1&&txt(e))w.push({kind:'clipped',where:short(e),msg:'текст обрезан'});});
});
var pre=document.createElement('pre');pre.id='__revizor_layout';pre.textContent=JSON.stringify({w:w,h:document.documentElement.scrollHeight});
document.body.appendChild(pre);}
addEventListener('load',function(){setTimeout(run,400);});
})();</script>"""


def chrome_path():
    for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Chromium.app/Contents/MacOS/Chromium"):
        if os.path.isfile(p):
            return p
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    return None


def probe_page(doc: Path) -> str:
    """Страница с пробой вёрстки во временном файле: <base> ведёт к папке документа, сам документ не трогается."""
    if kind(doc) == "url":
        src, base = fetch_page(doc), page_info(doc)["url"]
    else:
        src, base = doc.read_text(encoding="utf-8", errors="replace"), doc.parent.as_uri() + "/"
    tag = f'<base href="{html.escape(base, quote=True)}">'
    m = re.search(r"<head[^>]*>", src, re.I)
    src = src[:m.end()] + tag + src[m.end():] if m else tag + src
    last = None
    for last in re.finditer(r"</body\s*>", src, re.I):
        pass
    return src[:last.start()] + LAYOUT_PROBE + src[last.start():] if last else src + LAYOUT_PROBE


def run_chrome(chrome: str, args: list, timeout: float = 60) -> str:
    with tempfile.TemporaryDirectory(prefix="revizor-chrome-") as prof:
        proc = subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                               "--no-default-browser-check", "--allow-file-access-from-files",
                               f"--user-data-dir={prof}", "--virtual-time-budget=6000"] + args,
                              capture_output=True, text=True, timeout=timeout)
    return proc.stdout


def layout_pass(doc: Path):
    """Проверка вёрстки на 1440 и 390 px и скриншот на 1440. None – Chrome не найден."""
    chrome = chrome_path()
    if not chrome:
        return None
    page = CACHE / "check" / f"{sha(str(doc))}.layout.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(page, probe_page(doc))
    warns, height = [], 900
    for width in LAYOUT_WIDTHS:
        try:
            dom = run_chrome(chrome, [f"--window-size={width},900", "--dump-dom", page.as_uri()])
        except (OSError, subprocess.SubprocessError) as e:
            print(f"revizor: Chrome не отработал: {e}", file=sys.stderr)
            return None
        m = re.search(r'<pre id="__revizor_layout">(.*?)</pre>', dom, re.S)
        if not m:
            warns.append({"width": width, "kind": "probe", "where": "страница",
                          "msg": "проба не отработала: страница не загрузилась за 6 с или упала"})
            continue
        data = json.loads(html.unescape(m.group(1)))
        if width == LAYOUT_WIDTHS[0]:
            height = data.get("h") or height
        for w in data["w"]:
            w["width"] = width
            if not any(x["kind"] == w["kind"] and x["where"] == w["where"] for x in warns):
                warns.append(w)
    shot = CACHE / "check" / f"{sha(str(doc))}.png"
    try:
        run_chrome(chrome, [f"--window-size={LAYOUT_WIDTHS[0]},{min(max(int(height), 900), 16000)}",
                            f"--screenshot={shot}", page.as_uri()])
    except (OSError, subprocess.SubprocessError):
        shot = None
    return warns, (shot if shot and shot.is_file() else None)


def cmd_check(args) -> int:
    """Проверка до показа владельцу. Смысловой проход делает независимый читатель (`claude -p` без контекста автора),
    вёрстку HTML – headless Chrome. Агент исправляет найденное в исходнике, владелец получает документ уже чистым."""
    doc = resolve_doc(args)
    text = visible_text(doc)
    out = CACHE / "check" / f"{sha(str(doc))}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(out, text)
    lang = text_lang(text)
    skill, catalog, where = catalog_for(lang)
    base = rule_ids(skill, lang)
    bundle = is_markup(doc) and len(text) < 200
    print(f"Проверка до показа: {display(doc)}")
    print(f"Видимый текст: {out} ({len(text)} знаков)" + (" – бандл без текста: проверять исходник дека" if bundle else ""))
    found = None if (args.self or bundle) else reader_pass(text, catalog, base, lang)
    if found is None:
        print("\n".join([
            f"Язык: {lang}",
            f"Каталог примет: {catalog}{where}",
            f"Регулярки ловят сами, не искать: {base}",
            ("Независимого читателя нет (нужен `claude` в PATH), самопроверка автором. " if not args.self and not bundle else "")
            + "Найти приметы по разделу «Самопроверка до показа» скилла revizor и исправить их в исходнике документа. "
            "Пересобрать экспорт, если он есть, и только потом открывать редактор.",
        ]))
    else:
        saved = CACHE / "check" / f"{sha(str(doc))}.findings.json"
        atomic_write(saved, json.dumps(found, ensure_ascii=False, indent=2))
        print(f"Независимый читатель ({READER_MODEL}, каталог{where}): {len(found)} мест. JSON: {saved}")
        for f in found:
            rw = f.get("rewrite")
            tail = " → удалить" if rw == "" else f" → {rw}" if isinstance(rw, str) else ""
            print(f"  [{f.get('rule', '?')}] «{f['quote'][:160]}»{tail}" + (f"  ({f['why']})" if f.get("why") else ""))
        if found:
            print("Исправить каждое место в исходнике по скиллу стиля: факты и цифры не трогать. Читатель ошибается – "
                  "находку, с которой не согласен, пропустить и сказать владельцу одной строкой. "
                  "Пересобрать экспорт и только потом открывать редактор.")
    dups = repeated_sentences(text)
    if dups:
        print("Повторы предложений – оставить там, где отвечают на вопрос раздела:")
        for s, n in dups[:10]:
            print(f"  ×{n} {s[:120]}")
    if is_markup(doc) and not args.no_layout and os.environ.get("REVIZOR_NO_LAYOUT") != "1":
        res = layout_pass(doc)
        if res is None:
            print("Вёрстка: Chrome не найден, проверка пропущена.")
        else:
            warns, shot = res
            print(f"Вёрстка (1440 и 390 px): {len(warns)} замечаний." + (f" Скриншот 1440: {shot}" if shot else ""))
            for w in warns[:30]:
                print(f"  [{w['kind']}, {w['width']} px] {w['where']}: {w['msg']}")
            if warns:
                print("Это эвристики: исправить настоящие поломки, ложные пропустить. Скриншот посмотреть глазами.")
    return 0


def cmd_suggest(args) -> int:
    """Находки агента → файл правок. Цитата обязана быть точной подстрокой текста страницы, иначе подсветить нечего."""
    doc = resolve_doc(args)
    raw = sys.stdin.read() if args.findings == "-" else Path(args.findings).expanduser().read_text(encoding="utf-8")
    found = json.loads(raw)
    found = found.get("findings", []) if isinstance(found, dict) else found
    cache = json.loads(text_cache(doc).read_text(encoding="utf-8")) if text_cache(doc).is_file() else {"roots": []}
    roots = {r.get("key", ""): r["text"] for r in cache["roots"]}
    if not roots and not is_markup(doc):
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
    doc = resolve_doc(args)
    # wait стартует вместе с повторным open: пока open не открыл ревью заново, в файле ещё старый «завершено»
    grace = time.monotonic() + WAIT_GRACE
    orphaned = threading.Event()
    watch_owner(orphaned)
    opened = None  # метка открытия, при которой wait стартовал
    while True:
        if orphaned.is_set():
            print(f"Сессия, запустившая wait, закрыта: Live Edit для {doc.name} остановлен.")
            return 0
        got = take_sent(doc)
        if got is None and time.monotonic() < grace:
            time.sleep(args.interval)
            continue
        if got is None:
            print(f"Ревью {doc.name} завершено, Live Edit остановлен. Оставшиеся правки – в выводе open.")
            return 0
        fresh, review = got
        if review is not None:
            mark = review.get("opened")
            if opened is None:
                opened = mark
            elif mark != opened:
                print(f"{doc.name} открыт заново другим запуском open: этот Live Edit остановлен, правки заберёт новый wait.")
                return 0
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
    p = sub.add_parser("open", help="открыть документ в редакторе и ждать «Завершить раунд» или «Завершить работу»")
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
    p.add_argument("--note", help="одна пометка на все id")
    p.add_argument("--notes", help='пометки по id: JSON {"ID": "что сделано"}, файл или «-» (stdin)')
    p.set_defaults(fn=cmd_mark)
    p = sub.add_parser("wait", help="Live Edit: ждать правку, отправленную из редактора, и вывести её")
    p.add_argument("file")
    p.add_argument("--interval", type=float, default=1.0)
    p.set_defaults(fn=cmd_wait)
    p = sub.add_parser("check", help="проверка до показа: независимый читатель на нейрослоп и вёрстка HTML")
    p.add_argument("file")
    p.add_argument("--self", action="store_true", help="без независимого читателя: приметы ищет сам агент")
    p.add_argument("--no-layout", action="store_true", help="без проверки вёрстки в Chrome")
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("ps", help="какие документы Ревизор сейчас держит открытыми")
    p.set_defaults(fn=cmd_ps)
    p = sub.add_parser("stop", help="остановить сервер документа или все (--all)")
    p.add_argument("file", nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_stop)
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
    p.add_argument("--discover", action="store_true", help="ещё и поиск Spotlight по всему диску (macOS), разово для старых ревью")
    p.add_argument("--max", type=int, default=40, help="сколько сигналов в порции, остальное – в следующий раз")
    p.set_defaults(fn=cmd_pairs)
    p = sub.add_parser("notes", help="разобранные комментарии владельца для обучения плейбука документов (JSON)")
    p.add_argument("--ack", action="store_true", help="отметить последнюю порцию разобранной")
    p.add_argument("--count", action="store_true", help="только число новых комментариев")
    p.add_argument("--dir", action="append", help="искать файлы правок ещё и здесь")
    p.add_argument("--discover", action="store_true", help="ещё и поиск Spotlight по всему диску (macOS), разово для старых ревью")
    p.add_argument("--max", type=int, default=80, help="сколько комментариев в порции, остальное – в следующий раз")
    p.set_defaults(fn=cmd_notes)
    p = sub.add_parser("update", help="обновить Ревизор из репо (git pull) и показать, что поменялось")
    p.add_argument("--check", action="store_true", help="только проверить, есть ли новая версия (кэш на сутки)")
    p.add_argument("--force", action="store_true", help="с --check: спросить репо сейчас, мимо кэша")
    p.set_defaults(fn=cmd_update)
    args = ap.parse_args(argv)
    if fcntl is None and args.cmd in ("open", "mark", "wait", "suggest"):  # им нужна блокировка файла правок
        print("revizor: Windows пока не поддерживается, нужен macOS или Linux (можно WSL).", file=sys.stderr)
        return 2
    try:
        return args.fn(args)
    except ReviewError as e:
        print(f"revizor: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
