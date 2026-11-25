# Changelog

English · [Русский](CHANGELOG.ru.md)

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions: [SemVer](https://semver.org/).

## [1.8.0] – 2026-11-25

### Added

- Pages on a local dev server open by address: `revizor.py open http://localhost:4321/pricing` from the project folder. Revizor proxies the dev server, adds its panel to every HTML page and passes scripts, images, the site's own `/api/` and the hot reload websocket through. Tested with Astro on Vite. A landing page built with Astro, Next or Vite can be reviewed on the page, and Claude applies the edits in the project's source files. Only `localhost`, `127.0.0.1` and `::1` open. The review file lives in Revizor's cache next to a small anchor with the address and the project folder, so nothing is added to the project. `wait`, `show`, `mark`, `suggest` and `check` accept the same address.

### Changed

- The panel calls Revizor's API under `/_revizor/api/`, so it does not collide with a site's own `/api/`. The server still answers the old `/api/` paths for files, and a tab opened before the update keeps working.
- After "Finish round" the tab reloads the same address with a new `v` parameter, keeping the page's own path and query.

## [1.7.0] – 2026-11-25

### Added

- 15 base rules for English text in `rules/en.json`: "not just X, but Y", "it's not X, it's Y", filler openers, announcing the next block, model vocabulary (delve, seamless, testament to), sales clichés, shallow -ing riders, an inanimate actor with intent, "serves as", a triad of comparatives, staccato slogans, superlatives without a number, forward-looking transitions, chatbot residue, borrowed authority. Before this release only the em dash and a few model phrases were caught in English. An English rule fires only on lines without Cyrillic.
- `rules/semantic-en.md`: the base catalog for the semantic pass on English documents, used when the style skill has no English catalog. A new style skill starts with both catalogs.

### Changed

- A finding is explained in the language of the text it was found in: an English phrase gets an English title and reason even when the panel is in Russian. The agent writes its own findings in the document language too.
- The semantic pass and `check` list only the rules that fire on the document's language, so the agent does not get Russian rule ids for an English text.

## [1.6.3] – 2026-11-22

### Fixed

- Selected text in the panel's fields no longer turns invisible. A report with its own selection style, such as white text on a blue highlight, passed that white text into the panel, and on the light panel it disappeared.

## [1.6.2] – 2026-11-22

### Changed

- Bundled libraries updated to their latest versions: Motion 11.11.17 → 14.14.0, mdast-util-from-markdown 2.0.2 → 2.1.0, mdast-util-gfm 3.0.0 → 3.1.0, esbuild for the bundles 0.25.10 → 0.28.2. `npm audit` finds no vulnerabilities in either bundle. Motion is now built from the four functions Revizor and its site use, with `tools/vendor-motion/build.sh`, so the update can be repeated.
- CI runs on Python 3.14 too, and on the current versions of the checkout and setup-python actions.

## [1.6.1] – 2026-11-22

### Changed

- If the skill can't find `revizor.py`, the agent now says why and gives both install commands, plugin and clone. This happens when the skill folder was installed on its own, for example with `npx skills add`, which copies the folder without `revizor.py`. The README warns about it in Install.

## [1.6.0] – 2026-11-21

For an agent that updated Revizor mid-chat: the line that calls `revizor.py` in `skill/SKILL.md` has changed. Reread that section before "Launch".

### Added

- Revizor installs as a Claude Code plugin: `/plugin marketplace add qttc/revizor`, then `/plugin install revizor@revizor`. The repo has `.claude-plugin/plugin.json` and `marketplace.json`, and their version matches `revizor.py`. Installing from a clone with `install.sh` works as before.
- A demo in the README: a GIF of a full round on `examples/launch-recap.html`. You can open the example yourself.

### Changed

- The skill finds `revizor.py` next to its own folder (`${CLAUDE_SKILL_DIR}/..`). If the variable is not substituted, it uses the symlink as before, and the plugin cache last. The line also works in zsh: there a glob with no match would abort the command, so the cache is searched with `find`. A plugin install has no symlink, so this is what makes it work.
- `update` on a plugin install names the plugin update commands instead of suggesting a reinstall through git clone. It works with git only if the repo root is the Revizor folder itself: before, a plugin cache inside someone else's repo (`~/.claude` in dotfiles) looked like a clone to it.

## [1.5.2] – 2026-11-21

### Fixed

- A selection did not open Edit and Comment when Chrome had a translator extension such as Reverso. It listens to `dblclick` and `mouseup` on `window`: it collapses the selection, wraps the word in its own element and closes the toolbar with its popup. A double click is the usual way to select a word or a number in a table cell, so tables broke most often. Now, while the document has a selection, Revizor stops these events at the document level and reads the selection right on `mouseup`. The page's own handlers run as before, and a click without a selection still reaches `window`.

## [1.5.1] – 2026-11-21

### Fixed

- `open` and `wait` exit on their own when the session that started them is closed: every 5 s they check whether the parent and grandparent are alive. Before, the server and the polling loop stayed as orphans: in a week the author collected 21 processes, some of them hanging for 4 days. `open` then sets `status: stopped`, as on Ctrl+C, and saved edits stay in the file.

## [1.5.0] – 2026-11-20

For an agent that updated Revizor mid-chat: the "Command completion notice" section in `skill/SKILL.md` was rewritten. Reread it.

### Changed

- The "Finish review" button is split in two. "Finish round": the agent applies the edits and opens the new version in the same tab. Under the arrow, "Finish the work": the last round, the agent applies the edits, runs learning, wraps up and does not open the document again. Each has an explanation in the menu and its own confirmation. Agents took the end of a round for the end of the work and closed the session after the first round.
- The first line of the `open` summary tells the agent which button the owner pressed and what to do next.

### Added

- A `final` field in the review file with `status: done`: `true` means "Finish the work". A repeated `open` removes it. Older readers of the format ignore the field, and the format version stays the same.

## [1.4.0] – 2026-11-14

### Added

- Five regexes in the base rules: generator clichés («в эпоху цифровизации», «раскрыть потенциал», «играет ключевую роль»), officialese («в целях», «в рамках», «осуществлять», «носит характер»), split predicates («провести анализ»), chains of verbal nouns («обеспечение функционирования») and open-ended lists («и т.д.», «и так далее»).
- The base catalog of semantic patterns gains a summary paragraph at the end, a question answered right away, a detail that wanders across blocks and a stock metaphor. Officialese gets four rewriting techniques, judgment gets intensifiers and «уникальный» without proof.
- `check` prints sentences that appear in the text twice or more.

The lists and techniques come from the `human-writing-ru` skill of the [hh-autopilot](https://github.com/masterofnuggets313/hh-autopilot) project (MIT) and were checked against Revizor's rules. Its ban on all dashes, on the letter «ё» and on hyphenated ranges was not carried over: it contradicts the en-dash rule. Its case check needs `pymorphy3`, and Revizor runs on the standard library only.

## [1.3.0] – 2026-11-14

For an agent that updated Revizor mid-chat: `skill/SKILL.md` has a new step in "Launch" and a new section, "Self-check before showing". Reread them.

### Added

- `revizor.py check FILE`: an AI slop self-check before the owner sees the document. With no server and no browser, the command saves the visible text of the document (from HTML, without CSS and JS), picks the pattern catalog for the language and prints what the regexes already catch. The agent goes through the text with the same semantic pass the page runs on open and fixes the source itself. Before, this pass ran only on open, and the owner got 6–7 findings in every fresh document.

## [1.2.0] – 2026-11-11

For an agent that updated Revizor mid-chat: step 4 of "Applying" in `skill/SKILL.md` has changed. Reread it.

### Changed

- `mark` does not close a comment or a rejected edit without a note. The note says what changed in the document or why the edit was rejected. `notes` teaches the documents playbook from these notes: in the first round after 1.1.0, none of the 19 comments on one document had a note, and for the rejected ones there was no way to tell whether the rejection was justified.

### Added

- `mark --notes '{"ID": "note"}'`: a separate note for each id, as a JSON string, a file or from stdin (`-`). `--note` still puts one note on all ids.

## [1.1.1] – 2026-11-11

### Changed

- Before applying comments, the agent reads the documents playbook of the style skill and checks the whole document against it. Before, it fixed only the marked spot, and the same mistake stayed in the neighboring sections until the next round.

## [1.1.0] – 2026-11-11

For an agent that updated Revizor mid-chat: `skill/SKILL.md` has a new section, "Learning the documents playbook", and a new playbook, `skill/learn-docs.md`. Reread them.

### Added

- The owner's comments now teach the agent how to build a document. `revizor.py notes` returns resolved comments (applied and rejected) by document: on a fragment, on a block and on the whole document, with the section and what the comment refers to. The agent sorts them by the `skill/learn-docs.md` playbook into the documents playbook of the style skill, `references/documents.md`: order, sections, audience, numbers and charts, fact checks before generation. `notes --ack` closes a batch, and the ledger is `revizor-notes.json` in the skill.
- After "Finish review" the agent runs playbook learning when there are 5 or more new comments, and always on the words «обучи документы» or «забери комментарии».
- The style skill template gets `references/documents.md` and the `documents` and `notes_ledger` roles in `revizor.json`.

## [1.0.2] – 2026-11-06

### Fixed

- An HTML report with a wide table in its own horizontal scroll opened zoomed out: the layer took the table as wider than the window and shrank the whole page. Now elements inside a block with `overflow-x: auto` or `scroll` do not count toward the scale, and decks with slides wider than the window shrink as before.

## [1.0.1] – 2026-10-29

### Fixed

- In the English interface the logo and caption in the panel header and on the collapsed button stayed Russian: «РЕВИЗОРЪ», «нейрослоп – стоп». Now they read "REVIZOR" and "AI slop – stop", and the Russian caption uses an en dash instead of an em dash. The English screenshot in the README and the link preview image were reshot.

## [1.0.0] – 2026-10-28

First stable release. What changes only in a major version from now on is described in the "Compatibility" section of `docs/technical.md`.

For an agent that updated Revizor mid-chat: `skill/SKILL.md` has English triggers, a rule to answer the owner in the owner's language and updating by tag. Reread it.

### English interface

- The panel, toolbar, hints and check rules are in English. The language follows the browser; pin it with `"ui_lang": "ru"` or `"en"` in `~/.config/revizor/config.json` or with `REVIZOR_UI_LANG`. Strings the agent reads stay in Russian.
- README in English (`README.md`) and in Russian (`README.ru.md`), an English screenshot and a link preview image, `docs/social-preview.png`.

### Security

- The server accepts requests only with Host `127.0.0.1` or `localhost` and its own port. POST only with `Content-Type: application/json`, its own Origin and `Sec-Fetch-Site: same-origin`. Before this, any site in a neighboring tab could wipe the edits with a plain POST, finish the review or slip text into the agent's task, and read the document through DNS rebinding.
- Hidden files and folders next to the document (`.git`, `.env`) are not served. Review files are closed regardless of case, together with temporary `.tmp` files.
- The request body is limited to 10 MB. A malformed request gets 400 or 413 with an error message, and the server keeps running.

### Changed

- Documents with the same name and different extensions in one folder (`report.md` and `report.html`) now get their own review files: `report.md.review.json` and `report.html.review.json`. A single document keeps the short name `report.review.json`. An old file with the short name stays with the document recorded in it.
- A broken review file is no longer overwritten. The editor and the commands say which file cannot be read, and `show`, `mark` and the rest exit with code 2.
- A document that is not UTF-8 does not open, and `open` says so before starting the server.
- Ports, the document registry, the text for the check, the update cache and the learning ledger are written atomically, through a temporary file with its own name: two `open` runs at once no longer get in each other's way.
- Linux: the editor opens in the default browser, and so does macOS without Chrome. On Windows `open`, `mark`, `wait` and `suggest` exit with a clear message instead of failing on import, and the other commands work.
- `update` installs the latest release tag `vX.Y.Z`. Before, it took the tip of the branch, where unreleased commits could land between releases.
- `wait`, `show`, `mark` and `suggest` on a missing document exit with a message instead of hanging or crashing.
- `pairs` no longer searches the whole disk for review files with Spotlight: only documents opened in Revizor and `--dir`. A one-off search for old reviews is `pairs --discover`.
- `pairs --count` only counts. Before, it overwrote the batch, and `--ack` could mark pairs the agent had not seen.

### Added

- The format schema `docs/review.schema.json` and the "Compatibility" section in `docs/technical.md`: what is stable under SemVer from 1.0.0 and what is internal. The `stopped` status is documented.
- `install.sh --uninstall`.
- CI on GitHub Actions: macOS and Linux, Python 3.9–3.13. `SECURITY.md`, `CONTRIBUTING.md`, issue templates.
- `lang` on a text document's page, set by the document's language.

## [0.9.0] – 2026-10-28

For an agent that updated Revizor mid-chat: in `skill/learn.md` the "Input" and "Voice → rule" sections have changed, and in `skill/SKILL.md` the check task got a "Language" line. Reread both files.

### Added

- The style skill and its files are chosen by the document language. The language is detected by the dominant alphabet: Cyrillic is ru, Latin is en. The default pattern catalog is `references/*tells-<language>*.md`; a shared `*tells*.md` without a language fits both.
- In the skill's `revizor.json` a role can point to a file per language: `"catalog": {"ru": "...", "en": "..."}`. `examples`, `voice` and the other roles are split the same way. The new skill template keeps the `ai-tells-ru.md` and `ai-tells-en.md` catalogs.
- `style_skill` in `~/.config/revizor/config.json` can point to a skill per language: `{"ru": "...", "en": "..."}`. If the language is not in the setting, auto-discovery runs, and it does not take skills of other languages.
- Check rules with `lang: "en"` fire only on lines without Cyrillic, and `ru` only on lines with it.
- The README has a prompt that lets Claude install Revizor by itself.

### Changed

- The semantic check of English text uses the skill's English catalog. The task for the agent names the language. If there is no catalog in that language, the base Russian one is used with a note.
- `pairs` tags each pair with its language (`lang`) and returns `langs`: the skill, files and stats for each language in the batch. The top-level `skill` and `files` are for the language with more pairs in the batch. `pairs --ack` writes each pair to the ledger of the skill for its language.

### Fixed

- `wait`, started together with a repeated `open` after "Finish review", read the old "finished" status and exited right away. Now it waits 10 seconds to see whether the review is reopened.

## [0.8.2] – 2026-10-26

### Fixed

- A deck slide wider than the window was cut off on both sides if the deck centers its slides. Revizor measured the width through `scrollWidth`, which does not include overflow to the left. Now the width is computed from the bounds of the visible wide elements, and the whole slide fits to the left of the panel.

## [0.8.1] – 2026-10-23

For an agent that updated Revizor mid-chat: the "Command completion notice" section in `skill/SKILL.md` has changed. Reread it.

### Changed

- After "Finish review" there is no need to start Revizor a second time. The agent applies the edits and opens the document again by itself. The repeated `open` takes the previous port, and the tab that was waiting on the "Sent" screen reloads into the new version. If the tab was closed, a new one opens after 4 seconds.

## [0.8.0] – 2026-10-23

For an agent that updated Revizor mid-chat: the `revizor` skill has changed a lot since 0.5.0 (the `wait` loop on open, the AI slop check, style skill learning). Reread `skill/SKILL.md` in full and work by it from now on.

### Added

- The style skill learns from edits. `revizor.py pairs` collects new "before → after" pairs, accepted findings and "not AI slop" marks from all documents opened in Revizor. The agent sorts them by the `skill/learn.md` playbook: confirmation of a pattern, an example (no more than three per pattern), a candidate for a new pattern (promoted after three pairs from two documents), voice. A word replacement that keeps repeating becomes a check rule, and a rule that is often rejected gets narrowed or switched off. Work names and numbers are anonymized.
- A learning ledger in the skill (`revizor-learned.json`): which pairs were processed and per-rule stats. `pairs --ack` closes a batch.
- The style skill is created from the `templates/style-skill/` template if there is none. `revizor.py style init` does the same by hand.
- Learning runs by itself after "Finish review" once 5 new signals have piled up, and on the words «обучи стиль».
- `open` keeps a document registry in `~/.cache/revizor/docs.json`, and old reviews are found through Spotlight.
- A style skill that has only a `SKILL.md` about style joins learning instead of a second one being created.
- `learn_from` in `~/.config/revizor/config.json` trains the skill from one folder only. For people whose work spaces are isolated while the style skill is shared.
- A learning batch has no more than 40 signals, and the ledger stores hashes instead of document paths.

## [0.7.0] – 2026-10-23

### Added

- A semantic AI slop check by the agent. On open the page hands over its text, the `wait` loop brings the agent a task, the agent reads the text against the style skill's pattern catalog and records findings with a ready rewrite through `revizor.py suggest`. The findings show up in the same "AI slop" group with "→ becomes" and the "Accept" and "Adjust" buttons.
- A base catalog of semantic patterns, `rules/semantic-ru.md`, for people without their own style skill.
- The group header shows that the agent is reading the text, and when it does not respond. If the text changed since the last check, there is a "check again" button.

### Changed

- If an edit rewrote almost everything, the card shows "before" and "after" in full, without a word diff.
- `wait` waits for any work from the editor: first the check, then Live Edit edits.

## [0.6.0] – 2026-10-23

### Added

- An AI slop check on open: phrases that give away a language model are underlined with a red wave, and the panel has an "AI slop · N spots" group. Findings in one sentence make one row. "Fix" corrects mechanics on the spot (a dash, a comma after «Однако»), "Rewrite" turns the sentence into an edit with the mechanics already fixed, "To agent" asks the agent to rewrite it. The cross means "not AI slop": the spot is no longer shown, and the mark is saved in the review file.
- A click on the wavy highlight in the document opens its row in the panel.
- The style skill is found automatically: Revizor looks through Claude Code skills and takes the one that looks like a style canon. Its `revizor-rules.json` adds its own rules to the base ones, replaces or switches off base rules. `revizor.py style` shows what was found.
- 12 base rules in `rules/ru.json`: «не про X», the truncated antithesis, «не просто X, а Y», «не только X, но и Y», fillers, a promise without a cost, an importance flag, «Однако,», a summary label, an evaluative heading, the em dash, the model's English phrasing.
- In the summary for the agent, an edit from the check has a "pattern" line.

## [0.5.0] – 2026-10-22

### Added

- Live Edit (beta): a switch in the panel header. Edits go to the agent one by one with the "Send to agent" button on a card, and the document on the page updates by itself: text and Markdown in place, HTML by a reload that keeps the scroll position.
- A state on each card in Live Edit: "Queued for the agent" with a "Withdraw" button, "Agent is applying…" with locked fields, then a "Agent applied the edit" toast or the reason for a refusal. The bottom of the panel shows how many edits the agent has and how many are queued. An edit queued for over a minute suggests typing «поправь».
- `revizor.py wait`: the agent's loop in Live Edit. It waits for sent edits, takes them and exits with a summary. The skill starts it next to `open` and restarts it after each edit.
- The review file gets the `live`, `sent` and `taken` fields and the `stopped` status for a server stopped without finishing.

### Changed

- "Done" is now called "Finish review". In Live Edit it is a secondary button, and the main action is on the card.
- The server, `mark` and `wait` write the review file under a file lock.

## [0.4.0] – 2026-10-22

### Changed

- The panel header collapses when the edit list is scrolled: the «РЕВИЗОРЪ» line, the file name and the format stay, and the cards get more room. Scrolling back to the top of the list or clicking the title expands the header, and the pencil strokes are not drawn again. A short list does not collapse the header.
- The "Whole document" button and card are called "General comment".
- "Done" in the editor is a command for the agent to apply the edits: the skill applies them right away, without a question in the chat. «Поправь» stays for a tab closed without "Done".
- `/revizor` without a path opens the document the chat is working on.
- The README is rewritten: shorter, with a screenshot, formats with examples, the "Done" step.

### Fixed

- After "Edit" and "Comment" the cursor lands in the card's field, at the end of the text for an edit. Before, focus sometimes got lost: the first anchoring of a new edit to the text redrew the list, and `⌘A` then selected the whole page.
- Redrawing the list does not knock you out of the field you are typing in: focus and cursor return to their place.

## [0.3.2] – 2026-10-22

### Changed

- The skill checks the version before opening the editor: `revizor.py update --check` from a daily cache. If a new one is out, the agent first offers to update and only then opens the document. Before, the line about a new version stayed in the background process output, and the agent could miss it.
- `update --check` takes the answer from the cache, and `--check --force` asks the repo right away.

## [0.3.1] – 2026-10-22

### Added

- A new version check: once a day on `open`, Revizor looks at the public repo's tags through `git ls-remote`, and the result is cached in `~/.cache/revizor/`. If a newer version is found, there is a line in the output for the agent and a quiet line at the bottom of the panel. Without a network the check is skipped silently; `REVIZOR_NO_UPDATE_CHECK=1` turns it off.
- `revizor.py update` and the «обнови ревизор» skill trigger: `git pull --ff-only`, `install.sh` and the CHANGELOG sections between the old and new version. With local changes in the Revizor folder the update stops and names the files. `update --check` only checks.
- A test that the version in the code matches the latest CHANGELOG section.

### Changed

- All marks in the document are yellow, the same in Markdown as in HTML. An edit is a yellow background with strikethrough, a comment a lighter background with a dotted underline. Blue for comments was almost invisible on a dark background.
- The skill finds Revizor through its own symlink, so the clone can live at any path.
- README and the technical description for the public repo: no promises that depend on the author's personal setup, examples without data from work documents.

## [0.3.0] – 2026-10-22

Markdown became a full format.

### Added

- Two Markdown views, "Formatted" and "Text", with a switch at the top; the choice is stored in the review file (`view`). Edits and highlights are shared between both views.
- The formatted view runs on the mdast parser with GFM: headings, nested lists, tasks, tables with alignment, code with a language, quotes, links, images from the document folder, footnotes. Raw HTML is shown as text. The parser lives in `web/vendor` and is built by `tools/vendor-md/build.sh`.
- A selection in the formatted view maps to the source exactly, including through bold, links, escapes and entities.
- "Pick a block" in Markdown: a paragraph, an item, a table, code, or a whole section by clicking its heading. The review file stores source lines (`lines`).
- An edit's section in Markdown is the heading path: "Numbers › Escaping".
- The summary for the agent has a line number for edits in text and Markdown.
- The edit card: a word diff "before → after", a Markdown preview under the field, `⌘B` and `⌘I`.
- Links in the formatted view open on ⌘-click and do not navigate away during a review.

### Changed

- The header is a 19th-century poster after a proofreader went over it: «РЕВИЗОРЪ» between double rules, the «Ъ» struck out in red pencil, below it a struck-out «нейрослоп» (AI slop) and a margin note «стоп» (stop) after a dash. When the panel opens, the strokes are drawn; with reduced motion they appear in place at once. The collapsed panel pill and the "Sent" screen use the same poster.
- Playfair Display: variable files 400–900, roman and italic.
- Format: an "Auto" option instead of "Detect", the closed list shows "Auto · Report", and the hint "The format picks the style rules for edits" is a heading inside the list.
- The save status stays quiet at rest: it shows the number of edits, and after a write a short "Saved" with a flash of the indicator.
- `*.review.json` is in the repo's `.gitignore`.

## [0.2.0] – 2026-10-22

The Revizor panel was rebuilt for daily work.

### Added

- A glass panel: background blur and saturation, an edge with a highlight, a soft highlight along the edge following the cursor. The glass tone follows the page brightness.
- The "Revizor" title in bold italic Playfair Display, with the caption "Stop AI slop". The font and Motion 11.11.17 live in the repo with their licenses, so no network is needed.
- Motion: the quote flies from the selection into the new card, the panel slides in and collapses into a pill, the toolbar, toasts and confirmation appear briefly. With `prefers-reduced-motion` only opacity and color remain.
- The text format is detected automatically (deck, report, spec, email, Slack, doc, client copy), and a manual choice wins. The review file has a new `register_auto` field.
- Undo for deleting a card with a "Restore" button for 5 seconds.
- The toolbar also opens after a selection made by finger or keyboard.
- Narrow screen: the panel docks at the bottom on half the screen and sticks to the visible area, even if the page is wider than the phone.
- A save status with separate "server not responding" and "server error N" errors and a "Retry" button.

### Changed

- The "General" and "To block" buttons became a "Comment" group: "Whole document" and "Pick a block". In text without slides and charts only "Whole document" remains.
- The format moved to the line with the file name, and all labels start with a capital.
- A selection extends to word boundaries.
- Icons are drawn in SVG, and the `×` and `⇥` glyphs are gone.
- The README is rewritten for people, and the technical description moved to `docs/technical.md`.

### Fixed

- The letters «у», «с», «e», «c» typed in a panel field created cards if a selection remained in the document.
- The edit counter counted cards without changes.
- The section on a card repeated the slide title.
- Contrast of captions and placeholders and the focus ring on light glass are raised to WCAG levels.

## [0.1.0] – 2026-10-21

First version.

### Added

- `revizor.py open`: a local server and a Chrome tab for `.md`, `.markdown`, `.txt`, `.html`, `.htm`. Text is wrapped in a page, HTML is served as is with the edit layer built in, and files next to the document load by relative paths.
- The edit layer: a fragment edit ("before → after" and "why"), a comment on a fragment, a comment on a block by click, a general comment, the text register.
- Slides in same-origin frames are edited like the page. The layer survives a bundle that replaces `<html>` and finds edits in slides built by a script after load.
- Wide content shrinks to the space to the left of the panel. The panel collapses.
- The review file `*.review.json` next to the document: autosave, a history of applied and rejected edits, finding the spot by quote and neighboring text after the document changes.
- `revizor.py show` and `mark`: a summary of edits for the agent and an "applied" or "rejected" mark with a reason. For a bundle the summary asks to edit the source.
- An open tab does not bring back edits the agent already closed and moves them to the history.
- "Done" works only by click and with a confirmation in the panel.
- The `revizor` skill for Claude Code and `install.sh` with a symlink in `~/.claude/skills`.
