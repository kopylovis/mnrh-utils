#!/usr/bin/env python3
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import HOME, tilde

CLAUDE = os.environ.get("MNRH_CLAUDE_HOME", os.path.join(HOME, ".claude"))
CLAUDE_BIN = os.environ.get("MNRH_CLAUDE_BIN", "claude")
LOG = os.path.join(HOME, "Library", "Logs", "mnrh-claude-restart.log")
MNRH = shutil.which("mnrh") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "bin", "mnrh")
COMMAND_FILE = os.path.join(CLAUDE, "commands", "restart.md")
DROP_WITH_VALUE = {"-r", "--resume", "--session-id", "--fork-session", "-p", "--print"}
DROP = {"-c", "--continue", "-r", "--resume"}

FIND_TAB = {
    "Apple_Terminal": '''
on run argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is (item 1 of argv) then
          if (count of argv) > 1 then do script (item 2 of argv) in t
          return "ok"
        end if
      end repeat
    end repeat
  end tell
  return "notfound"
end run''',
    "iTerm.app": '''
on run argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if tty of s is (item 1 of argv) then
            if (count of argv) > 1 then tell s to write text (item 2 of argv)
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "notfound"
end run''',
}


class RestartError(Exception):
    pass


def ps(field, pid):
    r = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True)
    return r.stdout.strip()


def session_file(pid):
    return os.path.join(CLAUDE, "sessions", f"{pid}.json")


def find_claude():
    pid = os.getpid()
    for _ in range(30):
        if os.path.exists(session_file(pid)):
            return pid
        ppid = ps("ppid", pid)
        if not ppid or int(ppid) <= 1:
            break
        pid = int(ppid)
    env_pid = os.environ.get("CLAUDE_PID")
    if env_pid and os.path.exists(session_file(env_pid)):
        return int(env_pid)
    raise RestartError("не нашёл процесс Claude Code: запускай изнутри сессии (/restart или MCP)")


def resume_flags(args):
    out, i = [], 0
    while i < len(args):
        a = args[i]
        name = a.split("=", 1)[0]
        nxt = args[i + 1] if i + 1 < len(args) else None
        takes_value = nxt is not None and not nxt.startswith("-") and "=" not in a
        if name in DROP_WITH_VALUE or name in DROP:
            i += 2 if (name in DROP_WITH_VALUE and takes_value) else 1
            continue
        if a.startswith("-"):
            out.append(a)
            if takes_value:
                out.append(nxt)
                i += 1
        i += 1
    return out


def plan(update=True):
    pid = find_claude()
    try:
        with open(session_file(pid)) as f:
            info = json.load(f)
    except (OSError, ValueError) as e:
        raise RestartError(f"не прочитал {tilde(session_file(pid))}: {e}")
    sid = info.get("sessionId") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        raise RestartError("не знаю id текущей сессии")
    cwd = info.get("cwd") or os.getcwd()
    tty = ps("tty", pid)
    if not tty or tty in ("??", "-"):
        raise RestartError("у Claude нет терминала, перезапускать некуда")
    tty = "/dev/" + tty if not tty.startswith("/dev/") else tty
    term = os.environ.get("TERM_PROGRAM", "")
    if term not in FIND_TAB:
        raise RestartError(f"умею возвращать Claude только в Terminal и iTerm2, а тут {term or 'неизвестный терминал'}")

    argv = shlex.split(ps("args", pid))
    flags = resume_flags(argv[1:])
    parent = ps("ppid", pid)
    caffeinate = bool(parent) and os.path.basename(ps("comm", int(parent))) == "caffeinate"

    run = (["caffeinate", "-is"] if caffeinate else []) + [CLAUDE_BIN, "--resume", sid] + flags
    line = (f"cd {shlex.quote(cwd)} && "
            + (f"{{ {shlex.quote(CLAUDE_BIN)} update; " if update else "{ ") + shlex.join(run) + "; }")
    return {"pid": pid, "sid": sid, "cwd": cwd, "tty": tty, "term": term, "line": line,
            "title": info.get("name") or sid}


def osascript(term, *args):
    r = subprocess.run(["osascript", "-e", FIND_TAB[term], *args], capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout.strip() or r.stderr.strip())


def preflight(p):
    code, out = osascript(p["term"], p["tty"])
    if code != 0:
        raise RestartError("macOS не дал управлять терминалом: разреши в Системные настройки → "
                           f"Конфиденциальность и безопасность → Автоматизация ({out})")
    if out != "ok":
        raise RestartError(f"не нашёл вкладку с {p['tty']}")


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def watcher(p, delay):
    time.sleep(delay)
    log(f"restart {p['sid']} pid {p['pid']} tty {p['tty']}")
    try:
        os.kill(p["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(150):
        try:
            os.kill(p["pid"], 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        log("SIGTERM не помог, SIGKILL")
        try:
            os.kill(p["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.5)
    time.sleep(0.8)
    code, out = osascript(p["term"], p["tty"], p["line"])
    log(f"osascript {code} {out}: {p['line']}")


def detach(p, delay):
    if os.fork():
        return
    os.setsid()
    if os.fork():
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        watcher(p, delay)
    except Exception as e:
        log(f"ошибка: {e!r}")
    os._exit(0)


def restart(update=True, delay=1.5, dry=False):
    p = plan(update)
    preflight(p)
    if dry:
        return p
    detach(p, delay)
    return p


def cli(args):
    if "-h" in args or "--help" in args:
        print("mnrh claude restart            изнутри Claude Code: закрыть его и открыть эту же сессию в той же вкладке")
        print("                               перед запуском выполняется claude update")
        print("mnrh claude restart --no-update   без обновления")
        print("mnrh claude restart --dry-run     только показать, что будет сделано")
        print()
        print("Обычно вызывается из Claude: /restart или инструментом restart из MCP-сервера mnrh")
        print("(установить: mnrh claude setup). Лог: " + tilde(LOG))
        return 0
    try:
        p = restart(update="--no-update" not in args, dry="--dry-run" in args)
    except RestartError as e:
        print(f"mnrh claude restart: {e}", file=sys.stderr)
        return 1
    if "--dry-run" in args:
        print(f"закрою pid {p['pid']}, в {p['term']} {p['tty']} выполню:\n  {p['line']}")
    else:
        print(f"Перезапускаю Claude Code через пару секунд, сессия «{p['title']}» откроется снова в этой вкладке.")
    return 0


TOOL = {
    "name": "restart",
    "description": ("Перезапустить этот Claude Code: процесс закроется и через пару секунд откроется снова "
                    "с этой же сессией в той же вкладке терминала. Перед запуском выполняется claude update, "
                    "так что это же способ поставить обновление. Вызывай только когда пользователь сам просит "
                    "перезапуститься или обновиться; после вызова ничего больше не делай, процесс завершится."),
    "inputSchema": {
        "type": "object",
        "properties": {"update": {"type": "boolean", "description": "выполнить claude update перед запуском",
                                  "default": True}},
    },
}


def mcp():
    def send(msg):
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid, method = req.get("id"), req.get("method")
        if rid is None:
            continue
        if method == "initialize":
            ver = (req.get("params") or {}).get("protocolVersion") or "2025-06-18"
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": ver,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mnrh", "version": "1.0.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [TOOL]}})
        elif method == "tools/call":
            params = req.get("params") or {}
            if params.get("name") != "restart":
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "нет такого инструмента"}})
                continue
            update = (params.get("arguments") or {}).get("update", True)
            try:
                p = restart(update=bool(update), delay=2.5)
                text, err = (f"Перезапуск запущен: через пару секунд Claude Code закроется"
                             f"{', обновится' if update else ''} и снова откроет сессию {p['sid']} в {tilde(p['cwd'])}."), False
            except RestartError as e:
                text, err = f"Не получилось: {e}", True
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}], "isError": err}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"не умею {method}"}})


SLASH = """---
description: Перезапустить Claude Code с этой же сессией (с claude update перед запуском)
allowed-tools: Bash(mnrh claude restart:*)
---
!`mnrh claude restart $ARGUMENTS`

Claude Code сейчас перезапустится сам. Ничего не делай и ответь одним словом: «Перезапускаюсь».
"""


def setup(args):
    remove = "--remove" in args
    if remove:
        if os.path.exists(COMMAND_FILE):
            os.remove(COMMAND_FILE)
        subprocess.run(["claude", "mcp", "remove", "--scope", "user", "mnrh"], capture_output=True)
        print("Убрал /restart и MCP-сервер mnrh.")
        return 0
    os.makedirs(os.path.dirname(COMMAND_FILE), exist_ok=True)
    with open(COMMAND_FILE, "w") as f:
        f.write(SLASH)
    print(f"✓ /restart → {tilde(COMMAND_FILE)}")
    listed = subprocess.run(["claude", "mcp", "get", "mnrh"], capture_output=True, text=True)
    if listed.returncode == 0:
        print("✓ MCP-сервер mnrh уже подключён")
    else:
        r = subprocess.run(["claude", "mcp", "add", "--scope", "user", "mnrh", "--", MNRH, "claude", "mcp"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"✗ не подключил MCP: {r.stderr.strip() or r.stdout.strip()}")
            return 1
        print(f"✓ MCP-сервер mnrh: {tilde(MNRH)} claude mcp (инструмент restart)")
    print("Подхватится в новых сессиях Claude Code; в уже открытых — после перезапуска.")
    return 0


if __name__ == "__main__":
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("restart", [])
    if cmd == "mcp":
        mcp()
    elif cmd == "setup":
        sys.exit(setup(rest))
    else:
        sys.exit(cli(rest))
