#!/usr/bin/env bash
# mnrh shot <...> = agentmeister shot <...>
source "$(dirname "${BASH_SOURCE[0]}")/lib/agentmeister.sh"
cm_exec shot "$@"
