"""Список команд по группам и меню mnrh (share/mnrh/commands.tsv).

launcher.py help <реестр> "<команды>" <версия> [all]  — список по группам
launcher.py menu <реестр> "<команды>" <версия>        — меню; выбор печатает аргументы в stdout
"""
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from menu import pick

TTY = sys.stdout.isatty()


def bold(text):
    return f"\033[1m{text}\033[0m" if TTY else text


def load(registry, have):
    """[(группа, имя, описание, [(аргументы, что делает)])] — только команды, которые есть."""
    have = have.split()
    items, seen = [], set()
    with open(registry, encoding="utf-8") as f:
        for line in f:
            cols = line.rstrip("\n").split("\t")
            if line.startswith("#") or len(cols) < 3 or cols[1] not in have:
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
    print(f"mnrh {version} — утилиты для обслуживания Mac\n")
    groups = list(dict.fromkeys(g for g, *_ in items))
    width = max(len(g) for g in groups)
    for g in groups:
        if full:
            print(bold(g))
            for gg, n, desc, _ in items:
                if gg == g:
                    print(f"  mnrh {n:<12} {desc}")
            print()
        else:
            print(f"  {bold(g.ljust(width))}   {' · '.join(n for gg, n, *_ in items if gg == g)}")
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


def menu(items, version):
    width = max(len(n) for _, n, *_ in items) + 2
    gwidth = max(len(g) for g, *_ in items) + 2
    rows, group = [], None
    for g, name, desc, _ in items:
        # группа видна у первой команды группы: список читается блоками
        rows.append((f"{name} {desc} {g}", f"{(g if g != group else '').ljust(gwidth)}{name.ljust(width)}{desc}"))
        group = g
    at = 0
    while True:
        i = pick(rows, title=f"\x1b[1mmnrh {version}\x1b[0m — что сделать?", default=at)
        if i is None:
            return None
        at = i
        _, name, _, actions = items[i]
        if len(actions) == 1:
            action = actions[0][0]
        else:
            aw = max(len(f"mnrh {name} {a}".rstrip()) for a, _ in actions) + 3
            j = pick([(f"{a} {label}", f"{f'mnrh {name} {a}'.rstrip().ljust(aw)}{label}")
                      for a, label in actions], title=f"\x1b[1mmnrh {name}\x1b[0m   Esc — назад")
            if j is None:
                continue
            action = actions[j][0]
        try:
            rest = ask(action)
        except (KeyboardInterrupt, EOFError):
            return None
        if rest is not None:
            return [name] + rest


def main():
    mode, registry, have, version = sys.argv[1:5]
    items = load(registry, have)
    if mode == "help":
        show_help(items, version, len(sys.argv) > 5 and sys.argv[5] == "all")
        return 0
    choice = menu(items, version)
    if not choice:
        return 1
    print(" ".join(shlex.quote(a) for a in choice))
    return 0


sys.exit(main())
