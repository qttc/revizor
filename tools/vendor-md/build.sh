#!/usr/bin/env bash
# Пересборка парсера Markdown для web/vendor/mdast-gfm.js. Нужны node и npm, в рантайм Ревизора не входит.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
cp "$HERE/package.json" "$HERE/entry.js" "$WORK/"
cd "$WORK"
npm install --silent --no-audit --no-fund
npx esbuild entry.js --bundle --format=iife --global-name=__revizorMd --minify --legal-comments=none --target=chrome110 \
  --outfile="$HERE/../../web/vendor/mdast-gfm.js"
echo "готово: web/vendor/mdast-gfm.js. Версии в package.json, лицензии – web/vendor/LICENSE-mdast.md"
