import calendar
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import HOME, project_root, read_config, run, tilde
import claude_search
import mnrh_todo

LAST_CHARS = 1500
ASK_CHARS = 400


def enabled():
    return read_config().get("brief", "on") != "off"


def clip(text, n):
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def ago(ts):
    if not ts:
        return "?"
    try:
        t = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return ts[:16]
    s = time.time() - t
    if s < 3600:
        return f"{max(1, int(s // 60))} мин назад"
    if s < 86400:
        return f"{int(s // 3600)} ч назад"
    if s < 2 * 86400:
        return "вчера"
    return f"{int(s // 86400)} дн назад"


def git_part(root):
    if not os.path.isdir(os.path.join(root, ".git")) and not run(["git", "-C", root, "rev-parse", "--git-dir"]).strip():
        return [], {}
    branch = run(["git", "-C", root, "branch", "--show-current"]).strip() or "(detached)"
    status = run(["git", "-C", root, "status", "--porcelain"]).splitlines()
    ahead = run(["git", "-C", root, "rev-list", "--count", "@{u}..HEAD"]).strip()
    log = run(["git", "-C", root, "log", "-5", "--format=%h %ar: %s"]).strip().splitlines()
    tag = run(["git", "-C", root, "describe", "--tags", "--abbrev=0"]).strip()
    since = run(["git", "-C", root, "rev-list", "--count", f"{tag}..HEAD"]).strip() if tag else ""
    head = f"Ветка {branch}"
    if status:
        head += f", незакоммичено файлов: {len(status)}"
    if ahead and ahead != "0":
        head += f", не запушено коммитов: {ahead}"
    lines = [head]
    if tag:
        lines.append(f"Последний тег {tag}, коммитов после него: {since or '?'}")
    if log:
        lines.append("Последние коммиты:")
        lines += [f"  {l}" for l in log]
    return lines, {"branch": branch, "dirty": len(status), "ahead": ahead}


def last_session(root, cwd, exclude=None):
    from claude_sessions import encode, is_junk
    best = []
    for cwd in {root, os.path.abspath(cwd)}:
        pdir = os.path.join(claude_search.PROJECTS, encode(cwd))
        try:
            names = os.listdir(pdir)
        except OSError:
            continue
        for f in names:
            sid = f[:-6]
            path = os.path.join(pdir, f)
            if not f.endswith(".jsonl") or sid == exclude or not claude_search.UUID.match(sid) or os.path.islink(path):
                continue
            best.append((os.path.getmtime(path), sid, path))
    for _, sid, path in sorted(best, reverse=True):
        if is_junk(path):
            continue
        data = claude_search.update(sid, path)
        if data and any(m[0] == "claude" for m in data["msgs"]):
            return sid, data
    return None, None


def session_part(sid, data):
    msgs = data["msgs"]
    asks = [m for m in msgs if m[0] == "user"]
    replies = [m for m in msgs if m[0] == "claude"]
    title = claude_search.title(sid, data)
    lines = [f"Прошлая сессия в этом проекте: «{title}» ({sid[:8]}), последняя активность {ago(data.get('last'))}"]
    if asks:
        lines.append("Последний запрос пользователя:")
        lines.append(clip(asks[-1][2], ASK_CHARS))
    if replies:
        lines.append("Последний ответ Claude:")
        lines.append(clip(replies[-1][2], LAST_CHARS))
    return lines, title


def build(cwd, exclude=None):
    root = project_root(cwd)
    name = "~" if root == HOME else os.path.basename(root)
    lines = [f"Проект {name} ({tilde(root)})."]
    git_lines, g = git_part(root)
    lines += git_lines
    sid, data = last_session(root, cwd, exclude)
    title = None
    if data:
        sess, title = session_part(sid, data)
        lines += [""] + sess
        lines.append(f"Подробнее: session_read id={sid[:8]} at=-10.")
    todos = mnrh_todo.open_items(root)
    if todos:
        lines += ["", mnrh_todo.fmt(root, todos)]
    short = []
    if title:
        short.append(f"прошлая сессия «{clip(title, 50)}» ({ago(data.get('last'))})")
    if g.get("dirty"):
        short.append(f"незакоммичено {g['dirty']}")
    if todos:
        short.append(f"в бэклоге {len(todos)}")
    return "\n".join(lines), short


def context_for_model(cwd, exclude=None):
    text, short = build(cwd, exclude)
    head = ("mnrh: справка о проекте на старте сессии. Это фон: не пересказывай её без запроса, но используй, "
            "когда пользователь спрашивает, где остановились, что дальше или ссылается на прошлую работу.\n\n")
    user = ("mnrh: " + " · ".join(short) + " · /where — подробнее") if short else ""
    return head + text, user


def cli(args):
    if args[:1] in (["-h"], ["--help"]):
        print("mnrh claude brief          где мы остановились: git, прошлая сессия в этом проекте, бэклог")
        print("mnrh claude brief on|off   показывать ли эту справку Claude при старте новой сессии")
        print()
        print("В Claude Code: /where. При старте новой сессии хук SessionStart передаёт справку Claude")
        print("как фон, а тебе показывает одну строку.")
        return 0
    if args[:1] in (["on"], ["off"]):
        from mnrhlib import write_config
        write_config(brief=args[0])
        print(f"Справка при старте сессии {'включена' if args[0] == 'on' else 'выключена'}")
        return 0
    exclude = os.environ.get("CLAUDE_CODE_SESSION_ID")
    text, _ = build(os.getcwd(), exclude)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[2:] if sys.argv[1:2] == ["brief"] else sys.argv[1:]))
