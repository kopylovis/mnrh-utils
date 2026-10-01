#!/usr/bin/env python3
import argparse
import os
import re
import select
import sys
import termios
import time
import tty

HIDE, SHOW = "\x1b[?25l", "\x1b[?25h"
UP = ("\x1b[A", "\x1bOA", "\x10")
DOWN = ("\x1b[B", "\x1bOB", "\x0e", "\t")
HOME = ("\x1b[H", "\x1bOH", "\x1b[1~")
END = ("\x1b[F", "\x1bOF", "\x1b[4~")
DIGIT_PAUSE = 0.8
ANSI = re.compile(r"\x1b\[[0-9;]*m")
FROM_RU = str.maketrans("йцукенгшщзхъфывапролджэячсмитьбю", "qwertyuiop[]asdfghjkl;'zxcvbnm,.")


class Cancel(Exception):
    pass


def cut(text, width):
    out, seen, i = [], 0, 0
    while i < len(text) and seen < width:
        m = ANSI.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        out.append(text[i])
        seen += 1
        i += 1
    return "".join(out) + ("\x1b[0m" if "\x1b[" in text else "")


class Menu:
    marks = None
    esc = "выход"

    def __init__(self, tty_fd, items, title, label, start, default):
        self.fd = tty_fd
        self.items = items
        self.title = title
        self.label = label
        self.start = start
        self.pick_able = [i for i, (key, _) in enumerate(items) if key is not None]
        default = min(max(default, 0), len(items) - 1)
        self.sel = next((i for i in self.pick_able if i >= default), self.pick_able[0])
        self.query = ""
        self.digits = ""
        self.digits_at = 0.0
        self.top = 0
        self.drawn = 0

    def write(self, s):
        os.write(self.fd, s.encode())

    def view(self):
        q = self.query.lower()
        en = q.translate(FROM_RU)  # набрали в русской раскладке: «ыыр» -> «ssh»
        hit = {i for i in self.pick_able if q in self.items[i][0].lower() or en in self.items[i][0].lower()}
        out, header = [], None
        for i, (key, _) in enumerate(self.items):
            if key is None:
                header = i
            elif i in hit:
                if header is not None and (not out or out[-1] < header):
                    out.append(header)
                out.append(i)
        return out

    def choices(self, view=None):
        return [i for i in (self.view() if view is None else view) if self.items[i][0] is not None]

    def size(self):
        try:
            cols, rows = os.get_terminal_size(self.fd)
        except OSError:
            cols, rows = 0, 0
        return rows or 24, cols or 80

    def clear(self):
        if self.drawn:
            self.write("\r" + (f"\x1b[{self.drawn - 1}A" if self.drawn > 1 else "") + "\x1b[J")
        self.drawn = 0

    def draw(self):
        rows, cols = self.size()
        view = self.view()
        lines = [self.title] if self.title else []
        room = max(3, rows - len(lines) - 3)
        if self.sel in view:
            pos = view.index(self.sel)
            if pos < self.top:
                self.top = pos
            elif pos >= self.top + room:
                self.top = pos - room + 1
        self.top = max(0, min(self.top, max(0, len(view) - room)))
        shown = view[self.top:self.top + room]
        if self.top:
            lines.append(f"\x1b[2m     ↑ ещё {self.top}\x1b[0m")
        for i in shown:
            if self.items[i][0] is None:
                lines.append(cut(f"  \x1b[1;35m{self.items[i][1]}\x1b[0m", cols - 1))
                continue
            box = "" if self.marks is None else ("◉ " if i in self.marks else "○ ")
            text = f"{self.pick_able.index(i) + self.start:>3}) {box}{self.items[i][1]}"
            if i == self.sel:
                lines.append(f"\x1b[1;36m❯ {ANSI.sub('', text)[: cols - 3]}\x1b[0m")
            else:
                lines.append("  " + cut(text, cols - 3))
        rest = len(view) - self.top - len(shown)
        if rest > 0:
            lines.append(f"\x1b[2m     ↓ ещё {rest}\x1b[0m")
        if not self.choices(view):
            lines.append("\x1b[2m  ничего не найдено\x1b[0m")
        hint = f"↑↓ выбрать · Enter · цифра или буквы — быстрый переход · Esc — {self.esc}"
        if self.marks is not None:
            total = self.summary(self.marks) if self.summary else f"отмечено {len(self.marks)}"
            hint = f"{total} · Пробел — отметить · a — все/ничего · Enter — дальше · Esc — отмена"
        if self.query:
            hint = f"поиск: {self.query}▏ · Backspace стереть · Esc сбросить"
        lines.append("")
        lines.append(f"\x1b[2m{hint[: cols - 1]}\x1b[0m")
        self.clear()
        self.write("\r\n".join(lines))
        self.drawn = len(lines)

    def move(self, step):
        view = self.choices()
        if not view:
            return
        pos = view.index(self.sel) if self.sel in view else 0
        self.sel = view[(pos + step) % len(view)]

    def jump(self, ch):
        now = time.monotonic()
        buf = self.digits + ch if now - self.digits_at < DIGIT_PAUSE else ch
        if int(buf) - self.start >= len(self.pick_able):
            buf = ch
        self.digits, self.digits_at = buf, now
        idx = int(buf) - self.start
        if 0 <= idx < len(self.pick_able):
            self.sel = self.pick_able[idx]

    def refilter(self):
        view = self.choices()
        if view and self.sel not in view:
            self.sel = view[0]
        self.top = 0

    def read_key(self):
        data = os.read(self.fd, 64)
        if data == b"\x1b" and select.select([self.fd], [], [], 0.05)[0]:
            data += os.read(self.fd, 64)
        return data.decode("utf-8", "ignore")

    def handle(self, key):
        if key in UP:
            self.move(-1)
        elif key in DOWN:
            self.move(1)
        elif key in HOME:
            view = self.choices()
            if view:
                self.sel = view[0]
        elif key in END:
            view = self.choices()
            if view:
                self.sel = view[-1]
        elif key in ("\r", "\n"):
            return self.marks is not None or self.sel in self.choices()
        elif key == " " and self.marks is not None:
            self.marks ^= {self.sel}
            self.move(1)
        elif key == "a" and self.marks is not None and not self.query:
            self.marks = set() if self.marks >= set(self.pick_able) else set(self.pick_able)
        elif key in ("\x7f", "\x08"):
            self.query = self.query[:-1]
            self.refilter()
        elif key == "\x1b":
            if not self.query:
                raise Cancel
            self.query = ""
            self.refilter()
        elif key in ("\x03", "\x04"):
            raise Cancel
        elif key.startswith("\x1b"):
            pass
        else:
            for ch in key:
                if not ch.isprintable():
                    continue
                if ch.isdigit() and not self.query:
                    self.jump(ch)
                else:
                    self.query += ch
                    self.refilter()
        return False

    summary = None

    def run(self):
        self.write(HIDE)
        self.draw()
        while True:
            if self.handle(self.read_key()):
                return self.sel if self.marks is None else set(self.marks)
            self.draw()


def pick_many(items, title="", marked=(), summary=None):
    return pick(items, title=title, marked=set(marked), summary=summary)


def pick(items, title="", label="", start=1, default=0, marked=None, summary=None, esc=None):
    if not items or all(key is None for key, _ in items):
        return None
    fd = os.open("/dev/tty", os.O_RDWR)
    saved = termios.tcgetattr(fd)
    menu = Menu(fd, items, title, label, start, default)
    if esc:
        menu.esc = esc
    if marked is not None:
        menu.marks = set(marked)
        menu.summary = summary
    try:
        tty.setcbreak(fd)
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ISIG
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        choice = menu.run()
    except Cancel:
        choice = None
    finally:
        menu.clear()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        menu.write(SHOW)
    if choice is not None and label and marked is None:
        menu.write(f"{label}: \x1b[1m{items[choice][0]}\x1b[0m\r\n")
    os.close(fd)
    return choice


def choice(question, options, default=0, back=None, esc="назад"):
    fd = os.open("/dev/tty", os.O_RDWR)
    saved = termios.tcgetattr(fd)
    sel = min(max(default, 0), len(options) - 1)
    keys = {}
    for i, opt in enumerate(options):
        for k in opt[2] if len(opt) > 2 else ():
            keys[k] = i

    def draw():
        parts = []
        for i, opt in enumerate(options):
            label = f" {opt[1]} "
            parts.append(f"\x1b[1;30;46m{label}\x1b[0m" if i == sel else f"\x1b[2m{label}\x1b[0m")
        hint = f"\x1b[2m  ←→ · Enter · Esc — {esc}\x1b[0m"
        os.write(fd, f"\r\x1b[K\x1b[1m{question}\x1b[0m  {'  '.join(parts)}{hint}".encode())

    result = back
    try:
        tty.setcbreak(fd)
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ISIG
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        os.write(fd, HIDE.encode())
        draw()
        while True:
            data = os.read(fd, 16)
            if data == b"\x1b" and select.select([fd], [], [], 0.05)[0]:
                data += os.read(fd, 16)
            key = data.decode("utf-8", "ignore")
            if key in ("\x1b[D", "\x1bOD"):
                sel = (sel - 1) % len(options)
            elif key in ("\x1b[C", "\x1bOC", "\t"):
                sel = (sel + 1) % len(options)
            elif key in ("\r", "\n"):
                result = options[sel][0]
                break
            elif key in ("\x1b", "\x03", "\x04"):
                result = back
                break
            elif key.lower() in keys:
                result = options[keys[key.lower()]][0]
                break
            draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        label = next((o[1] for o in options if o[0] == result), "")
        os.write(fd, f"\r\x1b[K\x1b[1m{question}\x1b[0m  {label}\r\n{SHOW}".encode())
        os.close(fd)
    return result


def main():
    p = argparse.ArgumentParser(description="меню со стрелками; пункты «ключ<TAB>текст» из stdin, ответ — индекс с 0")
    p.add_argument("--title", default="")
    p.add_argument("--label", default="")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--default", type=int, default=0)
    a = p.parse_args()

    items = []
    for line in sys.stdin.read().splitlines():
        if not line.strip():
            continue
        key, _, text = line.partition("\t")
        items.append((key, text or key))
    if not items:
        return 1
    choice = pick(items, a.title, a.label, a.start, a.default)
    if choice is None:
        return 130
    print(choice)
    return 0


if __name__ == "__main__":
    sys.exit(main())
