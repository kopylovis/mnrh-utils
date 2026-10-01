import glob
import os
import re
import time

from mnrhlib import CONFIG, HOME, find_project, tilde

TODO_DIR = os.path.join(os.path.dirname(CONFIG), "todo")
ITEM = re.compile(r"^- \[( |x)\] (\d{4}-\d{2}-\d{2})(?: → (\d{4}-\d{2}-\d{2}))? (.*)$")


def key_of(root):
    return "home" if root.rstrip("/") == HOME else os.path.basename(root.rstrip("/")) or "root"


def path_of(root):
    return os.path.join(TODO_DIR, key_of(root) + ".md")


def load(root):
    items = []
    try:
        with open(path_of(root), encoding="utf-8") as f:
            for line in f:
                m = ITEM.match(line.rstrip("\n"))
                if m:
                    items.append({"done": m.group(1) == "x", "added": m.group(2), "closed": m.group(3),
                                  "text": m.group(4)})
    except OSError:
        pass
    return items


def save(root, items):
    os.makedirs(TODO_DIR, exist_ok=True)
    lines = [f"# {key_of(root)} ({tilde(root)})", ""]
    for it in items:
        closed = f" → {it['closed']}" if it.get("closed") else ""
        lines.append(f"- [{'x' if it['done'] else ' '}] {it['added']}{closed} {it['text']}")
    tmp = path_of(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path_of(root))


def open_items(root):
    return [it for it in load(root) if not it["done"]]


def fmt(root, items=None, title=True):
    items = open_items(root) if items is None else items
    head = f"Бэклог {key_of(root)}: " + (f"{len(items)}" if items else "пусто")
    lines = [head] if title else []
    lines += [f"  {i}. {it['text']}  ({it['added']})" for i, it in enumerate(items, 1)]
    return "\n".join(lines)


def add(root, text):
    text = " ".join(text.split())
    if not text:
        return "Пустая задача, нечего добавлять.", True
    items = load(root)
    if any(not it["done"] and it["text"].lower() == text.lower() for it in items):
        return f"Уже есть в бэклоге {key_of(root)}: {text}", False
    items.append({"done": False, "added": time.strftime("%Y-%m-%d"), "closed": None, "text": text})
    save(root, items)
    return f"Добавил в бэклог {key_of(root)} (№{len([i for i in items if not i['done']])}): {text}", False


def pick(root, ref):
    opened = open_items(root)
    ref = str(ref).strip()
    if ref.isdigit() and 1 <= int(ref) <= len(opened):
        return opened[int(ref) - 1], None
    hits = [it for it in opened if ref.lower() in it["text"].lower()] if ref else []
    if len(hits) == 1:
        return hits[0], None
    if hits:
        return None, f"«{ref}» подходит к нескольким задачам, дай номер:\n{fmt(root, hits, title=False)}"
    return None, f"Нет открытой задачи «{ref}».\n{fmt(root)}"


def done(root, ref):
    it, err = pick(root, ref)
    if err:
        return err, True
    items = load(root)
    for x in items:
        if not x["done"] and x["text"] == it["text"] and x["added"] == it["added"]:
            x.update(done=True, closed=time.strftime("%Y-%m-%d"))
            break
    save(root, items)
    return f"Готово: {it['text']}\n{fmt(root)}", False


def remove(root, ref):
    it, err = pick(root, ref)
    if err:
        return err, True
    items = [x for x in load(root) if not (not x["done"] and x["text"] == it["text"] and x["added"] == it["added"])]
    save(root, items)
    return f"Удалил: {it['text']}\n{fmt(root)}", False


def everything():
    lines = []
    for f in sorted(glob.glob(os.path.join(glob.escape(TODO_DIR), "*.md"))):
        name = os.path.basename(f)[:-3]
        with open(f, encoding="utf-8") as fh:
            opened = [ITEM.match(l.rstrip("\n")) for l in fh]
        opened = [m.group(4) for m in opened if m and m.group(1) == " "]
        if opened:
            lines.append(f"{name}: {len(opened)}")
            lines += [f"  {i}. {t}" for i, t in enumerate(opened, 1)]
    return "\n".join(lines) or "Бэклоги пусты.", False


def root_for(project, cwd=None):
    root, err = find_project(project or None, cwd)
    return root, err


LIST_TOOL = {
    "name": "todo_list",
    "description": ("Бэклог проекта: задачи «на потом», которые живут между сессиями (в отличие от списка задач "
                    "внутри сессии). Без project — проект текущей сессии; all=true — открытые задачи всех проектов."),
    "annotations": {"readOnlyHint": True},
    "inputSchema": {"type": "object", "properties": {
        "project": {"type": "string", "description": "имя папки проекта или путь"},
        "all": {"type": "boolean", "description": "все проекты"}}},
}
ADD_TOOL = {
    "name": "todo_add",
    "description": ("Добавить задачу в бэклог проекта, когда пользователь просит записать, запомнить или отложить "
                    "что-то на потом. Одна задача — одна короткая строка, понятная без контекста этой сессии."),
    "inputSchema": {"type": "object", "properties": {
        "text": {"type": "string"}, "project": {"type": "string"}}, "required": ["text"]},
}
DONE_TOOL = {
    "name": "todo_done",
    "description": ("Отметить задачу бэклога сделанной (или remove=true — удалить без отметки): номер из todo_list "
                    "или кусок текста. Вызывай, когда задача из бэклога действительно сделана или пользователь просит."),
    "inputSchema": {"type": "object", "properties": {
        "item": {"type": "string", "description": "номер или кусок текста"},
        "project": {"type": "string"}, "remove": {"type": "boolean"}}, "required": ["item"]},
}
TOOLS = [LIST_TOOL, ADD_TOOL, DONE_TOOL]


def handle(name, a, cwd=None):
    if name == "todo_list" and a.get("all"):
        return everything()
    root, err = root_for(a.get("project"), cwd)
    if err:
        return err, True
    if name == "todo_list":
        return fmt(root), False
    if name == "todo_add":
        return add(root, str(a.get("text") or ""))
    return (remove if a.get("remove") else done)(root, a.get("item", ""))


def usage():
    print("mnrh todo                      бэклог этого проекта (папка git, где ты сейчас)")
    print("mnrh todo <текст>              добавить задачу; то же: mnrh todo add <текст>")
    print("mnrh todo done <N|текст>       отметить сделанной")
    print("mnrh todo rm <N|текст>         удалить")
    print("mnrh todo all                  открытые задачи всех проектов")
    print("mnrh todo -p <проект> …        для другого проекта")
    print()
    print(f"Хранится в {tilde(TODO_DIR)}/<проект>.md. В Claude Code: /todo, инструменты todo_list, todo_add,")
    print("todo_done, а при старте сессии Claude видит открытые задачи проекта.")


def cli(args):
    if args[:1] in (["-h"], ["--help"]):
        usage()
        return 0
    project = None
    if "-p" in args:
        i = args.index("-p")
        project = args[i + 1] if i + 1 < len(args) else None
        args = args[:i] + args[i + 2:]
    if args[:1] == ["all"]:
        print(everything()[0])
        return 0
    root, err = root_for(project)
    if err:
        print(f"mnrh todo: {err}")
        return 1
    if not args or args[:1] == ["list"]:
        text, bad = fmt(root), False
    elif args[0] in ("done", "rm") and len(args) > 1:
        text, bad = (done if args[0] == "done" else remove)(root, " ".join(args[1:]))
    else:
        text, bad = add(root, " ".join(args[1:] if args[0] == "add" else args))
    print(text)
    return 1 if bad else 0
