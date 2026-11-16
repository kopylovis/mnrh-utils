"""Список команд по группам и меню mnrh (share/mnrh/commands.tsv).

launcher.py help <реестр> "<команды>" <версия> [all]  — список по группам
launcher.py menu <реестр> "<команды>" <версия>        — меню; выбор печатает аргументы в stdout
launcher.py actions <реестр> <команда> [позиция]     — меню действий одной команды; печатает «позиция\tаргументы»,
                                                       код 3 — у команды меньше двух действий, меню не нужно
"""
import os
import shlex
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import menu as menu_lib
from menu import Quit, Screen, width

menu_lib.BRAND = "mnrh"

TTY = sys.stdout.isatty()


def bold(text):
    return f"\033[1m{text}\033[0m" if TTY else text


def title(text):
    return f"\033[1;35m{text}\033[0m" if TTY else text


def subtitle(text):
    return f"\033[1;34m{text}\033[0m" if TTY else text


def load(registry, have):
    """[(группа, имя, описание, [(аргументы, что делает)])] — только команды, которые есть."""
    have = have.split()
    items, seen = [], set()
    with open(registry, encoding="utf-8") as f:
        for line in f:
            cols = line.rstrip("\n").split("\t")
            if line.startswith("#") or len(cols) < 3 or cols[1] not in have:
                continue
            seen.add(cols[1])
            if cols[0] == "-":
                continue
            actions = []
            for part in (cols[3].split(";;") if len(cols) > 3 and cols[3] else []):
                a, _, label = part.partition("|")
                actions.append((a.strip(), label.strip()))
            items.append((cols[0], cols[1], cols[2], actions or [("", cols[2])]))
            seen.add(cols[1])
    for name in have:
        if name not in seen:
            items.append(("Другое", name, "", [("", "")]))
    return items


def show_help(items, version, full):
    print(title(f"mnrh {version}") + " — утилиты для обслуживания Mac\n")
    groups = list(dict.fromkeys(g for g, *_ in items))
    width = max(len(g) for g in groups)
    for g in groups:
        if full:
            print(subtitle(g))
            for gg, n, desc, _ in items:
                if gg == g:
                    print(f"  mnrh {n:<12} {desc}")
            print()
        else:
            print(f"  {subtitle(g.ljust(width))}   {' · '.join(n for gg, n, *_ in items if gg == g)}")
    if not full:
        print()
    print("  mnrh                 меню: стрелки, поиск набором, Enter — выбрать")
    print("  mnrh <команда>       без аргументов — меню действий этой команды")
    print("  mnrh help -a         все команды с описаниями")
    print("  mnrh <команда> -h    справка по команде")


def ask(action, screen=None, title="", note=()):
    """«kill <порт>» -> спросить порт. На полноэкранном меню строка ввода в той же рамке, без него вопрос идет
    через терминал: stdout занят выбором. Пустой ответ или Esc: None."""
    if "<" not in action:
        return shlex.split(action)
    if screen is None:
        return prompt_tty(action)
    out = []
    for p in shlex.split(action.replace("<", "'<").replace(">", ">'")):
        if p.startswith("<") and p.endswith(">"):
            value = screen.ask(p[1:-1], title=title, note=note)
            if not value or not value.strip():
                return None
            out += shlex.split(value)
        else:
            out.append(p)
    return out


def prompt_tty(action):
    with open("/dev/tty", "w") as w, open("/dev/tty") as r:
        return prompt(action, w, r)


def prompt(action, tty, answers):
    out = []
    for p in shlex.split(action.replace("<", "'<").replace(">", ">'")):
        if p.startswith("<") and p.endswith(">"):
            tty.write(f"{p[1:-1]}: ")
            tty.flush()
            value = answers.readline().strip()
            if not value:
                return None
            out += shlex.split(value)
        else:
            out.append(p)
    return out


def dim(text):
    return f"\x1b[2m{text}\x1b[0m"


# значки только шириной в две клетки и без VS16, иначе едут рамки меню
ICONS = {"claudemeister": "🤖", "repos": "📚", "deps": "📦", "ssh": "🔑", "alert": "📣", "init": "🧰", "ram": "🧠",
         "disk": "💽", "gradle": "🐘", "kotlin": "🟣", "build-tools": "🔨", "sim": "📱", "killdaemons": "💀",
         "doctor": "🏥", "fix": "🔧", "path": "🧭", "outdated": "⏰", "login": "🚪", "sleep": "😴", "scroll": "🐭",
         "nomusic": "🔇", "tossling": "📋", "uninstall": "🧹", "ports": "🔌", "todo": "📝", "shot": "📸"}


def icon(name):
    mark = ICONS.get(name, "")
    return mark if width(mark) == 2 else "  "


def command_info(name, desc, actions):
    """Правая панель главного меню: что делает команда и какие у нее действия."""
    lines = [f"\x1b[1mmnrh {name}\x1b[0m", ""]
    if desc:
        lines += [desc, ""]
    if len(actions) > 1:
        lines.append(dim("ДЕЙСТВИЯ, ENTER ОТКРОЕТ СПИСОК"))
        for a, label in actions:
            lines += [label, dim(f"  mnrh {name} {a}".rstrip())]
    else:
        lines += [dim("ENTER ЗАПУСТИТ"), dim(f"mnrh {name} {actions[0][0]}".rstrip())]
    return lines


def action_info(name, desc, action):
    a, label = action
    lines = [f"\x1b[1m{label}\x1b[0m", "", dim(f"$ mnrh {name} {a}".rstrip())]
    asks = [p[1:-1] for p in shlex.split(a.replace("<", "'<").replace(">", ">'")) if p.startswith("<")]
    if asks:
        lines += ["", "Спросит " + ", ".join(asks) + " перед запуском."]
    if desc:
        lines += ["", dim(f"MNRH {name.upper()}"), desc]
    return lines


def pick_action(screen, name, desc, actions, back, at=0):
    """Действия одной команды на экране screen. Ответ: (позиция, аргументы) или None по Esc."""
    wide = max(len(l) for _, l in actions) + 3
    rows = [(f"{a} {label}", f"{label.ljust(wide)}{dim(f'mnrh {name} {a}'.rstrip())}") for a, label in actions]
    while True:
        at = screen.pick(rows, title=f"mnrh {name}", default=at, numbers=False, back=back,
                         describe=lambda i, w: action_info(name, desc, actions[i]))
        if at is None:
            return None
        a, label = actions[at]
        rest = ask(a, screen, title=f"mnrh {name}", note=[f"\x1b[1m{label}\x1b[0m", dim(f"mnrh {name} {a}")])
        if rest is not None:
            return at, rest


def menu(items, version, at=0):
    rows, index, group = [], [], None
    for k, (g, name, desc, _) in enumerate(items):
        if g != group:
            rows.append((None, g))
            index.append(None)
            group = g
        rows.append((f"{name} {desc} {g}", f"{icon(name)} {name}"))
        index.append(k)

    def describe(i, w):
        _, name, desc, actions = items[index[i]]
        return command_info(name, desc, actions)

    screen = Screen()
    try:
        while True:
            i = screen.pick(rows, title="Команды", default=at, numbers=False, back="выход", describe=describe)
            if i is None:
                return None
            at = i
            _, name, desc, actions = items[index[i]]
            if len(actions) == 1:
                rest = ask(actions[0][0], screen, title=f"mnrh {name}", note=[desc])
            else:
                got = pick_action(screen, name, desc, actions, back="назад")
                rest = None if got is None else got[1]
            if rest is not None:
                return at, [name] + rest
    except (Quit, KeyboardInterrupt, EOFError):
        return None
    finally:
        screen.close()


def main():
    if sys.argv[1:2] == ["after"]:
        return after(sys.argv[2] if len(sys.argv) > 2 else "0")
    if sys.argv[1:2] == ["actions"]:
        return actions(*sys.argv[2:5])
    mode, registry, have, version = sys.argv[1:5]
    items = load(registry, have)
    if mode == "help":
        show_help(items, version, len(sys.argv) > 5 and sys.argv[5] == "all")
        return 0
    try:
        at = int(sys.argv[5]) if len(sys.argv) > 5 else 0
    except ValueError:
        at = 0
    picked = menu(items, version, at)
    if not picked:
        return 1
    at, choice = picked
    print(f"{at}\t" + " ".join(shlex.quote(a) for a in choice))
    return 0


def actions(registry, name, at="0"):
    found = [(d, a) for _, n, d, a in load(registry, name) if n == name]
    if not found or len(found[0][1]) < 2:
        return 3
    desc, acts = found[0]
    try:
        at = int(at)
    except ValueError:
        at = 0
    screen = Screen()
    try:
        got = pick_action(screen, name, desc, acts, back="выход", at=at)
    except (Quit, KeyboardInterrupt, EOFError):
        got = None
    finally:
        screen.close()
    if got is None:
        return 1
    print(f"{got[0]}\t" + " ".join(shlex.quote(a) for a in got[1]))
    return 0


def restart_pending():
    try:
        job = os.path.join(os.path.expanduser("~"), ".cache", "mnrh", "restart", os.path.basename(os.ttyname(0)))
        return time.time() - os.path.getmtime(job) < 120
    except OSError:
        return False


def after(rc):
    if restart_pending():
        return 1
    if rc == "130":
        return 0
    from menu import choice
    head = "\x1b[32m✓ Готово\x1b[0m" if rc in ("0", "") else f"\x1b[33m! Завершилось с кодом {rc}\x1b[0m"
    try:
        print()
        answer = choice(head, [("menu", "↩ В меню", ""), ("quit", "Выйти", "qй")], default=0, back="quit", esc="выход")
    except OSError:
        return 1
    return 0 if answer == "menu" else 1


sys.exit(main())
