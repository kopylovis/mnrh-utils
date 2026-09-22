export MNRH_RESTART_HOOK=1

_mnrh_restart() {
  local f=$HOME/.cache/mnrh/restart/${TTY:t}
  [[ -e $f ]] || return 0
  local -a fresh=( $f(Nms-120) )
  local cmd=$(<$f)
  command rm -f -- $f
  (( $#fresh )) || return 0
  print -s -- $cmd
  eval $cmd
}

autoload -Uz add-zsh-hook
add-zsh-hook precmd _mnrh_restart
