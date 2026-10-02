"""Список команд по группам и меню mnrh (share/mnrh/commands.tsv).

launcher.py help <реестр> "<команды>" <версия> [all]  — список по группам
launcher.py menu <реестр> "<команды>" <версия>        — меню; выбор печатает аргументы в stdout
launcher.py actions <реестр> <команда>                — меню действий одной команды; печатает её аргументы
"""
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from menu import pick

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
    print("  mnrh help -a         все команды с описаниями")
    print("  mnrh <команда> -h    справка по команде")


def ask(action):
    """«kill <порт>» -> спросить порт. Вопрос и ответ идут через терминал: stdout занят выбором."""
    if "<" not in action:
        return shlex.split(action)
    return prompt_tty(action)


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


def pick_action(name, actions, esc):
    width = max(len(l) for _, l in actions) + 3
    j = pick([(f"{a} {label}", f"{label.ljust(width)}{dim(f'mnrh {name} {a}'.rstrip())}") for a, label in actions],
             title=f"\x1b[1;35mmnrh {name}\x1b[0m", esc=esc)
    return None if j is None else actions[j][0]


def menu(items, version, at=0):
    width = max(len(n) for _, n, *_ in items) + 2
    rows, index, group = [], [], None
    for k, (g, name, desc, _) in enumerate(items):
        if g != group:
            rows.append((None, g))
            index.append(None)
            group = g
        rows.append((f"{name} {desc} {g}", f"{name.ljust(width)}{dim(desc)}"))
        index.append(k)
    while True:
        i = pick(rows, title=f"\x1b[1;35mmnrh {version}\x1b[0m  {dim('что сделать?')}", default=at)
        if i is None:
            return None
        at = i
        _, name, _, actions = items[index[i]]
        action = actions[0][0] if len(actions) == 1 else pick_action(name, actions, esc="назад")
        if action is None:
            continue
        try:
            rest = ask(action)
        except (KeyboardInterrupt, EOFError):
            return None
        if rest is not None:
            return at, [name] + rest


def main():
    if sys.argv[1:2] == ["after"]:
        return after(sys.argv[2] if len(sys.argv) > 2 else "0")
    if sys.argv[1:2] == ["actions"]:
        return actions(*sys.argv[2:4])
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


def actions(registry, name):
    found = [a for _, n, _, a in load(registry, name) if n == name]
    if not found:
        return 1
    action = pick_action(name, found[0], esc="выход")
    if action is None:
        return 1
    try:
        rest = ask(action)
    except (KeyboardInterrupt, EOFError):
        return 1
    if rest is None:
        return 1
    print(" ".join(shlex.quote(a) for a in rest))
    return 0


def after(rc):
    from menu import choice
    head = "\x1b[32m✓ Готово\x1b[0m" if rc in ("0", "") else f"\x1b[33m! Завершилось с кодом {rc}\x1b[0m"
    try:
        print()
        answer = choice(head, [("menu", "↩ В меню", ""), ("quit", "Выйти", "qй")], default=0, back="quit", esc="выход")
    except OSError:
        return 1
    return 0 if answer == "menu" else 1


sys.exit(main())
