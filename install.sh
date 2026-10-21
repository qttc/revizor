#!/usr/bin/env bash
# Подключает revizor к Claude Code: ~/.claude/skills/revizor → skill/. Повторный запуск безопасен.
# Снимает ссылку ~/.claude/skills/review от версии до переименования, если она вела в этот проект.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
SKILLS="$HOME/.claude/skills"
LINK="$SKILLS/revizor"
OLD="$SKILLS/review"

if [[ -L "$OLD" && "$(readlink "$OLD")" == */Personal/review/skill ]]; then
  rm "$OLD"
  echo "unlink $OLD (старое имя)"
fi

if [[ -L "$LINK" && "$(readlink "$LINK")" == "$REPO/skill" ]]; then
  echo "ок   $LINK"
  exit 0
fi
if [[ -e "$LINK" || -L "$LINK" ]]; then
  echo "занято: $LINK – не трогаю, разберись руками" >&2
  exit 1
fi
mkdir -p "$SKILLS"
ln -s "$REPO/skill" "$LINK"
echo "link $LINK → $REPO/skill"
