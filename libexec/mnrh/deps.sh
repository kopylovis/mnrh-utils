#!/usr/bin/env bash
# mnrh deps <...> = claudemeister deps <...>
source "$(dirname "${BASH_SOURCE[0]}")/lib/claudemeister.sh"
cm_exec deps "$@"
