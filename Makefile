PREFIX ?= $(HOME)/.local
BIN := $(PREFIX)/bin

.PHONY: install uninstall test

install:
	@mkdir -p "$(BIN)"
	@ln -sfn "$(CURDIR)/bin/mnrh" "$(BIN)/mnrh"
	@echo "mnrh -> $(BIN)/mnrh (symlink на проект, правки работают сразу)"

uninstall:
	@rm -f "$(BIN)/mnrh"
	@echo "mnrh удалён из $(BIN)"

test:
	@bash -n bin/mnrh
	@for f in libexec/mnrh/*.sh; do bash -n "$$f" || exit 1; done
	@for f in libexec/mnrh/*.py libexec/mnrh/lib/*.py; do /usr/bin/python3 -m py_compile "$$f" || exit 1; done
	@./bin/mnrh help >/dev/null
	@./bin/mnrh help -a >/dev/null
	@MNRH_NO_MENU=1 ./bin/mnrh >/dev/null
	@./bin/mnrh path clean -n >/dev/null
	@for c in $$(ls libexec/mnrh | sed 's/\..*//' | grep -v '^lib$$'); do grep -q "	$$c	" share/mnrh/commands.tsv || { echo "нет в commands.tsv: $$c"; exit 1; }; done
	@awk -F'\t' '!/^#/ && $$1 != "-" && $$4 ~ /;;/ { n = split($$4, a, ";;"); for (i = 1; i <= n; i++) if (a[i] ~ /^\|/) { print "в commands.tsv у " $$2 " действие без имени"; bad = 1 } } END { exit bad }' share/mnrh/commands.tsv
	@/usr/bin/python3 libexec/mnrh/lib/launcher.py actions share/mnrh/commands.tsv outdated </dev/null; test $$? -eq 3
	@/usr/bin/python3 libexec/mnrh/lib/launcher.py actions share/mnrh/commands.tsv ports </dev/null; test $$? -eq 3
	@./bin/mnrh help | grep -q 'mnrh <команда>       без аргументов'
	@./bin/mnrh --version >/dev/null
	@./bin/mnrh ram -n 1 >/dev/null
	@./bin/mnrh ram status -n 1 >/dev/null
	@./bin/mnrh mem status -n 1 >/dev/null
	@./bin/mnrh killdaemons -l >/dev/null
	@./bin/mnrh killdaemons stop -h | grep -q 'killdaemons stop'
	@./bin/mnrh kd -h >/dev/null
	@./bin/mnrh ram -h >/dev/null
	@./bin/mnrh ram clean -h >/dev/null
	@./bin/mnrh fix -h >/dev/null
	@./bin/mnrh sleep -h >/dev/null
	@./bin/mnrh ssh -h >/dev/null
	@./bin/mnrh ports -h >/dev/null
	@./bin/mnrh path -h >/dev/null
	@! ./bin/mnrh path nothing >/dev/null
	@./bin/mnrh uninstall -h >/dev/null
	@./bin/mnrh login -h >/dev/null
	@./bin/mnrh outdated -h >/dev/null
	@./bin/mnrh login >/dev/null
	@./bin/mnrh login status >/dev/null
	@./bin/mnrh path status >/dev/null
	@./bin/mnrh uninstall --orphans -n >/dev/null
	@! ./bin/mnrh uninstall no-such-app-mnrh -n >/dev/null 2>&1
	@./bin/mnrh ports >/dev/null
	@./bin/mnrh ports kill 1 >/dev/null
	@! ./bin/mnrh ssh nothing >/dev/null
	@./bin/mnrh sleep >/dev/null
	@./bin/mnrh sleep status >/dev/null
	@./bin/mnrh nomusic status >/dev/null
	@./bin/mnrh scroll -h >/dev/null
	@./bin/mnrh scroll >/dev/null
	@./bin/mnrh scroll status >/dev/null
	@! ./bin/mnrh scroll nothing >/dev/null
	@swiftc -typecheck -swift-version 5 share/mnrh/scroll/main.swift
	@./bin/mnrh tossling -h | grep -q Tossling
	@./bin/mnrh tossy -h | grep -q Tossling
	@./bin/mnrh clip -h | grep -q Tossling
	@PATH=/usr/bin:/bin HOME=/nonexistent ./bin/mnrh tossling -h | grep -q 'отдельная программа'
	@./bin/mnrh alert -h | grep -q "alert run"
	@./bin/mnrh alert status </dev/null >/dev/null || true
	@LC_ALL=ru_RU.UTF-8 ./bin/mnrh ram </dev/null >/dev/null
	@LC_ALL=ru_RU.UTF-8 ./bin/mnrh doctor </dev/null >/dev/null
	@./bin/mnrh fix scroll -h >/dev/null
	@! ./bin/mnrh fix nothing >/dev/null
	@./bin/mnrh killdaemons -h >/dev/null
	@./bin/mnrh doctor -h >/dev/null
	@./bin/mnrh disk -h >/dev/null
	@./bin/mnrh repos -h >/dev/null
	@./bin/mnrh sim -h >/dev/null
	@./bin/mnrh init -h >/dev/null
	@./bin/mnrh init --show >/dev/null
	@./bin/mnrh init </dev/null >/dev/null 2>&1; test $$? -eq 2
	@./bin/mnrh init setup </dev/null >/dev/null 2>&1; test $$? -eq 2
	@./bin/mnrh gradle -h >/dev/null
	@./bin/mnrh gradle update </dev/null >/dev/null 2>&1; rc=$$?; test $$rc -eq 2 -o $$rc -eq 1 -o $$rc -eq 0
	@test -f share/mnrh/icon.png
	@./bin/mnrh gradle >/dev/null
	@./bin/mnrh gradle status >/dev/null
	@./bin/mnrh sim >/dev/null
	@./bin/mnrh sim status >/dev/null
	@./bin/mnrh repos >/dev/null
	@./bin/mnrh repos status >/dev/null
	@./bin/mnrh disk >/dev/null
	@./bin/mnrh disk status >/dev/null
	@./bin/mnrh disk clean </dev/null >/dev/null; rc=$$?; test $$rc -eq 2 -o $$rc -eq 0
	@./bin/mnrh doctor >/dev/null; test $$? -le 1
	@./bin/mnrh doctor status >/dev/null; test $$? -le 1
	@! ./bin/mnrh no-such-command >/dev/null 2>&1
	@bash -n libexec/mnrh/lib/claudemeister.sh
	@zsh -n share/mnrh/restart.zsh
	@for c in claude todo shot deps; do MNRH_CLAUDEMEISTER=/nonexistent ./bin/mnrh $$c -h 2>&1 >/dev/null | grep -q 'brew install kopylovis/tap/claudemeister' || { echo "mnrh $$c без claudemeister"; exit 1; }; done
	@MNRH_CLAUDEMEISTER=/bin/echo ./bin/mnrh todo add x | grep -qx 'todo add x'
	@MNRH_CLAUDEMEISTER=/bin/echo ./bin/mnrh claude setup --country KZ | grep -qx 'setup --country KZ'
	@find libexec -name __pycache__ -type d -exec rm -rf {} +
	@echo "все проверки пройдены"
