import os
import shutil
import subprocess
import sys

LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
sys.path.insert(0, LIB)
from mnrhlib import CONFIG, DEFAULT_DEV, HOME, has_flag, paint, read_config, tilde, write_config
from menu import pick

CANDIDATES = ["Developer", "Projects", "AndroidStudioProjects", "StudioProjects", "IdeaProjects",
              "XcodeProjects", "code", "src", "dev", "git", "repos", "work",
              "Documents/GitHub", "Documents/Projects", "Documents/Developer"]


def usage():
    print("mnrh init                 первая настройка: где лежат проекты, подключение к Claude Code")
    print("mnrh init <папка>         сразу задать папку с проектами")
    print("mnrh init --show          показать текущую настройку")
    print()
    print(f"Настройка хранится в {tilde(CONFIG)}; на время можно переопределить MNRH_PROJECTS=<папка>.")
    print("Папку используют mnrh claude, repos, gradle, disk и claude sessions.")


def repo_count(path):
    try:
        return sum(1 for d in os.listdir(path) if os.path.isdir(os.path.join(path, d, ".git")))
    except OSError:
        return 0


def candidates():
    seen, out = set(), []
    for name in CANDIDATES:
        p = os.path.join(HOME, name)
        if not os.path.isdir(p):
            continue
        key = os.path.realpath(p).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((p, repo_count(p)))
    out.sort(key=lambda x: -x[1])
    return out


def store(path):
    path = os.path.abspath(os.path.expanduser(path))
    write_config(projects=tilde(path))
    print(f"✓ папка проектов: {tilde(path)} ({repo_count(path)} git-репозиториев) → {tilde(CONFIG)}")
    return path


def choose_projects():
    current = read_config().get("projects")
    found = candidates()
    items = [(tilde(p), f"{tilde(p):<34} git-репозиториев: {n}") for p, n in found]
    paths = [p for p, _ in found]
    if not os.path.isdir(DEFAULT_DEV):
        items.append(("создать", f"создать {tilde(DEFAULT_DEV)}"))
        paths.append("create")
    items.append(("другая", "другая папка — ввести путь"))
    paths.append("other")
    default = 0
    if current:
        cur = os.path.abspath(os.path.expanduser(current))
        if cur in paths:
            default = paths.index(cur)
    title = "Где лежат твои проекты?" + (f" Сейчас: {current}" if current else "")
    i = pick(items, title=title, label="Проекты", default=default)
    if i is None:
        return None
    choice = paths[i]
    if choice == "create":
        os.makedirs(DEFAULT_DEV, exist_ok=True)
        return store(DEFAULT_DEV)
    if choice == "other":
        try:
            raw = input("Путь к папке с проектами: ").strip()
        except EOFError:
            return None
        path = os.path.abspath(os.path.expanduser(raw)) if raw else ""
        if not path or not os.path.isdir(path):
            print(f"Папки {tilde(path) or '(пусто)'} нет")
            return None
        return store(path)
    return store(choice)


def connect_claude():
    if not shutil.which("claude"):
        print("Claude Code не найден. Когда поставишь его, выполни: mnrh claude setup")
        return
    i = pick([("да", "да — /restart, /forget, MCP-сервер mnrh и хук zsh"), ("нет", "нет, позже: mnrh claude setup")],
             title="Подключить mnrh к Claude Code?", label="Claude Code")
    if i == 0:
        subprocess.run([sys.executable, os.path.join(LIB, "claude_restart.py"), "setup"])


def main():
    args = sys.argv[1:]
    if has_flag(args, "-h", "--help"):
        usage()
        return 0
    if has_flag(args, "--show"):
        conf = read_config()
        print(f"папка проектов: {conf.get('projects') or tilde(DEFAULT_DEV) + ' (по умолчанию)'}")
        print(f"файл настройки: {tilde(CONFIG)}{'' if os.path.exists(CONFIG) else ' (ещё не создан)'}")
        return 0
    paths = [a for a in args if not a.startswith("-")]
    if paths:
        path = os.path.abspath(os.path.expanduser(paths[0]))
        if not os.path.isdir(path):
            print(f"mnrh init: папки {tilde(path)} нет", file=sys.stderr)
            return 1
        store(path)
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("mnrh init: нужен терминал, или передай папку: mnrh init ~/Projects", file=sys.stderr)
        return 2
    if choose_projects() is None:
        return 1
    if not has_flag(args, "--projects-only"):
        connect_claude()
    return 0


if __name__ == "__main__":
    sys.exit(main())
