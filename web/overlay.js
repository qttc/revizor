// Слой правок поверх любой страницы: текста (data-revizor-mode="text"), Markdown ("md"), HTML-отчёта или дека.
// Подсветка – CSS Custom Highlight API, DOM документа не меняется. Панель – в shadow DOM, стили страницы её не задевают.
// Слайды во фреймах того же origin – отдельные «корни» со своим текстом; бандл, который подменяет <html>, панель переживает.
(() => {
  "use strict";
  if (window.__revizor) return;
  window.__revizor = true;

  const CTX = 32;
  const MODE = ["text", "md"].includes(document.documentElement.dataset.revizorMode) ? document.documentElement.dataset.revizorMode : "html";
  const IS_TEXT = MODE !== "html"; // текст и Markdown: позиции правок – позиции в файле
  const SKIP = "script, style, noscript, template, textarea";
  const REGISTERS = [["", "Определить сам"], ["report", "Отчёт"], ["deck", "Дек"], ["slack", "Slack"], ["spec", "Спека"],
    ["doc", "Док"], ["email", "Письмо"], ["client", "Клиентская копия"], ["other", "Другое"]];
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
  // В Markdown черновик – исходник выделения: если в нём есть ** или ссылка, правка сохранит оформление.
  const draft = (quote) => (IS_TEXT ? quote : quote.replace(/\s+/g, " ").trim());

  // ---------- корни: страница и фреймы слайдов ----------

  const main = {
    key: "", label: "", frame: null,
    get doc() { return document; }, get win() { return window; },
    get el() {
      if (MODE === "html") return document.body;
      return mdOn() ? document.getElementById("revizor-md") : document.getElementById("revizor-root");
    },
  };
  const frames = new Map(); // key → корень фрейма

  const rootOf = (it) => (it.frame ? frames.get(it.frame) || null : main);
  const allRoots = () => [main, ...frames.values()];
  const rootByDoc = (doc) => allRoots().find((r) => r.doc === doc) || null;

  // ---------- Markdown: оформленный вид с картой позиций исходника ----------

  // Парсер (mdast + GFM) даёт каждому узлу точные позиции в исходнике. Каждый текстовый узел оформленного вида
  // помнит, с какого смещения исходника он взят, поэтому выделение, подсветка и поиск места работают в позициях
  // файла – одинаково в «Оформлении» и в «Тексте». Формат *.review.json от вида не зависит.
  const MDP = MODE === "md" ? window.__revizorMd || null : null;
  let SOURCE = "";
  let mdMap = null;        // [{ node, start }] по возрастанию start
  let mdHeads = [];        // [{ start, depth, text, line }]
  let mdLines = [];        // смещения начала строк
  let view = "plain";      // "md" – оформление, "plain" – текст

  const mdOn = () => MODE === "md" && view === "md" && !!mdMap;
  const lineAt = (pos) => { let lo = 0, hi = mdLines.length - 1; while (lo < hi) { const m = (lo + hi + 1) >> 1; if (mdLines[m] <= pos) lo = m; else hi = m - 1; } return lo + 1; };

  function safeUrl(u) {
    const url = (u || "").trim();
    return /^(https?:|mailto:|#|\/|\.{0,2}\/|[^:]*$)/i.test(url) ? url : "";
  }

  // Текст узла в DOM с привязкой к исходнику. Обычно значение лежит в исходнике подряд. Экранирование (\*),
  // сущности (&amp;) и отступы продолжения строк ломают это – тогда значение режется на куски, которые совпадают.
  let mdPreview = false; // превью «стало» рисуется тем же кодом, но без привязки к исходнику
  function leaf(parent, value, pos) {
    if (!value) return;
    if (!pos || mdPreview) { parent.append(value); return; }
    const s0 = pos.start.offset, src = SOURCE.slice(s0, pos.end.offset);
    const at = src.indexOf(value);
    if (at >= 0) {
      const t = document.createTextNode(value);
      parent.append(t); mdMap.push({ node: t, start: s0 + at });
      return;
    }
    let i = 0, j = 0, cur = null;
    const push = (a, b, s) => { const t = document.createTextNode(value.slice(a, b)); parent.append(t); mdMap.push({ node: t, start: s }); };
    while (i < value.length) {
      if (j < src.length && value[i] === src[j]) { if (!cur) cur = { v: i, s: s0 + j }; i++; j++; continue; }
      if (cur) { push(cur.v, i, cur.s); cur = null; }
      const k = src.indexOf(value[i], j);
      if (k >= 0 && k - j <= 12) j = k;
      else { parent.append(value[i]); i++; }
    }
    if (cur) push(cur.v, i, cur.s);
  }

  function plainText(n) {
    if (n.value) return n.value;
    return (n.children || []).map(plainText).join("");
  }

  function mark(el, n) {
    if (n.position) {
      el.dataset.ls = n.position.start.line; el.dataset.le = n.position.end.line;
      el.dataset.s = n.position.start.offset; el.dataset.e = n.position.end.offset;
    }
    return el;
  }

  function inline(parent, n, defs) {
    const kids = (el) => { for (const c of n.children || []) inline(el, c, defs); return el; };
    switch (n.type) {
      case "text": return leaf(parent, n.value, n.position);
      case "emphasis": return parent.append(kids(document.createElement("em")));
      case "strong": return parent.append(kids(document.createElement("strong")));
      case "delete": return parent.append(kids(document.createElement("del")));
      case "inlineCode": { const c = document.createElement("code"); leaf(c, n.value, n.position); return parent.append(c); }
      case "break": return parent.append(document.createElement("br"));
      case "link": case "linkReference": {
        const a = document.createElement("a");
        const url = n.type === "link" ? n.url : (defs[n.identifier] || {}).url;
        const href = safeUrl(url);
        if (href) { a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; }
        if (n.title) a.title = n.title;
        return parent.append(kids(a));
      }
      case "image": case "imageReference": {
        const url = n.type === "image" ? n.url : (defs[n.identifier] || {}).url;
        const img = document.createElement("img");
        const src = safeUrl(url); if (src) img.src = src;
        img.alt = n.alt || ""; img.loading = "lazy";
        return parent.append(img);
      }
      case "footnoteReference": { const s = document.createElement("sup"); s.textContent = `[${n.label || n.identifier}]`; return parent.append(s); }
      case "html": { const s = document.createElement("span"); s.className = "raw"; leaf(s, n.value, n.position); return parent.append(s); }
      default:
        if (n.children) return parent.append(kids(document.createElement("span")));
        if (n.value) return leaf(parent, n.value, n.position);
    }
  }

  function block(n, defs, tight) {
    const kids = (el, t) => { for (const c of n.children || []) { const b = block(c, defs, t); if (b) el.append(b); } return el; };
    switch (n.type) {
      case "paragraph": {
        const p = document.createElement(tight ? "span" : "p");
        for (const c of n.children) inline(p, c, defs);
        return mark(p, n);
      }
      case "heading": {
        const h = document.createElement("h" + n.depth);
        for (const c of n.children) inline(h, c, defs);
        if (!mdPreview) mdHeads.push({ start: n.position.start.offset, depth: n.depth, text: clean(plainText(n), 60), line: n.position.start.line });
        return mark(h, n);
      }
      case "thematicBreak": return mark(document.createElement("hr"), n);
      case "blockquote": return mark(kids(document.createElement("blockquote")), n);
      case "list": {
        const l = document.createElement(n.ordered ? "ol" : "ul");
        if (n.ordered && n.start != null && n.start !== 1) l.start = n.start;
        for (const it of n.children) {
          const li = mark(document.createElement("li"), it);
          if (typeof it.checked === "boolean") {
            li.className = "task";
            const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = it.checked; cb.disabled = true;
            li.append(cb);
          }
          for (const c of it.children) { const b = block(c, defs, !n.spread && !it.spread); if (b) li.append(b); }
          l.append(li);
        }
        return mark(l, n);
      }
      case "code": {
        const pre = document.createElement("pre"), code = document.createElement("code");
        if (n.lang) code.dataset.lang = n.lang;
        leaf(code, n.value, n.position);
        pre.append(code);
        return mark(pre, n);
      }
      case "table": {
        const wrap = mark(document.createElement("div"), n); wrap.className = "table";
        const t = document.createElement("table");
        n.children.forEach((row, ri) => {
          const tr = mark(document.createElement("tr"), row);
          row.children.forEach((cell, ci) => {
            const td = mark(document.createElement(ri === 0 ? "th" : "td"), cell);
            if (n.align && n.align[ci]) td.style.textAlign = n.align[ci];
            for (const c of cell.children) inline(td, c, defs);
            tr.append(td);
          });
          t.append(tr);
        });
        wrap.append(t);
        return wrap;
      }
      case "footnoteDefinition": {
        const d = mark(document.createElement("div"), n); d.className = "fn";
        d.append(`[${n.label || n.identifier}] `);
        return kids(d);
      }
      case "definition": return null;
      case "html": { const d = mark(document.createElement("div"), n); d.className = "raw"; leaf(d, n.value, n.position); return d; }
      default: {
        if (!n.position) return null;
        const d = mark(document.createElement("div"), n); d.className = "raw";
        leaf(d, SOURCE.slice(n.position.start.offset, n.position.end.offset), n.position);
        return d;
      }
    }
  }

  function renderMarkdown() {
    const plain = document.getElementById("revizor-root"), box = document.getElementById("revizor-md");
    if (!plain) return;
    SOURCE = plain.textContent;
    mdLines = [0];
    for (let i = 0; i < SOURCE.length; i++) if (SOURCE[i] === "\n") mdLines.push(i + 1);
    if (!MDP || !box) return;
    let tree;
    try { tree = MDP.parse(SOURCE); } catch (e) { return; } // не разобрался – остаётся «Текст»
    const defs = {};
    (function walk(n) { if (n.type === "definition") defs[n.identifier] = n; (n.children || []).forEach(walk); })(tree);
    mdMap = []; mdHeads = [];
    box.textContent = "";
    for (const c of tree.children) { const b = block(c, defs, false); if (b) box.append(b); }
    mdMap.sort((a, b) => a.start - b.start);
    mdHeads.sort((a, b) => a.start - b.start);
  }

  function renderPreview(el, md) {
    el.textContent = "";
    let tree;
    try { tree = MDP.parse(md); } catch (e) { el.textContent = md; return; }
    mdPreview = true;
    try { for (const c of tree.children) { const b = block(c, {}, true); if (b) el.append(b); } } finally { mdPreview = false; }
  }

  // Ссылки в оформленном виде не уводят со страницы, пока идёт ревью: открываются по ⌘-клику.
  if (MODE === "md") document.addEventListener("click", (e) => {
    const a = e.target && e.target.closest ? e.target.closest("#revizor-md a[href]") : null;
    if (!a || blockMode || e.metaKey || e.ctrlKey) return;
    e.preventDefault();
    toast("Ссылка открывается по ⌘-клику");
  }, true);

  // Раздел правки – путь по заголовкам: «Зачем › Как работает».
  function mdWhere(pos) {
    const stack = [];
    for (const h of mdHeads) {
      if (h.start > pos) break;
      while (stack.length && stack[stack.length - 1].depth >= h.depth) stack.pop();
      stack.push(h);
    }
    return clean(stack.map((h) => h.text).join(" › "), 80);
  }

  // Блок в Markdown – элемент с позициями: абзац, пункт, таблица, код. Заголовок – весь раздел до следующего
  // заголовка того же уровня или выше.
  function mdBlockLines(el) {
    let a = +el.dataset.ls, b = +el.dataset.le;
    if (/^H[1-6]$/.test(el.tagName)) {
      const depth = +el.tagName[1], start = +el.dataset.s;
      const next = mdHeads.find((h) => h.start > start && h.depth <= depth);
      b = next ? next.line - 1 : mdLines.length;
      while (b > a && !SOURCE.slice(mdLines[b - 1], mdLines[b] ?? SOURCE.length).trim()) b--;
    }
    return [a, b];
  }

  function mdBlockLabel(el) {
    const text = clean(el.textContent, 90);
    switch (el.tagName) {
      case "H1": case "H2": case "H3": case "H4": case "H5": case "H6": return `раздел: ${text}`;
      case "LI": return `пункт: ${text}`;
      case "PRE": return `код${el.querySelector("code")?.dataset.lang ? " " + el.querySelector("code").dataset.lang : ""}: ${clean(el.textContent, 60)}`;
      case "BLOCKQUOTE": return `цитата: ${text}`;
      case "UL": case "OL": return `список: ${text}`;
      default: return el.classList.contains("table") || el.tagName === "TR" ? `таблица: ${text}` : text || "(без текста)";
    }
  }


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
    if (ready) { anchorRoot(r); paint(); renderCards(); if (registerAuto) applyAutoRegister(); }
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
    if (r === main && mdOn()) return { nodes: mdMap, text: SOURCE };
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
    // «03 · Цифры» и заголовок слайда «Цифры за неделю» – одно и то же место, второй раз не пишем
    const tail = (r.label.split(" · ").pop() || "").toLowerCase(), low = inner.toLowerCase();
    if (tail && low && (low.startsWith(tail) || tail.startsWith(low))) inner = "";
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
    if (it.lines && r === main && MODE === "md") {
      if (!mdOn()) return null;
      const byLine = r.el.querySelector(`[data-ls="${it.lines[0]}"]:not(span)`);
      if (byLine) return byLine;
    }
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

  // ---------- движение ----------

  // Motion приходит вместе со слоем в изолированной обёртке (window.__revizorMotion). Нет его – WAAPI.
  const M = window.__revizorMotion || null;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  const EASE = [0.16, 1, 0.3, 1];
  const EASE_CSS = "cubic-bezier(0.16, 1, 0.3, 1)";

  function anim(el, keyframes, opts = {}) {
    if (!el) return Promise.resolve();
    let kf = keyframes;
    // Меньше движения: только прозрачность и цвет, без сдвигов, масштаба и размытия.
    if (reduce.matches) {
      kf = {};
      for (const k of ["opacity", "backgroundColor", "color"]) if (k in keyframes) kf[k] = keyframes[k];
      if (!Object.keys(kf).length) return Promise.resolve();
      opts = { ...opts, duration: Math.min(opts.duration || 0.2, 0.15), delay: 0 };
    }
    const duration = opts.duration ?? 0.22;
    try {
      if (M && M.animate) {
        const c = M.animate(el, kf, { duration, delay: opts.delay || 0, ease: opts.ease || EASE });
        return Promise.resolve(c && c.finished ? c.finished : c);
      }
    } catch (e) { /* падаем на WAAPI */ }
    const frames = {};
    for (const [k, v] of Object.entries(kf)) frames[k === "x" || k === "y" || k === "scale" ? "transform" : k] = v;
    if (kf.x || kf.y || kf.scale) {
      const n = Math.max(...["x", "y", "scale"].map((k) => (Array.isArray(kf[k]) ? kf[k].length : 0)));
      frames.transform = Array.from({ length: n }, (_, i) => {
        const pick = (k, d) => (Array.isArray(kf[k]) ? kf[k][Math.min(i, kf[k].length - 1)] : d);
        return `translate(${pick("x", 0)}px, ${pick("y", 0)}px) scale(${pick("scale", 1)})`;
      });
    }
    return el.animate(frames, { duration: duration * 1000, delay: (opts.delay || 0) * 1000, easing: EASE_CSS, fill: "backwards" }).finished;
  }

  // ---------- shadow DOM: панель и тулбар ----------

  const host = document.createElement("revizor-overlay");
  const sh = host.attachShadow({ mode: "open" });
  ["keydown", "keyup", "keypress", "wheel"].forEach((t) => host.addEventListener(t, (e) => e.stopPropagation()));

  // @font-face из shadow DOM Chrome не подхватывает – шрифт объявляется в документе вместе с подсветкой.
  const FONT = "Revizor Playfair";
  const face = (file, weight, style, range) =>
    `@font-face { font-family: "${FONT}"; src: url(/_revizor/vendor/${file}) format("woff2"); font-weight: ${weight}; font-style: ${style}; font-display: swap; unicode-range: ${range}; }`;
  const CYR = "U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116";
  const LAT = "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD";
  const hl = document.createElement("style");
  hl.textContent = HL_CSS + `
    ${face("playfair-700-italic-cyrillic.woff2", 700, "italic", CYR)}
    ${face("playfair-700-italic-latin.woff2", 700, "italic", LAT)}
    ${face("playfair-400-cyrillic.woff2", 400, "normal", CYR)}
    ${face("playfair-400-latin.woff2", 400, "normal", LAT)}
    html[data-revizor-panel="side"] { padding-right: 404px !important; }
    html[data-revizor-panel="sheet"] { padding-bottom: 52vh !important; }`;

  const ICON = {
    close: '<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>',
    collapse: '<rect x="2.5" y="3" width="11" height="10" rx="2.5"/><path d="M10 3v10M5 6.5L6.8 8 5 9.5"/>',
    note: '<path d="M3 4.5A1.5 1.5 0 0 1 4.5 3h7A1.5 1.5 0 0 1 13 4.5v5A1.5 1.5 0 0 1 11.5 11H7l-3 2.2V11h0A1 1 0 0 1 3 10z"/>',
    block: '<path d="M3 5.5V4a1 1 0 0 1 1-1h1.5M10.5 3H12a1 1 0 0 1 1 1v1.5M13 10.5V12a1 1 0 0 1-1 1h-1.5M5.5 13H4a1 1 0 0 1-1-1v-1.5"/><path d="M7 7l4.5 1.7-1.9.7-.7 1.9z"/>',
    edit: '<path d="M10.2 3.3l2.5 2.5M3 13l.6-2.9 7.3-7.3a1.2 1.2 0 0 1 1.7 0l.6.6a1.2 1.2 0 0 1 0 1.7L5.9 12.4z"/>',
    comment: '<path d="M3 4.5A1.5 1.5 0 0 1 4.5 3h7A1.5 1.5 0 0 1 13 4.5v5A1.5 1.5 0 0 1 11.5 11H7l-3 2.2V11h0A1 1 0 0 1 3 10z"/>',
    check: '<path d="M3.5 8.5l3 3 6-7"/>',
    reject: '<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>',
    retry: '<path d="M12.5 8a4.5 4.5 0 1 1-1.3-3.2M12.5 3v2.5H10"/>',
    auto: '<path d="M8 2.5v2M8 11.5v2M2.5 8h2M11.5 8h2M4.2 4.2l1.4 1.4M10.4 10.4l1.4 1.4M4.2 11.8l1.4-1.4M10.4 5.6l1.4-1.4"/>',
  };
  const icon = (name, size = 16) =>
    `<svg width="${size}" height="${size}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name]}</svg>`;

  sh.innerHTML = `
<style>
:host { all: initial; }
.p {
  --tint: rgba(250, 250, 252, .62); --tint-strong: rgba(252, 252, 253, .82);
  --fill: rgba(255, 255, 255, .5); --fill-hover: rgba(255, 255, 255, .72); --field: rgba(255, 255, 255, .66);
  --ink: #141417; --muted: rgba(20, 20, 26, .66); --faint: rgba(20, 20, 26, .6);
  --hair: rgba(20, 20, 26, .1); --rim: rgba(255, 255, 255, .7); --rim-low: rgba(255, 255, 255, .18);
  --edit: #b07800; --edit-soft: rgba(214, 150, 0, .16); --comment: #2563d9; --comment-soft: rgba(37, 99, 217, .13);
  --doc: #6a5acd; --danger: #c0352b; --ok: #1f8a4c; --ok-soft: rgba(31, 138, 76, .16);
  --solid: #17171b; --solid-ink: #fbfbfc; --focus: #2563d9;
  --menu: rgba(24, 24, 28, .78); --menu-ink: #f7f7f8;
  --shadow: 0 30px 70px -20px rgba(20, 20, 40, .34), 0 8px 24px -10px rgba(20, 20, 40, .2);
  font: 13.5px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif; color: var(--ink);
  -webkit-font-smoothing: antialiased;
}
.p[data-tone="dark"] {
  --tint: rgba(30, 30, 36, .56); --tint-strong: rgba(34, 34, 40, .84);
  --fill: rgba(255, 255, 255, .07); --fill-hover: rgba(255, 255, 255, .12); --field: rgba(0, 0, 0, .26);
  --ink: #f3f3f6; --muted: rgba(243, 243, 246, .7); --faint: rgba(243, 243, 246, .6);
  --hair: rgba(255, 255, 255, .1); --rim: rgba(255, 255, 255, .28); --rim-low: rgba(255, 255, 255, .06);
  --edit: #f0b43a; --edit-soft: rgba(240, 180, 58, .16); --comment: #7aa7ff; --comment-soft: rgba(122, 167, 255, .16);
  --doc: #a99cff; --danger: #ff7a6e; --ok: #5fd08f; --ok-soft: rgba(95, 208, 143, .2);
  --solid: #f3f3f6; --solid-ink: #141417; --focus: #7aa7ff;
  --menu: rgba(52, 52, 60, .82); --menu-ink: #f7f7f8;
  --shadow: 0 30px 70px -20px rgba(0, 0, 0, .6), 0 8px 24px -10px rgba(0, 0, 0, .4);
}
* { box-sizing: border-box; }
button, select, input, textarea { font: inherit; color: inherit; }
button { cursor: pointer; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
::selection { background: var(--comment-soft); }

/* стекло: размытие и насыщенность фона, кромка с бликом сверху, внутреннее свечение снизу, мягкая тень */
.glass { position: relative; background: var(--tint);
  -webkit-backdrop-filter: blur(32px) saturate(190%) brightness(1.04); backdrop-filter: blur(32px) saturate(190%) brightness(1.04);
  box-shadow: inset 0 1px 0 0 var(--rim), inset 0 0 0 .5px var(--rim-low), inset 0 -24px 48px -36px var(--rim), var(--shadow); }
.glass::before { content: ""; position: absolute; inset: 0; border-radius: inherit; padding: 1px; pointer-events: none;
  background: linear-gradient(155deg, var(--rim), transparent 32%, transparent 68%, var(--rim-low));
  -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0); -webkit-mask-composite: xor; mask-composite: exclude; }
/* блик за курсором живёт только на кромке толщиной 1px – стекло ловит свет краем, без пятна на всю панель */
.glass::after { content: ""; position: absolute; inset: 0; border-radius: inherit; padding: 1px; pointer-events: none; opacity: var(--lo, 0);
  background: radial-gradient(220px circle at var(--lx, 50%) var(--ly, 0%), rgba(255, 255, 255, .75), transparent 70%);
  -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0); -webkit-mask-composite: xor; mask-composite: exclude;
  transition: opacity .6s ease-out; }
@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) { .glass { background: var(--tint-strong); } }

#panel { position: fixed; top: 12px; right: 12px; bottom: 12px; width: 380px; border-radius: 26px; z-index: 2147483646;
  display: flex; flex-direction: column; overflow: hidden; }
#panel[hidden], #pill[hidden], #confirm[hidden], #bottom[hidden], #toast[hidden], #tb[hidden], #cover[hidden], #box[hidden] { display: none; }

header { padding: 20px 20px 0 22px; display: grid; grid-template-columns: 1fr auto; column-gap: 10px; align-items: start; }
h1 { margin: 0; font: italic 700 34px/1 "${FONT}", "Playfair Display", Georgia, serif; letter-spacing: -.005em; }
.tagline { margin-top: 6px; font: 400 15px/1.2 "${FONT}", "Playfair Display", Georgia, serif; color: var(--muted); letter-spacing: .01em; }
.meta { grid-column: 1 / -1; display: flex; align-items: center; gap: 8px; margin-top: 16px; padding-top: 12px; border-top: .5px solid var(--hair); min-width: 0; }
.docname { flex: 1; min-width: 0; color: var(--muted); font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.icon-btn { width: 30px; height: 30px; display: grid; place-items: center; border: 0; border-radius: 10px; background: transparent; color: var(--muted);
  transition: background .15s, color .15s; }
.icon-btn:hover { background: var(--fill-hover); color: var(--ink); }

/* формат – тихий выпадающий список в строке файла; значок слева, когда формат Ревизор определил сам */
.format { position: relative; display: inline-flex; align-items: center; flex: none; color: var(--muted); }
.format select { appearance: none; -webkit-appearance: none; field-sizing: content; height: 26px; padding: 0 22px 0 8px; border: 0; border-radius: 8px;
  background: transparent; font-size: 12.5px; font-weight: 600; color: var(--ink); cursor: pointer; transition: background .15s; }
.format select:hover { background: var(--fill-hover); }
.format::after { content: ""; position: absolute; right: 8px; top: 9px; width: 5px; height: 5px; border-right: 1.5px solid var(--muted);
  border-bottom: 1.5px solid var(--muted); transform: rotate(45deg); pointer-events: none; }
.auto { display: inline-flex; color: var(--comment); }
.auto[hidden] { display: none; }

/* добавить: две равные кнопки с полными названиями, отдельной группой под шапкой */
.tools { padding: 12px 14px 12px; display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.tools-label { grid-column: 1 / -1; padding: 0 4px 2px; font-size: 12px; color: var(--muted); }
.act { display: flex; align-items: center; gap: 8px; height: 38px; padding: 0 12px; border-radius: 13px; border: 0; text-align: left;
  background: var(--fill); box-shadow: inset 0 0 0 .5px var(--hair); color: var(--ink); font-size: 13px; font-weight: 500;
  transition: background .15s, box-shadow .15s, color .15s; }
.act svg { flex: none; color: var(--muted); }
.act:hover { background: var(--fill-hover); }
.act[aria-pressed="true"] { background: var(--comment); color: #fff; box-shadow: none; }
.act[aria-pressed="true"] svg { color: #fff; }

#cards { flex: 1; overflow: auto; padding: 4px 12px 12px; display: flex; flex-direction: column; gap: 8px;
  scrollbar-width: thin; scrollbar-color: var(--hair) transparent; overscroll-behavior: contain;
  -webkit-mask: linear-gradient(transparent, #000 10px, #000 calc(100% - 14px), transparent); mask: linear-gradient(transparent, #000 10px, #000 calc(100% - 14px), transparent); }
.empty { margin: 8px 8px 0; color: var(--muted); font-size: 13px; line-height: 1.55; }
.empty b { color: var(--ink); font-weight: 600; }
.empty ol { margin: 10px 0 0; padding-left: 18px; }
.empty li { margin: 4px 0; }

.card { border-radius: 18px; padding: 11px 12px 12px; background: var(--fill); box-shadow: inset 0 0 0 .5px var(--hair); transition: background .15s; }
.card:hover, .card:focus-within { background: var(--fill-hover); }
.card.orphan { box-shadow: inset 0 0 0 1px var(--hair); background: transparent; }
.head { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--muted); margin-bottom: 7px; min-height: 22px; }
.dot { width: 7px; height: 7px; border-radius: 50%; flex: none; background: var(--edit); box-shadow: 0 0 0 3px var(--edit-soft); }
.comment .dot { background: var(--comment); box-shadow: 0 0 0 3px var(--comment-soft); }
.doc .dot { background: var(--doc); box-shadow: 0 0 0 3px color-mix(in srgb, var(--doc) 18%, transparent); }
.kind { color: var(--ink); font-weight: 600; }
.where { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lost { color: var(--danger); }
.card .icon-btn { width: 28px; height: 28px; border-radius: 9px; margin: -3px -6px -3px 0; }
.quote { font-size: 12.5px; color: var(--muted); margin: 0 0 8px; padding: 6px 9px; border-radius: 10px; background: var(--edit-soft); cursor: pointer;
  max-height: 4.6em; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; }
.edit .quote { text-decoration: line-through; text-decoration-color: color-mix(in srgb, var(--edit) 70%, transparent); }
.quote.diff { text-decoration: none; background: var(--fill); box-shadow: inset 0 0 0 .5px var(--hair); color: var(--ink); -webkit-line-clamp: 6; max-height: none; }
.quote.diff del { color: var(--muted); text-decoration: line-through; text-decoration-color: color-mix(in srgb, var(--edit) 80%, transparent); }
.quote.diff ins { text-decoration: none; background: var(--ok-soft); color: var(--ink); border-radius: 4px; padding: 0 2px; }
.preview { margin-top: 6px; padding: 7px 10px; border-radius: 12px; font-size: 13px; line-height: 1.5; box-shadow: inset 0 0 0 .5px var(--hair); }
.preview[hidden] { display: none; }
.preview > * { margin: 0; }
.preview code { font: 12px ui-monospace, Menlo, monospace; background: var(--fill); padding: 0 4px; border-radius: 5px; }
.preview a { color: var(--comment); }

#view { position: fixed; top: 14px; transform: translateX(-50%); z-index: 2147483646; display: flex; gap: 2px; padding: 3px; border-radius: 999px; }
#view[hidden] { display: none; }
#view button { height: 30px; padding: 0 14px; border: 0; border-radius: 999px; background: none; color: var(--muted); font-size: 12.5px; font-weight: 500;
  transition: background .15s, color .15s; }
#view button:hover { color: var(--ink); }
#view button[aria-checked="true"] { background: var(--solid); color: var(--solid-ink); }
.comment .quote { background: var(--comment-soft); }
textarea, input { width: 100%; border: 0; border-radius: 12px; padding: 8px 10px; background: var(--field); box-shadow: inset 0 0 0 .5px var(--hair);
  color: var(--ink); transition: box-shadow .15s, background .15s; }
textarea { min-height: 62px; resize: vertical; line-height: 1.45; }
input { margin-top: 6px; font-size: 12.5px; }
textarea:focus, input:focus { outline: none; box-shadow: inset 0 0 0 1.5px var(--focus), 0 0 0 3px var(--comment-soft); }
textarea::placeholder, input::placeholder { color: var(--faint); }

details { margin: 6px 6px 0; font-size: 12.5px; color: var(--muted); }
summary { cursor: pointer; padding: 4px 2px; list-style: none; }
summary::-webkit-details-marker { display: none; }
summary::before { content: ""; display: inline-block; width: 5px; height: 5px; margin: 0 8px 2px 2px; border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor; transform: rotate(-45deg); transition: transform .2s; }
details[open] summary::before { transform: rotate(45deg); }
.hist { list-style: none; margin: 4px 0 0; padding: 0; }
.hist li { display: flex; gap: 8px; align-items: flex-start; margin: 6px 0; }
.hist svg { flex: none; margin-top: 2px; }
.hist .ok { color: var(--ok); } .hist .no { color: var(--danger); }

footer { padding: 10px 16px 14px; }
.row { display: flex; align-items: center; gap: 10px; min-height: 36px; }
#status { flex: 1; color: var(--muted); font-size: 12.5px; display: flex; align-items: center; gap: 7px; min-width: 0; }
#status .led { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); flex: none; transition: background .2s, opacity .3s; }
#status[data-state="idle"] .led { opacity: 0; }
#status[data-state="saving"] .led { background: var(--edit); }
#status[data-state="error"] { color: var(--danger); }
#status[data-state="error"] .led { background: var(--danger); }
#retry { display: none; }
#status[data-state="error"] + #retry { display: inline-flex; }
.btn { height: 36px; padding: 0 16px; border-radius: 999px; border: 0; font-weight: 600; font-size: 13px; background: var(--fill); color: var(--ink);
  box-shadow: inset 0 0 0 .5px var(--hair); transition: transform .12s, background .15s; }
.btn:hover { background: var(--fill-hover); }
.btn:active { transform: scale(.97); }
.btn.solid { background: var(--solid); color: var(--solid-ink); box-shadow: none; }
.btn.solid:hover { background: color-mix(in srgb, var(--solid) 88%, var(--comment)); }
#confirm-text { flex: 1; font-size: 12.5px; color: var(--ink); }

#pill { position: fixed; right: 18px; bottom: 18px; z-index: 2147483646; height: 44px; padding: 0 18px 0 16px; border-radius: 999px; border: 0;
  display: flex; align-items: center; gap: 10px; color: var(--ink); }
#pill .t { font: italic 700 20px/1 "${FONT}", Georgia, serif; }
#pill .n { min-width: 20px; height: 20px; padding: 0 6px; border-radius: 999px; background: var(--solid); color: var(--solid-ink); font-size: 11.5px;
  font-weight: 600; display: grid; place-items: center; }

#tb { position: fixed; display: flex; gap: 2px; padding: 4px; border-radius: 14px; z-index: 2147483647; color: var(--menu-ink);
  background: var(--menu); -webkit-backdrop-filter: blur(24px) saturate(180%); backdrop-filter: blur(24px) saturate(180%);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .16), 0 12px 30px -8px rgba(0, 0, 0, .45); transform-origin: 50% 100%; }
#tb { box-shadow: inset 0 1px 0 rgba(255, 255, 255, .16), inset 0 0 0 .5px rgba(255, 255, 255, .14), 0 12px 30px -8px rgba(0, 0, 0, .45) !important; }
@media (hover: none) { #tb button { height: 44px !important; } kbd { display: none; } textarea, input, select { font-size: 16px !important; } }
#tb button { display: inline-flex; align-items: center; gap: 7px; height: 32px; padding: 0 12px; border: 0; border-radius: 10px; background: none;
  color: inherit; font-size: 13px; }
#tb button:hover { background: rgba(255, 255, 255, .14); }
kbd { font: 600 10.5px/1 -apple-system, sans-serif; padding: 3px 5px; border-radius: 5px; background: rgba(255, 255, 255, .14); opacity: .85; }

@media (prefers-reduced-motion: reduce) { #box, .glass::after, .act, .btn, .card { transition: none !important; } }
.quote { width: 100%; border: 0; text-align: left; font: inherit; font-size: 12.5px; }
.toast-act { margin-left: 12px; border: 0; background: none; color: var(--comment); font-weight: 600; padding: 0; }
.text-mode #add-block { display: none; }
.text-mode .tools { grid-template-columns: 1fr; }
#box { position: fixed; pointer-events: none; border-radius: 10px; z-index: 2147483645;
  box-shadow: 0 0 0 2px var(--comment), 0 0 0 6px var(--comment-soft); background: color-mix(in srgb, var(--comment) 6%, transparent);
  transition: left .12s, top .12s, width .12s, height .12s; }
#toast { position: fixed; z-index: 2147483647; max-width: 340px; padding: 10px 16px; border-radius: 16px; font-size: 13px; color: var(--ink); }
.ghost { position: fixed; z-index: 2147483647; pointer-events: none; padding: 6px 9px; border-radius: 10px; font-size: 12.5px; color: var(--ink);
  background: var(--edit-soft); box-shadow: 0 10px 24px -10px rgba(0, 0, 0, .35); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.ghost.comment { background: var(--comment-soft); }
#cover { position: fixed; inset: 0; z-index: 2147483647; display: grid; place-items: center; text-align: center;
  background: color-mix(in srgb, var(--tint) 60%, transparent); -webkit-backdrop-filter: blur(24px) saturate(160%); backdrop-filter: blur(24px) saturate(160%); }
#cover h2 { margin: 0 0 10px; font: italic 700 46px/1 "${FONT}", Georgia, serif; }
#cover p { margin: 0; color: var(--muted); font-size: 14px; }

@media (max-width: 760px) {
  /* снизу панель лежит прямо на тексте документа – подложка плотнее, чтобы строки под ней не спорили с карточками */
  #panel { top: auto; left: 8px; right: 8px; bottom: 8px; width: auto; height: 50vh; border-radius: 24px; background: var(--tint-strong); }
  header { padding: 14px 16px 0 18px; } h1 { font-size: 26px; } .tagline { font-size: 13px; margin-top: 3px; }
  .meta { margin-top: 10px; padding-top: 8px; } .tools { padding: 8px 12px; } .tools-label { display: none; }
  #toast { left: 16px !important; right: 16px; bottom: calc(50vh + 20px) !important; max-width: none; }
}
</style>
<div class="p">
  <section id="panel" class="glass" aria-label="Ревизор – правки к документу">
    <header>
      <div><h1>Ревизор</h1><div class="tagline">Стоп нейрослоп</div></div>
      <button class="icon-btn" id="min" title="Свернуть панель" aria-label="Свернуть панель">${icon("collapse", 18)}</button>
      <div class="meta">
        <span class="docname" id="name">…</span>
        <label class="format" title="Формат текста. Попадает в стилевые пары вместе с правками">
          <span class="auto" id="auto" title="Формат определён автоматически">${icon("auto", 14)}</span>
          <select id="reg" name="revizor-format" aria-label="Формат текста"></select>
        </label>
      </div>
    </header>
    <div class="tools" role="group" aria-labelledby="tools-label">
      <span class="tools-label" id="tools-label">Комментарий</span>
      <button class="act" id="add-doc" title="Комментарий, который касается всего документа">${icon("note", 16)}Весь документ</button>
      <button class="act" id="add-block" aria-pressed="false" title="Кликни по слайду, графику или таблице на странице">${icon("block", 16)}<span>Выбрать блок</span></button>
    </div>
    <div id="cards" role="list" aria-label="Правки"></div>
    <footer>
      <div class="row" id="bottom">
        <span id="status" role="status" aria-live="polite"><span class="led"></span><span id="status-text">Загрузка…</span></span>
        <button class="btn" id="retry">${icon("retry", 14)} Повторить</button>
        <button class="btn solid" id="done">Готово</button>
      </div>
      <div class="row" id="confirm" hidden>
        <span id="confirm-text"></span>
        <button class="btn" id="confirm-no">Отмена</button>
        <button class="btn solid" id="confirm-yes">Отправить</button>
      </div>
    </footer>
  </section>
  <button id="pill" class="glass" hidden aria-label="Открыть панель правок"><span class="t">Ревизор</span><span class="n" id="pill-n">0</span></button>
  <div id="view" class="glass" role="radiogroup" aria-label="Вид документа" hidden>
    <button data-v="md" role="radio" aria-checked="false">Оформление</button><button data-v="plain" role="radio" aria-checked="false">Текст</button>
  </div>
  <div id="tb" role="toolbar" aria-label="Действия с выделением" hidden>
    <button data-kind="edit">${icon("edit", 14)}Изменить<kbd>E</kbd></button>
    <button data-kind="comment">${icon("comment", 14)}Комментарий<kbd>C</kbd></button>
  </div>
  <div id="box" hidden></div>
  <div id="toast" class="glass" role="status" aria-live="polite" hidden></div>
  <div id="cover" hidden><div><h2>Отправлено</h2><p id="cover-text">Вкладку можно закрыть.</p></div></div>
</div>`;
  const $ = (s) => sh.querySelector(s);

  // Тон стекла – по яркости страницы под панелью, как у Liquid Glass. Тема системы решает, только если фон прозрачный.
  function pageTone() {
    for (const el of [document.body, document.documentElement]) {
      if (!el) continue;
      const m = getComputedStyle(el).backgroundColor.match(/[\d.]+/g);
      if (!m || (m[3] !== undefined && +m[3] < 0.05)) continue;
      const [r, g, b] = m.map(Number);
      return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 > 0.5 ? "light" : "dark";
    }
    return MODE === "html" ? "light" : (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }
  const syncTone = () => { sh.querySelector(".p").dataset.tone = pageTone(); };
  // в тексте нет слайдов и графиков: «Выбрать блок» там не нужен
  const canBlock = () => MODE === "html" || mdOn();
  const syncTextMode = () => sh.querySelector(".p").classList.toggle("text-mode", !canBlock());
  syncTextMode();
  // фокус ушёл в поле панели – тулбар выделения больше не нужен
  sh.addEventListener("focusin", (e) => { if (!e.composedPath().some((n) => n.id === "tb")) hideToolbar(); });
  $("#reg").innerHTML = REGISTERS.map(([v, t]) => `<option value="${v}">${t}</option>`).join("");

  // Блик на кромке стекла идёт за курсором – так панель читается как материал, а не как плашка.
  for (const g of [$("#panel"), $("#pill")]) {
    g.addEventListener("pointermove", (e) => {
      const r = g.getBoundingClientRect();
      g.style.setProperty("--lx", `${e.clientX - r.left}px`); g.style.setProperty("--ly", `${e.clientY - r.top}px`); g.style.setProperty("--lo", "1");
    });
    g.addEventListener("pointerleave", () => g.style.setProperty("--lo", "0"));
  }

  // Бандл может заменить <html> целиком: тогда панель, стиль подсветки и фреймы подключаются заново.
  let lastHtml = null;
  function ensureAttached() {
    const html = document.documentElement;
    if (!html) return;
    if (host.parentNode !== html) html.append(host);
    if (document.head && hl.parentNode !== document.head) document.head.append(hl);
    syncPanelAttr();
    syncTone();
    if (html !== lastHtml) {
      lastHtml = html;
      if (blockMode) html.setAttribute("data-revizor-block", "");
      if (ready) { anchorRoot(main); paint(); renderCards(); }
      refit();
    }
    scanFrames();
    if (ready && registerAuto) applyAutoRegister();
  }
  let scheduled = false;
  new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; ensureAttached(); });
  }).observe(document, { childList: true, subtree: true });

  // ---------- формат текста: определяется сам, ручной выбор главнее ----------

  let registerAuto = true;

  function detectRegister() {
    const slides = document.querySelectorAll(".slide, [data-slide], section.slide").length;
    if (frames.size >= 3 || slides >= 3) return "deck";
    const text = index(main).text;
    const cyr = (text.match(/[а-яё]/gi) || []).length, lat = (text.match(/[a-z]/gi) || []).length;
    if (MODE === "html") {
      const visuals = main.el ? main.el.querySelectorAll("svg, canvas, table").length : 0;
      if (lat > cyr * 2 && lat > 400) return "client";
      return visuals >= 2 ? "report" : "doc";
    }
    const t = text.trim();
    const heads = (t.match(/^#{1,6}\s/gm) || []).length;
    if (/^#{1,6}\s*(требования|критерии|acceptance|user stor|сценари|ограничения|out of scope|definition of done)/im.test(t)) return "spec";
    if (/^(привет|добрый|здравствуй|коллеги|hi\b|hello|dear)/i.test(t) && /\n\s*(спасибо|с уважением|best|regards|thanks)/i.test(t)) return "email";
    if (!heads && t.length < 1500) return "slack";
    const nums = (t.match(/\d+[.,]?\d*\s?(%|п\.\s?п\.|\$|₽|€|k\b|тыс|млн)/gi) || []).length;
    if (nums >= 5 || /^\s*\|.+\|\s*$/m.test(t)) return "report";
    return "doc";
  }

  function applyAutoRegister() {
    const r = detectRegister();
    if (r === register) return;
    register = r;
    syncRegisterUI();
    scheduleSave();
  }
  function syncRegisterUI() { $("#reg").value = register; $("#auto").hidden = !registerAuto || !register; }

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
      e.innerHTML = `<b>Правок пока нет.</b><ol>
        <li>Выдели текст – появится «Изменить» и «Комментарий».</li>
        ${!canBlock() ? "" : MODE === "md" ? "<li>«Выбрать блок» – клик по абзацу, таблице или заголовку раздела.</li>" : "<li>«Выбрать блок» – клик по слайду, графику или таблице.</li>"}
        <li>«Весь документ» – комментарий про документ целиком.</li></ol>
        <p>Закончил – напиши агенту «поправь».</p>`;
      box.append(e);
    }
    for (const it of all) box.append(card(it));
    if (closed.length) {
      const d = document.createElement("details");
      d.innerHTML = `<summary>История · ${closed.length}</summary><ul class="hist"></ul>`;
      for (const c of closed) {
        const li = document.createElement("li");
        const what = c.kind === "edit" ? `«${clean(c.quote, 60)}» → «${clean(c.replacement, 60)}»`
          : (c.quote ? `«${clean(c.quote, 40)}»: ` : c.where ? `${c.where}: ` : "") + clean(c.comment, 80);
        li.innerHTML = `<span class="${c.state === "applied" ? "ok" : "no"}">${icon(c.state === "applied" ? "check" : "reject", 14)}</span><span></span>`;
        li.lastChild.textContent = what + (c.note ? " – " + c.note : "");
        d.querySelector("ul").append(li);
      }
      box.append(d);
    }
    $("#pill-n").textContent = String(items.length);
  }

  function card(it) {
    const c = document.createElement("article");
    const lost = it.scope === "text" && (it.orphan || !rootOf(it));
    c.className = `card ${it.scope === "doc" ? "doc" : it.kind}${lost ? " orphan" : ""}`;
    c.dataset.id = it.id;
    c.setAttribute("role", "listitem");
    const kind = it.scope === "doc" ? "Весь документ" : it.scope === "block" ? "Блок" : it.kind === "edit" ? "Правка" : "Комментарий";
    c.innerHTML = `<div class="head"><span class="dot"></span><span class="kind"></span><span class="where"></span>
      <button class="icon-btn" title="Удалить" aria-label="Удалить правку">${icon("close", 14)}</button></div>`;
    c.querySelector(".kind").textContent = kind;
    const where = c.querySelector(".where");
    where.textContent = lost ? "место не найдено" : it.where || "";
    if (lost) where.classList.add("lost");
    c.querySelector(".icon-btn").onclick = () => removeItem(it, c);
    if (it.scope !== "doc") {
      const q = document.createElement("button"); q.type = "button"; q.className = "quote"; q.title = "Показать в документе";
      q.onclick = () => reveal(it);
      c.append(q);
      renderQuote(q, it);
    }
    const ta = document.createElement("textarea");
    ta.setAttribute("aria-label", it.kind === "edit" ? "Как должно быть" : "Комментарий");
    ta.name = `revizor-${it.id}`;
    if (it.kind === "edit") {
      ta.value = it.replacement;
      const q = c.querySelector(".quote");
      let pv = null;
      if (MDP) { pv = document.createElement("div"); pv.className = "preview"; pv.setAttribute("aria-label", "Как правка будет выглядеть"); }
      const refresh = () => {
        renderQuote(q, it);
        if (!pv) return;
        const show = /[*_`~\[\]#>|]/.test(it.replacement) && it.replacement !== draft(it.quote);
        pv.hidden = !show;
        if (show) renderPreview(pv, it.replacement);
      };
      let raf = 0;
      ta.oninput = () => { it.replacement = ta.value; scheduleSave(); cancelAnimationFrame(raf); raf = requestAnimationFrame(refresh); };
      if (MODE === "md") ta.addEventListener("keydown", (e) => mdShortcut(e, ta));
      if (pv) { pv.hidden = true; ta.after(pv); }
      requestAnimationFrame(refresh);
      const why = document.createElement("input"); why.placeholder = "почему – необязательно"; why.value = it.why || "";
      why.setAttribute("aria-label", "Почему");
      why.name = `revizor-${it.id}-why`;
      why.oninput = () => { it.why = why.value; scheduleSave(); };
      c.append(ta);
      if (pv) c.append(pv);
      c.append(why);
    } else {
      ta.value = it.comment || ""; ta.placeholder = it.scope === "doc" ? "что поправить во всём документе" : "что поправить";
      ta.oninput = () => { it.comment = ta.value; scheduleSave(); };
      c.append(ta);
    }
    return c;
  }

  // «Было → стало» пословно: удалённое зачёркнуто, добавленное подсвечено. Пока правка не тронута – просто цитата.
  function renderQuote(q, it) {
    if (!q) return;
    q.textContent = "";
    q.classList.remove("diff");
    if (it.scope === "block") { q.textContent = it.snippet; return; }
    const before = clean(it.quote, 1e6), after = clean(it.replacement || "", 1e6);
    if (it.kind !== "edit" || !after || before === after) { q.textContent = clean(it.quote, 300); return; }
    q.classList.add("diff");
    for (const [op, text] of wordDiff(before, after)) {
      if (op === "=") { q.append(text); continue; }
      const sp = document.createElement(op === "-" ? "del" : "ins"); sp.textContent = text; q.append(sp);
    }
  }

  function wordDiff(a, b) {
    const A = a.split(/(\s+)/).filter(Boolean), B = b.split(/(\s+)/).filter(Boolean);
    if (A.length * B.length > 160000) return [["-", a], ["=", " "], ["+", b]];
    const n = A.length, m = B.length, L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    const out = [], add = (op, t) => { const last = out[out.length - 1]; if (last && last[0] === op) last[1] += t; else out.push([op, t]); };
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (A[i] === B[j]) { add("=", A[i]); i++; j++; }
      else if (L[i + 1][j] >= L[i][j + 1]) add("-", A[i++]);
      else add("+", B[j++]);
    }
    while (i < n) add("-", A[i++]);
    while (j < m) add("+", B[j++]);
    return out;
  }

  // ⌘B и ⌘I в поле «стало» оборачивают выделение в разметку Markdown.
  function mdShortcut(e, ta) {
    if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
    const k = e.key.toLowerCase();
    const wrap = k === "b" || k === "и" ? "**" : k === "i" || k === "ш" ? "_" : null;
    if (!wrap) return;
    e.preventDefault();
    const { selectionStart: a, selectionEnd: b, value: v } = ta;
    const sel = v.slice(a, b);
    const on = sel.startsWith(wrap) && sel.endsWith(wrap) && sel.length >= wrap.length * 2;
    const next = on ? sel.slice(wrap.length, sel.length - wrap.length) : wrap + sel + wrap;
    ta.setRangeText(next, a, b, sel ? "select" : "end");
    if (!sel) ta.setSelectionRange(a + wrap.length, a + wrap.length);
    ta.dispatchEvent(new Event("input"));
  }

  async function removeItem(it, el) {
    const at = items.indexOf(it);
    items = items.filter((i) => i !== it);
    paint(); scheduleSave();
    toast(`${it.kind === "edit" ? "Правка удалена" : "Комментарий удалён"}`, { label: "Вернуть", run: () => {
      items.splice(Math.min(at, items.length), 0, it);
      if (it.scope === "text") anchorRoot(rootOf(it) || main);
      paint(); renderCards(); focusCard(it.id); scheduleSave();
    } });
    el.style.overflow = "hidden";
    await anim(el, { opacity: [1, 0], height: [`${el.offsetHeight}px`, "0px"], marginBottom: ["0px", "-8px"], paddingTop: ["11px", "0px"], paddingBottom: ["12px", "0px"] }, { duration: 0.2 });
    renderCards();
  }

  function reveal(it) {
    const r = rootOf(it);
    if (!r) return toast("Слайд ещё не загрузился");
    if (r.frame) r.frame.scrollIntoView({ block: "center", behavior: reduce.matches ? "auto" : "smooth" });
    if (it.scope === "block") {
      const el = findBlock(it);
      if (!el) return toast("Блок не найден – страница поменялась");
      if (!r.frame) el.scrollIntoView({ block: "center", behavior: reduce.matches ? "auto" : "smooth" });
      setTimeout(() => { outline(r, el); setTimeout(() => outline(null), 1200); }, 400);
      return;
    }
    if (it.orphan) return;
    const rg = rangeFor(r, index(r), it.start, it.end);
    if (!r.frame) (rg.startContainer.parentElement || r.el).scrollIntoView({ block: "center", behavior: reduce.matches ? "auto" : "smooth" });
    const C = r.win.CSS, H = r.win.Highlight;
    if (C && C.highlights && H) {
      C.highlights.set("rv-flash", new H(rg));
      setTimeout(() => C.highlights.delete("rv-flash"), 900);
    }
  }

  function focusCard(id, { quiet = false } = {}) {
    setPanel(true);
    const c = sh.querySelector(`.card[data-id="${id}"]`);
    if (!c) return null;
    c.scrollIntoView({ block: "nearest", behavior: reduce.matches ? "auto" : "smooth" });
    if (!quiet) anim(c, { backgroundColor: ["rgba(255, 196, 0, .28)", getComputedStyle(c).backgroundColor] }, { duration: 0.9 })
      .then(() => { c.style.backgroundColor = ""; }); // иначе инлайн-фон перебьёт :hover
    c.querySelector("textarea").focus({ preventScroll: true });
    return c;
  }

  // Главный момент: цитата перелетает от выделения в свою карточку – видно, откуда правка и куда она легла.
  async function flyToCard(it, fromRect) {
    const c = focusCard(it.id, { quiet: true });
    if (!c) return;
    const target = c.querySelector(".quote") || c;
    if (!fromRect || reduce.matches) { anim(c, { opacity: [0, 1] }, { duration: 0.18 }); return; }
    const to = target.getBoundingClientRect();
    const g = document.createElement("div");
    g.className = "ghost" + (it.kind === "comment" ? " comment" : "");
    g.textContent = it.scope === "block" ? it.snippet : clean(it.quote, 80);
    Object.assign(g.style, { left: `${to.left}px`, top: `${to.top}px`, width: `${to.width}px`, height: `${Math.min(to.height, 34)}px` });
    sh.querySelector(".p").append(g);
    const dx = fromRect.left - to.left, dy = fromRect.top - to.top;
    const sx = Math.max(0.2, Math.min(3, fromRect.width / to.width));
    target.style.opacity = "0";
    anim(c, { opacity: [0.4, 1], y: [10, 0] }, { duration: 0.42, delay: 0.06 });
    await anim(g, { x: [dx, 0], y: [dy, 0], scale: [sx, 1], opacity: [0.9, 1] }, { duration: 0.48 });
    target.style.opacity = "";
    await anim(g, { opacity: [1, 0] }, { duration: 0.14 });
    g.remove();
  }

  function outline(r, el) {
    const b = $("#box");
    if (!r || !el) { b.hidden = true; return; }
    const rc = toTop(r, el.getBoundingClientRect());
    Object.assign(b.style, { left: rc.left - 3 + "px", top: rc.top - 3 + "px", width: rc.width + 6 + "px", height: rc.height + 6 + "px" });
    b.hidden = false;
  }

  let toastTimer = null;
  function toast(msg, action) {
    const t = $("#toast");
    t.textContent = msg;
    if (action) {
      const b = document.createElement("button"); b.className = "toast-act"; b.textContent = action.label;
      b.onclick = () => { clearTimeout(toastTimer); t.hidden = true; action.run(); };
      t.append(b);
    }
    const sheet = isSheet();
    t.style.left = sheet ? "16px" : "auto";
    t.style.right = sheet ? "16px" : panelOpen ? "416px" : "18px";
    t.style.bottom = sheet ? "" : "24px";
    const fresh = t.hidden;
    t.hidden = false;
    if (fresh) anim(t, { opacity: [0, 1], y: [10, 0] }, { duration: 0.24 });
    clearTimeout(toastTimer);
    toastTimer = setTimeout(async () => { await anim(t, { opacity: [1, 0], y: [0, 6] }, { duration: 0.16 }); t.hidden = true; }, action ? 5000 : 2800);
  }

  // ---------- выделение → правка или комментарий ----------

  function readSelection(r) {
    const sel = r.win.getSelection();
    if (!sel || !sel.rangeCount || sel.isCollapsed) return null;
    const rg = sel.getRangeAt(0);
    if (!r.el || !r.el.contains(rg.commonAncestorContainer)) return null;
    const idx = index(r);
    let s = offsetOf(r, idx, rg.startContainer, rg.startOffset), e = offsetOf(r, idx, rg.endContainer, rg.endOffset);
    // край выделения посреди слова дотягивается до границы слова: правка не начинается с «и|нсталлы»
    const w = (ch) => ch !== undefined && /[\p{L}\p{N}]/u.test(ch);
    while (s > 0 && w(idx.text[s - 1]) && w(idx.text[s])) s--;
    while (e < idx.text.length && w(idx.text[e - 1]) && w(idx.text[e])) e++;
    while (s < e && /\s/.test(idx.text[s])) s++;
    while (e > s && /\s/.test(idx.text[e - 1])) e--;
    if (s >= e) return null;
    return { r, start: s, end: e, rect: toTop(r, rg.getBoundingClientRect()), idx, node: rg.startContainer };
  }

  function showToolbar(sel) {
    const tb = $("#tb");
    const fresh = tb.hidden;
    tb.hidden = false;
    const w = tb.offsetWidth, h = tb.offsetHeight;
    const above = sel.rect.top > h + 16;
    tb.style.transformOrigin = above ? "50% 100%" : "50% 0%";
    tb.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, sel.rect.left + sel.rect.width / 2 - w / 2)) + "px";
    tb.style.top = (above ? sel.rect.top - h - 10 : sel.rect.bottom + 10) + "px";
    if (fresh) anim(tb, { opacity: [0, 1], scale: [0.94, 1], y: [above ? 4 : -4, 0] }, { duration: 0.16 });
  }
  function hideToolbar() { $("#tb").hidden = true; pendingSel = null; }

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
      where: MODE === "md" ? mdWhere(sel.start) : MODE === "text" ? whereText(text, sel.start) : whereEl(sel.r, el),
      created: new Date().toISOString(), state: "pending",
    };
    if (sel.r.key) it.frame = sel.r.key;
    if (kind === "edit") { it.replacement = draft(quote); it.why = ""; } else { it.comment = ""; }
    items.push(it);
    sel.r.win.getSelection().removeAllRanges();
    paint(); renderCards(); flyToCard(it, sel.rect); scheduleSave();
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
    $("#add-block").setAttribute("aria-pressed", String(on));
    $("#add-block span").textContent = on ? "Отменить · Esc" : "Выбрать блок";
    for (const r of allRoots()) r.doc.documentElement.toggleAttribute("data-revizor-block", on);
    if (on) toast("Кликни по блоку. Esc – отмена");
    else { outline(null); hoverEl = null; }
  }

  function blockTarget(r, e) {
    if (r === main && e.composedPath().includes(host)) return null;
    if (r === main && mdOn()) {
      const b = e.target && e.target.closest ? e.target.closest("[data-ls]:not(span):not(td):not(th):not(tr)") : null;
      return b && r.el.contains(b) && b !== r.el ? b : null;
    }
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
    const from = toTop(r, el.getBoundingClientRect());
    setBlockMode(false);
    // клик по фрейму целиком – комментарий ко всему слайду
    if (el.tagName === "IFRAME") {
      const fr = [...frames.values()].find((x) => x.frame === el);
      if (fr) { r = fr; el = fr.doc.body; }
    }
    const md = r === main && mdOn() && el.dataset.ls;
    const it = { id: uid(), kind: "comment", scope: "block", selector: cssPath(r, el),
      snippet: md ? mdBlockLabel(el) : r.key && el === r.doc.body ? "слайд целиком" : blockLabel(el),
      text: clean(el.textContent, 120), where: md ? mdWhere(+el.dataset.s) : whereEl(r, el), comment: "", created: new Date().toISOString(), state: "pending" };
    if (md) it.lines = mdBlockLines(el);
    if (r.key) it.frame = r.key;
    items.push(it); renderCards();
    flyToCard(it, { left: from.left, top: from.top, width: Math.min(from.width, 320), height: 34 });
    scheduleSave();
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
      if (!(r === main && e.composedPath().includes(host))) $("#tb").hidden = true;
    });
    // Выделение пальцем или Shift+стрелками не даёт mouseup: тулбар появляется, когда выделение перестало меняться.
    let selTimer = null;
    d.addEventListener("selectionchange", () => {
      if (blockMode) return;
      clearTimeout(selTimer);
      selTimer = setTimeout(() => {
        const sel = readSelection(r);
        if (sel) { pendingSel = sel; showToolbar(sel); }
      }, 300);
    });
    d.addEventListener("keydown", onKey, true);
  }

  function onKey(e) {
    if (e.key === "Escape" && blockMode) { setBlockMode(false); return; }
    // E/C и у/с – буквы. В поле панели или страницы это ввод, а не команда.
    const t = e.composedPath()[0];
    if (t && t.closest && t.closest("input, textarea, select, [contenteditable]:not([contenteditable=false])")) return;
    if ($("#tb").hidden || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "e" || e.key === "у") { e.preventDefault(); create("edit"); }
    if (e.key === "c" || e.key === "с") { e.preventDefault(); create("comment"); }
    if (e.key === "Escape") hideToolbar();
  }

  // ---------- сохранение ----------

  function setStatus(state, text) {
    $("#status").dataset.state = state;
    $("#status-text").textContent = text;
  }
  function payload() { const p = { register: register || null, register_auto: registerAuto, items }; if (MODE === "md") p.view = view; return p; }
  // Статус молчит, пока нечего сказать: число правок, короткое «Сохранено» после записи, ошибка – надолго.
  let quietTimer = null;
  function restStatus() {
    const n = items.filter(keep).length;
    setStatus(n ? "ok" : "idle", n ? `${n} ${plural(n)}` : "");
  }
  function scheduleSave() {
    clearTimeout(quietTimer);
    $("#status").dataset.state = "saving";
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 400);
  }
  async function save() {
    try {
      const r = await fetch("/api/review", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
      if (!r.ok) throw new Error(r.status);
      syncClosed((await r.json()).closed || []);
      setStatus("ok", "Сохранено");
      const led = $("#status .led");
      anim(led, { scale: [1, 1.9, 1], opacity: [1, 0.7, 1] }, { duration: 0.5 }).then(() => { led.style.opacity = ""; led.style.transform = ""; });
      anim($("#status-text"), { opacity: [0, 1], x: [-4, 0] }, { duration: 0.2 });
      quietTimer = setTimeout(restStatus, 1600);
    } catch (e) {
      setStatus("error", /^\d+$/.test(e.message) ? `Не сохранилось: ошибка сервера ${e.message}` : "Не сохранилось: сервер не отвечает");
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
      setStatus("error", "Не отправилось: сервер остановлен");
      return;
    }
    $("#cover-text").textContent = `${items.length} ${plural(items.length)} ушли агенту. Вкладку можно закрыть.`;
    const cv = $("#cover");
    cv.hidden = false;
    anim(cv, { opacity: [0, 1] }, { duration: 0.3 });
    anim(cv.querySelector("h2"), { opacity: [0, 1], y: [14, 0] }, { duration: 0.5, delay: 0.08 });
  }

  // ---------- кнопки панели ----------

  $("#tb").addEventListener("mousedown", (e) => e.preventDefault());
  $("#tb").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) create(b.dataset.kind); });
  $("#add-doc").onclick = () => {
    const it = { id: uid(), kind: "comment", scope: "doc", comment: "", created: new Date().toISOString(), state: "pending" };
    items.unshift(it); renderCards();
    const c = focusCard(it.id, { quiet: true });
    anim(c, { opacity: [0, 1], y: [-8, 0] }, { duration: 0.26 });
    scheduleSave();
  };
  $("#add-block").onclick = () => setBlockMode(!blockMode);
  $("#reg").onchange = (e) => { register = e.target.value; registerAuto = !register; if (registerAuto) applyAutoRegister(); syncRegisterUI(); scheduleSave(); };
  $("#retry").onclick = () => { scheduleSave(); };

  // «Готово» необратимо: сервер закрывается и правки уходят агенту. Поэтому только кликом и с подтверждением, без горячей клавиши.
  async function askDone(on) {
    const n = items.filter(keep).length;
    $("#confirm-text").textContent = n ? `Отправить агенту ${n} ${plural(n)}? Редактор закроется.` : "Правок нет. Закрыть редактор?";
    const show = on ? $("#confirm") : $("#bottom"), hide = on ? $("#bottom") : $("#confirm");
    hide.hidden = true; show.hidden = false;
    anim(show, { opacity: [0, 1], y: [6, 0] }, { duration: 0.18 });
    (on ? $("#confirm-no") : $("#done")).focus();
  }
  $("#done").onclick = () => askDone(true);
  $("#confirm-no").onclick = () => askDone(false);
  $("#confirm-yes").onclick = () => { $("#confirm").hidden = true; $("#bottom").hidden = false; done(); };

  // ---------- панель: сбоку на широком экране, снизу на узком ----------

  const PANEL_W = 404; // 380 панели + отступы стекла от края
  // innerWidth растёт от отступа под панель и широкого контента, поэтому решает media query по ширине экрана
  const sheetMq = window.matchMedia("(max-width: 760px)");
  const isSheet = () => sheetMq.matches;
  sheetMq.addEventListener("change", () => refit());

  // Страница шире телефона (широкая таблица отчёта) раздувает layout viewport, и fixed-панель уезжает за экран.
  // Нижняя панель и пилюля держатся за видимую область.
  function placeSheet() {
    const p = $("#panel"), pill = $("#pill"), vv = window.visualViewport;
    const reset = () => { for (const el of [p, pill]) for (const k of ["left", "top", "right", "bottom", "width", "height"]) el.style[k] = ""; };
    if (!isSheet() || !vv) return reset();
    const h = Math.round(vv.height * 0.5);
    Object.assign(p.style, { left: `${vv.offsetLeft + 8}px`, top: `${vv.offsetTop + vv.height - h - 8}px`, width: `${vv.width - 16}px`, height: `${h}px`, right: "auto", bottom: "auto" });
    const pw = pill.offsetWidth || 150;
    Object.assign(pill.style, { left: `${vv.offsetLeft + vv.width - pw - 16}px`, top: `${vv.offsetTop + vv.height - 60}px`, right: "auto", bottom: "auto" });
  }
  if (window.visualViewport) for (const ev of ["resize", "scroll"]) window.visualViewport.addEventListener(ev, placeSheet);
  function syncPanelAttr() {
    const html = document.documentElement;
    $("#view").style.left = panelOpen && !isSheet() ? "calc((100vw - 404px) / 2)" : "50%";
    if (!panelOpen) html.removeAttribute("data-revizor-panel");
    else html.setAttribute("data-revizor-panel", isSheet() ? "sheet" : "side");
  }
  async function setPanel(open, { animate = true } = {}) {
    if (open === panelOpen && !$("#panel").hidden === open) return;
    panelOpen = open;
    const p = $("#panel"), pill = $("#pill");
    if (open) {
      pill.hidden = true; p.hidden = false;
      syncPanelAttr(); fit(); placeSheet();
      // filter на стекле мешает backdrop-filter – после входа снимаем инлайн-стиль
      if (animate) anim(p, { opacity: [0, 1], x: [isSheet() ? 0 : 28, 0], y: [isSheet() ? 28 : 0, 0], filter: ["blur(6px)", "blur(0px)"] }, { duration: 0.42 })
        .then(() => { p.style.filter = ""; });
    } else {
      syncPanelAttr(); fit();
      if (animate) await anim(p, { opacity: [1, 0], x: [0, isSheet() ? 0 : 20], y: [0, isSheet() ? 20 : 0], scale: [1, 0.98] }, { duration: 0.2 });
      p.hidden = true; pill.hidden = false; placeSheet();
      if (animate) anim(pill, { opacity: [0, 1], scale: [0.9, 1] }, { duration: 0.26 });
    }
  }

  // Широкий контент (дек 1280px) ужимается через zoom на <body>, чтобы помещался слева от панели. Панель свёрнута – масштаб 100%.
  function fit() {
    const b = document.body;
    if (!b) return;
    b.style.zoom = "";
    if (!panelOpen || isSheet()) return;
    const html = document.documentElement;
    const content = Math.max(html.scrollWidth - PANEL_W, b.scrollWidth); // у body бывает overflow-x: hidden
    const avail = html.clientWidth - PANEL_W;
    if (content > avail + 1) b.style.zoom = String(Math.max(0.5, avail / content));
  }
  let fitTimer = null;
  const refit = () => { clearTimeout(fitTimer); fitTimer = setTimeout(() => { syncPanelAttr(); fit(); placeSheet(); }, 100); };
  window.addEventListener("resize", refit);
  $("#min").onclick = async () => { await setPanel(false); $("#pill").focus(); };
  $("#pill").onclick = async () => { await setPanel(true); $("#min").focus(); };

  // ---------- старт ----------

  // ---------- вид Markdown: «Оформление» или «Текст» ----------

  function setView(v, save = true) {
    if (MODE !== "md") return;
    if (v === "md" && !mdMap) v = "plain";
    const changed = v !== view;
    view = v;
    document.documentElement.setAttribute("data-revizor-view", v);
    for (const b of sh.querySelectorAll("#view button")) b.setAttribute("aria-checked", String(b.dataset.v === v));
    if (!changed) return;
    hideToolbar();
    if (blockMode) setBlockMode(false);
    syncTextMode();
    if (ready) { anchorRoot(main); paint(); renderCards(); if (save) scheduleSave(); }
  }
  if (MODE === "md") {
    renderMarkdown();
    setView(mdMap ? "md" : "plain", false);
    $("#view").hidden = !mdMap;
    $("#view").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setView(b.dataset.v); });
  } else if (MODE === "text") renderMarkdown(); // только SOURCE и строки, без оформления

  attach(main);
  ensureAttached();
  (async function init() {
    let state;
    try {
      state = await (await fetch("/api/state")).json();
    } catch (e) {
      setStatus("error", "Сервер не отвечает – перезапусти revizor");
      return;
    }
    const { doc, review } = state;
    $("#name").textContent = doc.name;
    $("#name").title = doc.path;
    register = review.register || "";
    registerAuto = review.register_auto !== false && !(review.register && review.register_auto === undefined);
    if (MODE === "md" && review.view) setView(review.view, false);
    items = review.items.filter((i) => (i.state || "pending") === "pending");
    closed = review.items.filter((i) => (i.state || "pending") !== "pending");
    ready = true;
    allRoots().forEach(anchorRoot);
    panelOpen = false;
    setPanel(true);
    paint(); renderCards();
    if (registerAuto) applyAutoRegister();
    syncRegisterUI();
    if (items.length) setStatus("ok", `${items.length} ${plural(items.length)} с прошлого раза`); else restStatus();
    if (!(window.CSS && CSS.highlights)) toast("Браузер без CSS Highlight API: правки сохраняются, подсветки не будет");
  })();
})();
