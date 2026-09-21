#!/usr/bin/env bash
set -uo pipefail

DEV="$HOME/Developer"
CLAUDE_BIN="${MNRH_CLAUDE_BIN:-claude}"

help() {
  cat <<'EOF'
mnrh claude                 выбрать проект в ~/Developer и запустить в нём Claude Code
mnrh claude root            запустить в домашнем каталоге
mnrh claude <имя>           сразу в проекте (достаточно начала имени)
mnrh claude ... -n          обычная сессия без вопроса
mnrh claude ... -k          caffeinate без вопроса
mnrh claude ... -- <аргументы claude>   например: mnrh claude kcalm -- --continue

caffeinate (по умолчанию) не даёт Mac уснуть, пока идёт сессия:
удобно для долгих задач и Remote Control с телефона. Экран при этом гаснуть может.
EOF
}

target="" mode="" passthrough=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) help; exit 0 ;;
    -n|--normal) mode=normal ;;
    -k|--caffeinate) mode=caffeinate ;;
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

dirs=()
for d in "$DEV"/*/; do
  [ -d "$d" ] && dirs+=("${d%/}")
done

match() {
  local q="$1" d found=()
  for d in "${dirs[@]}"; do
    case "$(basename "$d")" in "$q") echo "$d"; return 0 ;; esac
  done
  for d in "${dirs[@]}"; do
    case "$(basename "$d")" in "$q"*) found+=("$d") ;; esac
  done
  if [ "${#found[@]}" -eq 1 ]; then echo "${found[0]}"; return 0; fi
  if [ "${#found[@]}" -gt 1 ]; then
    echo "mnrh claude: «${q}» подходит к нескольким: $(for f in "${found[@]}"; do printf '%s ' "$(basename "$f")"; done)" >&2
  else
    echo "mnrh claude: проекта «${q}» нет в $DEV" >&2
  fi
  return 1
}

choose() {
  local i=1 d name branch last ans
  echo "Проекты в ~/Developer:" >&2
  for d in "${dirs[@]}"; do
    name=$(basename "$d")
    branch=$(git -C "$d" branch --show-current 2>/dev/null)
    last=$(git -C "$d" log -1 --format=%cr 2>/dev/null)
    printf '  %2d) %-30s %-28s %s\n' "$i" "$name" "${branch:--}" "$last" >&2
    i=$((i + 1))
  done
  printf '   0) ~ (домашний каталог)\n\nНомер или начало имени: ' >&2
  read -r ans
  case "$ans" in
    0|root|"~") echo "$HOME" ;;
    ''|*[!0-9]*) [ -n "$ans" ] && match "$ans" || return 1 ;;
    *) [ "$ans" -ge 1 ] && [ "$ans" -le "${#dirs[@]}" ] && echo "${dirs[$((ans - 1))]}" || { echo "mnrh claude: нет пункта $ans" >&2; return 1; } ;;
  esac
}

if [ -z "$target" ]; then
  dir=$(choose) || exit 1
elif [ "$target" = "root" ] || [ "$target" = "~" ]; then
  dir="$HOME"
else
  dir=$(match "$target") || exit 1
fi

if [ -z "$mode" ]; then
  printf '\nРежим сессии в %s:\n  1) caffeinate — Mac не уснёт, пока идёт сессия (Enter)\n  2) обычная\nВыбор [1]: ' "${dir/#$HOME/~}"
  read -r ans
  case "$ans" in 2) mode=normal ;; *) mode=caffeinate ;; esac
fi

cd "$dir" || exit 1
echo "-> ${dir/#$HOME/~} · $mode"
if [ "$mode" = caffeinate ]; then
  exec caffeinate -is "$CLAUDE_BIN" "${passthrough[@]+"${passthrough[@]}"}"
else
  exec "$CLAUDE_BIN" "${passthrough[@]+"${passthrough[@]}"}"
fi
