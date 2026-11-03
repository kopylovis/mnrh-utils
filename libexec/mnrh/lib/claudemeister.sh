# mnrh claude, todo, shot и deps теперь в отдельной программе claudemeister: эти команды передают ей все аргументы.
# MNRH_CLAUDEMEISTER=<путь> задает программу явно.
cm_exec() {
  local cm="${MNRH_CLAUDEMEISTER-}" p
  if [ -z "${MNRH_CLAUDEMEISTER+x}" ]; then
    cm=$(command -v claudemeister)
    for p in /opt/homebrew/bin/claudemeister /usr/local/bin/claudemeister "$HOME/.local/bin/claudemeister"; do
      [ -n "$cm" ] && break
      [ -x "$p" ] && cm="$p"
    done
  fi
  if [ -z "$cm" ] || [ ! -x "$cm" ]; then
    echo "mnrh: эта команда теперь в claudemeister, а его нет. Поставить: brew install kopylovis/tap/claudemeister" >&2
    exit 127
  fi
  exec "$cm" "$@"
}
