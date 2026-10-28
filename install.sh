#!/usr/bin/env bash
# Подключает revizor к Claude Code: ~/.claude/skills/revizor → skill/. Повторный запуск безопасен.
# --uninstall снимает ссылку. Снимает ссылку ~/.claude/skills/review от версии до переименования, если она вела в этот проект.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
SKILLS="$HOME/.claude/skills"
LINK="$SKILLS/revizor"
OLD="$SKILLS/review"

if [[ "${1:-}" == "--uninstall" ]]; then
  if [[ -L "$LINK" && "$(readlink "$LINK")" == "$REPO/skill" ]]; then
    rm "$LINK"
    echo "unlink $LINK"
  else
    echo "нет ссылки $LINK на этот клон – снимать нечего"
  fi
  cat <<MSG
Остались, удаляются руками, если не нужны:
  $REPO – сам клон
  ~/.cache/revizor – порты, реестр документов, кэш проверки обновлений
  ~/.config/revizor – настройки, если создавал
  *.review.json рядом с документами – история правок
  скилл стиля в $SKILLS, если его создал Ревизор (в нём revizor.json)
MSG
  exit 0
fi

if [[ -L "$OLD" && "$(readlink "$OLD")" == */review/skill ]]; then
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
