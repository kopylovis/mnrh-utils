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
	@./bin/mnrh --version >/dev/null
	@./bin/mnrh ram -n 1 >/dev/null
	@./bin/mnrh killdaemons -l >/dev/null
	@./bin/mnrh ram -h >/dev/null
	@./bin/mnrh killdaemons -h >/dev/null
	@./bin/mnrh doctor -h >/dev/null
	@./bin/mnrh disk -h >/dev/null
	@./bin/mnrh repos -h >/dev/null
	@./bin/mnrh sim -h >/dev/null
	@./bin/mnrh claude -h >/dev/null
	@./bin/mnrh init -h >/dev/null
	@./bin/mnrh init --show >/dev/null
	@./bin/mnrh init </dev/null >/dev/null 2>&1; test $$? -eq 2
	@./bin/mnrh claude sessions -h >/dev/null
	@./bin/mnrh claude sessions -l >/dev/null
	@./bin/mnrh claude restart -h >/dev/null
	@./bin/mnrh claude forget -h >/dev/null
	@echo junk | ./bin/mnrh claude notice | wc -c | grep -q '^ *0$$'
	@echo '{"session_id":"00000000-0000-0000-0000-000000000000"}' | ./bin/mnrh claude notice | wc -c | grep -q '^ *0$$'
	@zsh -n share/mnrh/restart.zsh
	@printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | ./bin/mnrh claude mcp | grep '"restart"' | grep -q '"forget"'
	@./bin/mnrh gradle -h >/dev/null
	@./bin/mnrh gradle >/dev/null
	@./bin/mnrh claude </dev/null >/dev/null 2>&1; test $$? -eq 2
	@./bin/mnrh sim >/dev/null
	@./bin/mnrh repos >/dev/null
	@./bin/mnrh disk >/dev/null
	@./bin/mnrh disk --apply </dev/null >/dev/null; test $$? -eq 2
	@./bin/mnrh doctor >/dev/null; test $$? -le 1
	@! ./bin/mnrh no-such-command >/dev/null 2>&1
	@find libexec -name __pycache__ -type d -exec rm -rf {} +
	@echo "все проверки пройдены"
