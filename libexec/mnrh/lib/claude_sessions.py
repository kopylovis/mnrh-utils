#!/usr/bin/env python3
import collections
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import DEV, HOME, confirm, has_flag, paint, projects, tilde
from menu import pick
import claude_search

CLAUDE = os.environ.get("MNRH_CLAUDE_HOME", os.path.join(HOME, ".claude"))
PROJECTS = os.path.join(CLAUDE, "projects")
CLAUDE_BIN = os.environ.get("MNRH_CLAUDE_BIN", "claude")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def usage():
    print("mnrh claude sessions                  сохранённые сессии: выбрать стрелками, продолжить, перенести, удалить")
    print("mnrh claude sessions -l               просто список")
    print("mnrh claude sessions resume <id> [-n] продолжить в её папке (по умолчанию через caffeinate, -n обычная)")
    print("mnrh claude sessions mv <id> <папка>  перенести в другую папку: путь, имя проекта из папки проектов или root")
    print("mnrh claude sessions rm <id>... [-y]  удалить вместе с историей запросов этой сессии")
    print("mnrh claude sessions clean [-y]       битые ссылки, сессии без единого ответа, папки удалённых проектов")
    print("mnrh claude sessions search <слова>   поиск по всем сессиям; -p <проект>, -d <дней>")
    print("mnrh claude sessions show <id> [N]    прочитать сессию с сообщения N (из search), -N — с конца")
    print()
    print("<id> — начало id из списка, 4–8 символов обычно хватает.")
    print("Перенос переписывает cwd внутри сессии, так что `claude --resume` найдёт её уже в новой папке.")


def encode(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def decode(name, base="/"):
    if not name.startswith("-"):
        return None
    rest = name[1:]
    if not rest:
        return base
    try:
        entries = os.listdir(base)
    except OSError:
        return None
    for e in sorted(entries, key=len, reverse=True):
        enc = encode(e)
        if rest == enc or rest.startswith(enc + "-"):
            full = os.path.join(base, e)
            if not os.path.isdir(full):
                continue
            if rest == enc:
                return full
            found = decode(rest[len(enc):], full)
            if found:
                return found
    return None


def tree_size(path):
    if os.path.islink(path) or not os.path.exists(path):
        return 0
    if not os.path.isdir(path):
        return os.lstat(path).st_size
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def fmt_size(n):
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} ГБ"
    if n >= 10 << 20:
        return f"{n / (1 << 20):.0f} МБ"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} МБ"
    return f"{max(1, n // 1024)} КБ" if n else "0"


def ago(ts):
    s = time.time() - ts
    if s < 3600:
        return f"{max(1, int(s // 60))} мин назад"
    if s < 86400:
        return f"{int(s // 3600)} ч назад"
    if s < 30 * 86400:
        return f"{int(s // 86400)} дн назад"
    return time.strftime("%d.%m.%Y", time.localtime(ts))


def running_ids():
    ids = {}
    d = os.path.join(CLAUDE, "sessions")
    try:
        names = os.listdir(d)
    except OSError:
        return ids
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, n)) as f:
                o = json.load(f)
            pid = int(o.get("pid") or 0)
            if pid <= 0:
                continue
            try:
                os.kill(pid, 0)
            except PermissionError:
                pass
            ids[o["sessionId"]] = pid
        except (OSError, ValueError, KeyError, TypeError, ProcessLookupError):
            pass
    return ids


def read_meta(path):
    meta = {"cwd": None, "branch": None, "custom": None, "ai": None, "prompt": None, "answered": False}
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return meta
    with f:
        for line in f:
            if not meta["answered"] and '"type":"assistant"' in line:
                meta["answered"] = True
            if meta["cwd"] is None and '"cwd"' in line:
                try:
                    o = json.loads(line)
                    meta["cwd"] = o.get("cwd")
                    meta["branch"] = o.get("gitBranch")
                except ValueError:
                    pass
            for marker, key, field in (('"custom-title"', "custom", "customTitle"),
                                       ('"ai-title"', "ai", "aiTitle"),
                                       ('"last-prompt"', "prompt", "lastPrompt")):
                if marker in line:
                    try:
                        o = json.loads(line)
                        if o.get(field):
                            meta[key] = o[field]
                    except ValueError:
                        pass
    return meta


LOCAL_MARKERS = ("<command-name>", "<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>")


def is_junk(path):
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return False
    with f:
        for line in f:
            if '"type":"assistant"' in line:
                return False
            if '"type":"user"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                return False
            if o.get("type") != "user" or o.get("isMeta"):
                continue
            c = (o.get("message") or {}).get("content")
            if isinstance(c, str):
                texts = [c]
            elif isinstance(c, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in c):
                texts = [b.get("text", "") for b in c]
            else:
                return False
            if not texts or not all(any(m in t for m in LOCAL_MARKERS) for t in texts):
                return False
    return True


def load():
    running = running_ids()
    out = []
    try:
        dirs = sorted(os.listdir(PROJECTS))
    except OSError:
        return out
    for d in dirs:
        pdir = os.path.join(PROJECTS, d)
        if not os.path.isdir(pdir):
            continue
        home = decode(d)
        for f in os.listdir(pdir):
            sid = f[:-6]
            if not f.endswith(".jsonl") or not UUID.match(sid):
                continue
            path = os.path.join(pdir, f)
            if not os.path.exists(path):
                continue
            link = os.path.islink(path)
            meta = read_meta(path)
            extra = os.path.join(pdir, sid)
            custom = meta["custom"]
            try:
                with open(os.path.join(extra, "custom-title.json")) as fh:
                    custom = json.load(fh).get("customTitle") or custom
            except (OSError, ValueError, AttributeError):
                pass
            cwd = home or meta["cwd"] or d
            out.append({
                "id": sid, "pdir": pdir, "path": path, "extra": extra, "link": link,
                "target": os.path.realpath(path) if link else None,
                "cwd": cwd, "started_in": meta["cwd"], "branch": meta["branch"],
                "title": custom or meta["ai"] or meta["prompt"] or "",
                "prompt": meta["prompt"], "answered": meta["answered"],
                "size": 0 if link else os.lstat(path).st_size + tree_size(extra),
                "mtime": os.stat(path).st_mtime,
                "running": running.get(sid),
            })
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return out


def folder(cwd):
    if cwd.startswith(DEV + "/") and "/" not in cwd[len(DEV) + 1:]:
        return cwd[len(DEV) + 1:]
    return tilde(cwd)


def clip(text, n):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def flags(s):
    out = ""
    if s["running"]:
        out += " ● открыта"
    if s["link"]:
        out += " ↪ ссылка"
    if not os.path.isdir(s["cwd"]):
        out += " ✗ папки нет"
    return out


def row(s):
    return (f"{ago(s['mtime']):>13}  {fmt_size(s['size']):>7}  {clip(folder(s['cwd']), 24):<24}  "
            f"{clip(s['title'] or '—', 60)}{flags(s)}")


def summary(ss):
    return f"Сессии Claude Code: {len(ss)}, {fmt_size(sum(s['size'] for s in ss))} в {tilde(PROJECTS)}"


def print_list(ss):
    print(summary(ss))
    print()
    print(paint(f"  {'id':<8}  {'когда':>13}  {'размер':>7}  {'папка':<24}  название", "2"))
    for s in ss:
        print(f"  {s['id'][:8]}  {row(s)}")


def find(ss, prefix):
    hits = [s for s in ss if s["id"].startswith(prefix.lower())]
    if not hits:
        hits = [s for s in ss if s["title"] == prefix]
    if len(hits) == 1:
        return hits[0]
    if hits:
        sys.exit(f"mnrh claude sessions: «{prefix}» подходит к {len(hits)} сессиям, дай больше символов id")
    sys.exit(f"mnrh claude sessions: сессии «{prefix}» нет")


def resolve_dest(arg):
    if arg in ("root", "~"):
        return HOME
    path = os.path.abspath(os.path.expanduser(arg))
    if os.path.isdir(path):
        return path
    names = [p for p in projects() if os.path.basename(p) == arg] or \
            [p for p in projects() if os.path.basename(p).startswith(arg)]
    if len(names) == 1:
        return names[0]
    if names:
        sys.exit(f"mnrh claude sessions: «{arg}» подходит к нескольким проектам: "
                 + " ".join(os.path.basename(p) for p in names))
    sys.exit(f"mnrh claude sessions: папки «{arg}» нет")


def dump(o):
    text = json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(o, separators=(",", ":"))
    return text


def rewrite(src, dst, olds, new):
    tmp = dst + ".mnrh-tmp"
    changed = 0
    with open(src, encoding="utf-8", errors="surrogateescape", newline="") as fi, \
            open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as fo:
        for line in fi:
            if '"cwd"' in line:
                try:
                    o = json.loads(line)
                except ValueError:
                    o = None
                if isinstance(o, dict) and o.get("cwd") in olds:
                    o["cwd"] = new
                    line = dump(o) + ("\n" if line.endswith("\n") else "")
                    changed += 1
            fo.write(line)
    shutil.copystat(src, tmp)
    os.replace(tmp, dst)
    return changed


def remove_if_empty(pdir):
    try:
        if not os.listdir(pdir):
            os.rmdir(pdir)
    except OSError:
        pass


def move(s, dest):
    if s["running"]:
        sys.exit("mnrh claude sessions: сессия сейчас открыта, сначала закрой её")
    dest = os.path.abspath(dest)
    tdir = os.path.join(PROJECTS, encode(dest))
    if tdir == s["pdir"]:
        print(f"Сессия уже в {tilde(dest)}")
        return
    target = os.path.join(tdir, s["id"] + ".jsonl")
    if os.path.lexists(target):
        sys.exit(f"mnrh claude sessions: в {tilde(tdir)} уже есть сессия с таким id")
    os.makedirs(tdir, exist_ok=True)
    olds = {s["cwd"], s["started_in"]} - {None}
    changed = 0
    if s["link"]:
        os.rename(s["path"], target)
    else:
        changed = rewrite(s["path"], target, olds, dest)
        os.remove(s["path"])
    if os.path.lexists(s["extra"]):
        extra = os.path.join(tdir, s["id"])
        shutil.move(s["extra"], extra)
        if not s["link"] and os.path.isdir(extra):
            for root, _, files in os.walk(extra):
                for f in files:
                    if f.endswith(".jsonl"):
                        p = os.path.join(root, f)
                        changed += rewrite(p, p, olds, dest)
    remove_if_empty(s["pdir"])
    print(f"Перенесено: {tilde(s['cwd'])} → {tilde(dest)}" + (f", cwd переписан в {changed} строках" if changed else ""))
    print(paint(f"Продолжить: cd {tilde(dest)} && claude --resume {s['id']}", "2"))


def delete(s):
    if s["running"]:
        sys.exit("mnrh claude sessions: сессия сейчас открыта, сначала закрой её")
    for p in (s["path"], s["extra"]):
        if os.path.islink(p) or os.path.isfile(p):
            os.remove(p)
        elif os.path.isdir(p):
            shutil.rmtree(p)
    if not s["link"]:
        for sub in ("file-history", "session-env"):
            p = os.path.join(CLAUDE, sub, s["id"])
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
        forget_history(s["id"])
    claude_search.drop(s["id"])
    remove_if_empty(s["pdir"])


def forget_history(sid):
    path = os.path.join(CLAUDE, "history.jsonl")
    for _ in range(3):
        try:
            size = os.stat(path).st_size
        except OSError:
            return
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
            lines = f.readlines()
        kept = [l for l in lines if f'"sessionId":"{sid}"' not in l]
        if len(kept) == len(lines):
            return
        tmp = path + ".mnrh-tmp"
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.writelines(kept)
        shutil.copymode(path, tmp)
        if os.stat(path).st_size != size:
            os.remove(tmp)
            continue
        os.replace(tmp, path)
        return


def resume(s, mode):
    if s["running"]:
        sys.exit("mnrh claude sessions: сессия уже открыта в другом окне")
    if not os.path.isdir(s["cwd"]):
        sys.exit(f"mnrh claude sessions: папки {tilde(s['cwd'])} больше нет, перенеси сессию: "
                 f"mnrh claude sessions mv {s['id'][:8]} <папка>")
    os.chdir(s["cwd"])
    print(f"-> {tilde(s['cwd'])} · {mode} · {s['title'] or s['id']}")
    sys.stdout.flush()
    cmd = [CLAUDE_BIN, "--resume", s["id"]]
    if mode == "caffeinate":
        cmd = ["caffeinate", "-is"] + cmd
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        sys.exit(f"mnrh claude sessions: не запустился {cmd[0]}: {e}")


def clean(assume_yes):
    everything = load()
    broken, empty, orphans = [], [], []
    for d in sorted(os.listdir(PROJECTS)):
        pdir = os.path.join(PROJECTS, d)
        if not os.path.isdir(pdir) or os.path.islink(pdir):
            continue
        for f in os.listdir(pdir):
            p = os.path.join(pdir, f)
            if os.path.islink(p) and not os.path.exists(p):
                broken.append(p)
        sessions = [s for s in everything if s["pdir"] == pdir]
        cwds = collections.Counter(s["cwd"] for s in sessions if s["cwd"])
        home = cwds.most_common(1)[0][0] if cwds else decode(d)
        if home is None or not os.path.isdir(home):
            if os.listdir(pdir):
                orphans.append((pdir, home, sessions))
            continue
        for s in sessions:
            if not s["answered"] and not s["running"] and not s["link"]:
                empty.append(s)

    did = False
    if broken:
        print(f"Битые ссылки ({len(broken)}):")
        for p in broken:
            print(f"  {tilde(p)} → {os.readlink(p)}")
        if confirm("Удалить?", assume_yes):
            for p in broken:
                os.remove(p)
            did = True
        print()
    if empty:
        print(f"Сессии без единого ответа Claude ({len(empty)}, {fmt_size(sum(s['size'] for s in empty))}):")
        for s in empty:
            print(f"  {s['id'][:8]}  {row(s)}")
        if confirm("Удалить?", assume_yes):
            for s in empty:
                delete(s)
            did = True
        print()
    if orphans:
        print("Папки проектов, которых больше нет на диске:")
        for pdir, home, sessions in orphans:
            mem = " + memory" if os.path.isdir(os.path.join(pdir, "memory")) else ""
            print(f"  {tilde(home) if home else os.path.basename(pdir)}: {len(sessions)} сесс.{mem}, "
                  f"{fmt_size(tree_size(pdir))}")
        if any(s["running"] for _, _, ss in orphans for s in ss):
            print("  среди них есть открытые сессии, эти папки пропущу")
        if confirm("Удалить вместе с сессиями и памятью?", assume_yes):
            for pdir, _, sessions in orphans:
                if any(s["running"] for s in sessions):
                    continue
                for s in sessions:
                    delete(s)
                if os.path.isdir(pdir):
                    shutil.rmtree(pdir)
            did = True
        print()
    for d in os.listdir(PROJECTS):
        p = os.path.join(PROJECTS, d)
        if os.path.isdir(p) and not os.path.islink(p):
            remove_if_empty(p)
    if not (broken or empty or orphans):
        print("Чистить нечего.")
    elif did:
        print("Готово.")


def pick_dest(s):
    options = [(os.path.basename(p), p) for p in projects() if p != s["cwd"]]
    if s["cwd"] != HOME:
        options.insert(0, ("~", HOME))
    items = [(k, "~ (домашний каталог)" if p == HOME else k) for k, p in options]
    items.append(("другая", "другая папка — ввести путь"))
    i = pick(items, title=f"Куда перенести из {tilde(s['cwd'])}:", label="Куда")
    if i is None:
        return None
    if i < len(options):
        return options[i][1]
    try:
        raw = input("Путь: ").strip()
    except EOFError:
        return None
    if not raw:
        return None
    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(path):
        print(f"Папки {tilde(path)} нет")
        return None
    return path


def details(s):
    print()
    print(paint(s["title"] or "без названия", "1"))
    print(f"  id       {s['id']}")
    print(f"  папка    {tilde(s['cwd'])}" + (f"  ({s['branch']})" if s["branch"] else ""))
    print(f"  размер   {fmt_size(s['size'])}, последнее изменение {ago(s['mtime'])}")
    if s["started_in"] and s["started_in"] != s["cwd"]:
        print(f"  начата в {tilde(s['started_in'])}")
    if s["prompt"] and s["prompt"] != s["title"]:
        print(f"  запрос   {clip(s['prompt'], 100)}")
    if s["link"]:
        print(f"  ссылка на {tilde(s['target'])}")
    if s["running"]:
        print(paint(f"  открыта сейчас (pid {s['running']}), переносить и удалять нельзя", "33"))
    print()


def interactive():
    pos = 0
    while True:
        ss = load()
        if not ss:
            print("Сохранённых сессий нет.")
            return 0
        items = [(f"{folder(s['cwd'])} {s['title']} {s['id']}", row(s)) for s in ss]
        i = pick(items, title=summary(ss), default=min(pos, len(ss) - 1))
        if i is None:
            return 0
        pos = i
        s = ss[i]
        details(s)
        if s["running"]:
            actions = [("назад", "← к списку")]
        else:
            actions = [("caffeinate", "продолжить — caffeinate, Mac не уснёт"),
                       ("обычная", "продолжить — обычная сессия"),
                       ("перенести", "перенести в другую папку"),
                       ("удалить", f"удалить ({fmt_size(s['size'])})"),
                       ("назад", "← к списку")]
        a = pick(actions, title="Что сделать:", label="Действие")
        if a is None:
            return 0
        act = actions[a][0]
        if act == "caffeinate":
            resume(s, "caffeinate")
        elif act == "обычная":
            resume(s, "normal")
        elif act == "перенести":
            dest = pick_dest(s)
            if dest:
                move(s, dest)
                input(paint("Enter — к списку ", "2"))
        elif act == "удалить":
            if confirm(f"Удалить «{clip(s['title'] or s['id'], 50)}» безвозвратно?", False):
                delete(s)
                print("Удалено.")


def project_sessions(cwd):
    pdir = os.path.join(PROJECTS, encode(os.path.abspath(cwd)))
    running = running_ids()
    out, busy = [], 0
    try:
        names = os.listdir(pdir)
    except OSError:
        return out, busy
    for f in names:
        sid = f[:-6]
        path = os.path.join(pdir, f)
        if not f.endswith(".jsonl") or not UUID.match(sid) or os.path.islink(path) or not os.path.isfile(path):
            continue
        if sid in running:
            busy += 1
            continue
        if is_junk(path):
            continue
        data = claude_search.update(sid, path)
        if not data:
            continue
        out.append({"id": sid, "title": claude_search.title(sid, data), "mtime": os.stat(path).st_mtime,
                    "size": os.lstat(path).st_size + tree_size(os.path.join(pdir, sid)),
                    "msgs": sum(1 for m in data["msgs"] if m[0] in ("user", "claude"))})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out, busy


def choose_session(cwd):
    ss, busy = project_sessions(cwd)
    if not ss:
        print("new")
        return 0
    items = [("новая", "+ новая сессия")]
    for x in ss:
        items.append((x["title"], f"{ago(x['mtime']):>13}  {fmt_size(x['size']):>7}  {x['msgs']:>5} сообщ.  "
                                  f"{clip(x['title'], 60)}"))
    note = f" (ещё {busy} открыты в других окнах)" if busy else ""
    i = pick(items, title=f"Сессии в {folder(os.path.abspath(cwd))}{note}:", label="Сессия", start=0)
    if i is None:
        return 130
    print("new" if i == 0 else ss[i - 1]["id"])
    return 0


def main():
    args = sys.argv[1:]
    if has_flag(args, "-h", "--help"):
        usage()
        return 0
    if args[:1] == ["choose"] and len(args) == 2:
        return choose_session(args[1])
    if not os.path.isdir(PROJECTS):
        print(f"Нет {tilde(PROJECTS)}, сессий нет.")
        return 0
    yes = has_flag(args, "-y", "--yes")
    args = [a for a in args if a not in ("-y", "--yes")]
    cmd = args[0] if args else None

    if cmd is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return interactive()
        print_list(load())
    elif cmd in ("-l", "--list", "ls", "list"):
        print_list(load())
    elif cmd == "resume" and len(args) >= 2:
        mode = "normal" if has_flag(args, "-n", "--normal") else "caffeinate"
        resume(find(load(), args[1]), mode)
    elif cmd == "mv" and len(args) == 3:
        move(find(load(), args[1]), resolve_dest(args[2]))
    elif cmd == "rm" and len(args) >= 2:
        ss = load()
        targets = [find(ss, a) for a in args[1:]]
        for s in targets:
            print(f"  {s['id'][:8]}  {row(s)}")
        if confirm(f"Удалить {len(targets)}?", yes):
            for s in targets:
                delete(s)
            print("Удалено.")
    elif cmd == "clean":
        clean(yes)
    elif cmd == "search" and len(args) >= 2:
        rest, opts = [], {}
        i = 1
        while i < len(args):
            if args[i] in ("-p", "-d") and i + 1 < len(args):
                opts[args[i]] = args[i + 1]
                i += 2
                continue
            rest.append(args[i])
            i += 1
        days = opts.get("-d")
        if days is not None and not days.isdigit():
            sys.exit("mnrh claude sessions: после -d нужно число дней")
        text, err = claude_search.search(" ".join(rest), project=opts.get("-p"), days=int(days) if days else None,
                                         exclude=[os.environ["CLAUDE_CODE_SESSION_ID"]]
                                         if os.environ.get("CLAUDE_CODE_SESSION_ID") else None)
        print(text.replace("session_read с id и at=<номер после #>", "mnrh claude sessions show <id> <номер после #>"))
        return 1 if err else 0
    elif cmd == "show" and len(args) in (2, 3):
        at = None
        if len(args) == 3:
            try:
                at = int(args[2].lstrip("#"))
            except ValueError:
                sys.exit("mnrh claude sessions: номер сообщения — число, например 120 или -10")
        text, err = claude_search.read(args[1], at=at)
        print(text)
        return 1 if err else 0
    else:
        usage()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
