import http.client
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import revizor  # noqa: E402


def post(url, data):
    req = urllib.request.Request(url, json.dumps(data).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.doc = Path(self.tmp.name) / "report.md"
        self.doc.write_text("# Итоги\n\nКонверсия выросла на фоне нового онбординга.\n", encoding="utf-8")
        self.done = threading.Event()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), revizor.make_handler(self.doc, self.done))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def edit(self, id_="r1", state="pending"):
        return {"id": id_, "kind": "edit", "scope": "text", "start": 9, "end": 26, "quote": "Конверсия выросла",
                "prefix": "# Итоги\n\n", "suffix": " на фоне", "replacement": "Конверсия +3 п.п.", "state": state}

    def test_state_returns_doc_and_empty_review(self):
        data = json.loads(urllib.request.urlopen(self.base + "/api/state").read())
        self.assertEqual(data["doc"]["type"], "md")
        self.assertEqual(data["review"]["items"], [])

    def test_text_shell_keeps_text_and_loads_overlay(self):
        page = urllib.request.urlopen(self.base + "/").read().decode()
        self.assertIn('data-revizor-mode="md"', page)
        self.assertIn('<base href="/doc/">', page)
        self.assertIn('<article id="revizor-md"></article>', page)
        self.assertIn("Конверсия выросла на фоне нового онбординга.", page)
        self.assertIn("/_revizor/overlay.js", page)
        self.assertIn(b"__revizor", urllib.request.urlopen(self.base + "/_revizor/overlay.js").read())

    def test_save_writes_sidecar_next_to_doc(self):
        post(self.base + "/api/review", {"register": "report", "items": [self.edit()]})
        side = self.tmp.name + "/report.review.json"
        saved = json.loads(Path(side).read_text(encoding="utf-8"))
        self.assertEqual(saved["register"], "report")
        self.assertEqual(saved["status"], "open")
        self.assertEqual(saved["items"][0]["replacement"], "Конверсия +3 п.п.")
        self.assertFalse(self.done.is_set())

    def test_overlay_bundle_isolates_motion(self):
        js = urllib.request.urlopen(self.base + "/_revizor/overlay.js").read().decode()
        self.assertIn("var define,exports,module;var prev=window.Motion;", js)
        self.assertIn("window.__revizorMotion=window.Motion", js)
        self.assertLess(js.index("__revizorMotion=window.Motion"), js.index("window.__revizor = true"))

    def test_overlay_header_compacts_on_scroll(self):
        js = urllib.request.urlopen(self.base + "/_revizor/overlay.js").read().decode()
        self.assertIn("#panel.compact h1", js)
        self.assertIn('cardsBox.addEventListener("scroll", syncCompact', js)
        self.assertIn("Общий комментарий", js)
        self.assertNotIn("Весь документ", js)

    def test_md_bundle_has_parser_only_for_markdown(self):
        js = urllib.request.urlopen(self.base + "/_revizor/overlay.js").read().decode()
        self.assertIn("window.__revizorMd=__revizorMd", js)

    def test_summary_gives_line_numbers(self):
        r = revizor.load(self.doc)
        r["items"] = [self.edit(), {"id": "b", "kind": "comment", "scope": "block", "selector": "p", "snippet": "абзац",
                                    "lines": [3, 3], "comment": "короче", "state": "pending"}]
        out = revizor.summary(self.doc, r)
        self.assertIn("правка · строка 3", out)
        self.assertIn("строка: 3", out)

    def test_fonts_served_and_vendor_locked(self):
        r = urllib.request.urlopen(self.base + "/_revizor/vendor/playfair-700-italic-cyrillic.woff2")
        self.assertEqual(r.headers["Content-Type"], "font/woff2")
        self.assertEqual(r.read()[:4], b"wOF2")
        for bad in ("/_revizor/vendor/motion.js", "/_revizor/vendor/..%2F..%2Frevizor.py", "/_revizor/vendor/nope.woff2"):
            with self.assertRaises(urllib.error.HTTPError, msg=bad):
                urllib.request.urlopen(self.base + bad)

    def test_register_auto_saved(self):
        post(self.base + "/api/review", {"register": "deck", "register_auto": True, "items": []})
        saved = revizor.load(self.doc)
        self.assertEqual((saved["register"], saved["register_auto"]), ("deck", True))

    def test_done_sets_status_and_event(self):
        post(self.base + "/api/done", {"items": [self.edit()]})
        self.assertTrue(self.done.wait(1))
        self.assertEqual(revizor.load(self.doc)["status"], "done")

    def test_open_tab_cannot_resurrect_item_agent_closed(self):
        post(self.base + "/api/review", {"items": [self.edit("r1")]})
        r = revizor.load(self.doc)
        r["items"][0]["state"] = "applied"
        revizor.save(self.doc, r)
        resp = post(self.base + "/api/review", {"items": [self.edit("r1")]})  # вкладка ещё держит r1
        self.assertEqual([i["state"] for i in revizor.load(self.doc)["items"]], ["applied"])
        self.assertEqual([c["id"] for c in resp["closed"]], ["r1"])

    def test_page_cannot_drop_closed_items(self):
        r = revizor.load(self.doc)
        r["items"] = [self.edit("old", state="applied")]
        revizor.save(self.doc, r)
        post(self.base + "/api/review", {"items": [self.edit("new")]})
        ids = {i["id"]: i["state"] for i in revizor.load(self.doc)["items"]}
        self.assertEqual(ids, {"old": "applied", "new": "pending"})


class HardeningTest(ServerTest.__base__):
    """Сервер на 127.0.0.1 доступен любой вкладке браузера: чужие сайты, DNS-rebinding, кривые запросы."""

    setUp, tearDown, edit = ServerTest.setUp, ServerTest.tearDown, ServerTest.edit

    def raw(self, method, path, body=b"", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        h = {"Host": f"127.0.0.1:{self.server.server_address[1]}", "Content-Length": str(len(body))}
        h.update(headers or {})
        for k, v in h.items():
            if v is not None:
                c.putheader(k, v)
        c.endheaders(body)
        r = c.getresponse()
        out = (r.status, r.read())
        c.close()
        return out

    def json_post(self, path, body, **headers):
        return self.raw("POST", path, json.dumps(body).encode(), dict({"Content-Type": "application/json"}, **headers))[0]

    def test_foreign_host_is_refused(self):
        self.assertEqual(self.raw("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.raw("GET", "/api/state", headers={"Host": f"localhost:{self.server.server_address[1]}"})[0], 200)

    def test_cross_site_post_is_refused(self):
        post(self.base + "/api/review", {"items": [self.edit()]})
        self.assertEqual(self.raw("POST", "/api/review", b'{"items": []}', {"Content-Type": "text/plain"})[0], 403)
        self.assertEqual(self.json_post("/api/review", {"items": []}, Origin="https://evil.example"), 403)
        self.assertEqual(self.json_post("/api/done", {"items": []}, **{"Sec-Fetch-Site": "cross-site"}), 403)
        self.assertEqual(len(revizor.load(self.doc)["items"]), 1)
        self.assertFalse(self.done.is_set())
        self.assertEqual(self.json_post("/api/review", {"items": []}, Origin=self.base, **{"Sec-Fetch-Site": "same-origin"}), 200)

    def test_hidden_files_and_sidecars_are_closed(self):
        d = Path(self.tmp.name)
        (d / "sub" / ".git").mkdir(parents=True)
        (d / "sub" / ".git" / "config").write_text("secret", encoding="utf-8")
        (d / ".env").write_text("TOKEN=1", encoding="utf-8")
        (d / "other.review.json").write_text("{}", encoding="utf-8")
        (d / "img.png").write_bytes(b"png")
        for path in ("/doc/sub/.git/config", "/doc/.env", "/doc/OTHER.REVIEW.JSON", "/doc/other.review.json.tmp"):
            self.assertEqual(self.raw("GET", path)[0], 404, path)
        self.assertEqual(self.raw("GET", "/doc/img.png"), (200, b"png"))
        self.assertEqual(self.raw("GET", "/doc/%00")[0], 400)

    def test_bad_bodies_get_errors_and_server_survives(self):
        for body in (b"[]", b"{bad", json.dumps({"items": [1]}).encode(), json.dumps({"items": [{"kind": "edit"}]}).encode()):
            self.assertEqual(self.raw("POST", "/api/review", body, {"Content-Type": "application/json"})[0], 400, body)
        self.assertEqual(self.raw("POST", "/api/review", b"", {"Content-Type": "application/json", "Content-Length": "-1"})[0], 413)
        self.assertEqual(self.raw("POST", "/api/scan", b"x" * 10,
                                  {"Content-Type": "application/json", "Content-Length": str(revizor.MAX_BODY + 1)})[0], 413)
        self.assertEqual(self.raw("GET", "/api/state")[0], 200)

    def test_broken_sidecar_is_reported_and_kept(self):
        revizor.sidecar(self.doc).write_text("{не json", encoding="utf-8")
        status, body = self.raw("GET", "/api/state")
        self.assertEqual(status, 500)
        self.assertIn("не читается", json.loads(body)["error"])
        self.assertEqual(self.json_post("/api/review", {"items": []}), 500)
        self.assertEqual(revizor.sidecar(self.doc).read_text(encoding="utf-8"), "{не json")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(revizor.main(["show", str(self.doc)]), 2)
        self.assertIn("не читается", err.getvalue())


class SidecarTest(unittest.TestCase):
    """report.md и его экспорт report.html в одной папке – правки не смешиваются."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_doc_keeps_short_name(self):
        (self.d / "report.md").write_text("x", encoding="utf-8")
        self.assertEqual(revizor.sidecar(self.d / "report.md").name, "report.review.json")

    def test_twins_get_own_files(self):
        md, html = self.d / "report.md", self.d / "report.html"
        md.write_text("x", encoding="utf-8")
        html.write_text("<p>x</p>", encoding="utf-8")
        self.assertEqual(revizor.sidecar(md).name, "report.md.review.json")
        self.assertEqual(revizor.sidecar(html).name, "report.html.review.json")

    def test_old_short_file_stays_with_its_doc(self):
        md, html = self.d / "report.md", self.d / "report.html"
        md.write_text("x", encoding="utf-8")
        r = revizor.load(md)
        r["items"] = [{"id": "a", "quote": "x", "state": "pending"}]
        revizor.save(md, r)  # ревью до появления экспорта – короткое имя
        html.write_text("<p>x</p>", encoding="utf-8")
        self.assertEqual(revizor.sidecar(md).name, "report.review.json")
        self.assertEqual(revizor.sidecar(html).name, "report.html.review.json")
        self.assertEqual(revizor.load(html)["items"], [])
        self.assertEqual(len(revizor.load(md)["items"]), 1)


class HtmlServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name) / "reports"
        d.mkdir()
        (d / "img").mkdir()
        (d / "img" / "chart.svg").write_text("<svg/>", encoding="utf-8")
        (Path(self.tmp.name) / "secret.txt").write_text("нельзя", encoding="utf-8")
        self.doc = d / "market.html"
        self.doc.write_text("<html><body><section><h2>Рынок</h2><p>Текст</p></section></BODY></html>", encoding="utf-8")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), revizor.make_handler(self.doc, threading.Event()))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def get(self, path):
        try:
            r = urllib.request.urlopen(self.base + path)
            return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, ""

    def test_root_redirects_to_doc_with_overlay_before_body_end(self):
        code, page = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn('<script src="/_revizor/overlay.js" defer></script></BODY>', page)

    def test_relative_assets_served(self):
        self.assertEqual(self.get("/doc/img/chart.svg"), (200, "<svg/>"))

    def test_no_access_outside_doc_folder_or_to_sidecar(self):
        revizor.save(self.doc, revizor.load(self.doc))
        self.assertEqual(self.get("/doc/../secret.txt")[0], 404)
        self.assertEqual(self.get("/doc/%2e%2e/secret.txt")[0], 404)
        self.assertEqual(self.get("/doc/market.review.json")[0], 404)

    def test_block_comment_in_summary(self):
        r = revizor.load(self.doc)
        r["items"] = [{"id": "b1", "kind": "comment", "scope": "block", "selector": "section:nth-of-type(1)",
                       "snippet": "Рынок Текст", "where": "Рынок", "comment": "добавь источник", "state": "pending"}]
        out = revizor.summary(self.doc, r)
        self.assertIn("комментарий к блоку · Рынок", out)
        self.assertIn("добавь источник", out)


class PlainTextTest(unittest.TestCase):
    def test_txt_has_single_view_and_no_parser(self):
        with tempfile.TemporaryDirectory() as d:
            doc = Path(d) / "note.txt"
            doc.write_text("# не заголовок\nпросто текст", encoding="utf-8")
            page = revizor.text_shell(doc)
            self.assertIn('data-revizor-mode="text"', page)
            self.assertNotIn("revizor-md", page.split("</style>")[1])
            self.assertNotIn(b"__revizorMd=", revizor.overlay_bundle(with_md=False))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.doc = Path(self.tmp.name) / "note.txt"
        self.doc.write_text("Текст", encoding="utf-8")
        r = revizor.load(self.doc)
        r["items"] = [
            {"id": "a", "kind": "edit", "scope": "text", "quote": "Текст", "replacement": "Новый текст",
             "why": "конкретнее", "state": "pending"},
            {"id": "b", "kind": "comment", "scope": "doc", "comment": "короче", "state": "pending"},
        ]
        revizor.save(self.doc, r)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            revizor.main(list(argv))
        return buf.getvalue()

    def test_show_lists_pending(self):
        out = self.run_cli("show", str(self.doc))
        self.assertIn("2 ждут агента", out)
        self.assertIn("стало: Новый текст", out)
        self.assertIn("почему: конкретнее", out)
        self.assertIn("общий комментарий", out)

    def test_mark_resolves_items(self):
        self.run_cli("mark", str(self.doc), "a", "--state", "applied")
        self.run_cli("mark", str(self.doc), "b", "--state", "rejected", "--note", "уже коротко")
        items = {i["id"]: i for i in revizor.load(self.doc)["items"]}
        self.assertEqual(items["a"]["state"], "applied")
        self.assertEqual(items["b"]["note"], "уже коротко")
        self.assertIn("0 ждут агента", self.run_cli("show", str(self.doc)))

    def test_mark_unknown_id_fails(self):
        with self.assertRaises(SystemExit):
            self.run_cli("mark", str(self.doc), "zzz", "--state", "applied")



class LiveEditTest(ServerTest.__base__):
    """Live Edit: страница отправляет правку, `wait` её забирает, агент отмечает. Состояние общее – файл правок."""

    tearDown, edit = ServerTest.tearDown, ServerTest.edit

    def setUp(self):
        ServerTest.setUp(self)
        self.addCleanup(setattr, revizor, "WAIT_GRACE", revizor.WAIT_GRACE)
        revizor.WAIT_GRACE = 0

    def wait(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            revizor.main(["wait", str(self.doc), "--interval", "0.05"])
        return buf.getvalue()

    def test_wait_takes_only_sent_items(self):
        post(self.base + "/api/review", {"live": True, "items": [dict(self.edit("a"), sent="2026-10-22T10:00:00Z"),
                                                                  self.edit("b")]})
        out = self.wait()
        self.assertIn("Live Edit: 1 правка", out)
        self.assertIn("[a]", out)
        self.assertNotIn("[b]", out)
        items = {i["id"]: i for i in revizor.load(self.doc)["items"]}
        self.assertTrue(items["a"].get("taken"))
        self.assertFalse(items["b"].get("taken"))
        self.assertTrue(revizor.load(self.doc)["live"])

    def test_page_cannot_change_or_drop_taken_item(self):
        post(self.base + "/api/review", {"items": [dict(self.edit("a"), sent="t")]})
        self.wait()
        # страница ещё не узнала, что агент забрал правку: прислала её изменённой, а потом без неё
        post(self.base + "/api/review", {"items": [dict(self.edit("a"), replacement="другое", sent="t")]})
        post(self.base + "/api/review", {"items": []})
        items = revizor.load(self.doc)["items"]
        self.assertEqual([(i["id"], i["replacement"]) for i in items], [("a", "Конверсия +3 п.п.")])

    def test_done_summary_skips_taken_items(self):
        post(self.base + "/api/review", {"items": [dict(self.edit("a"), sent="t"), self.edit("b")]})
        self.wait()
        out = revizor.summary(self.doc, revizor.load(self.doc))
        self.assertIn("1 ждут агента", out)
        self.assertIn("Ещё применяются в Live Edit: a", out)
        self.assertNotIn("[a]", out)

    def test_wait_stops_when_review_finished(self):
        post(self.base + "/api/done", {"items": [dict(self.edit("a"), sent="t")]})
        self.assertIn("завершено", self.wait())
        self.assertFalse(revizor.load(self.doc)["items"][0].get("taken"))

    def test_wait_outlasts_old_done_while_reopening(self):
        post(self.base + "/api/done", {"items": []})
        revizor.WAIT_GRACE = 5
        out = []
        t = threading.Thread(target=lambda: out.append(self.wait()))
        t.start()
        time.sleep(0.3)  # wait уже прочитал старый «завершено», а open только сейчас открывает ревью заново
        r = revizor.load(self.doc)
        r["status"] = "open"
        revizor.save(self.doc, r)
        post(self.base + "/api/review", {"items": [dict(self.edit("a"), sent="t")]})
        t.join(5)
        self.assertIn("Live Edit: 1 правка", out[0])

    def test_source_endpoint_serves_current_text(self):
        self.doc.write_text("# Новое\n", encoding="utf-8")
        self.assertEqual(urllib.request.urlopen(self.base + "/api/source").read().decode(), "# Новое\n")

    def test_overlay_has_live_ui(self):
        js = urllib.request.urlopen(self.base + "/_revizor/overlay.js").read().decode()
        for s in ("Live Edit", "Отправить агенту", "Агент применяет…", "Завершить ревью", "/api/source"):
            self.assertIn(s, js)
        self.assertNotIn(">Готово<", js)



class StyleSkillTest(unittest.TestCase):
    """Скилл стиля находится сам, без настройки: по revizor.json, каталогу примет или описанию в SKILL.md."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.skills = Path(self.tmp.name).resolve()
        self.saved = (revizor.SKILLS_DIR, revizor.CONFIG, os.environ.pop("REVIZOR_STYLE_SKILL", None))
        revizor.SKILLS_DIR, revizor.CONFIG = self.skills, self.skills / "no-config.json"

    def tearDown(self):
        revizor.SKILLS_DIR, revizor.CONFIG = self.saved[0], self.saved[1]
        if self.saved[2] is not None:
            os.environ["REVIZOR_STYLE_SKILL"] = self.saved[2]
        self.tmp.cleanup()

    def skill(self, name, skill_md="", files=()):
        d = self.skills / name
        (d / "references").mkdir(parents=True)
        (d / "SKILL.md").write_text(skill_md, encoding="utf-8")
        for f in files:
            (d / f).write_text("", encoding="utf-8")
        return d

    def test_finds_skill_with_tells_catalog(self):
        self.skill("deploy", "---\nname: deploy\n---")
        self.skill("writing-style", "---\nname: writing-style\ndescription: канон стиля\n---", ["references/ai-tells-ru.md"])
        self.assertEqual(revizor.find_style_skill()["name"], "writing-style")

    def test_manifest_wins_and_nothing_found_is_none(self):
        self.assertIsNone(revizor.find_style_skill())
        self.skill("voice", "стиль", ["references/ai-tells-ru.md"])
        self.skill("my-own", "", ["revizor.json"])
        self.assertEqual(revizor.find_style_skill()["name"], "my-own")

    def test_skill_rules_extend_replace_and_switch_off(self):
        d = self.skill("writing-style", "стиль", ["references/ai-tells-ru.md"])
        (d / "revizor-rules.json").write_text(json.dumps({"rules": [
            {"id": "mine", "title": "Своё", "pattern": "x", "why": "y"},
            {"id": "filler", "title": "Филлер по-моему", "pattern": "в целом", "why": "z"},
            {"id": "em-dash", "off": True}]}), encoding="utf-8")
        rules = {r["id"]: r for r in revizor.load_rules(revizor.find_style_skill())["rules"]}
        self.assertEqual(rules["mine"]["source"], "writing-style")
        self.assertEqual(rules["filler"]["title"], "Филлер по-моему")
        self.assertNotIn("em-dash", rules)
        self.assertEqual(rules["ne-pro"]["source"], "base")

    def test_broken_skill_rules_fall_back_to_base(self):
        d = self.skill("writing-style", "стиль", ["references/ai-tells-ru.md"])
        (d / "revizor-rules.json").write_text("{не json", encoding="utf-8")
        self.assertIn("ne-pro", {r["id"] for r in revizor.load_rules(revizor.find_style_skill())["rules"]})

    def test_text_lang_by_main_alphabet(self):
        self.assertEqual(revizor.text_lang("Конверсия выросла после релиза v2.1"), "ru")
        self.assertEqual(revizor.text_lang("Conversion grew – см. отчёт"), "en")

    def test_catalog_follows_document_language(self):
        d = self.skill("writing-style", "стиль", ["references/ai-tells-ru.md", "references/ai-tells-en.md"])
        sk = revizor.find_style_skill()
        self.assertEqual(Path(revizor.skill_files(sk, "en")["catalog"]), d / "references/ai-tells-en.md")
        self.assertEqual(Path(revizor.skill_files(sk)["catalog"]), d / "references/ai-tells-ru.md")

    def test_catalog_for_missing_language_is_new_file(self):
        d = self.skill("writing-style", "стиль", ["references/ai-tells-ru.md"])
        self.assertEqual(Path(revizor.skill_files(revizor.find_style_skill(), "en")["catalog"]), d / "references/ai-tells-en.md")
        g = self.skill("generic", "", ["references/tells.md", "revizor.json"])
        self.assertEqual(Path(revizor.skill_files({"name": "generic", "path": str(g)}, "en")["catalog"]), g / "references/tells.md")

    def test_manifest_roles_by_language(self):
        d = self.skill("my-own", "", ["revizor.json"])
        (d / "revizor.json").write_text(json.dumps({"files": {
            "examples": {"ru": "ru/pairs.md", "en": "en/pairs.md"}, "voice": "voice.md"}}), encoding="utf-8")
        sk = revizor.find_style_skill()
        self.assertEqual(Path(revizor.skill_files(sk, "en")["examples"]), d / "en/pairs.md")
        self.assertEqual(Path(revizor.skill_files(sk, "ru")["examples"]), d / "ru/pairs.md")
        self.assertEqual(Path(revizor.skill_files(sk, "en")["voice"]), d / "voice.md")

    def test_style_skill_per_language_in_config(self):
        ru = self.skill("writing-style", "стиль", ["references/ai-tells-ru.md"])
        en = self.skill("writing-en", "", ["references/ai-tells-en.md"])
        revizor.CONFIG.write_text(json.dumps({"style_skill": {"ru": str(ru), "en": str(en)}}), encoding="utf-8")
        self.assertEqual(revizor.find_style_skill(lang="en")["name"], "writing-en")
        self.assertEqual(revizor.find_style_skill(lang="ru")["name"], "writing-style")
        self.assertEqual(revizor.find_style_skill()["name"], "writing-style")
        revizor.CONFIG.write_text(json.dumps({"style_skill": {"en": str(en)}}), encoding="utf-8")
        self.assertEqual(revizor.find_style_skill(lang="ru")["name"], "writing-style")  # языка нет в настройке – автопоиск

    def test_base_rules_are_complete(self):
        for r in revizor.load_rules()["rules"]:
            self.assertTrue(r["id"] and r["title"] and r["pattern"] and r["why"], r)


class RadarServerTest(ServerTest.__base__):
    setUp, tearDown, edit = ServerTest.setUp, ServerTest.tearDown, ServerTest.edit

    def test_rules_endpoint_and_dismissed_persist(self):
        data = json.loads(urllib.request.urlopen(self.base + "/api/rules").read())
        self.assertIn("ne-pro", {r["id"] for r in data["rules"]})
        post(self.base + "/api/review", {"items": [], "dismissed": [{"rule": "ne-pro", "quote": "не про", "prefix": "Это "}]})
        self.assertEqual(revizor.load(self.doc)["dismissed"][0]["rule"], "ne-pro")

    def test_summary_names_radar_rule(self):
        post(self.base + "/api/review", {"items": [dict(self.edit("a"), rule="ne-pro,em-dash", source="radar")]})
        self.assertIn("приём: ne-pro,em-dash", revizor.summary(self.doc, revizor.load(self.doc)))

    def test_overlay_has_radar(self):
        js = urllib.request.urlopen(self.base + "/_revizor/overlay.js").read().decode()
        for s in ("rv-slop", "Нейрослоп", "Это не нейрослоп", "/api/rules"):
            self.assertIn(s, js)



class SemanticScanTest(ServerTest.__base__):
    """Смысловая проверка: страница просит, `wait` выдаёт задание, агент пишет находки через `suggest`."""

    setUp, tearDown = ServerTest.setUp, ServerTest.tearDown

    def setUpTexts(self):
        self.saved_texts = revizor.TEXTS
        revizor.TEXTS = Path(self.tmp.name) / "texts"

    def cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            revizor.main(list(argv))
        return buf.getvalue()

    def request(self, text="# Итоги\n\nКонверсия выросла на фоне нового онбординга. Именно это и важно.\n"):
        self.setUpTexts()
        self.addCleanup(setattr, revizor, "TEXTS", self.saved_texts)
        return post(self.base + "/api/scan", {"roots": [{"key": "", "text": text}]})

    def test_scan_request_goes_to_wait_first(self):
        self.assertEqual(self.request()["scan"]["state"], "requested")
        post(self.base + "/api/review", {"items": [dict(ServerTest.edit(self), sent="t")]})
        out = self.cli("wait", str(self.doc), "--interval", "0.05")
        self.assertIn("Проверка на нейрослоп", out)
        self.assertIn(str(revizor.text_cache(self.doc)), out)
        self.assertEqual(revizor.load(self.doc)["scan"]["state"], "taken")
        self.assertIn("Live Edit: 1 правка", self.cli("wait", str(self.doc), "--interval", "0.05"))

    def test_suggest_anchors_quotes_and_reports_missing(self):
        self.request()
        f = Path(self.tmp.name) / "f.json"
        f.write_text(json.dumps([
            {"quote": "Именно это и важно.", "rewrite": "", "rule": "moral", "title": "Мораль", "why": "пересказ"},
            {"quote": "нет такого", "rule": "x"},
            {"quote": "Конверсия выросла", "rule": "y", "title": "Y"}]), encoding="utf-8")
        out = self.cli("suggest", str(self.doc), str(f))
        self.assertIn("Находок записано: 2", out)
        self.assertIn("нет такого", out)
        review = revizor.load(self.doc)
        moral = review["findings"][0]
        self.assertEqual(moral["rewrite"], "")
        self.assertIsNone(review["findings"][1]["rewrite"])
        self.assertEqual(moral["prefix"][-8:], "нбординга. "[-8:])
        self.assertEqual(review["scan"]["state"], "done")

    def test_page_save_keeps_findings_and_scan(self):
        self.request()
        f = Path(self.tmp.name) / "f.json"
        f.write_text(json.dumps([{"quote": "Именно это и важно."}]), encoding="utf-8")
        self.cli("suggest", str(self.doc), str(f))
        post(self.base + "/api/review", {"items": []})
        review = revizor.load(self.doc)
        self.assertEqual(len(review["findings"]), 1)
        self.assertEqual(review["scan"]["state"], "done")

    def test_scan_uses_catalog_of_text_language(self):
        skills = Path(self.tmp.name).resolve() / "skills"
        (skills / "writing-style" / "references").mkdir(parents=True)
        for f in ("ai-tells-ru.md", "ai-tells-en.md"):
            (skills / "writing-style" / "references" / f).write_text("", encoding="utf-8")
        saved = (revizor.SKILLS_DIR, revizor.CONFIG, os.environ.pop("REVIZOR_STYLE_SKILL", None))
        revizor.SKILLS_DIR, revizor.CONFIG = skills, skills / "no-config.json"
        self.addCleanup(lambda: (setattr(revizor, "SKILLS_DIR", saved[0]), setattr(revizor, "CONFIG", saved[1]),
                                 saved[2] is not None and os.environ.__setitem__("REVIZOR_STYLE_SKILL", saved[2])))
        self.request("# Results\n\nConversion grew after the new onboarding. And that changes everything.\n")
        out = self.cli("wait", str(self.doc), "--interval", "0.05")
        self.assertIn("Язык: en", out)
        self.assertIn("ai-tells-en.md (скилл writing-style)", out)

    def test_scan_needs_text(self):
        with self.assertRaises(urllib.error.HTTPError):
            post(self.base + "/api/scan", {"roots": []})



class LearnTest(unittest.TestCase):
    """Обучение: пары из файлов правок, журнал в скилле, скилл из шаблона, если своего нет."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.saved = {k: getattr(revizor, k) for k in ("SKILLS_DIR", "CONFIG", "CACHE", "REGISTRY", "BATCH")}
        self.saved_which = revizor.shutil.which
        revizor.SKILLS_DIR, revizor.CONFIG = t / "skills", t / "no-config.json"
        revizor.CACHE, revizor.REGISTRY, revizor.BATCH = t / "cache", t / "cache" / "docs.json", t / "cache" / "batch.json"
        revizor.shutil.which = lambda name: None  # Spotlight в тестах не трогаем: только реестр
        self.env = os.environ.pop("REVIZOR_STYLE_SKILL", None)
        self.doc = t / "work" / "report.md"
        self.doc.parent.mkdir()
        self.doc.write_text("Текст", encoding="utf-8")
        revizor.remember_doc(self.doc)
        r = revizor.load(self.doc)
        r["register"] = "report"
        r["items"] = [
            {"id": "a", "kind": "edit", "scope": "text", "quote": "Локальная страница", "replacement": "Мини-тула", "state": "applied"},
            {"id": "b", "kind": "edit", "scope": "text", "quote": "так", "replacement": "так ", "state": "applied"},
            {"id": "c", "kind": "edit", "scope": "text", "quote": "x", "replacement": "y", "state": "pending"},
            {"id": "d", "kind": "edit", "scope": "text", "quote": "Однако, нет", "replacement": "Однако нет", "state": "applied", "rule": "odnako", "source": "radar"},
            {"id": "e", "kind": "comment", "scope": "doc", "comment": "короче", "state": "applied"}]
        r["dismissed"] = [{"rule": "ne-pro", "quote": "не про", "prefix": "это "}]
        revizor.save(self.doc, r)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(revizor, k, v)
        revizor.shutil.which = self.saved_which
        if self.env is not None:
            os.environ["REVIZOR_STYLE_SKILL"] = self.env
        self.tmp.cleanup()

    def cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            revizor.main(list(argv))
        return buf.getvalue()

    def test_creates_skill_from_template_when_none(self):
        out = json.loads(self.cli("pairs"))
        self.assertTrue(out["created"])
        d = Path(out["skill"]["path"])
        self.assertEqual(d.name, "writing-style")
        self.assertIn("name: writing-style", (d / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue((d / "references" / "ai-tells-ru.md").is_file())
        self.assertEqual(revizor.find_style_skill()["name"], "writing-style")  # создан – дальше находится сам
        self.assertFalse(json.loads(self.cli("pairs"))["created"])

    def test_pairs_take_only_real_applied_edits(self):
        out = json.loads(self.cli("pairs"))
        self.assertEqual([p["before"] for p in out["pairs"]], ["Локальная страница"])
        self.assertEqual(out["pairs"][0]["register"], "report")
        self.assertEqual([c["rule"] for c in out["confirmed"]], ["odnako"])
        self.assertEqual([d["rule"] for d in out["dismissed"]], ["ne-pro"])

    def test_ack_moves_batch_to_ledger_with_stats(self):
        self.cli("pairs")
        self.assertIn("3 записей", self.cli("pairs", "--ack"))
        out = json.loads(self.cli("pairs"))
        self.assertEqual(out["pairs"] + out["dismissed"] + out["confirmed"], [])
        self.assertEqual(out["stats"]["odnako"]["taken"], 1)
        self.assertEqual(out["stats"]["ne-pro"]["dismissed"], 1)
        self.assertEqual(self.cli("pairs", "--count").strip(), "0")

    def test_learn_from_limits_where_skill_learns(self):
        revizor.CONFIG.write_text(json.dumps({"learn_from": str(self.doc.parent)}), encoding="utf-8")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.tmp.name)
        self.assertEqual(self.cli("pairs", "--count").strip(), "0")
        self.assertFalse((revizor.SKILLS_DIR / "writing-style").exists())  # и скилл не создан
        os.chdir(self.doc.parent)
        self.assertEqual(self.cli("pairs", "--count").strip(), "2")  # пары и отклонения; подтверждения не считаются

    def test_weak_style_skill_is_used_instead_of_new(self):
        d = revizor.SKILLS_DIR / "my-voice"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: my-voice\ndescription: мой стиль текстов\n---", encoding="utf-8")
        out = json.loads(self.cli("pairs"))
        self.assertFalse(out["created"])
        self.assertEqual(out["skill"]["name"], "my-voice")

    def test_ledger_keeps_hashes_and_migrates_paths(self):
        out = json.loads(self.cli("pairs"))
        self.assertRegex(out["pairs"][0]["key"], r"^[0-9a-f]{16}$")
        ledger = Path(out["files"]["ledger"])
        ledger.write_text(json.dumps({"seen": [f"{revizor.sidecar(self.doc)}#a"], "stats": {}}), encoding="utf-8")
        self.assertEqual(json.loads(self.cli("pairs"))["pairs"], [])  # старый ключ-путь узнан
        self.cli("pairs", "--ack")
        self.assertNotIn("/", ledger.read_text(encoding="utf-8").split('"seen"')[1].split("]")[0])

    def test_count_does_not_replace_batch(self):
        self.cli("pairs")
        r = revizor.load(self.doc)
        r["items"].append({"id": "g", "kind": "edit", "scope": "text", "quote": "старое", "replacement": "новое", "state": "applied"})
        revizor.save(self.doc, r)
        self.assertEqual(self.cli("pairs", "--count").strip(), "3")
        self.cli("pairs", "--ack")  # отмечается порция, которую агент видел, без новой правки
        self.assertEqual([p["before"] for p in json.loads(self.cli("pairs"))["pairs"]], ["старое"])

    def test_spotlight_only_on_discover(self):
        calls = []
        revizor.shutil.which = lambda name: "/usr/bin/mdfind"
        run = revizor.subprocess.run
        self.addCleanup(setattr, revizor.subprocess, "run", run)
        revizor.subprocess.run = lambda cmd, **kw: calls.append(cmd) or type("R", (), {"stdout": ""})()
        revizor.known_sidecars()
        self.assertEqual(calls, [])
        revizor.known_sidecars(discover=True)
        self.assertEqual(calls[0][0], "mdfind")

    def test_batch_is_capped(self):
        out = json.loads(self.cli("pairs", "--max", "2"))
        self.assertEqual(len(out["pairs"]) + len(out["dismissed"]) + len(out["confirmed"]), 2)

    def test_pairs_carry_language_and_its_files(self):
        r = revizor.load(self.doc)
        r["items"].append({"id": "f", "kind": "edit", "scope": "text", "quote": "This is not X, it is Y",
                           "replacement": "This is Y", "state": "applied"})
        revizor.save(self.doc, r)
        out = json.loads(self.cli("pairs"))
        langs = {p["before"]: p["lang"] for p in out["pairs"]}
        self.assertEqual(langs, {"Локальная страница": "ru", "This is not X, it is Y": "en"})
        self.assertTrue(out["langs"]["en"]["files"]["catalog"].endswith("references/ai-tells-en.md"))
        self.assertTrue(out["langs"]["ru"]["files"]["catalog"].endswith("references/ai-tells-ru.md"))

    def test_ack_goes_to_ledger_of_language_skill(self):
        en = revizor.SKILLS_DIR / "writing-en"
        (en / "references").mkdir(parents=True)
        (en / "references" / "ai-tells-en.md").write_text("", encoding="utf-8")
        revizor.CONFIG.write_text(json.dumps({"style_skill": {"en": str(en)}}), encoding="utf-8")
        r = revizor.load(self.doc)
        r["items"].append({"id": "f", "kind": "edit", "scope": "text", "quote": "not X, it is Y", "replacement": "Y", "state": "applied"})
        revizor.save(self.doc, r)
        out = json.loads(self.cli("pairs"))
        self.assertEqual(out["langs"]["en"]["skill"]["name"], "writing-en")
        self.cli("pairs", "--ack")
        en_seen = json.loads((en / "revizor-learned.json").read_text(encoding="utf-8"))["seen"]
        ru_seen = json.loads(Path(out["langs"]["ru"]["files"]["ledger"]).read_text(encoding="utf-8"))["seen"]
        self.assertEqual(len(en_seen), 1)
        self.assertEqual(len(ru_seen), 3)
        self.assertEqual(self.cli("pairs", "--count").strip(), "0")  # разобранное видно по обоим журналам

    def test_skill_files_follow_existing_layout(self):
        d = revizor.SKILLS_DIR / "writing-style" / "references"
        d.mkdir(parents=True)
        (d / "ai-tells-ru.md").write_text("", encoding="utf-8")
        (d.parent / "SKILL.md").write_text("стиль", encoding="utf-8")
        files = revizor.skill_files(revizor.find_style_skill())
        self.assertTrue(files["catalog"].endswith("references/ai-tells-ru.md"))
        self.assertTrue(files["ledger"].endswith("revizor-learned.json"))


if __name__ == "__main__":
    unittest.main()


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (revizor.UPDATE_CACHE, revizor.latest_release, os.environ.pop("REVIZOR_NO_UPDATE_CHECK", None))
        revizor.UPDATE_CACHE = Path(self.tmp.name) / "update.json"
        self.calls = 0

    def tearDown(self):
        revizor.UPDATE_CACHE, revizor.latest_release, env = self.saved
        os.environ.pop("REVIZOR_NO_UPDATE_CHECK", None)
        if env is not None:
            os.environ["REVIZOR_NO_UPDATE_CHECK"] = env
        self.tmp.cleanup()

    def fake(self, version):
        def latest(timeout=3.0):
            self.calls += 1
            return version
        revizor.latest_release = latest

    def test_newer_version_reported_and_cached(self):
        self.fake("99.0.0")
        info = revizor.check_update()
        self.assertEqual(info, {"current": revizor.__version__, "latest": "99.0.0"})
        revizor.check_update()  # второй раз – из кэша, без сети
        self.assertEqual(self.calls, 1)

    def test_same_or_older_version_is_quiet(self):
        self.fake(revizor.__version__)
        self.assertIsNone(revizor.check_update())
        self.fake("0.0.1")
        self.assertIsNone(revizor.check_update(force=True))

    def test_offline_is_quiet(self):
        self.fake(None)
        self.assertIsNone(revizor.check_update())

    def test_can_be_switched_off(self):
        self.fake("99.0.0")
        os.environ["REVIZOR_NO_UPDATE_CHECK"] = "1"
        self.assertIsNone(revizor.check_update())
        self.assertEqual(self.calls, 0)

    def test_update_moves_to_latest_tag_not_branch_head(self):
        if not revizor.shutil.which("git"):
            self.skipTest("нет git")
        t = Path(self.tmp.name)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        git = lambda cwd, *a: revizor.subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, env=env, text=True).stdout.strip()
        origin = t / "origin"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        (origin / "install.sh").write_text("true\n", encoding="utf-8")
        for v, tag in (("0.0.1", True), ("0.0.2", True), ("0.0.3", False)):
            (origin / "revizor.py").write_text(f'__version__ = "{v}"\n', encoding="utf-8")
            (origin / "CHANGELOG.md").write_text(f"## [{v}] – 2026-01-01\n", encoding="utf-8")
            git(origin, "add", "-A")
            git(origin, "commit", "-q", "-m", v)
            if tag:
                git(origin, "tag", f"v{v}")
        clone = t / "clone"
        revizor.subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
        git(clone, "reset", "-q", "--hard", "v0.0.1")
        saved = (revizor.ROOT, revizor.__version__)
        self.addCleanup(lambda: (setattr(revizor, "ROOT", saved[0]), setattr(revizor, "__version__", saved[1])))
        revizor.ROOT, revizor.__version__ = clone, "0.0.1"
        out = io.StringIO()
        with redirect_stdout(out):
            revizor.main(["update"])
        self.assertIn("0.0.1 → 0.0.2", out.getvalue())
        self.assertEqual(git(clone, "rev-parse", "HEAD"), git(origin, "rev-parse", "v0.0.2^{commit}"))

    def test_parse_version(self):
        self.assertEqual(revizor.parse_version("v0.3.1"), (0, 3, 1))
        self.assertIsNone(revizor.parse_version("latest"))


class BrowserTest(unittest.TestCase):
    def setUp(self):
        self.saved = (revizor.sys.platform, revizor.subprocess.run, revizor.webbrowser.open)
        self.calls = []
        revizor.webbrowser.open = lambda url: self.calls.append(("default", url)) or True

    def tearDown(self):
        revizor.sys.platform, revizor.subprocess.run, revizor.webbrowser.open = self.saved

    def test_linux_uses_default_browser(self):
        revizor.sys.platform = "linux"
        revizor.subprocess.run = lambda *a, **kw: self.fail("на Linux open -a не вызывается")
        revizor.open_browser("http://127.0.0.1:1/")
        self.assertEqual(self.calls, [("default", "http://127.0.0.1:1/")])

    def test_mac_without_chrome_falls_back(self):
        revizor.sys.platform = "darwin"
        revizor.subprocess.run = lambda cmd, **kw: type("R", (), {"returncode": 1})()
        revizor.open_browser("http://127.0.0.1:1/")
        self.assertEqual(len(self.calls), 1)

    def test_missing_doc_exits(self):
        with self.assertRaises(SystemExit):
            revizor.main(["wait", "/нет/такого.md"])


class ReleaseHygieneTest(unittest.TestCase):
    def test_version_matches_changelog(self):
        text = (Path(revizor.__file__).parent / "CHANGELOG.md").read_text(encoding="utf-8")
        top = re.search(r"(?m)^## \[(\d+\.\d+\.\d+)\]", text).group(1)
        self.assertEqual(top, revizor.__version__, "подними __version__ или раздел CHANGELOG")

    def test_changelog_between_versions(self):
        out = revizor.changelog_between("0.2.0", "0.3.0")
        self.assertIn("## [0.3.0]", out)
        self.assertNotIn("## [0.2.0]", out)


def listening(port):
    if not port:
        return False
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


class ReopenTest(unittest.TestCase):
    """После «Завершить ревью» агент снова запускает `open`: тот же порт, старая вкладка подхватывает сервер."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name).resolve()
        self.doc = t / "report.md"
        self.doc.write_text("# Итоги\n", encoding="utf-8")
        self.saved = (revizor.CACHE, revizor.REGISTRY, revizor.REOPEN_WAIT, revizor.check_update, revizor.open_browser)
        revizor.CACHE, revizor.REGISTRY, revizor.REOPEN_WAIT = t / "cache", t / "cache" / "docs.json", 1
        revizor.check_update = lambda: None
        self.opened = []
        revizor.open_browser = self.opened.append

    def tearDown(self):
        revizor.CACHE, revizor.REGISTRY, revizor.REOPEN_WAIT, revizor.check_update, revizor.open_browser = self.saved
        self.tmp.cleanup()

    def run_open(self, tab=False):
        """Один круг `open` до «Завершить ревью». tab – старая вкладка опрашивает сервер."""
        prev, opened = revizor.last_port(self.doc), len(self.opened)
        out = io.StringIO()
        th = threading.Thread(target=lambda: revizor.main(["open", str(self.doc)]), daemon=True)
        with redirect_stdout(out):
            th.start()
            deadline = time.time() + 5
            while time.time() < deadline and not listening(revizor.last_port(self.doc)):
                time.sleep(0.02)
            base = f"http://127.0.0.1:{revizor.last_port(self.doc)}"
            if tab:
                urllib.request.urlopen(base + "/api/state", timeout=2).read()
            while time.time() < deadline and not tab and len(self.opened) == opened:
                time.sleep(0.02)
            post(base + "/api/done", {"items": []})
            th.join(5)
        self.assertFalse(th.is_alive())
        return prev, revizor.last_port(self.doc)

    def test_second_open_reuses_port_and_keeps_tab(self):
        self.run_open()
        self.assertEqual(len(self.opened), 1)
        prev, new = self.run_open(tab=True)
        self.assertEqual(prev, new)
        self.assertEqual(len(self.opened), 1)

    def test_closed_tab_gets_new_one(self):
        self.run_open()
        self.run_open()
        self.assertEqual(len(self.opened), 2)
