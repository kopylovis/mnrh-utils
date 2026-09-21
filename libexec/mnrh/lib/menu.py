#!/usr/bin/env python3
import argparse
import os
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


class Cancel(Exception):
    pass


class Menu:
    def __init__(self, tty_fd, items, title, label, start, default):
        self.fd = tty_fd
        self.items = items
        self.title = title
        self.label = label
        self.start = start
        self.sel = min(max(default, 0), len(items) - 1)
        self.query = ""
        self.digits = ""
        self.digits_at = 0.0
        self.top = 0
        self.drawn = 0

    def write(self, s):
        os.write(self.fd, s.encode())

    def view(self):
        q = self.query.lower()
        return [i for i, (key, _) in enumerate(self.items) if q in key.lower()]

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
            text = f"{i + self.start:>3}) {self.items[i][1]}"[: cols - 3]
            if i == self.sel:
                lines.append(f"\x1b[1;36m❯ {text}\x1b[0m")
            else:
                lines.append(f"  {text}")
        rest = len(view) - self.top - len(shown)
        if rest > 0:
            lines.append(f"\x1b[2m     ↓ ещё {rest}\x1b[0m")
        if not view:
            lines.append("\x1b[2m  ничего не найдено\x1b[0m")
        hint = "↑↓ выбрать · Enter · цифра или буквы — быстрый переход · Esc — выход"
        if self.query:
            hint = f"поиск: {self.query}▏ · Backspace стереть · Esc сбросить"
        lines.append("")
        lines.append(f"\x1b[2m{hint[: cols - 1]}\x1b[0m")
        self.clear()
        self.write("\r\n".join(lines))
        self.drawn = len(lines)

    def move(self, step):
        view = self.view()
        if not view:
            return
        pos = view.index(self.sel) if self.sel in view else 0
        self.sel = view[(pos + step) % len(view)]

    def jump(self, ch):
        now = time.monotonic()
        buf = self.digits + ch if now - self.digits_at < DIGIT_PAUSE else ch
        if int(buf) - self.start >= len(self.items):
            buf = ch
        self.digits, self.digits_at = buf, now
        idx = int(buf) - self.start
        if 0 <= idx < len(self.items):
            self.sel = idx

    def refilter(self):
        view = self.view()
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
            view = self.view()
            if view:
                self.sel = view[0]
        elif key in END:
            view = self.view()
            if view:
                self.sel = view[-1]
        elif key in ("\r", "\n"):
            return self.sel in self.view()
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

    def run(self):
        self.write(HIDE)
        self.draw()
        while True:
            if self.handle(self.read_key()):
                return self.sel
            self.draw()


def pick(items, title="", label="", start=1, default=0):
    if not items:
        return None
    fd = os.open("/dev/tty", os.O_RDWR)
    saved = termios.tcgetattr(fd)
    menu = Menu(fd, items, title, label, start, default)
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
    if choice is not None and label:
        menu.write(f"{label}: \x1b[1m{items[choice][0]}\x1b[0m\r\n")
    os.close(fd)
    return choice


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
