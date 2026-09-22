export MNRH_RESTART_HOOK=1

_mnrh_restart() {
  local f=$HOME/.cache/mnrh/restart/${TTY:t} cmd
  local -a fresh
  while [[ -e $f ]]; do
    fresh=( $f(Nms-120) )
    cmd=$(<$f)
    command rm -f -- $f
    (( $#fresh )) || return 0
    print -s -- $cmd
    eval $cmd
  done
}

autoload -Uz add-zsh-hook
add-zsh-hook precmd _mnrh_restart
