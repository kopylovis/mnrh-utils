#!/usr/bin/env python3
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import HOME, tilde
import claude_mac
import claude_open
import claude_search

CLAUDE = os.environ.get("MNRH_CLAUDE_HOME", os.path.join(HOME, ".claude"))
CLAUDE_BIN = os.environ.get("MNRH_CLAUDE_BIN", "claude")
LOG = os.path.join(HOME, "Library", "Logs", "mnrh-claude-restart.log")
MNRH = shutil.which("mnrh") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "bin", "mnrh")
COMMANDS = os.path.join(CLAUDE, "commands")
COMMAND_FILE = os.path.join(COMMANDS, "restart.md")
FORGET_FILE = os.path.join(COMMANDS, "forget.md")
OLD_FORGET_FILE = os.path.join(COMMANDS, "forget-session.md")
QUEUE = os.path.join(HOME, ".cache", "mnrh", "restart")
NOTICES = os.path.join(HOME, ".cache", "mnrh", "notice")
NOTICE_TTL = 300
SETTINGS = os.path.join(CLAUDE, "settings.json")
ZSHRC = os.path.join(os.environ.get("ZDOTDIR") or HOME, ".zshrc")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
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


def split_caffeinate(args):
    out, i = [], 0
    while i < len(args) and args[i].startswith("-"):
        out.append(args[i])
        if args[i] in ("-t", "-w") and i + 1 < len(args):
            out.append(args[i + 1])
            i += 1
        i += 1
    return out, args[i:]


def caffeinate_args(pid):
    candidates = []
    parent = ps("ppid", pid)
    if parent and os.path.basename(ps("comm", int(parent))) == "caffeinate":
        candidates.append(int(parent))
    r = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] == str(pid) and os.path.basename(parts[2]) == "caffeinate":
            candidates.append(int(parts[0]))
    for c in candidates:
        flags, utility = split_caffeinate(shlex.split(ps("args", c))[1:])
        if utility:
            return flags
    return None


def drop_name(flags):
    out, i = [], 0
    while i < len(flags):
        if flags[i] in ("-n", "--name"):
            i += 2
            continue
        if flags[i].startswith("--name="):
            i += 1
            continue
        out.append(flags[i])
        i += 1
    return out


SHELLS = {"zsh", "bash", "sh", "fish", "login"}


def launcher(pid):
    parent = ps("ppid", pid)
    while parent and int(parent) > 1:
        name = os.path.basename(ps("comm", int(parent))).lstrip("-")
        if name != "caffeinate":
            return name
        parent = ps("ppid", int(parent))
    return ""


def no_hook_reason(pid, term):
    head = f"в этом терминале ({term}) Claude возвращается во вкладку только через хук zsh, а его здесь нет: "
    parent = launcher(pid)
    if parent not in SHELLS:
        return (head + f"Claude запущен не из shell, а напрямую ({parent or 'неизвестно кем'}), например вкладкой "
                "Claude Code в IDE, и после выхода вкладке некуда вернуться. Открой обычную вкладку терминала "
                "и запусти claude там")
    if parent != "zsh":
        return head + f"shell здесь {parent}, а хук есть только для zsh"
    hooked = any("share/mnrh/restart.zsh" in l for l in zshrc_lines())
    if not hooked:
        return head + f"в {tilde(ZSHRC)} нет строки с хуком. Выполни mnrh claude setup и открой новую вкладку"
    return (head + f"в {tilde(ZSHRC)} он есть, но эта вкладка открыта раньше. Открой новую вкладку терминала "
            "и запусти claude там")


def plan(update=True, forget=False):
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
    if os.environ.get("MNRH_RESTART_HOOK") == "1":
        via = "zsh"
    elif term in FIND_TAB:
        via = term
    else:
        raise RestartError(no_hook_reason(pid, term or os.environ.get("TERMINAL_EMULATOR") or "неизвестный"))

    argv = shlex.split(ps("args", pid))
    flags = resume_flags(argv[1:])
    parent = ps("ppid", pid)
    caffeinate = caffeinate_args(pid)
    if forget:
        flags = drop_name(flags)

    new_sid = str(uuid.uuid4()) if forget else sid
    run = ((["caffeinate"] + caffeinate if caffeinate is not None else []) + [CLAUDE_BIN]
           + (["--session-id", new_sid] if forget else ["--resume", sid]) + flags)
    update = update and not forget
    line = (f"cd {shlex.quote(cwd)} && "
            + (f"{{ {shlex.quote(CLAUDE_BIN)} update; " if update else "{ ")
            + ("clear; printf '\\033[3J'; " if forget else "") + shlex.join(run) + "; }")
    return {"pid": pid, "sid": sid, "cwd": cwd, "tty": tty, "term": term, "via": via, "line": line, "forget": forget,
            "title": session_title(sid) or info.get("name") or sid, "new_sid": new_sid,
            "version": claude_version()}


def osascript(term, *args):
    r = subprocess.run(["osascript", "-e", FIND_TAB[term], *args], capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout.strip() or r.stderr.strip())


def preflight(p):
    if p["via"] == "zsh":
        os.makedirs(QUEUE, exist_ok=True)
        return
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
    log(f"restart {p['sid']} pid {p['pid']} tty {p['tty']} via {p['via']}")
    job = os.path.join(QUEUE, os.path.basename(p["tty"]))
    write_notice(p, "pending" if p["forget"] else "done")
    if p["via"] == "zsh":
        with open(job, "w") as f:
            f.write(p["line"])
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
    if p["forget"]:
        ok = forget_session(p["sid"])
        write_notice(p, "done" if ok else "failed")
    if p["via"] == "zsh":
        log(f"zsh: {p['line']}")
        return
    time.sleep(0.8)
    code, out = osascript(p["term"], p["tty"], p["line"])
    log(f"osascript {code} {out}: {p['line']}")


def claude_version(path=None):
    path = path or os.environ.get("CLAUDE_CODE_EXECPATH") or os.path.realpath(shutil.which("claude") or "")
    name = os.path.basename(path)
    return name if re.match(r"^\d+(\.\d+)+$", name) else ""


def session_title(sid):
    import claude_sessions
    for path in glob.glob(os.path.join(CLAUDE, "projects", "*", sid + ".jsonl")):
        meta = claude_sessions.read_meta(path)
        return meta["custom"] or meta["ai"] or meta["prompt"]
    return ""


def write_notice(p, status):
    os.makedirs(NOTICES, exist_ok=True)
    data = {"kind": "forget" if p["forget"] else "restart", "status": status, "title": p["title"],
            "old_sid": p["sid"], "version": p["version"], "at": time.time()}
    tmp = os.path.join(NOTICES, p["new_sid"] + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(NOTICES, p["new_sid"] + ".json"))


def notice_text(n):
    title = n.get("title") or n.get("old_sid", "")
    if len(title) > 60:
        title = title[:59] + "…"
    now = claude_version()
    if n["kind"] == "restart":
        was = n.get("version")
        if was and now and was != now:
            ver = f"обновлён {was} → {now}"
        else:
            ver = f"версия {now or was}" + (", обновлений не было" if was and now else "")
        return f"✓ mnrh: Claude Code перезапущен ({ver}). Сессия «{title}» продолжается."
    if n.get("status") == "done":
        return (f"✓ mnrh: прошлая сессия «{title}» удалена — переписка, история запросов и file-history. "
                "Это новая чистая сессия.")
    if n.get("status") == "failed":
        return f"! mnrh: прошлую сессию «{title}» не удалось найти и удалить, проверь: mnrh claude sessions -l"
    return f"… mnrh: прошлая сессия «{title}» ещё удаляется, проверь позже: mnrh claude sessions -l"


def hook_payload():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def background(fn, *args):
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
        fn(*args)
    except Exception as e:
        log(f"ошибка: {e!r}")
    os._exit(0)


def drop_junk(path, why):
    import claude_sessions
    sid = os.path.basename(path)[:-6]
    if os.path.islink(path) or sid in claude_sessions.running_ids() or not claude_sessions.is_junk(path):
        return False
    for s in claude_sessions.load():
        if s["id"] == sid and not s["link"]:
            s["running"] = None
            claude_sessions.delete(s)
            log(f"пустая сессия {sid} удалена ({why})")
            return True
    return False


def warm_up(pdir, keep):
    sweep(pdir, keep)
    claude_search.refresh()


def sweep(pdir, keep):
    for name in os.listdir(pdir):
        path = os.path.join(pdir, name)
        if not name.endswith(".jsonl") or not UUID_RE.match(name[:-6]) or name[:-6] == keep:
            continue
        try:
            if time.time() - os.path.getmtime(path) < 60:
                continue
        except OSError:
            continue
        drop_junk(path, "уборка при старте")


def session_end():
    payload = hook_payload()
    sid, path = payload.get("session_id"), payload.get("transcript_path")
    if not sid or not UUID_RE.match(sid) or not path or not path.endswith(sid + ".jsonl"):
        return 0
    try:
        pid = find_claude()
    except RestartError:
        pid = None

    def later():
        if payload.get("reason") == "clear" or pid is None:
            time.sleep(2)
        else:
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            time.sleep(0.5)
        drop_junk(path, f"закрытие: {payload.get('reason') or '?'}")

    background(later)
    return 0


def notice():
    payload = hook_payload()
    sid = payload.get("session_id")
    if not sid or not UUID_RE.match(sid):
        return 0
    path = payload.get("transcript_path")
    if path and os.path.isdir(os.path.dirname(path)):
        background(warm_up, os.path.dirname(path), sid)
    path = os.path.join(NOTICES, sid + ".json")
    n = None
    for _ in range(30):
        try:
            with open(path) as f:
                n = json.load(f)
        except (OSError, ValueError):
            return 0
        if n.get("status") != "pending":
            break
        time.sleep(0.1)
    try:
        os.remove(path)
    except OSError:
        pass
    if time.time() - n.get("at", 0) > NOTICE_TTL:
        return 0
    print(json.dumps({"systemMessage": notice_text(n)}, ensure_ascii=False))
    return 0


def forget_session(sid):
    import claude_sessions
    for s in claude_sessions.load():
        if s["id"] == sid:
            s["running"] = None
            claude_sessions.delete(s)
            log(f"forget: удалил {sid}")
            return True
    log(f"forget: сессии {sid} не нашёл")
    return False


def detach(p, delay):
    background(watcher, p, delay)


def restart(update=True, delay=1.5, dry=False, forget=False):
    p = plan(update, forget)
    preflight(p)
    if dry:
        return p
    detach(p, delay)
    return p


def cli(args, forget=False):
    name = "forget" if forget else "restart"
    if "-h" in args or "--help" in args:
        if forget:
            print("mnrh claude forget             изнутри Claude Code: закрыть его, удалить эту сессию без следа")
            print("                               (переписка, история запросов, file-history) и открыть чистый Claude")
            print("                               в той же папке и вкладке — как /clear, только старое не сохраняется")
            print("mnrh claude forget --dry-run   только показать, что будет сделано")
        else:
            print("mnrh claude restart            изнутри Claude Code: закрыть его и открыть эту же сессию в той же вкладке")
            print("                               перед запуском выполняется claude update")
            print("mnrh claude restart --no-update   без обновления")
            print("mnrh claude restart --dry-run     только показать, что будет сделано")
        print()
        print("Обычно вызывается из Claude: " + ("/forget или инструментом forget"
              if forget else "/restart или инструментом restart") + " из MCP-сервера mnrh")
        print("(установить: mnrh claude setup). Лог: " + tilde(LOG))
        return 0
    try:
        p = restart(update="--no-update" not in args, dry="--dry-run" in args, forget=forget)
    except RestartError as e:
        print(f"mnrh claude {name}: {e}", file=sys.stderr)
        return 1
    if "--dry-run" in args:
        extra = f"удалю сессию {p['sid']}, " if forget else ""
        print(f"закрою pid {p['pid']}, {extra}в {p['tty']} через {p['via']} выполню:\n  {p['line']}")
    elif forget:
        print(f"Через пару секунд Claude Code закроется, сессия «{p['title']}» будет удалена, и откроется чистая.")
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


FORGET_TOOL = {
    "name": "forget",
    "description": ("Забыть эту сессию: Claude Code закроется, текущая переписка удалится с диска без следа "
                    "(вместе с историей запросов), и в той же папке и вкладке откроется новый чистый Claude — "
                    "как /clear, только старое не сохраняется. Вызывай только когда пользователь сам просит забыть "
                    "или удалить эту сессию; после вызова ничего больше не делай."),
    "inputSchema": {"type": "object", "properties": {}},
}


def mnrh_version():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
    try:
        with open(os.path.join(root, "VERSION")) as f:
            return f.read().strip() or "0"
    except OSError:
        return "0"


def current_sid():
    try:
        with open(session_file(find_claude())) as f:
            return json.load(f).get("sessionId")
    except (RestartError, OSError, ValueError):
        return None


def call_restart(tool, a):
    update = a.get("update", True)
    try:
        if tool == "forget":
            p = restart(update=False, delay=2.5, forget=True)
            return (f"Готово: через пару секунд Claude Code закроется, сессия {p['sid']} будет удалена, "
                    f"и в {tilde(p['cwd'])} откроется новая."), False
        p = restart(update=bool(update), delay=2.5)
        return (f"Перезапуск запущен: через пару секунд Claude Code закроется"
                f"{', обновится' if update else ''} и снова откроет сессию {p['sid']} в {tilde(p['cwd'])}."), False
    except RestartError as e:
        return f"Не получилось: {e}", True


HANDLERS = {
    "restart": lambda a: call_restart("restart", a),
    "forget": lambda a: call_restart("forget", a),
    "open_claude": lambda a: claude_open.open_claude(a, MNRH),
    "session_search": lambda a: claude_search.handle("session_search", a, current_sid()),
    "session_read": lambda a: claude_search.handle("session_read", a),
    "mac_status": lambda a: claude_mac.handle("mac_status", a, MNRH),
    "free_memory": lambda a: claude_mac.handle("free_memory", a, MNRH),
}
READ_ONLY = [t["name"] for t in claude_search.TOOLS + claude_mac.TOOLS if t["annotations"].get("readOnlyHint")]


def tools():
    return [TOOL, FORGET_TOOL, claude_open.TOOL] + claude_search.TOOLS + claude_mac.TOOLS


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
                "serverInfo": {"name": "mnrh", "version": mnrh_version()}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": tools()}})
        elif method == "tools/call":
            params = req.get("params") or {}
            handler = HANDLERS.get(params.get("name"))
            if not handler:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "нет такого инструмента"}})
                continue
            args = params.get("arguments")
            try:
                text, err = handler(args if isinstance(args, dict) else {})
            except Exception as e:
                text, err = f"Не получилось: {e!r}", True
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}], "isError": err}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"не умею {method}"}})


FORGET_SLASH = """---
description: Забыть эту сессию без следа и открыть чистый Claude Code в той же папке
allowed-tools: Bash(mnrh claude forget:*)
---
!`mnrh claude forget $ARGUMENTS`

Эта сессия сейчас удалится, и Claude Code откроется заново. Ничего не делай и ответь одним словом: «Забываю».
"""


SLASH = """---
description: Перезапустить Claude Code с этой же сессией (с claude update перед запуском)
allowed-tools: Bash(mnrh claude restart:*)
---
!`mnrh claude restart $ARGUMENTS`

Claude Code сейчас перезапустится сам. Ничего не делай и ответь одним словом: «Перезапускаюсь».
"""


def hook_file():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
    m = re.match(r"(.*)/Cellar/mnrh/[^/]+/(.*)", root)
    if m:
        root = f"{m.group(1)}/opt/mnrh/{m.group(2)}"
    return os.path.join(root, "share", "mnrh", "restart.zsh")


def zshrc_lines():
    try:
        with open(ZSHRC) as f:
            return f.read().splitlines(keepends=True)
    except OSError:
        return []


def write_zshrc(lines):
    with open(ZSHRC, "w") as f:
        f.writelines(lines)


HOOKS = [("SessionStart", "claude notice", False), ("SessionEnd", "claude session-end", False),
         ("UserPromptSubmit", "claude notify-hook", True), ("Stop", "claude notify-hook", True),
         ("Notification", "claude notify-hook", True)]


def notice_hook(install):
    try:
        with open(SETTINGS) as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {}
    except ValueError as e:
        print(f"✗ {tilde(SETTINGS)} не читается как JSON ({e}), хуки Claude Code не трогаю")
        return
    hooks = settings.setdefault("hooks", {})
    for event, cmd, run_async in HOOKS:
        entries = [e for e in hooks.get(event, [])
                   if not any(cmd in h.get("command", "") for h in e.get("hooks", []))]
        if install:
            hook = {"type": "command", "command": f"{shlex.quote(MNRH)} {cmd}", "timeout": 10}
            if run_async:
                hook["async"] = True
            entries.append({"hooks": [hook]})
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks")
    allow_rules = [f"mcp__mnrh__{name}" for name in READ_ONLY]
    perms = settings.setdefault("permissions", {})
    allow = [r for r in perms.get("allow", []) if r not in allow_rules] + (allow_rules if install else [])
    if allow:
        perms["allow"] = allow
    else:
        perms.pop("allow", None)
    if not perms:
        settings.pop("permissions")
    tmp = SETTINGS + ".mnrh-tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS)
    if install:
        print(f"✓ сообщения после /restart и /forget, удаление пустых сессий → хуки SessionStart и SessionEnd "
              f"в {tilde(SETTINGS)}")
        print("✓ уведомления, когда Claude ждёт тебя → хуки UserPromptSubmit, Stop и Notification "
              "(mnrh claude notify -h)")
        print(f"✓ без вопроса о разрешении: {', '.join(READ_ONLY)} (они только читают)")


def setup(args):
    marker = "share/mnrh/restart.zsh"
    if "--remove" in args:
        for f in (COMMAND_FILE, FORGET_FILE, OLD_FORGET_FILE):
            if os.path.exists(f):
                os.remove(f)
        subprocess.run(["claude", "mcp", "remove", "--scope", "user", "mnrh"], capture_output=True)
        notice_hook(False)
        lines = zshrc_lines()
        kept = [l for l in lines if marker not in l]
        if kept != lines:
            write_zshrc(kept)
        print(f"Убрал /restart, /forget, MCP-сервер mnrh и хук из {tilde(ZSHRC)}.")
        return 0
    os.makedirs(os.path.dirname(COMMAND_FILE), exist_ok=True)
    if os.path.exists(OLD_FORGET_FILE):
        os.remove(OLD_FORGET_FILE)
    for path, text in ((COMMAND_FILE, SLASH), (FORGET_FILE, FORGET_SLASH)):
        with open(path, "w") as f:
            f.write(text)
        print(f"✓ /{os.path.basename(path)[:-3]} → {tilde(path)}")

    notice_hook(True)
    hook = hook_file()
    line = f'[ -f "{hook}" ] && source "{hook}"\n'
    lines = zshrc_lines()
    if line in lines:
        print(f"✓ хук перезапуска уже в {tilde(ZSHRC)}")
    else:
        lines = [l for l in lines if marker not in l]
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        write_zshrc(lines + [line])
        print(f"✓ хук перезапуска → {tilde(ZSHRC)} (работает в новых вкладках терминала)")

    listed = subprocess.run(["claude", "mcp", "get", "mnrh"], capture_output=True, text=True)
    if listed.returncode == 0:
        print("✓ MCP-сервер mnrh уже подключён")
    else:
        r = subprocess.run(["claude", "mcp", "add", "--scope", "user", "mnrh", "--", MNRH, "claude", "mcp"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"✗ не подключил MCP: {r.stderr.strip() or r.stdout.strip()}")
            return 1
        print(f"✓ MCP-сервер mnrh: {tilde(MNRH)} claude mcp (инструменты: {', '.join(t['name'] for t in tools())})")
    import claude_notify
    if claude_notify.ensure_app():
        print(f"✓ приложение уведомлений → {tilde(claude_notify.AGENT.app)}; проверить: mnrh claude notify test")
    else:
        print("✗ не собрал приложение уведомлений: нужен swiftc (xcode-select --install)")
    print("Подхватится в новых вкладках и сессиях Claude Code; в уже открытых — после перезапуска.")
    return 0


if __name__ == "__main__":
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("restart", [])
    if cmd == "mcp":
        mcp()
    elif cmd == "notice":
        sys.exit(notice())
    elif cmd == "session-end":
        sys.exit(session_end())
    elif cmd == "setup":
        sys.exit(setup(rest))
    else:
        sys.exit(cli(rest, forget=cmd == "forget"))
