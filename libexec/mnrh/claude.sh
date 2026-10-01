#!/usr/bin/env bash
set -uo pipefail

CONF="${XDG_CONFIG_HOME:-$HOME/.config}/mnrh/config"
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"

projects_dir() {
  local d="${MNRH_PROJECTS:-}"
  [ -z "$d" ] && [ -f "$CONF" ] && d=$(sed -n 's/^projects=//p' "$CONF" | head -1)
  d="${d:-$HOME/Developer}"
  d="${d/#\~/$HOME}"
  echo "$d"
}
CLAUDE_BIN="${MNRH_CLAUDE_BIN:-claude}"
MENU="$LIB/menu.py"

help() {
  cat <<'EOF'
mnrh claude                 выбрать проект в папке проектов (mnrh init) и запустить в нём Claude Code
mnrh claude root            запустить в домашнем каталоге
mnrh claude <имя>           сразу в проекте (достаточно начала имени) или в папке по пути
mnrh claude ... -n          обычная сессия без вопроса
mnrh claude ... -k          caffeinate без вопроса
mnrh claude ... --new       новая сессия, не предлагать продолжить сохранённые
mnrh claude ... -- <аргументы claude>   например: mnrh claude kcalm -- --continue
mnrh claude sessions        сохранённые сессии: размер, продолжить, перенести в другую папку, удалить
                            (подробнее: mnrh claude sessions -h)
mnrh claude restart         изнутри Claude: закрыть и открыть эту же сессию заново, с claude update
mnrh claude forget          изнутри Claude: удалить эту сессию без следа и открыть чистую в той же папке
mnrh claude slim            изнутри Claude: сжать эту сессию (старые скриншоты, длинные выводы) и открыть снова
mnrh claude brief           где мы остановились: git, прошлая сессия, бэклог (/where)
mnrh claude parallel "<…>"  задача в отдельной ветке и worktree, во втором окне Claude (/parallel)
mnrh claude ask <пр> <…>    спросить отдельный Claude про другой проект, только чтение (/ask)
mnrh claude release-notes   материал для «Что нового» к релизу (/release-notes)
mnrh claude guard           защита от опасных команд и чтения секретов (on, off, skip <правило>, test)
mnrh claude notify          уведомления, когда Claude ждёт тебя (on, off, after <сек>, test)
mnrh claude setup           поставить /restart, MCP-сервер mnrh и хук zsh для перезапуска (--remove убрать)

caffeinate (по умолчанию) не даёт Mac уснуть, пока идёт сессия:
удобно для долгих задач и Remote Control с телефона. Экран при этом гаснуть может.
EOF
}

case "${1:-}" in
  sessions) shift; exec /usr/bin/python3 "$(dirname "$MENU")/claude_sessions.py" "$@" ;;
  brief) exec /usr/bin/python3 "$(dirname "$MENU")/claude_brief.py" "$@" ;;
  parallel) exec /usr/bin/python3 "$(dirname "$MENU")/claude_parallel.py" "$@" ;;
  ask) exec /usr/bin/python3 "$(dirname "$MENU")/claude_ask.py" "$@" ;;
  release-notes) exec /usr/bin/python3 "$(dirname "$MENU")/release_notes.py" "$@" ;;
  statusline) exec /usr/bin/python3 "$(dirname "$MENU")/claude_status.py" ;;
  guard|guard-hook) exec /usr/bin/python3 "$(dirname "$MENU")/claude_guard.py" "$@" ;;
  notify|notify-hook|focus) exec /usr/bin/python3 "$(dirname "$MENU")/claude_notify.py" "$@" ;;
  restart|forget|slim|mcp|setup|notice|session-end) exec /usr/bin/python3 "$(dirname "$MENU")/claude_restart.py" "$@" ;;
esac

target="" mode="" new=0 passthrough=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) help; exit 0 ;;
    -n|--normal) mode=normal ;;
    -k|--caffeinate) mode=caffeinate ;;
    --new) new=1 ;;
    --) shift; passthrough=("$@"); break ;;
    -*) echo "mnrh claude: неизвестный флаг $1" >&2; exit 2 ;;
    *) [ -z "$target" ] && target="$1" || { echo "mnrh claude: лишний аргумент $1" >&2; exit 2; } ;;
  esac
  shift
done

if [ ! -t 0 ] || [ ! -t 1 ]; then
  echo "mnrh claude: нужен терминал — Claude Code интерактивный." >&2
  exit 2
fi

DEV=$(projects_dir)
if [ ! -f "$CONF" ] && [ -z "${MNRH_PROJECTS:-}" ] && [ ! -d "$DEV" ]; then
  /usr/bin/python3 "$(dirname "$LIB")/init.py" --projects-only || exit 1
  DEV=$(projects_dir)
fi

dirs=()
for d in "$DEV"/*/; do
  [ -d "$d" ] && dirs+=("${d%/}")
done

match() {
  local q="$1" d found=()
  for d in ${dirs[@]+"${dirs[@]}"}; do
    case "$(basename "$d")" in "$q") echo "$d"; return 0 ;; esac
  done
  for d in ${dirs[@]+"${dirs[@]}"}; do
    case "$(basename "$d")" in "$q"*) found+=("$d") ;; esac
  done
  if [ "${#found[@]}" -eq 1 ]; then echo "${found[0]}"; return 0; fi
  if [ "${#found[@]}" -gt 1 ]; then
    echo "mnrh claude: «${q}» подходит к нескольким: $(for f in "${found[@]}"; do printf '%s ' "$(basename "$f")"; done)" >&2
  else
    echo "mnrh claude: проекта «${q}» нет в ${DEV/#$HOME/~}" >&2
  fi
  return 1
}

choose() {
  local d name branch last idx
  idx=$(
    {
      printf '~\t~ (домашний каталог)\n'
      for d in ${dirs[@]+"${dirs[@]}"}; do
        name=$(basename "$d")
        branch=$(git -C "$d" branch --show-current 2>/dev/null)
        last=$(git -C "$d" log -1 --format=%cr 2>/dev/null)
        printf '%s\t%-30s %-28s %s\n' "$name" "$name" "${branch:--}" "$last"
      done
    } | /usr/bin/python3 "$MENU" --title "Проекты в ${DEV/#$HOME/~}:" --label "Проект" --start 0 --default 1
  ) || return 1
  if [ "$idx" -eq 0 ]; then echo "$HOME"; else echo "${dirs[$((idx - 1))]}"; fi
}

if [ -z "$target" ]; then
  dir=$(choose) || exit 1
elif [ "$target" = "root" ] || [ "$target" = "~" ]; then
  dir="$HOME"
elif case "$target" in */*) true ;; *) false ;; esac && [ -d "$target" ]; then
  dir=$(cd "$target" && pwd)
else
  dir=$(match "$target") || exit 1
fi

for a in ${passthrough[@]+"${passthrough[@]}"}; do
  case "$a" in
    -c|--continue|-r|--resume|--resume=*|--session-id|--session-id=*|-p|--print|--teleport|--teleport=*|--from-pr|--from-pr=*) new=1 ;;
  esac
done

if [ "$new" -eq 0 ]; then
  sid=$(/usr/bin/python3 "$LIB/claude_sessions.py" choose "$dir") || exit 1
  [ -n "$sid" ] && [ "$sid" != new ] && passthrough=(--resume "$sid" ${passthrough[@]+"${passthrough[@]}"})
fi

if [ -z "$mode" ]; then
  idx=$(printf 'caffeinate\tcaffeinate — Mac не уснёт, пока идёт сессия\nобычная\tобычная\n' |
    /usr/bin/python3 "$MENU" --title "Режим сессии в ${dir/#$HOME/~}:" --label "Режим") || exit 1
  [ "$idx" = 1 ] && mode=normal || mode=caffeinate
fi

cd "$dir" || exit 1
echo "-> ${dir/#$HOME/~} · $mode"
if [ "$mode" = caffeinate ]; then
  exec caffeinate -is "$CLAUDE_BIN" "${passthrough[@]+"${passthrough[@]}"}"
else
  exec "$CLAUDE_BIN" "${passthrough[@]+"${passthrough[@]}"}"
fi
