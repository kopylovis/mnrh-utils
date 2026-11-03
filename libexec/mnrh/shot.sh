#!/usr/bin/env bash
# mnrh shot <...> = claudemeister shot <...>
source "$(dirname "${BASH_SOURCE[0]}")/lib/claudemeister.sh"
cm_exec shot "$@"
