#!/usr/bin/env bash
set -uo pipefail

help() {
  cat <<'EOF'
mnrh killdaemons      остановить демоны сборки Gradle и Kotlin любых версий
mnrh killdaemons -l   только показать, ничего не трогать
mnrh killdaemons -f   снять и тех, кто прямо сейчас занят сборкой
EOF
}

free_gb() {
  vm_stat | awk -v ps="$(sysctl -n hw.pagesize 2>/dev/null || echo 16384)" '
    /Pages free/        {gsub(/\./,""); f=$3}
    /Pages speculative/ {gsub(/\./,""); s=$3}
    END {printf "%.2f", (f+s)*ps/1073741824}'
}

swap_mb() { sysctl -n vm.swapusage | sed -n 's/.*used = \([0-9.]*\)M.*/\1/p'; }

main() {
  local list_only=0 force=0 busy_cpu=5.0
  local pattern='GradleDaemon|KotlinCompileDaemon|kotlin-daemon|GradleWrapperMain|GradleWorkerMain'
  while [ $# -gt 0 ]; do
    case "$1" in
      -l|--list)  list_only=1 ;;
      -f|--force) force=1 ;;
      -h|--help)  help; return 0 ;;
      *) echo "mnrh killdaemons: неизвестный аргумент $1" >&2; return 2 ;;
    esac
    shift
  done

  local rows=() line
  while IFS= read -r line; do
    [ -n "$line" ] && rows+=("$line")
  done < <(ps -Ao pid=,pcpu=,rss=,etime=,command= | grep -E "$pattern" | grep -v grep)

  if [ "${#rows[@]}" -eq 0 ]; then
    echo "Демонов сборки не запущено. Свободно $(free_gb) ГБ."
    return 0
  fi

  local topout
  topout=$(top -l 1 -stats pid,mem 2>/dev/null | awk 'f{print $1, $2} /^PID/{f=1}')

  printf '%-7s %-25s %-17s %16s %7s\n' PID ТИП ВОЗРАСТ ПАМЯТЬ CPU
  printf -- '-----------------------------------------------------------------\n'

  local pids=() busy=() alive=() row pid cpu rss age type ver mark is_busy memraw memmb memstr total_mb=0
  for row in "${rows[@]}"; do
    set -- $row
    pid=$1; cpu=$2; rss=$3; age=$4
    case "$row" in
      *GradleDaemon*)
        ver=$(printf '%s\n' "$row" | grep -oE 'GradleDaemon [0-9][0-9.]*' | awk '{print $2}')
        type="Gradle ${ver:-?}" ;;
      *KotlinCompileDaemon*|*kotlin-daemon*) type="Kotlin compile" ;;
      *GradleWorkerMain*)                    type="Gradle worker" ;;
      *GradleWrapperMain*)                   type="Gradle wrapper" ;;
      *)                                     type="JVM" ;;
    esac
    is_busy=$(awk -v c="$cpu" -v t="$busy_cpu" 'BEGIN{print (c+0 > t) ? 1 : 0}')
    mark=""
    if [ "$is_busy" = "1" ] && [ "$force" -eq 0 ]; then
      mark="  <- занят, пропускаю"; busy+=("$pid")
    else
      pids+=("$pid")
    fi
    memraw=$(printf '%s\n' "$topout" | awk -v p="$pid" '$1==p{print $2; exit}')
    memmb=$(awk -v v="${memraw:-0M}" 'BEGIN{sub(/[+-]$/,"",v); u=substr(v,length(v),1); n=v+0;
      m=(u=="G")?n*1024:(u=="K")?n/1024:(u=="B")?n/1048576:(u=="T")?n*1048576:n; printf "%.1f", m}')
    memstr=$(awk -v m="$memmb" 'BEGIN{ if (m>=1024) printf "%.2f ГБ", m/1024; else printf "%.0f МБ", m }')
    [ -z "$mark" ] && total_mb=$(awk -v a="$total_mb" -v b="$memmb" 'BEGIN{printf "%.1f", a+b}')
    printf '%-7s %-22s %-10s %12s %6s%%%s\n' "$pid" "$type" "$age" "$memstr" "$cpu" "$mark"
  done

  awk -v m="$total_mb" 'BEGIN{ if (m>=1024) printf "\nИтого держат: %.2f ГБ\n", m/1024; else printf "\nИтого держат: %.0f МБ\n", m }'
  [ "$list_only" -eq 1 ] && return 0

  if [ "${#pids[@]}" -eq 0 ]; then
    printf '\nВсе демоны сейчас работают. Снять принудительно: mnrh killdaemons -f\n'
    return 0
  fi

  local before_free before_swap after_free after_swap msg
  before_free=$(free_gb); before_swap=$(swap_mb)
  echo
  kill "${pids[@]}" 2>/dev/null

  for _ in 1 2 3 4 5 6 7 8 9 10; do
    alive=()
    for pid in "${pids[@]}"; do kill -0 "$pid" 2>/dev/null && alive+=("$pid"); done
    [ "${#alive[@]}" -eq 0 ] && break
    sleep 1
  done

  if [ "${#alive[@]}" -gt 0 ]; then
    echo "Не отозвались на SIGTERM, добиваю: ${alive[*]}"
    kill -9 "${alive[@]}" 2>/dev/null
    sleep 2
  fi

  sleep 1
  after_free=$(free_gb); after_swap=$(swap_mb)
  msg="Остановлено демонов: ${#pids[@]}"
  [ "${#busy[@]}" -gt 0 ] && msg="$msg, пропущено занятых: ${#busy[@]}"
  echo "$msg"
  awk -v a="$before_free" -v b="$after_free" -v c="$before_swap" -v d="$after_swap" \
    'BEGIN{printf "Свободно: %.2f -> %.2f ГБ   |   swap: %.0f -> %.0f МБ\n", a, b, c, d}'
}

main "$@"
