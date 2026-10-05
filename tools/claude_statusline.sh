#!/usr/bin/env bash
# Claude Code status line: directory, git branch, model · effort, context used %.
#   ➜  my-repo  git:(main) ✗  Opus 5.5·medium  ctx 42%
# Context % turns yellow above 60% and red above 80%, a heads-up before auto-compact.
# Hide it (e.g. for presentations): touch ~/.claude/.statusline-off  — remove the file to restore.
# Requires jq.
input=$(cat)
[ -f ~/.claude/.statusline-off ] && exit 0

GREEN='\033[1;32m'
CYAN='\033[0;36m'
BLUE='\033[1;34m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
RESET='\033[0m'

cwd=$(echo "$input" | jq -r '.cwd // .workspace.current_dir // ""')
printf "${GREEN}➜${RESET}  ${CYAN}%s${RESET}" "$(basename "$cwd")"

branch=$(GIT_OPTIONAL_LOCKS=0 git -C "$cwd" symbolic-ref --short HEAD 2>/dev/null)
if [ -n "$branch" ]; then
  dirty=""
  GIT_OPTIONAL_LOCKS=0 git -C "$cwd" status --porcelain 2>/dev/null | grep -q . && dirty=" ${YELLOW}✗${RESET}"
  printf "  ${BLUE}git:(${RED}%s${BLUE})${RESET}%b" "$branch" "$dirty"
fi

model=$(echo "$input" | jq -r '.model.display_name // empty')
effort=$(echo "$input" | jq -r '.effort.level // empty')
[ -n "$model" ] && printf "  ${CYAN}%s%s${RESET}" "$model" "${effort:+·$effort}"

ctx=$(echo "$input" | jq -r '.context_window.used_percentage // empty')
if [ -n "$ctx" ]; then
  if [ "$ctx" -gt 80 ]; then c=$RED; elif [ "$ctx" -gt 60 ]; then c=$YELLOW; else c=$GREEN; fi
  printf "  ${c}ctx %s%%${RESET}" "$ctx"
fi
