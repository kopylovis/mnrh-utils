#!/usr/bin/env bash
# mnrh agentmeister <...> = agentmeister <...>; старое имя mnrh claude ведет сюда же
source "$(dirname "${BASH_SOURCE[0]}")/lib/agentmeister.sh"
cm_exec "$@"
