#!/usr/bin/env bash
# Claude Code status line: directory, git branch, model · effort, context size.
#   ➜  my-repo  git:(main) ✗  Opus 5.5·medium  ctx 85k
# ctx = tokens in the context window, re-read on every turn. Yellow above 150k, red above 300k:
# a cue to /compact (same task) or /clear (new task).
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

tok=$(echo "$input" | jq -r '.context_window.total_input_tokens // 0')
if [ "$tok" -gt 0 ]; then
  if [ "$tok" -gt 300000 ]; then c=$RED; elif [ "$tok" -gt 150000 ]; then c=$YELLOW; else c=$GREEN; fi
  printf "  ${c}ctx %sk${RESET}" "$((tok / 1000))"
fi
