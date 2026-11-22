#!/usr/bin/env bash
# Пересборка web/vendor/motion.js. Нужны node и npm, в рантайм Ревизора не входит.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
cp "$HERE/package.json" "$HERE/entry.js" "$WORK/"
cd "$WORK"
npm install --silent --no-audit --no-fund
npx esbuild entry.js --bundle --format=iife --minify --legal-comments=none --target=chrome110,safari16 \
  --outfile="$HERE/../../web/vendor/motion.js"
echo "готово: web/vendor/motion.js. Версия в package.json, лицензия – web/vendor/LICENSE-motion.md"
