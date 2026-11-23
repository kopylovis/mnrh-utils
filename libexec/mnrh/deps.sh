#!/usr/bin/env bash
# mnrh deps <...> = agentmeister deps <...>
source "$(dirname "${BASH_SOURCE[0]}")/lib/agentmeister.sh"
cm_exec deps "$@"
