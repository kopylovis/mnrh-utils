# mnrh claudemeister, todo, shot и deps теперь в отдельной программе agentmeister: эти команды передают ей все аргументы.
# MNRH_CLAUDEMEISTER=<путь> задает программу явно.
cm_exec() {
  local cm="${MNRH_CLAUDEMEISTER-}" p
  if [ -z "${MNRH_CLAUDEMEISTER+x}" ]; then
    cm=$(command -v agentmeister)
    for p in /opt/homebrew/bin/agentmeister /usr/local/bin/agentmeister "$HOME/.local/bin/agentmeister"; do
      [ -n "$cm" ] && break
      [ -x "$p" ] && cm="$p"
    done
  fi
  if [ -z "$cm" ] || [ ! -x "$cm" ]; then
    echo "mnrh: эта команда теперь в agentmeister, а его нет. Поставить: brew install kopylovis/tap/agentmeister" >&2
    exit 127
  fi
  exec "$cm" "$@"
}
