"""MCP-инструмент open_claude: новый Claude Code в новом окне терминала через mnrh claude.

Без проекта возвращает список, из которого выбирать (его показывает Claude в чате),
с проектом — открывает окно Terminal или iTerm2, запускает mnrh claude и ждёт, пока Claude поднимется.
"""
import os
import re
import shlex
import subprocess
import time

from mnrhlib import HOME, projects_dir, run, tilde

OPEN = {
    "Apple_Terminal": '''
on run argv
  tell application "Terminal"
    activate
    set t to do script (item 1 of argv)
    return tty of t
  end tell
end run''',
    "iTerm.app": '''
on run argv
  tell application "iTerm2"
    activate
    set w to (create window with default profile)
    tell current session of w
      write text (item 1 of argv)
      return tty
    end tell
  end tell
end run''',
}
# текст вкладки по tty: отсюда берём ссылку Remote Control
READ = {
    "Apple_Terminal": '''
on run argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is (item 1 of argv) then return (get history of t) as text
      end repeat
    end repeat
  end tell
  return ""
end run''',
    "iTerm.app": '''
on run argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if tty of s is (item 1 of argv) then return contents of s
        end repeat
      end repeat
    end repeat
  end tell
  return ""
end run''',
}
MODES = {"caffeinate": "-k", "normal": "-n"}
RC_URL = re.compile(r"https://claude\.ai/code/session_\w+")

TOOL = {
    "name": "open_claude",
    "description": (
        "Открыть новый Claude Code в новом окне терминала (Terminal или iTerm2 — тот, где идёт эта сессия) "
        "через mnrh claude. Вызови сначала без project: получишь список проектов и режимов — покажи его "
        "пользователю и спроси, что выбрать (если он уже назвал проект, можно сразу). Потом вызови с project "
        "(имя папки или начало имени, «~» — домашний каталог) и mode. Remote Control включается сразу, "
        "ссылка на сессию (claude.ai/code) приходит в ответе — дай её пользователю. "
        "Результат — где открылось и запустился ли Claude."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "проект: имя папки или его начало; «~» — домашний каталог"},
            "mode": {"type": "string", "enum": ["caffeinate", "normal"],
                     "description": "caffeinate — Mac не уснёт, пока идёт сессия (по умолчанию); normal — обычная"},
            "prompt": {"type": "string", "description": "первый запрос для нового Claude (необязательно)"},
            "continue": {"type": "boolean", "description": "продолжить последнюю сессию в этом проекте (claude --continue)"},
            "remote_control": {"type": "boolean", "default": True,
                               "description": "включить Remote Control (/rc): сессию можно вести с телефона и в браузере; "
                                              "по умолчанию да"},
        },
    },
}


def project_dirs():
    root = projects_dir()
    if not os.path.isdir(root):
        return root, []
    return root, sorted(os.path.join(root, d) for d in os.listdir(root)
                        if os.path.isdir(os.path.join(root, d)) and not d.startswith("."))


def git_line(d):
    branch = run(["git", "-C", d, "branch", "--show-current"], timeout=5).strip()
    last = run(["git", "-C", d, "log", "-1", "--format=%cr"], timeout=5).strip()
    return f"{branch or '-'}, {last}" if last else (branch or "не git")


def choices(root, dirs, note=""):
    lines = [note] if note else []
    lines.append(f"Проекты в {tilde(root)} (ветка, последний коммит):")
    lines.append("  ~  домашний каталог")
    lines += [f"  {os.path.basename(d)}  ({git_line(d)})" for d in dirs]
    lines.append("")
    lines.append("Режим: caffeinate — Mac не уснёт, пока идёт сессия (по умолчанию); normal — обычная.")
    lines.append("Можно добавить первый запрос (prompt) или продолжить прошлую сессию (continue).")
    lines.append("Покажи список пользователю и спроси, какой проект и режим открыть, "
                 "затем вызови open_claude с project и mode.")
    return "\n".join(lines)


def match(query, dirs):
    if query in ("~", "root", "home", HOME):
        return [HOME]
    q = query.lower()
    exact = [d for d in dirs if os.path.basename(d).lower() == q]
    return exact or [d for d in dirs if os.path.basename(d).lower().startswith(q)]


def terminal():
    term = os.environ.get("TERM_PROGRAM", "")
    return term if term in OPEN else "Apple_Terminal"


def claude_on(tty):
    """pid Claude Code на этом терминале, если он уже поднялся."""
    out = run(["ps", "-t", tty.replace("/dev/", ""), "-o", "pid=,command="], timeout=5)
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        words = cmd.split()
        if words and (os.path.basename(words[0]) == "claude" or
                      os.path.basename(words[0]) == "node" and any(w.endswith("/claude") for w in words[1:2])):
            return int(pid)
    return None


def open_claude(args, mnrh):
    """-> (текст, ошибка ли)"""
    root, dirs = project_dirs()
    project = (args.get("project") or "").strip()
    if not project:
        return choices(root, dirs), False
    found = match(project, dirs)
    if len(found) != 1:
        note = (f"«{project}» подходит к нескольким: {', '.join(os.path.basename(d) for d in found)}." if found
                else f"Проекта «{project}» нет в {tilde(root)}.")
        return choices(root, dirs, note), True
    target = found[0]
    mode = args.get("mode") or "caffeinate"
    if mode not in MODES:
        return f"Режим бывает caffeinate или normal, а не «{mode}».", True

    name = "~" if target == HOME else os.path.basename(target)
    cmd = [mnrh, "claude", name, MODES[mode]]
    rc = args.get("remote_control", True) is not False
    # имя у --remote-control необязательное: без него первый запрос принялся бы за имя
    extra = (["--remote-control", "home" if name == "~" else name] if rc else []) + \
        (["--continue"] if args.get("continue") else []) + ([args["prompt"]] if args.get("prompt") else [])
    if extra:
        cmd += ["--"] + extra
    line = " ".join(shlex.quote(c) for c in cmd)

    term = terminal()
    r = subprocess.run(["osascript", "-e", OPEN[term], line], capture_output=True, text=True, timeout=30)
    tty = r.stdout.strip()
    app = "iTerm2" if term == "iTerm.app" else "Terminal"
    if r.returncode or not tty.startswith("/dev/"):
        err = (r.stderr or r.stdout).strip()
        hint = (" Нужно разрешение: Настройки → Конфиденциальность → Автоматизация → разрешить управлять "
                f"{app}.") if "-1743" in err or "not allowed" in err.lower() else ""
        return f"Не получилось открыть окно {app}: {err or 'osascript без ответа'}.{hint}", True

    pid = None
    for _ in range(40):  # до 20 секунд: caffeinate, node, первый запуск
        pid = claude_on(tty)
        if pid:
            break
        time.sleep(0.5)
    where = f"{tilde(target)}, режим {mode}"
    url = remote_url(term, tty) if pid and rc else ""
    if pid:
        text = f"Открыл новое окно {app} ({tty}): Claude Code запущен в {where}, pid {pid}."
        if url:
            text += f" Remote Control включён: {url}"
        elif rc:
            text += " Remote Control запрошен, но ссылка за 20 секунд не появилась — загляни в окно."
    else:
        text = (f"Открыл новое окно {app} ({tty}) и запустил там `{' '.join(cmd[1:])}`, но за 20 секунд "
                f"Claude Code не появился — загляни в окно ({where}).")
    if args.get("prompt"):
        text += " Первый запрос передан."
    return text, not pid


def remote_url(term, tty):
    """Ссылка на сессию Remote Control из текста вкладки; ждём до 20 секунд."""
    for _ in range(40):
        r = subprocess.run(["osascript", "-e", READ[term], tty], capture_output=True, text=True, timeout=15)
        found = RC_URL.findall(r.stdout)
        if found:
            return found[-1]
        time.sleep(0.5)
    return ""
