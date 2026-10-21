// Слой правок поверх любой страницы: текстового документа (data-revizor-mode="text"), HTML-отчёта или дека.
// Подсветка – CSS Custom Highlight API, DOM документа не меняется. Панель – в shadow DOM, стили страницы её не задевают.
// Слайды во фреймах того же origin – отдельные «корни» со своим текстом; бандл, который подменяет <html>, панель переживает.
(() => {
  "use strict";
  if (window.__revizor) return;
  window.__revizor = true;

  const CTX = 32;
  const MODE = document.documentElement.dataset.revizorMode === "text" ? "text" : "html";
  const SKIP = "script, style, noscript, template, textarea";
  const REGISTERS = [["", "регистр…"], ["report", "отчёт"], ["deck", "дек"], ["slack", "Slack"], ["spec", "спека"],
    ["doc", "док"], ["email", "письмо"], ["client", "клиентская копия"], ["other", "другое"]];
  const HL_CSS = `
    ::highlight(rv-edit) { background-color: rgba(255, 196, 0, .38); text-decoration: line-through 1.5px rgba(160, 100, 0, .9); }
    ::highlight(rv-comment) { background-color: rgba(70, 140, 255, .28); }
    ::highlight(rv-flash) { background-color: rgba(255, 150, 0, .75); }
    html[data-revizor-block] * { cursor: crosshair !important; }`;

  let items = [];   // живые правки (state = pending)
  let closed = [];  // применённые и отклонённые, только для истории
  let register = "";
  let pendingSel = null;
  let saveTimer = null;
  let blockMode = false;
  let hoverEl = null;
  let panelOpen = true;
  let ready = false;

  const uid = () => "r" + Date.now().toString(36) + Math.random().toString(36).slice(2, 5);
  const clean = (s, n) => { s = (s || "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
  // Черновик «стало»: в HTML переносы и отступы вёрстки схлопываются, в тексте остаются как есть.
  const draft = (quote) => (MODE === "text" ? quote : quote.replace(/\s+/g, " ").trim());

  // ---------- корни: страница и фреймы слайдов ----------

  const main = {
    key: "", label: "", frame: null,
    get doc() { return document; }, get win() { return window; },
    get el() { return MODE === "text" ? document.getElementById("revizor-root") : document.body; },
  };
  const frames = new Map(); // key → корень фрейма

  const rootOf = (it) => (it.frame ? frames.get(it.frame) || null : main);
  const allRoots = () => [main, ...frames.values()];
  const rootByDoc = (doc) => allRoots().find((r) => r.doc === doc) || null;

  function frameKey(f) {
    const all = [...document.querySelectorAll("iframe")];
    return f.title || f.getAttribute("name") || f.id || "frame-" + all.indexOf(f);
  }

  function frameLabel(f) {
    if (f.title) return clean(f.title, 60);
    const sec = f.closest("section, article, figure, .slide, [data-slide]");
    const h = sec && sec.querySelector("h1, h2, h3, h4, figcaption");
    return h ? clean(h.textContent, 60) : frameKey(f);
  }

  // force – по событию load: документ тот же, но текст слайда мог догрузиться после первой регистрации.
  function registerFrame(f, force) {
    let doc = null;
    try { doc = f.contentDocument; } catch (e) { return; } // чужой origin – слой туда не попадает
    if (!doc || !doc.body || !doc.body.childNodes.length) return; // пустой about:blank до загрузки слайда
    const key = frameKey(f);
    const old = frames.get(key);
    if (old && old.doc === doc) {
      if (force && ready) { anchorRoot(old); paint(); renderCards(); }
      return;
    }
    const r = { key, label: frameLabel(f), frame: f, doc, win: f.contentWindow, get el() { return this.doc.body; } };
    frames.set(key, r);
    const st = doc.createElement("style"); st.textContent = HL_CSS; (doc.head || doc.documentElement).append(st);
    attach(r);
    if (ready) { anchorRoot(r); paint(); renderCards(); }
    refit();
  }

  function scanFrames() {
    for (const f of document.querySelectorAll("iframe")) {
      if (!f.__rv) { f.__rv = true; f.addEventListener("load", () => registerFrame(f, true)); }
      registerFrame(f);
    }
  }

  // Координаты внутри фрейма → координаты окна верхнего уровня, с учётом масштаба фрейма.
  function toTop(r, rect) {
    if (!r.frame) return rect;
    const fr = r.frame.getBoundingClientRect();
    const sx = fr.width / (r.frame.clientWidth || fr.width), sy = fr.height / (r.frame.clientHeight || fr.height);
    return { left: fr.left + rect.left * sx, top: fr.top + rect.top * sy, width: rect.width * sx, height: rect.height * sy,
      bottom: fr.top + rect.bottom * sy, right: fr.left + rect.right * sx };
  }

  // ---------- текст корня и смещения ----------

  function index(r) {
    const nodes = [];
    let text = "";
    if (!r.el) return { nodes, text };
    const w = r.doc.createTreeWalker(r.el, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentElement && n.parentElement.closest(SKIP) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    for (let n = w.nextNode(); n; n = w.nextNode()) { nodes.push({ node: n, start: text.length }); text += n.data; }
    return { nodes, text };
  }

  function offsetOf(r, idx, node, off) {
    const hit = idx.nodes.find((e) => e.node === node);
    if (hit) return hit.start + off;
    const p = r.doc.createRange();
    p.setStart(node, off);
    for (const e of idx.nodes) if (p.comparePoint(e.node, 0) >= 0) return e.start;
    return idx.text.length;
  }

  function rangeFor(r, idx, start, end) {
    const at = (pos, isEnd) => {
      for (const e of idx.nodes) {
        const len = e.node.data.length;
        if (pos < e.start + len || (isEnd && pos === e.start + len)) return [e.node, Math.max(0, pos - e.start)];
      }
      const last = idx.nodes[idx.nodes.length - 1];
      return [last.node, last.node.data.length];
    };
    const rg = r.doc.createRange();
    rg.setStart(...at(start, false));
    rg.setEnd(...at(end, true));
    return rg;
  }

  // Место правки ищется по цитате и соседнему тексту: документ мог поменяться с прошлого открытия.
  function anchor(idx, it) {
    const text = idx.text;
    if (text.slice(it.start, it.end) === it.quote) { it.orphan = false; return; }
    let best = -1, bestScore = -1;
    for (let i = text.indexOf(it.quote); i !== -1; i = text.indexOf(it.quote, i + 1)) {
      const pre = text.slice(Math.max(0, i - CTX), i), suf = text.slice(i + it.quote.length, i + it.quote.length + CTX);
      const score = common(pre, it.prefix || "", true) + common(suf, it.suffix || "", false);
      if (score > bestScore) { best = i; bestScore = score; }
    }
    if (best === -1) { it.orphan = true; return; }
    it.start = best; it.end = best + it.quote.length; it.orphan = false;
  }
  function common(a, b, fromEnd) {
    let n = 0;
    while (n < a.length && n < b.length && (fromEnd ? a[a.length - 1 - n] === b[b.length - 1 - n] : a[n] === b[n])) n++;
    return n;
  }

  function anchorRoot(r) {
    const idx = index(r);
    const mine = items.filter((i) => i.scope === "text" && (i.frame || "") === r.key);
    mine.forEach((it) => anchor(idx, it));
    let prevEnd = -1;
    for (const it of mine.filter((i) => !i.orphan).sort((a, b) => a.start - b.start)) {
      if (it.start < prevEnd) it.orphan = true; else prevEnd = it.end;
    }
  }

  // ---------- где: слайд, раздел, заголовок ----------

  function whereText(text, pos) {
    const before = text.slice(0, pos).split("\n").reverse().find((l) => /^#{1,6}\s/.test(l));
    return before ? clean(before.replace(/^#+\s*/, ""), 60) : "";
  }

  function whereEl(r, el) {
    if (!el) return r.label;
    const sec = el.closest("section, article, [data-slide], .slide");
    const h = sec && sec.querySelector("h1, h2, h3");
    let inner = h ? clean(h.textContent, 60) : "";
    if (!inner) {
      let last = null;
      for (const x of r.doc.querySelectorAll("h1, h2, h3")) {
        if (x.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) last = x; else break;
      }
      inner = last ? clean(last.textContent, 60) : sec ? clean(sec.getAttribute("aria-label") || sec.id, 60) : "";
    }
    return [r.label, inner].filter(Boolean).join(" · ");
  }

  function cssPath(r, el) {
    const parts = [];
    for (; el && el !== r.doc.body && el.nodeType === 1; el = el.parentElement) {
      if (el.id) { parts.unshift("#" + CSS.escape(el.id)); break; }
      let i = 1;
      for (let s = el.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === el.tagName) i++;
      parts.unshift(el.tagName.toLowerCase() + ":nth-of-type(" + i + ")");
    }
    return parts.join(" > ") || "body";
  }

  function findBlock(it) {
    const r = rootOf(it);
    if (!r) return null;
    let el = null;
    try { el = r.doc.querySelector(it.selector); } catch (e) { /* битый селектор */ }
    const snip = (s) => clean(s, 40);
    const want = snip(it.text || it.snippet);
    if (el && snip(el.textContent) === want) return el;
    for (const x of r.el.querySelectorAll("*")) if (snip(x.textContent) === want && x.children.length) return x;
    return el;
  }

  // Подпись блока для карточки и агента: название графика или таблицы, у прочего – начало текста.
  function blockLabel(el) {
    const named = el.getAttribute("aria-label") ||
      (el.querySelector("figcaption, caption, h1, h2, h3, h4") || {}).textContent ||
      (el.closest("figure") && (el.closest("figure").querySelector("figcaption") || {}).textContent);
    const tag = el.tagName.toLowerCase();
    const kind = el.querySelector("svg, canvas") || tag === "svg" ? "график" : el.querySelector("table") || tag === "table" ? "таблица" : "";
    const text = clean(named, 80) || clean(el.textContent, 120);
    return (kind ? kind + ": " : "") + (text || "(без текста)");
  }

  // ---------- shadow DOM: панель и тулбар ----------

  const host = document.createElement("revizor-overlay");
  const sh = host.attachShadow({ mode: "open" });
  ["keydown", "keyup", "keypress", "wheel"].forEach((t) => host.addEventListener(t, (e) => e.stopPropagation()));
  const hl = document.createElement("style");
  hl.textContent = HL_CSS + `\n    html[data-revizor-panel] { padding-right: 380px !important; }`;

  sh.innerHTML = `
<style>
:host { all: initial; }
.p { --bg:#ffffff; --sub:#f6f5f1; --ink:#1d1c1a; --muted:#6b6862; --line:#e3e0d8; --edit:#d9a400; --comment:#3d7bd9; --accent:#1d1c1a; --accent-ink:#fff;
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); }
@media (prefers-color-scheme: dark) { .p { --bg:#1f1e1b; --sub:#161614; --ink:#ecebe6; --muted:#9a978f; --line:#34322d; --accent:#ecebe6; --accent-ink:#161614; } }
* { box-sizing: border-box; }
button, select, input, textarea { font: inherit; color: inherit; }
#panel { position: fixed; top: 0; right: 0; bottom: 0; width: 380px; background: var(--bg); border-left: 1px solid var(--line);
  display: flex; flex-direction: column; z-index: 2147483646; box-shadow: -8px 0 24px rgba(0,0,0,.08); }
#panel.min { display: none; }
#pill { position: fixed; right: 16px; bottom: 16px; z-index: 2147483646; display: none; }
#pill.on { display: block; }
.top { padding: 10px 14px; border-bottom: 1px solid var(--line); display: flex; flex-direction: column; gap: 8px; }
.row { display: flex; gap: 8px; align-items: center; }
.name { font-weight: 600; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
select { background: var(--sub); border: 1px solid var(--line); border-radius: 6px; padding: 3px 6px; }
.btn { border: 1px solid var(--line); background: var(--sub); border-radius: 6px; padding: 4px 10px; cursor: pointer; }
.btn:hover { border-color: var(--muted); }
.btn.on { background: var(--comment); border-color: var(--comment); color: #fff; }
.btn.primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); font-weight: 600; }
.x { border: 0; background: none; color: var(--muted); cursor: pointer; font-size: 16px; line-height: 1; padding: 2px 4px; }
.x:hover { color: var(--ink); }
#cards { flex: 1; overflow: auto; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px; }
.empty { color: var(--muted); font-size: 13px; }
.card { border: 1px solid var(--line); border-left: 3px solid var(--edit); border-radius: 8px; padding: 9px 11px; }
.card.comment { border-left-color: var(--comment); }
.card.doc { border-left-color: var(--muted); }
.card.orphan { opacity: .75; border-style: dashed; }
.card.flash { animation: fl .9s ease-out; }
@keyframes fl { from { background: rgba(255,170,0,.35); } }
.head { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: var(--muted); margin-bottom: 5px; gap: 6px; }
.head span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.quote { font-size: 13px; color: var(--muted); margin-bottom: 6px; cursor: pointer; max-height: 4.4em; overflow: hidden; }
.card.edit .quote { text-decoration: line-through; }
textarea, input { width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; background: var(--sub); }
textarea { min-height: 58px; resize: vertical; }
input { margin-top: 6px; font-size: 13px; }
details { font-size: 13px; color: var(--muted); }
li { margin: 4px 0; }
.bottom { padding: 10px 14px; border-top: 1px solid var(--line); display: flex; align-items: center; gap: 10px; }
#status { color: var(--muted); font-size: 13px; flex: 1; }
.confirm { display: none; background: var(--sub); }
.confirm.on { display: flex; }
#bottom.off { display: none; }
#confirm-text { flex: 1; font-size: 13px; }
#tb { position: fixed; display: none; gap: 4px; background: var(--accent); color: var(--accent-ink); border-radius: 8px; padding: 4px;
  box-shadow: 0 4px 16px rgba(0,0,0,.25); z-index: 2147483647; }
#tb button { background: none; border: 0; color: inherit; padding: 4px 10px; border-radius: 5px; cursor: pointer; font-size: 13px; }
#tb button:hover { background: rgba(127,127,127,.3); }
kbd { opacity: .6; font: 11px ui-monospace, monospace; margin-left: 4px; }
#box { position: fixed; pointer-events: none; border: 2px solid var(--comment); background: rgba(70,140,255,.08); border-radius: 4px;
  z-index: 2147483645; display: none; }
#toast { position: fixed; bottom: 72px; right: 400px; background: var(--accent); color: var(--accent-ink); padding: 8px 14px; border-radius: 8px;
  display: none; z-index: 2147483647; max-width: 360px; }
#done-cover { position: fixed; inset: 0; background: var(--sub); display: none; align-items: center; justify-content: center; text-align: center; z-index: 2147483647; }
#done-cover h2 { margin: 0 0 8px; font-size: 22px; }
#done-cover p { color: var(--muted); margin: 0; }
</style>
<div class="p">
  <div id="panel">
    <div class="top">
      <div class="row"><span class="name" id="name">…</span><button class="x" id="min" title="Свернуть">⇥</button></div>
      <div class="row">
        <select id="reg" title="Для чего текст: попадёт в стилевые пары"></select>
        <button class="btn" id="add-doc">+ Общий</button>
        <button class="btn" id="add-block" title="Кликни по слайду, графику или блоку">+ К блоку</button>
      </div>
    </div>
    <div id="cards"></div>
    <div class="bottom" id="bottom"><span id="status">Загрузка…</span><button class="btn primary" id="done">Готово</button></div>
    <div class="bottom confirm" id="confirm"><span id="confirm-text"></span><button class="btn" id="confirm-no">Отмена</button><button class="btn primary" id="confirm-yes">Отправить</button></div>
  </div>
  <button class="btn primary" id="pill">Правки</button>
  <div id="tb"><button data-kind="edit">Изменить<kbd>E</kbd></button><button data-kind="comment">Комментарий<kbd>C</kbd></button></div>
  <div id="box"></div>
  <div id="toast"></div>
  <div id="done-cover"><div><h2>Отправлено агенту</h2><p id="done-text">Вкладку можно закрыть.</p></div></div>
</div>`;
  const $ = (s) => sh.querySelector(s);
  $("#reg").innerHTML = REGISTERS.map(([v, t]) => `<option value="${v}">${t}</option>`).join("");

  // Бандл может заменить <html> целиком: тогда панель, стиль подсветки и фреймы подключаются заново.
  let lastHtml = null;
  function ensureAttached() {
    const html = document.documentElement;
    if (!html) return;
    if (host.parentNode !== html) html.append(host);
    if (document.head && hl.parentNode !== document.head) document.head.append(hl);
    html.toggleAttribute("data-revizor-panel", panelOpen);
    if (html !== lastHtml) {
      lastHtml = html;
      if (blockMode) html.setAttribute("data-revizor-block", "");
      if (ready) { anchorRoot(main); paint(); renderCards(); }
      refit();
    }
    scanFrames();
  }
  let scheduled = false;
  new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; ensureAttached(); });
  }).observe(document, { childList: true, subtree: true });

  // ---------- отрисовка ----------

  const textItemsOf = (key) => items.filter((i) => i.scope === "text" && !i.orphan && (i.frame || "") === key).sort((a, b) => a.start - b.start);
  const order = (i) => [...frames.keys()].indexOf(i.frame) + 1; // 0 – страница, дальше слайды по порядку
  const textItems = () => items.filter((i) => i.scope === "text" && !i.orphan && rootOf(i))
    .sort((a, b) => order(a) - order(b) || a.start - b.start);

  function paint() {
    for (const r of allRoots()) {
      const C = r.win.CSS, H = r.win.Highlight;
      if (!C || !C.highlights || !H) continue;
      const idx = index(r);
      const edit = new H(), comment = new H();
      for (const it of textItemsOf(r.key)) (it.kind === "edit" ? edit : comment).add(rangeFor(r, idx, it.start, it.end));
      C.highlights.set("rv-edit", edit);
      C.highlights.set("rv-comment", comment);
    }
  }

  function renderCards() {
    const box = $("#cards");
    box.textContent = "";
    const all = [
      ...items.filter((i) => i.scope === "doc"),
      ...items.filter((i) => i.scope === "block"),
      ...textItems(),
      ...items.filter((i) => i.scope === "text" && (i.orphan || !rootOf(i))),
    ];
    if (!all.length) {
      const e = document.createElement("div"); e.className = "empty";
      e.textContent = "Выдели текст и нажми «Изменить» или «Комментарий». «+ К блоку» – комментарий к слайду, графику или таблице, «+ Общий» – ко всему документу.";
      box.append(e);
    }
    for (const it of all) box.append(card(it));
    if (closed.length) {
      const d = document.createElement("details");
      d.innerHTML = `<summary>История: ${closed.length}</summary><ul></ul>`;
      for (const c of closed) {
        const li = document.createElement("li");
        const what = c.kind === "edit" ? `«${clean(c.quote, 60)}» → «${clean(c.replacement, 60)}»`
          : (c.quote ? `«${clean(c.quote, 40)}»: ` : c.where ? `[${c.where}] ` : "") + clean(c.comment, 80);
        li.textContent = `${c.state === "applied" ? "✓" : "✗"} ${what}${c.note ? " – " + c.note : ""}`;
        d.querySelector("ul").append(li);
      }
      box.append(d);
    }
    $("#pill").textContent = `Правки · ${items.length}`;
  }

  function card(it) {
    const c = document.createElement("div");
    const lost = it.scope === "text" && (it.orphan || !rootOf(it));
    c.className = `card ${it.scope === "doc" ? "doc" : it.kind}${lost ? " orphan" : ""}`;
    c.dataset.id = it.id;
    const head = document.createElement("div"); head.className = "head";
    const label = document.createElement("span");
    const kind = it.scope === "doc" ? "Общий комментарий" : it.scope === "block" ? "К блоку" : it.kind === "edit" ? "Правка" : "Комментарий";
    label.textContent = kind + (it.where ? " · " + it.where : "") + (lost ? " · место не найдено" : "");
    const x = document.createElement("button"); x.className = "x"; x.title = "Удалить"; x.textContent = "×";
    x.onclick = () => { items = items.filter((i) => i !== it); paint(); renderCards(); scheduleSave(); };
    head.append(label, x); c.append(head);
    if (it.scope !== "doc") {
      const q = document.createElement("div"); q.className = "quote";
      q.textContent = it.scope === "block" ? it.snippet : clean(it.quote, 300);
      q.onclick = () => reveal(it);
      c.append(q);
    }
    const ta = document.createElement("textarea");
    if (it.kind === "edit") {
      ta.value = it.replacement;
      ta.oninput = () => { it.replacement = ta.value; scheduleSave(); };
      const why = document.createElement("input"); why.placeholder = "почему (необязательно)"; why.value = it.why || "";
      why.oninput = () => { it.why = why.value; scheduleSave(); };
      c.append(ta, why);
    } else {
      ta.value = it.comment || ""; ta.placeholder = "что поправить";
      ta.oninput = () => { it.comment = ta.value; scheduleSave(); };
      c.append(ta);
    }
    return c;
  }

  function reveal(it) {
    const r = rootOf(it);
    if (!r) return toast("Слайд не загружен");
    if (r.frame) r.frame.scrollIntoView({ block: "center", behavior: "smooth" });
    if (it.scope === "block") {
      const el = findBlock(it);
      if (!el) return toast("Блок не найден – страница поменялась");
      if (!r.frame) el.scrollIntoView({ block: "center", behavior: "smooth" });
      setTimeout(() => { outline(r, el); setTimeout(() => outline(null), 1200); }, 400);
      return;
    }
    if (it.orphan) return;
    const rg = rangeFor(r, index(r), it.start, it.end);
    if (!r.frame) (rg.startContainer.parentElement || r.el).scrollIntoView({ block: "center", behavior: "smooth" });
    const C = r.win.CSS, H = r.win.Highlight;
    if (C && C.highlights && H) {
      C.highlights.set("rv-flash", new H(rg));
      setTimeout(() => C.highlights.delete("rv-flash"), 900);
    }
  }

  function focusCard(id) {
    setPanel(true);
    const c = sh.querySelector(`.card[data-id="${id}"]`);
    if (!c) return;
    c.scrollIntoView({ block: "nearest", behavior: "smooth" });
    c.classList.remove("flash"); void c.offsetWidth; c.classList.add("flash");
    c.querySelector("textarea").focus();
  }

  function outline(r, el) {
    const b = $("#box");
    if (!r || !el) { b.style.display = "none"; return; }
    const rc = toTop(r, el.getBoundingClientRect());
    Object.assign(b.style, { display: "block", left: rc.left - 2 + "px", top: rc.top - 2 + "px", width: rc.width + 4 + "px", height: rc.height + 4 + "px" });
  }

  function toast(msg) {
    const t = $("#toast"); t.textContent = msg; t.style.display = "block";
    clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = "none"), 2600);
  }

  // ---------- выделение → правка или комментарий ----------

  function readSelection(r) {
    const sel = r.win.getSelection();
    if (!sel || !sel.rangeCount || sel.isCollapsed) return null;
    const rg = sel.getRangeAt(0);
    if (!r.el || !r.el.contains(rg.commonAncestorContainer)) return null;
    const idx = index(r);
    let s = offsetOf(r, idx, rg.startContainer, rg.startOffset), e = offsetOf(r, idx, rg.endContainer, rg.endOffset);
    while (s < e && /\s/.test(idx.text[s])) s++;
    while (e > s && /\s/.test(idx.text[e - 1])) e--;
    if (s >= e) return null;
    return { r, start: s, end: e, rect: toTop(r, rg.getBoundingClientRect()), idx, node: rg.startContainer };
  }

  function showToolbar(sel) {
    const tb = $("#tb");
    tb.style.display = "flex";
    const w = tb.offsetWidth;
    tb.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, sel.rect.left + sel.rect.width / 2 - w / 2)) + "px";
    tb.style.top = (sel.rect.top > 50 ? sel.rect.top - tb.offsetHeight - 8 : sel.rect.bottom + 8) + "px";
  }
  function hideToolbar() { $("#tb").style.display = "none"; pendingSel = null; }

  function create(kind) {
    const sel = pendingSel;
    hideToolbar();
    if (!sel) return;
    if (textItemsOf(sel.r.key).some((i) => sel.start < i.end && i.start < sel.end)) {
      toast("Выделение задевает другую правку – поправь её карточку справа");
      return;
    }
    const text = sel.idx.text;
    const quote = text.slice(sel.start, sel.end);
    const el = sel.node.nodeType === 1 ? sel.node : sel.node.parentElement;
    const it = {
      id: uid(), kind, scope: "text", start: sel.start, end: sel.end, quote,
      prefix: text.slice(Math.max(0, sel.start - CTX), sel.start), suffix: text.slice(sel.end, sel.end + CTX),
      where: MODE === "text" ? whereText(text, sel.start) : whereEl(sel.r, el),
      created: new Date().toISOString(), state: "pending",
    };
    if (sel.r.key) it.frame = sel.r.key;
    if (kind === "edit") { it.replacement = draft(quote); it.why = ""; } else { it.comment = ""; }
    items.push(it);
    sel.r.win.getSelection().removeAllRanges();
    paint(); renderCards(); focusCard(it.id); scheduleSave();
  }

  function itemAtPoint(r, x, y) {
    let node, off;
    if (r.doc.caretPositionFromPoint) { const p = r.doc.caretPositionFromPoint(x, y); if (!p) return null; node = p.offsetNode; off = p.offset; }
    else if (r.doc.caretRangeFromPoint) { const rg = r.doc.caretRangeFromPoint(x, y); if (!rg) return null; node = rg.startContainer; off = rg.startOffset; }
    else return null;
    if (!r.el || !r.el.contains(node)) return null;
    const pos = offsetOf(r, index(r), node, off);
    return textItemsOf(r.key).find((i) => pos >= i.start && pos < i.end) || null;
  }

  // ---------- комментарий к блоку ----------

  function setBlockMode(on) {
    blockMode = on;
    $("#add-block").classList.toggle("on", on);
    for (const r of allRoots()) r.doc.documentElement.toggleAttribute("data-revizor-block", on);
    if (on) toast("Кликни по блоку. Esc – отмена");
    else { outline(null); hoverEl = null; }
  }

  function blockTarget(r, e) {
    if (r === main && e.composedPath().includes(host)) return null;
    let el = e.target;
    if (!el || el.nodeType !== 1 || !r.el || !r.el.contains(el) || el === r.el) return null;
    // фрейм слайда на странице – это блок целиком
    if (el.tagName === "IFRAME") return el;
    // мелкий инлайн-элемент поднимаем до блока, у svg-графика берём его контейнер
    const svg = el.closest("svg"); if (svg) el = svg.parentElement && svg.parentElement !== r.el ? svg.parentElement : svg;
    while (el.parentElement && el.parentElement !== r.el && r.win.getComputedStyle(el).display.startsWith("inline")) el = el.parentElement;
    return el;
  }

  function createBlock(r, el) {
    setBlockMode(false);
    // клик по фрейму целиком – комментарий ко всему слайду
    if (el.tagName === "IFRAME") {
      const fr = [...frames.values()].find((x) => x.frame === el);
      if (fr) { r = fr; el = fr.doc.body; }
    }
    const it = { id: uid(), kind: "comment", scope: "block", selector: cssPath(r, el), snippet: r.key && el === r.doc.body ? "слайд целиком" : blockLabel(el),
      text: clean(el.textContent, 120), where: whereEl(r, el), comment: "", created: new Date().toISOString(), state: "pending" };
    if (r.key) it.frame = r.key;
    items.push(it); renderCards(); focusCard(it.id); scheduleSave();
  }

  // ---------- события в каждом корне ----------

  // Слайд или отчёт может достраиваться скриптом после load: при изменении текста правки ищут место заново.
  function scheduleHeal(r) {
    if (!ready) return;
    clearTimeout(r.healTimer);
    r.healTimer = setTimeout(() => {
      const mine = items.filter((i) => i.scope === "text" && (i.frame || "") === r.key);
      const before = mine.map((i) => `${i.start}:${i.orphan}`).join();
      anchorRoot(r); paint();
      if (mine.map((i) => `${i.start}:${i.orphan}`).join() !== before) renderCards();
    }, 250);
  }

  function attach(r) {
    const d = r.doc;
    new r.win.MutationObserver(() => scheduleHeal(r)).observe(d, { childList: true, subtree: true, characterData: true });
    d.addEventListener("mousemove", (e) => {
      if (!blockMode) return;
      const el = blockTarget(r, e);
      if (el !== hoverEl) { hoverEl = el; outline(el ? r : null, el); }
    }, true);
    d.addEventListener("click", (e) => {
      if (blockMode) {
        const el = blockTarget(r, e);
        if (!el) return;
        e.preventDefault(); e.stopPropagation();
        createBlock(r, el);
        return;
      }
      if ((r === main && e.composedPath().includes(host)) || !r.win.getSelection().isCollapsed) return;
      const it = itemAtPoint(r, e.clientX, e.clientY);
      if (it) focusCard(it.id);
    }, true);
    d.addEventListener("mouseup", (e) => {
      if (blockMode || (r === main && e.composedPath().includes(host))) return;
      setTimeout(() => {
        const sel = readSelection(r);
        if (sel) { pendingSel = sel; showToolbar(sel); } else if (!pendingSel || pendingSel.r === r) hideToolbar();
      }, 0);
    });
    d.addEventListener("mousedown", (e) => {
      if (!(r === main && e.composedPath().includes(host))) $("#tb").style.display = "none";
    });
    d.addEventListener("keydown", onKey, true);
  }

  function onKey(e) {
    if (e.key === "Escape" && blockMode) { setBlockMode(false); return; }
    if ($("#tb").style.display !== "flex" || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "e" || e.key === "у") { e.preventDefault(); create("edit"); }
    if (e.key === "c" || e.key === "с") { e.preventDefault(); create("comment"); }
    if (e.key === "Escape") hideToolbar();
  }

  // ---------- сохранение ----------

  function payload() { return { register: register || null, items }; }
  function scheduleSave() {
    $("#status").textContent = "Сохраняю…";
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 400);
  }
  async function save() {
    try {
      const r = await fetch("/api/review", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
      if (!r.ok) throw new Error(r.status);
      syncClosed((await r.json()).closed || []);
      $("#status").textContent = `Сохранено · ${items.length} ${plural(items.length)}`;
    } catch (e) {
      $("#status").textContent = "Не сохранилось: сервер остановлен?";
    }
  }
  // Агент закрывает правки, пока вкладка открыта: убрать их из живых и показать в истории.
  function syncClosed(list) {
    const ids = new Set(list.map((c) => c.id));
    const gone = items.filter((i) => ids.has(i.id));
    closed = list;
    if (!gone.length) return;
    items = items.filter((i) => !ids.has(i.id));
    paint(); renderCards();
    toast(`Агент закрыл ${gone.length} ${plural(gone.length)}`);
  }
  function plural(n) {
    const m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return "правка";
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return "правки";
    return "правок";
  }

  const keep = (i) => (i.kind === "edit" ? i.replacement !== draft(i.quote) : (i.comment || "").trim());
  async function done() {
    // Пустые карточки агенту не нужны: правка без изменений, комментарий без текста.
    items = items.filter(keep);
    clearTimeout(saveTimer);
    try {
      const r = await fetch("/api/done", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
      if (!r.ok) throw new Error(r.status);
    } catch (e) {
      toast("Не отправилось: сервер остановлен?"); return;
    }
    $("#done-text").textContent = `${items.length} ${plural(items.length)} ушли агенту. Вкладку можно закрыть.`;
    $("#done-cover").style.display = "flex";
  }

  // ---------- кнопки панели ----------

  $("#tb").addEventListener("mousedown", (e) => e.preventDefault());
  $("#tb").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) create(b.dataset.kind); });
  $("#add-doc").onclick = () => {
    const it = { id: uid(), kind: "comment", scope: "doc", comment: "", created: new Date().toISOString(), state: "pending" };
    items.unshift(it); renderCards(); focusCard(it.id); scheduleSave();
  };
  $("#add-block").onclick = () => setBlockMode(!blockMode);
  $("#reg").onchange = (e) => { register = e.target.value; scheduleSave(); };
  // «Готово» необратимо: сервер закрывается и правки уходят агенту. Поэтому только кликом и с подтверждением, без горячей клавиши.
  function askDone(on) {
    const n = items.filter(keep).length;
    $("#confirm-text").textContent = `Отправить агенту ${n} ${plural(n)}? Редактор закроется.`;
    $("#confirm").classList.toggle("on", on);
    $("#bottom").classList.toggle("off", on);
  }
  $("#done").onclick = () => askDone(true);
  $("#confirm-no").onclick = () => askDone(false);
  $("#confirm-yes").onclick = () => { askDone(false); done(); };
  function setPanel(open) {
    panelOpen = open;
    $("#panel").classList.toggle("min", !open);
    $("#pill").classList.toggle("on", !open);
    document.documentElement.toggleAttribute("data-revizor-panel", open);
    fit();
  }

  // Панель занимает 380px справа. Если контент шире оставшегося места (дек 1280px), страница ужимается через zoom
  // на <body>, чтобы слайд целиком был слева от панели. Панель свёрнута – масштаб обратно 100%.
  const PANEL_W = 380;
  function fit() {
    const b = document.body;
    if (!b) return;
    b.style.zoom = "";
    if (!panelOpen) return;
    const html = document.documentElement;
    const content = Math.max(html.scrollWidth - PANEL_W, b.scrollWidth); // у body бывает overflow-x: hidden
    const avail = html.clientWidth - PANEL_W;
    if (content > avail + 1) b.style.zoom = String(Math.max(0.5, avail / content));
  }
  let fitTimer = null;
  const refit = () => { clearTimeout(fitTimer); fitTimer = setTimeout(fit, 100); };
  window.addEventListener("resize", refit);
  $("#min").onclick = () => setPanel(false);
  $("#pill").onclick = () => setPanel(true);

  // ---------- старт ----------

  attach(main);
  ensureAttached();
  (async function init() {
    const { doc, review } = await (await fetch("/api/state")).json();
    $("#name").textContent = doc.name;
    $("#name").title = doc.path;
    register = review.register || "";
    $("#reg").value = register;
    items = review.items.filter((i) => (i.state || "pending") === "pending");
    closed = review.items.filter((i) => (i.state || "pending") !== "pending");
    ready = true;
    allRoots().forEach(anchorRoot);
    setPanel(true); paint(); renderCards();
    $("#status").textContent = items.length ? `${items.length} ${plural(items.length)} с прошлого раза` : "Правок пока нет";
    if (!(window.CSS && CSS.highlights)) toast("Браузер без CSS Highlight API: правки сохраняются, подсветки не будет");
  })();
})();
