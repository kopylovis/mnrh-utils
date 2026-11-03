#!/usr/bin/env bash
# mnrh todo <...> = claudemeister todo <...>
source "$(dirname "${BASH_SOURCE[0]}")/lib/claudemeister.sh"
cm_exec todo "$@"
