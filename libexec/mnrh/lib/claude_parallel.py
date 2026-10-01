import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import HOME, find_project, projects_dir, tilde

TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                     "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))
COPY = ["local.properties", ".env", ".env.*", "fastlane/.env", "fastlane/.env.*"]


def git(root, *args, check=False):
    r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=120)
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"git {' '.join(args)} упал")
    return r.stdout.strip()


def slug(text):
    low = "".join(TRANSLIT.get(ch, ch) for ch in text.lower())
    out = ""
    for w in re.findall(r"[a-z0-9]+", low):
        if len(out) + len(w) + 1 > 28:
            break
        out = f"{out}-{w}" if out else w
    return out or time.strftime("task-%m%d-%H%M")


def worktree_root(root):
    return os.path.join(projects_dir(), ".worktrees", os.path.basename(root.rstrip("/")))


def copy_local(root, path):
    copied = []
    for pattern in COPY:
        for src in glob.glob(os.path.join(glob.escape(root), pattern)):
            rel = os.path.relpath(src, root)
            dst = os.path.join(path, rel)
            if os.path.isfile(src) and not os.path.exists(dst) and not git(root, "ls-files", rel):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                copied.append(rel)
    return copied


PERMISSION_MODES = ["default", "acceptEdits", "auto", "plan"]


def start(task, project=None, branch=None, mode="caffeinate", cwd=None, mnrh=None, permission=None, rc=True):
    root, err = find_project(project, cwd)
    if err:
        return err, True
    if not git(root, "rev-parse", "--git-dir"):
        return f"{tilde(root)} — не git-репозиторий, worktree не сделать", True
    task = " ".join((task or "").split())
    if not task:
        return "Нужна задача: что сделать параллельно", True
    base = git(root, "branch", "--show-current") or git(root, "rev-parse", "--short", "HEAD")
    name = branch or f"mnrh/{slug(task)}"
    if git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
        name = f"{name}-{time.strftime('%H%M%S')}"
    path = os.path.join(worktree_root(root), name.replace("/", "-"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        git(root, "worktree", "add", "-b", name, path, "HEAD", check=True)
    except RuntimeError as e:
        return f"Не создал worktree: {e}", True
    git(root, "config", f"branch.{name}.mnrhBase", base)
    git(root, "config", f"branch.{name}.mnrhTask", task)
    copied = copy_local(root, path)
    dirty = git(root, "status", "--porcelain")
    prompt = (f"Это параллельная задача в отдельном git worktree {path}, ветка {name}, ответвлена от {base} "
              f"проекта {root}. Задача: {task}\n\nРаботай только в этой папке. Если нужны зависимости "
              f"(node_modules и т. п.), поставь их здесь. Когда закончишь, закоммить результат в ветку {name} "
              f"(не сливай и не пушь) и коротко отчитайся, что сделано и как проверить.")
    import claude_open
    flags = ["--permission-mode", permission] if permission in PERMISSION_MODES and permission != "default" else []
    text, bad = claude_open.open_claude({"path": path, "mode": mode, "prompt": prompt, "flags": flags,
                                         "remote_control": rc}, mnrh or "mnrh")
    lines = [f"Ветка {name} от {base}, worktree {tilde(path)}."]
    if copied:
        lines.append("Скопировал неотслеживаемые настройки: " + ", ".join(copied) + ".")
    if dirty:
        lines.append("В основной папке есть незакоммиченные изменения — в worktree их нет, он от последнего коммита.")
    lines.append(text)
    lines.append(f"Потом: mnrh claude parallel merge {name} — влить, drop {name} — выбросить.")
    return "\n".join(lines), bad


def running_in(path):
    for f in glob.glob(os.path.join(HOME, ".claude", "sessions", "*.json")):
        try:
            with open(f) as fh:
                info = json.load(fh)
            if (info.get("cwd") or "").startswith(path):
                os.kill(int(info.get("pid") or 0), 0)
                return True
        except (OSError, ValueError, ProcessLookupError, TypeError):
            continue
    return False


def entries(root):
    out, cur = [], {}
    for line in git(root, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if cur.get("path", "").startswith(worktree_root(root) + "/") and cur.get("branch"):
                out.append(cur)
            cur = {}
        elif line.startswith("worktree "):
            cur["path"] = line[9:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "")
    for e in out:
        b = e["branch"]
        e["base"] = git(root, "config", f"branch.{b}.mnrhBase") or git(root, "branch", "--show-current")
        e["task"] = git(root, "config", f"branch.{b}.mnrhTask")
        e["ahead"] = git(root, "rev-list", "--count", f"{e['base']}..{b}") or "?"
        e["dirty"] = len(git(e["path"], "status", "--porcelain").splitlines())
        e["running"] = running_in(e["path"])
    return out


def listing(project=None, cwd=None):
    root, err = find_project(project, cwd)
    if err:
        return err, True
    es = entries(root)
    if not es:
        return f"Параллельных задач в {os.path.basename(root)} нет.", False
    lines = [f"Параллельные задачи {os.path.basename(root)}:"]
    for e in es:
        state = "Claude работает" if e["running"] else "Claude закрыт"
        lines.append(f"  {e['branch']}: коммитов {e['ahead']} поверх {e['base']}, незакоммичено {e['dirty']}, {state}")
        if e["task"]:
            lines.append(f"    задача: {e['task'][:160]}")
    return "\n".join(lines), False


def finish(branch, action, project=None, cwd=None):
    root, err = find_project(project, cwd)
    if err:
        return err, True
    match = [e for e in entries(root) if e["branch"] == branch or e["branch"].endswith("/" + branch)]
    if len(match) != 1:
        return f"Нет параллельной задачи «{branch}». " + listing(project, cwd)[0], True
    e = match[0]
    if e["running"]:
        return f"В {tilde(e['path'])} ещё работает Claude — сначала закрой его.", True
    if action == "merge":
        if e["dirty"]:
            return f"В worktree незакоммичено файлов: {e['dirty']}. Сначала закоммить их там.", True
        if git(root, "status", "--porcelain", "--untracked-files=no"):
            return "В основной папке есть незакоммиченные изменения — сначала закоммить или отложи их.", True
        current = git(root, "branch", "--show-current")
        if current != e["base"]:
            return f"Основная папка сейчас на ветке {current}, а задача ответвлена от {e['base']}. Переключись на неё.", True
        r = subprocess.run(["git", "-C", root, "merge", "--no-ff", "--no-edit", e["branch"]],
                           capture_output=True, text=True)
        if r.returncode:
            subprocess.run(["git", "-C", root, "merge", "--abort"], capture_output=True)
            return f"Слить без конфликтов не вышло, слияние отменено:\n{(r.stdout + r.stderr).strip()[-1500:]}", True
    subprocess.run(["git", "-C", root, "worktree", "remove", "--force", e["path"]], capture_output=True)
    git(root, "branch", "-d" if action == "merge" else "-D", e["branch"])
    for key in ("mnrhBase", "mnrhTask"):
        git(root, "config", "--unset", f"branch.{e['branch']}.{key}")
    if action == "merge":
        return f"Влил {e['branch']} в {e['base']} ({e['ahead']} коммитов), worktree и ветку убрал. Пушить — сам.", False
    return f"Выбросил {e['branch']}: worktree и ветка удалены.", False


START_TOOL = {
    "name": "parallel_task",
    "description": ("Запустить задачу параллельно: создать git worktree на новой ветке (от текущего коммита проекта) "
                    "и открыть там второй Claude Code в новом окне терминала с этой задачей и Remote Control. "
                    "Второй Claude закоммитит результат в свою ветку; потом parallel_finish вольёт или выбросит её. "
                    "Вызывай, когда пользователь просит сделать что-то параллельно, в фоне или в отдельной ветке."),
    "inputSchema": {"type": "object", "properties": {
        "task": {"type": "string", "description": "задача для второго Claude, понятная без контекста этой сессии"},
        "project": {"type": "string", "description": "проект; по умолчанию текущий"},
        "branch": {"type": "string", "description": "имя ветки; по умолчанию mnrh/<по задаче>"},
        "mode": {"type": "string", "enum": ["caffeinate", "normal"]},
        "permission_mode": {"type": "string", "enum": PERMISSION_MODES,
                            "description": "режим разрешений второго Claude; по умолчанию обычный (спрашивает, "
                                           "вопрос приходит уведомлением). auto или acceptEdits — только если "
                                           "пользователь так сказал"},
        "remote_control": {"type": "boolean", "description": "Remote Control, по умолчанию да"}},
        "required": ["task"]},
}
LIST_TOOL = {
    "name": "parallel_list",
    "description": "Параллельные задачи проекта: ветки, сколько коммитов, есть ли незакоммиченное, работает ли там Claude.",
    "annotations": {"readOnlyHint": True},
    "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}},
}
FINISH_TOOL = {
    "name": "parallel_finish",
    "description": ("Закончить параллельную задачу: merge — влить её ветку в ветку, от которой она ответвлена "
                    "(без пуша), drop — выбросить ветку и worktree. Только с согласия пользователя."),
    "annotations": {"destructiveHint": True},
    "inputSchema": {"type": "object", "properties": {
        "branch": {"type": "string"}, "action": {"type": "string", "enum": ["merge", "drop"]},
        "project": {"type": "string"}}, "required": ["branch", "action"]},
}
TOOLS = [START_TOOL, LIST_TOOL, FINISH_TOOL]


def handle(name, a, cwd=None, mnrh=None):
    if name == "parallel_task":
        return start(a.get("task"), a.get("project"), a.get("branch"), a.get("mode") or "caffeinate", cwd, mnrh,
                     a.get("permission_mode"), a.get("remote_control", True) is not False)
    if name == "parallel_list":
        return listing(a.get("project"), cwd)
    return finish(a.get("branch", ""), a.get("action"), a.get("project"), cwd)


def main():
    args = sys.argv[2:] if sys.argv[1:2] == ["parallel"] else sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print('mnrh claude parallel "<задача>" [-p проект] [-b ветка] [-n] [--permission-mode auto] [--no-rc]')
        print("                         новая ветка и worktree от текущего коммита, в новом окне — Claude с задачей")
        print("mnrh claude parallel list              параллельные задачи этого проекта")
        print("mnrh claude parallel merge <ветка>     влить в исходную ветку (без пуша) и убрать worktree")
        print("mnrh claude parallel drop <ветка>      выбросить ветку и worktree")
        print()
        print(f"Worktree лежат в {tilde(os.path.join(projects_dir(), '.worktrees'))}/<проект>/. В Claude Code: /parallel.")
        return 0 if args else 2
    opts = {}
    rest = []
    i = 0
    while i < len(args):
        if args[i] in ("-p", "-b", "--permission-mode") and i + 1 < len(args):
            opts[args[i]] = args[i + 1]
            i += 2
            continue
        if args[i] in ("-n", "--no-rc"):
            opts[args[i]] = True
        else:
            rest.append(args[i])
        i += 1
    if rest[:1] == ["list"]:
        text, bad = listing(opts.get("-p"))
    elif rest[:1] in (["merge"], ["drop"]) and len(rest) == 2:
        text, bad = finish(rest[1], rest[0], opts.get("-p"))
    else:
        from claude_restart import MNRH
        text, bad = start(" ".join(rest), opts.get("-p"), opts.get("-b"), "normal" if opts.get("-n") else "caffeinate",
                          mnrh=MNRH, permission=opts.get("--permission-mode"), rc=not opts.get("--no-rc"))
    print(text)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
