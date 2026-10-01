import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import HOME, read_config, settings_name, settings_path, tilde, write_config
from swiftagent import SwiftAgent

AGENT = SwiftAgent("notify", "mnrh Notify", "com.mnrh.notify")
STATE = os.path.join(HOME, ".cache", "mnrh", "notify")
DEFAULT_AFTER = 20
TERMINALS = {"com.apple.Terminal": "Terminal", "com.googlecode.iterm2": "iTerm2"}

FOCUS = {
    "Terminal": '''
on run argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is (item 1 of argv) then
          set selected of t to true
          set index of w to 1
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end tell
  return "notfound"
end run''',
    "iTerm2": '''
on run argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if tty of s is (item 1 of argv) then
            select w
            tell t to select
            tell s to select
            activate
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "notfound"
end run''',
}

SELECTED = {
    "Terminal": 'tell application "Terminal" to return tty of selected tab of front window',
    "iTerm2": 'tell application "iTerm2" to return tty of current session of current window',
}


def usage():
    print("mnrh claude notify              уведомления, когда Claude ждёт тебя: состояние")
    print("mnrh claude notify on | off     включить или выключить")
    print(f"mnrh claude notify after <сек>  о готовом ответе — только если ход шёл дольше (сейчас "
          f"{after()} с, по умолчанию {DEFAULT_AFTER})")
    print("mnrh claude notify sound on|off звук")
    print("mnrh claude notify test         показать пробное уведомление")
    print()
    print("Уведомление приходит, если Claude просит разрешение или задаёт вопрос, долго ждёт ответа")
    print("или закончил долгий ход, а ты смотришь не на его вкладку. Нажатие переключает на неё")
    print("(Terminal и iTerm2 — на саму вкладку, в IDE — на окно приложения).")
    print("Ставит mnrh claude setup: хуки UserPromptSubmit, Stop и Notification.")


def enabled():
    return read_config().get("notify", "on") != "off"


def after():
    try:
        return max(0, int(read_config().get("notify_after", DEFAULT_AFTER)))
    except ValueError:
        return DEFAULT_AFTER


def sound():
    return read_config().get("notify_sound", "on") != "off"


def ensure_app():
    if not os.path.exists(AGENT.src):
        return False
    try:
        with contextlib.redirect_stdout(sys.stderr):
            AGENT.build()
    except SystemExit:
        return False
    return os.access(AGENT.bin, os.X_OK)


def launch(args):
    subprocess.run(["open", "-g", "-n", "-a", AGENT.app, "--args"] + args,
                   capture_output=True, timeout=15)


def post(title, subtitle, body, sid, click=None, wait=False):
    args = ["post", "--title", title, "--subtitle", subtitle, "--body", body, "--id", sid]
    if click:
        args += ["--click", click]
    if not sound():
        args += ["--sound", "off"]
    if not wait:
        launch(args)
        return "ok"
    fd, result = tempfile.mkstemp(prefix="mnrh-notify-")
    os.close(fd)
    os.remove(result)
    try:
        launch(args + ["--result", result])
        for _ in range(600):
            if os.path.exists(result):
                with open(result) as f:
                    return f.read().strip()
            time.sleep(0.1)
        return "timeout"
    finally:
        with contextlib.suppress(OSError):
            os.remove(result)


def osascript(script, *args, timeout=5):
    try:
        r = subprocess.run(["osascript", "-e", script, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def front_bundle():
    try:
        front = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=3).stdout.strip()
        out = subprocess.run(["lsappinfo", "info", "-only", "bundleid", front],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r'"CFBundleIdentifier"="([^"]*)"', out)
    return m.group(1) if m else ""


def watching(bundle, tty):
    if not bundle or front_bundle() != bundle:
        return False
    term = TERMINALS.get(bundle)
    if not term:
        return True
    return osascript(SELECTED[term]) == tty


def focus(tty, bundle):
    term = TERMINALS.get(bundle)
    if term and tty and osascript(FOCUS[term], tty) == "ok":
        return 0
    if bundle:
        subprocess.run(["open", "-b", bundle], capture_output=True)
    return 0


def state_path(sid):
    return os.path.join(STATE, sid + ".json")


def load_state(sid):
    try:
        with open(state_path(sid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(sid, st):
    os.makedirs(STATE, exist_ok=True)
    tmp = f"{state_path(sid)}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, state_path(sid))


def prune_states():
    cutoff = time.time() - 7 * 86400
    for name in os.listdir(STATE) if os.path.isdir(STATE) else []:
        p = os.path.join(STATE, name)
        with contextlib.suppress(OSError):
            if os.path.getmtime(p) < cutoff:
                os.remove(p)


def plain(text, n=180):
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    text = re.sub(r"[*_`#>|]+", "", text)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def own_terminal():
    from claude_restart import RestartError, find_claude, ps
    try:
        pid = find_claude()
    except RestartError:
        return None
    tty = ps("tty", pid)
    if not tty or "?" in tty:
        return None
    return "/dev/" + tty if not tty.startswith("/dev/") else tty


def session_title(sid, path):
    import claude_search
    if not path or not os.path.isfile(path):
        return ""
    data = claude_search.update(sid, path)
    t = claude_search.title(sid, data) if data else ""
    return "" if t == "без названия" else t


def message_for(payload, st):
    event = payload.get("hook_event_name")
    if event == "Stop":
        started = st.get("prompt_at")
        if not started or time.time() - started < after():
            return None
        took = int(time.time() - started)
        took = f"{took // 60} мин {took % 60} с" if took >= 60 else f"{took} с"
        return f"Готово за {took}", plain(payload.get("last_assistant_message")) or "Ход закончен"
    if event != "Notification":
        return None
    kind, msg = payload.get("notification_type"), payload.get("message") or ""
    if kind == "permission_prompt":
        m = re.search(r"permission to use (.+?)\.?$", msg)
        return "Нужно разрешение", (m.group(1) if m else plain(msg))
    if kind in ("elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"):
        return "Нужен ответ", plain(msg) or "Claude задаёт вопрос"
    if kind == "idle_prompt" and not st.get("notified"):
        return "Ждёт тебя", plain(st.get("last") or "") or "Claude ждёт ответа"
    return None


def hook():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    sid = payload.get("session_id") or ""
    if not re.match(r"^[0-9a-f-]{36}$", sid) or not enabled():
        return 0
    st = load_state(sid)
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit":
        had = st.get("notified")
        save_state(sid, {"prompt_at": time.time()})
        if had and os.path.exists(AGENT.bin):
            launch(["remove", "--id", sid])
        prune_states()
        return 0
    if event == "Stop" and payload.get("last_assistant_message"):
        st["last"] = payload["last_assistant_message"][:2000]
        save_state(sid, st)
    what = message_for(payload, st)
    if not what:
        return 0
    tty = own_terminal()
    if not tty:
        return 0
    bundle = os.environ.get("__CFBundleIdentifier", "")
    if watching(bundle, tty):
        return 0
    if not ensure_app():
        return 0
    head, body = what
    cwd = payload.get("cwd") or ""
    project = "~" if cwd.rstrip("/") == HOME else os.path.basename(cwd.rstrip("/")) or "Claude"
    title = session_title(sid, payload.get("transcript_path"))
    mnrh = mnrh_path()
    click = f"{shlex.quote(mnrh)} claude focus {shlex.quote(tty)} {shlex.quote(bundle)}" if mnrh else None
    post(f"{project}: {head}", title, body, sid, click)
    st["notified"] = True
    save_state(sid, st)
    return 0


def mnrh_path():
    from claude_restart import MNRH
    return MNRH if os.access(MNRH, os.X_OK) else None


def status():
    on = enabled()
    print(f"Уведомления: {'включены' if on else 'выключены'}; о готовом ответе — если ход дольше {after()} с; "
          f"звук {'есть' if sound() else 'выключен'}")
    print(f"Приложение: {tilde(AGENT.app)}" + ("" if os.path.exists(AGENT.bin) else " (соберётся при первом уведомлении)"))
    try:
        with open(os.path.join(HOME, ".claude", "settings.json")) as f:
            hooks = json.load(f).get("hooks", {})
        have = [e for e in ("UserPromptSubmit", "Stop", "Notification")
                if any("claude notify-hook" in h.get("command", "") for x in hooks.get(e, []) for h in x.get("hooks", []))]
    except (OSError, ValueError, AttributeError):
        have = []
    print("Хуки Claude Code: " + (", ".join(have) if have else "не стоят — mnrh claude setup"))


def test():
    if not ensure_app():
        print("Не собрал приложение уведомлений: нужен swiftc из Xcode Command Line Tools")
        return 1
    tty = os.ttyname(0) if os.isatty(0) else ""
    bundle = os.environ.get("__CFBundleIdentifier", "")
    mnrh = mnrh_path()
    click = f"{shlex.quote(mnrh)} claude focus {shlex.quote(tty)} {shlex.quote(bundle)}" if mnrh and tty else None
    print("Отправляю пробное уведомление. В первый раз macOS спросит, можно ли mnrh Notify их показывать — разреши.")
    r = post("mnrh: пробное уведомление", "Claude Code", "Нажми, чтобы вернуться в эту вкладку",
             "00000000-0000-0000-0000-000000000000", click, wait=True)
    if r == "ok":
        print("✓ показано")
        return 0
    if r.startswith("denied"):
        print(f"✗ macOS не разрешает уведомления: {settings_path('settings', 'notifications')} → mnrh Notify → "
              f"{settings_name('allow')}")
    else:
        print(f"✗ не получилось: {r}")
    return 1


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "notify-hook":
        return hook()
    if cmd == "focus":
        return focus(args[1] if len(args) > 1 else "", args[2] if len(args) > 2 else "")
    rest = args[1:]
    if not rest or rest[0] in ("status",):
        status()
        return 0
    if rest[0] in ("-h", "--help"):
        usage()
        return 0
    if rest[0] in ("on", "off"):
        write_config(notify=rest[0])
        print(f"Уведомления {'включены' if rest[0] == 'on' else 'выключены'}")
        return 0
    if rest[0] == "sound" and len(rest) == 2 and rest[1] in ("on", "off"):
        write_config(notify_sound=rest[1])
        print(f"Звук {'включён' if rest[1] == 'on' else 'выключен'}")
        return 0
    if rest[0] == "after" and len(rest) == 2 and rest[1].isdigit():
        write_config(notify_after=rest[1])
        print(f"О готовом ответе — если ход шёл дольше {rest[1]} с")
        return 0
    if rest[0] == "test":
        return test()
    usage()
    return 2


if __name__ == "__main__":
    sys.exit(main())
