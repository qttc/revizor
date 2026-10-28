# Contributing

Thanks for your interest. Revizor is small on purpose, and a few rules keep it that way.

## Ground rules

- Python standard library only (3.9+), one JavaScript file in `web/overlay.js`, no build step. New dependencies need a discussion first.
- The `*.review.json` format is a public contract, see [docs/review.schema.json](docs/review.schema.json). Renaming or removing a field means a new format version and a migration note in `CHANGELOG.md`.
- The overlay never changes the document DOM: highlights use the CSS Custom Highlight API, the panel lives in shadow DOM.
- Irreversible actions in the editor happen only on click with confirmation, never on a hotkey.
- Test data, fixtures and examples are made up. No real product names, companies, metrics or texts from work documents.

## Before a pull request

```
python3 -m unittest discover tests
```

Changes to `web/overlay.js` also need a manual check in Chrome on `tests/fixtures/markdown.md` (both views), a plain text file, an HTML report and a deck with slides in iframes, at 1440 and 390 px wide.

A change in behavior gets a `CHANGELOG.md` entry and the same version in `revizor.py` (`__version__`); a test checks they match. Commits follow [Conventional Commits](https://www.conventionalcommits.org/).

Agent-specific rules for working in this repo live in [CLAUDE.md](CLAUDE.md), the architecture in [docs/technical.md](docs/technical.md).
