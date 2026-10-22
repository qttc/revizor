import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
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


if __name__ == "__main__":
    unittest.main()
