#!/usr/bin/env bash
# mnrh todo <...> = agentmeister todo <...>
source "$(dirname "${BASH_SOURCE[0]}")/lib/agentmeister.sh"
cm_exec todo "$@"
