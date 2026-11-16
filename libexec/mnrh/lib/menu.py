#!/usr/bin/env python3
import argparse
import os
import re
import select
import sys
import termios
import textwrap
import time
import tty
import unicodedata

HIDE, SHOW = "\x1b[?25l", "\x1b[?25h"
TITLE, SUBTITLE = "1;35", "1;34"
UP = ("\x1b[A", "\x1bOA", "\x10")
DOWN = ("\x1b[B", "\x1bOB", "\x0e", "\t")
HOME = ("\x1b[H", "\x1bOH", "\x1b[1~")
END = ("\x1b[F", "\x1bOF", "\x1b[4~")
DIGIT_PAUSE = 0.8
ANSI = re.compile(r"\x1b\[[0-9;]*m")
KEY = re.compile(r"\x1b\[[0-9;]*[A-Za-z~]|\x1bO[A-Za-z]|[\x00-\x1f\x7f]|[^\x00-\x1f\x7f]+")
FROM_RU = str.maketrans("йцукенгшщзхъфывапролджэячсмитьбю", "qwertyuiop[]asdfghjkl;'zxcvbnm,.")


class Cancel(Exception):
    pass


class Quit(Cancel):
    """Ctrl+C: выйти совсем, а не на шаг назад, как Esc."""


# нажатия, пришедшие одной пачкой после Enter или Esc: их получит следующее меню на том же экране
PENDING = []
UNFINISHED = re.compile(rb"\x1b(\[[0-9;]*|O)?$")


def read_keys(fd):
    """Прочитать нажатия. Стрелка (\x1bOB) может прийти в два чтения: без дочитывания меню увидело бы Esc и букву."""
    data = os.read(fd, 64)
    while UNFINISHED.search(data) and select.select([fd], [], [], 0.05)[0]:
        more = os.read(fd, 64)
        if not more:
            break
        data += more
    return data.decode("utf-8", "ignore")


def split_keys(data):
    """Быстрые нажатия приходят одним чтением: «\x1b[B\x1b[B» это две стрелки вниз, а не неизвестная клавиша."""
    return KEY.findall(data) or [data]


def tr(ru, en):
    """Строка на языке интерфейса claudemeister; без mnrhlib рядом по-русски."""
    try:
        from mnrhlib import tr as translate
        return translate(ru, en)
    except Exception:
        return ru


def write_all(fd, text):
    """Записать все: кадр с картинками весит десятки килобайт, и os.write может взять только часть."""
    data = text.encode()
    while data:
        data = data[os.write(fd, data):]


def cells(ch):
    """Сколько клеток терминала занимает символ: эмодзи и иероглифы две, модификаторы ноль."""
    if unicodedata.combining(ch) or ch in "\u200d\ufe0e\ufe0f":
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def width(text):
    return sum(cells(ch) for ch in ANSI.sub("", text))


def cut(text, width):
    out, seen, i = [], 0, 0
    while i < len(text):
        m = ANSI.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        w = cells(text[i])
        if seen + w > width:
            break
        out.append(text[i])
        seen += w
        i += 1
    return "".join(out) + ("\x1b[0m" if "\x1b[" in text else "")


def fit(text, size):
    """Обрезать по ширине и добить пробелами до нее же: строка ровно size клеток."""
    text = cut(text, size)
    return text + " " * max(0, size - width(text))


class Menu:
    marks = None
    esc = None
    numbers = True

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
        write_all(self.fd, s)

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
        title = self.title if not self.title or "\x1b[" in self.title else f"\x1b[{TITLE}m{self.title}\x1b[0m"
        lines = [title] if self.title else []
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
            lines.append(f"\x1b[2m     ↑ {tr('ещё', 'more')} {self.top}\x1b[0m")
        for i in shown:
            if self.items[i][0] is None:
                lines.append(cut(f"  \x1b[{SUBTITLE}m{self.items[i][1]}\x1b[0m", cols - 1))
                continue
            num = f"{self.pick_able.index(i) + self.start:>3}) "
            on = self.marks is not None and i in self.marks
            box = "" if self.marks is None else ("◉ " if on else "○ ")
            body = self.items[i][1]
            if i == self.sel:
                plain = ANSI.sub("", body)[: max(0, cols - 3 - len(num) - len(box))]
                mark = f"\x1b[1;32m{box}\x1b[0;1;36m" if on else box
                lines.append(f"\x1b[1;36m❯ {num}{mark}{plain}\x1b[0m")
            elif on:
                lines.append("  " + cut(f"{num}\x1b[32m{box}{body}", cols - 3))
            else:
                lines.append("  " + cut(f"{num}{box}{body}", cols - 3))
        rest = len(view) - self.top - len(shown)
        if rest > 0:
            lines.append(f"\x1b[2m     ↓ {tr('ещё', 'more')} {rest}\x1b[0m")
        if not self.choices(view):
            lines.append(f"\x1b[2m  {tr('ничего не найдено', 'nothing found')}\x1b[0m")
        esc = self.esc or tr("выход", "quit")
        hint = tr(f"↑↓ выбрать · Enter · цифра или буквы — быстрый переход · Esc — {esc}",
                  f"↑↓ move · Enter · a number or letters to jump · Esc to {esc}")
        if self.marks is not None:
            total = self.summary(self.marks) if self.summary else tr(f"отмечено {len(self.marks)}",
                                                                      f"{len(self.marks)} marked")
            hint = tr(f"{total} · Пробел — отметить · a — все/ничего · Enter — дальше · Esc — отмена",
                      f"{total} · Space to mark · a for all/none · Enter to go on · Esc to cancel")
        if self.query:
            hint = tr(f"поиск: {self.query}▏ · Backspace стереть · Esc сбросить",
                      f"search: {self.query}▏ · Backspace to erase · Esc to clear")
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
        return read_keys(self.fd)

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
            raise Quit
        elif key.startswith("\x1b"):
            pass
        else:
            for ch in key:
                if not ch.isprintable():
                    continue
                if ch.isdigit() and not self.query and self.numbers:
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
            for key in split_keys(self.read_key()):
                if self.handle(key):
                    return self.sel if self.marks is None else set(self.marks)
            self.draw()

ALT_ON, ALT_OFF = "\x1b[?1049h", "\x1b[?1049l"
# программа запущена внутри рамки меню (run_framed): ее меню рисуются на том же экране, без переключения
IN_FRAME = os.environ.get("MNRH_IN_FRAME") == "1"
REDRAW = "\x1b]7777;mnrh-redraw\x07"
FRAME, FRAME_DIM, ACCENT, ROW_BG = "38;5;110", "38;5;240", "38;5;110", "48;5;238"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
# имя над рамками и папка share/<имя>/icons: этот же файл лежит в mnrh, имя берется по bin/ проекта
BRAND = next((name for name in ("claudemeister", "mnrh") if os.path.exists(os.path.join(ROOT, "bin", name))),
             "claudemeister")


_ICONS = None


def config_value(key):
    """Строка key=значение из ~/.config/mnrh/config, общего для claudemeister и mnrh."""
    path = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "mnrh", "config")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k.strip() == key:
                    return v.strip().lower()
    except OSError:
        pass
    return ""


def images_ok():
    """Умеет ли терминал картинки в строке по протоколу iTerm2: iTerm2 и WezTerm, в том числе по ssh (LC_TERMINAL).
    В tmux и screen картинкам нужен проброс, там остаются emoji. Явно задают MNRH_ICONS=emoji или images
    и строка icons=emoji или icons=images в ~/.config/mnrh/config."""
    want = os.environ.get("MNRH_ICONS", "").lower() or config_value("icons")
    if want in ("emoji", "images"):
        return want == "images"
    if os.environ.get("TMUX") or os.environ.get("TERM", "").startswith(("screen", "tmux")):
        return False
    return (os.environ.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm")
            or os.environ.get("LC_TERMINAL") in ("iTerm2", "WezTerm"))


def icons():
    """emoji -> готовая вставка картинки. Набор берется из share/<BRAND>/icons (icons.json и PNG); без него и в
    терминалах без картинок пусто, и меню показывает emoji."""
    global _ICONS
    if _ICONS is not None:
        return _ICONS
    _ICONS = {}
    if not images_ok():
        return _ICONS
    folder = os.path.join(ROOT, "share", BRAND, "icons")
    try:
        import base64
        import json
        with open(os.path.join(folder, "icons.json"), encoding="utf-8") as f:
            mapping = json.load(f)
        for emoji, name in mapping.items():
            with open(os.path.join(folder, name + ".png"), "rb") as f:
                data = f.read()
            img = (f"\x1b]1337;File=inline=1;width=2;height=1;preserveAspectRatio=1;size={len(data)}:"
                   f"{base64.b64encode(data).decode()}\x07")
            # две клетки сначала заливаются пробелами (фон выбранной строки остается), картинка ложится поверх,
            # курсор возвращается и уходит на две клетки вправо: так не важно, куда его ставит терминал
            _ICONS[emoji] = f"  \x1b[2D\x1b7{img}\x1b8\x1b[2C"
    except (OSError, ValueError):
        _ICONS = {}
    return _ICONS


def with_icons(frame):
    """Подменить emoji на картинки в готовом кадре. Ширина та же, две клетки, поэтому раскладка не меняется."""
    table = icons()
    if not table:
        return frame
    for emoji, img in table.items():
        if emoji in frame:
            frame = frame.replace(emoji, img)
    return frame


def brand():
    """Строка над рамками: имя и версия из VERSION в корне проекта."""
    try:
        with open(os.path.join(ROOT, "VERSION")) as f:
            ver = f"  \x1b[2mv{f.read().strip()}\x1b[0m"
    except OSError:
        ver = ""
    return f"\x1b[1;35m{BRAND}\x1b[0m{ver}"


def box(title, body, w, h, active):
    """Рамка w на h клеток с заголовком в верхней линии; body: строки внутри, лишние отрезаются."""
    color = FRAME if active else FRAME_DIM
    title = cut(title, max(0, w - 4))
    head = f"\x1b[{ACCENT if active else FRAME_DIM}m{title}\x1b[0m" if title else ""
    top = f"\x1b[{color}m╭─\x1b[0m{head}\x1b[{color}m{'─' * max(0, w - 3 - width(title))}╮\x1b[0m"
    side = f"\x1b[{color}m│\x1b[0m"
    lines = [top]
    for k in range(h - 2):
        lines.append(f"{side}{fit(body[k] if k < len(body) else '', w - 2)}\x1b[0m{side}")
    lines.append(f"\x1b[{color}m╰{'─' * (w - 2)}╯\x1b[0m")
    return lines


def wrap(lines, w):
    """Длинные строки без цвета переносятся по словам с тем же отступом, цветные обрезаются."""
    out = []
    for line in lines:
        if width(line) <= w or "\x1b[" in line:
            out.append(line)
            continue
        indent = line[:len(line) - len(line.lstrip())]
        out += textwrap.wrap(line, w, subsequent_indent=indent) or [""]
    return out


class Panel(Menu):
    """Меню на весь экран: слева список в рамке, справа описание выбранного пункта. Tab прячет описание.
    Без describe список один на всю ширину."""

    def __init__(self, tty_fd, items, title, default, describe=None, heads=("", ""), hint="", start=1, numbers=False):
        super().__init__(tty_fd, items, title, "", start, default)
        self.describe = describe
        self.heads = heads
        self.hint = hint
        self.numbers = numbers
        self.side = describe is not None
        self.shape = None

    def clear(self):
        pass

    def body(self, view, w, h):
        if self.sel in view:
            pos = view.index(self.sel)
            if pos < self.top:
                self.top = pos
            elif pos >= self.top + h:
                self.top = pos - h + 1
        self.top = max(0, min(self.top, max(0, len(view) - h)))
        digits = len(str(len(self.pick_able) + self.start - 1))
        out = []
        for i in view[self.top:self.top + h]:
            key, text = self.items[i]
            if key is None:
                out.append(f"  \x1b[{FRAME_DIM}m{text}\x1b[0m")
                continue
            if self.numbers:
                text = f"\x1b[2m{self.pick_able.index(i) + self.start:>{digits}}\x1b[0m  {text}"
            if self.marks is not None:
                text = ("\x1b[32m◉\x1b[0m " if i in self.marks else "\x1b[2m○\x1b[0m ") + text
            if i == self.sel:
                row = cut(f" › {text}", w)
                on = f"\x1b[0;{ROW_BG};1m"
                out.append(on + re.sub(r"\x1b\[0?m", on, row) + " " * (w - width(row)) + "\x1b[0m")
            else:
                out.append(f"   {text}")
        if not self.choices(view):
            out.append(f"\x1b[2m   {tr('ничего не найдено', 'nothing found')}\x1b[0m")
        if self.top and out:
            out[0] = fit(out[0], w - 3) + "\x1b[2m ↑\x1b[0m"
        if len(view) > self.top + h and out:
            out[-1] = fit(out[-1], w - 3) + "\x1b[2m ↓\x1b[0m"
        return out

    def draw(self):
        rows, cols = self.size()
        self.shape = (rows, cols)
        view = self.view()
        gap = 2
        h = max(5, rows - 3)
        digits = len(str(len(self.pick_able) + self.start - 1)) + 2 if self.numbers else 0
        need = max([width(text) + digits + 6 for key, text in self.items if key is not None]
                   + [width(self.heads[0]) + 8])
        # описание справа только если рядом со списком целиком ему остается хотя бы 34 клетки; иначе список важнее
        side = self.side and cols - gap * 2 - 1 - max(34, need) >= 34
        lw = max(34, need) if side else cols - gap * 2
        rw = cols - gap * 2 - lw - 1
        left = box(f" › {self.heads[0]} ", self.body(view, lw - 2, h - 2), lw, h, True)
        screen = left
        if side:
            text = self.describe(self.sel, rw - 4) if self.sel in self.choices(view) else []
            right = box(f" {self.heads[1]} ", [" " + line for line in wrap(text, rw - 4)], rw, h, False)
            screen = [a + " " + b for a, b in zip(left, right)]
        if self.query:
            hint = tr(f"поиск: {self.query}▏ · Backspace стереть · Esc сбросить",
                      f"search: {self.query}▏ · Backspace to erase · Esc to clear")
        elif self.marks is not None:
            total = self.summary(self.marks) if self.summary else tr(f"отмечено {len(self.marks)}",
                                                                      f"{len(self.marks)} marked")
            hint = tr(f"{total} · Пробел отметить · a все · Enter дальше · Esc отмена",
                      f"{total} · Space mark · a all · Enter go on · Esc cancel")
        else:
            hint = self.hint
        pad = " " * gap
        lines = ([pad + cut(self.title, cols - gap)] + [pad + line for line in screen]
                 + [pad + f"\x1b[2m{cut(hint, cols - gap * 2)}\x1b[0m"])
        self.write(with_icons("\x1b[H" + "\x1b[K\r\n".join(lines) + "\x1b[K\x1b[J"))

    def handle(self, key):
        if key == "\t":
            if self.describe is not None:
                self.side = not self.side
            return False
        return super().handle(key)

    def run(self):
        self.write(HIDE)
        self.draw()
        while True:
            if PENDING:
                keys = PENDING[:]
                del PENDING[:]
            elif select.select([self.fd], [], [], 0.3)[0]:
                keys = split_keys(self.read_key())
            else:
                if self.size() != self.shape or getattr(self.describe, "dirty", lambda: False)():
                    self.draw()
                continue
            for n, key in enumerate(keys):
                try:
                    done = self.handle(key)
                except Cancel:
                    PENDING[:] = keys[n + 1:]
                    raise
                if done:
                    PENDING[:] = keys[n + 1:]
                    return self.sel if self.marks is None else set(self.marks)
            self.draw()


def roomy(fd):
    """Хватит ли места на рамки. MNRH_PLAIN_MENU=1 оставляет старый список в строку."""
    if os.environ.get("MNRH_PLAIN_MENU"):
        return False
    try:
        cols, rows = os.get_terminal_size(fd)
    except OSError:
        return False
    return cols >= 60 and rows >= 14


class Screen:
    """Один полноэкранный режим на несколько меню подряд: между ними экран не мигает. suspend() временно
    возвращает обычный терминал для вывода команды или ввода строки."""

    def __init__(self):
        self.fd = None
        self.saved = None

    def open(self):
        if self.fd is not None:
            return self
        self.fd = os.open("/dev/tty", os.O_RDWR)
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[3] &= ~termios.ISIG
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        # внутри рамки родительского меню экран уже полноэкранный: переключать его значит мигнуть
        os.write(self.fd, ((HIDE if IN_FRAME else ALT_ON + HIDE) + "\x1b[2J").encode())
        return self

    def close(self):
        del PENDING[:]
        if self.fd is None:
            return
        try:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            os.write(self.fd, (SHOW if IN_FRAME else ALT_OFF + SHOW).encode())
            if IN_FRAME:
                # родитель увидит метку в выводе и нарисует свою рамку поверх этого меню
                sys.stdout.write(REDRAW)
                sys.stdout.flush()
        finally:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def suspend(self):
        screen = self

        class Suspended:
            def __enter__(self):
                screen.close()

            def __exit__(self, *exc):
                screen.open()

        return Suspended()

    def roomy(self):
        return roomy(self.fd)

    def ask(self, question, title="", note=()):
        """Строка ввода в рамке на этом же экране. Enter: введенный текст (может быть пустым), Esc: None."""
        self.open()
        text = ""
        while True:
            try:
                cols, rows = os.get_terminal_size(self.fd)
            except OSError:
                cols, rows = 80, 24
            w = max(20, cols - 4)
            body = [" " + line for line in wrap(list(note), w - 4)]
            body += ["" if note else None, f" \x1b[1m{question}\x1b[0m  {text}\x1b[7m \x1b[0m"]
            body = [line for line in body if line is not None]
            hint = tr("Enter готово · Esc назад", "Enter done · Esc back")
            lines = (["  " + cut(brand(), cols - 2)] + ["  " + line for line in box(f" › {heading(title)} ", body,
                                                                                     w, max(5, rows - 3), True)]
                     + ["  " + f"\x1b[2m{hint}\x1b[0m"])
            write_all(self.fd, with_icons("\x1b[H" + "\x1b[K\r\n".join(lines) + "\x1b[K\x1b[J"))
            keys = PENDING[:] if PENDING else split_keys(read_keys(self.fd))
            del PENDING[:]
            for n, key in enumerate(keys):
                if key in ("\r", "\n"):
                    PENDING[:] = keys[n + 1:]
                    return text
                if key in ("\x03", "\x04"):
                    raise Quit
                if key == "\x1b":
                    PENDING[:] = keys[n + 1:]
                    return None
                if key in ("\x7f", "\x08"):
                    text = text[:-1]
                elif key == "\x15":  # Ctrl+U стирает строку
                    text = ""
                elif not key.startswith("\x1b"):
                    text += "".join(ch for ch in key if ch.isprintable())

    def pick(self, items, title="", default=0, describe=None, start=1, numbers=True, back=None, top=None,
             marked=None, summary=None):
        """Список в рамке на этом экране. Esc: None, Ctrl+C: исключение Quit. С marked (набор индексов) это список
        с отметками: ответ набор отмеченных, summary(отметки) дает строку итога внизу."""
        if not items or all(key is None for key, _ in items):
            return None
        self.open()
        back = back or tr("назад", "back")
        tab = tr("Tab описание · ", "Tab description · ") if describe else ""
        hint = (tr(f"↑↓ выбрать · Enter · цифра или буквы, быстрый переход · {tab}Esc {back}",
                   f"↑↓ move · Enter · a number or letters to jump · {tab}Esc {back}") if numbers else
                tr(f"↑↓ выбрать · Enter · буквы ищут · {tab}Esc {back}",
                   f"↑↓ move · Enter · type to search · {tab}Esc {back}"))
        if roomy(self.fd):
            menu = Panel(self.fd, items, top or brand(), default, describe,
                         (heading(title), tr("Описание", "Description")), hint, start=start, numbers=numbers)
        else:
            # окно меньше 60 на 14: рамки не влезут, старый список в строку на том же экране
            os.write(self.fd, b"\x1b[H\x1b[2J")
            menu = Menu(self.fd, items, heading(title), "", start, default)
            menu.esc = back
        if marked is not None:
            menu.marks = set(marked)
            menu.summary = summary
        try:
            return menu.run()
        except Quit:
            raise
        except Cancel:
            return None


class Output:
    """Вывод программы для рамки: строки с цветами. Понимает \r (строка пишется заново), \x1b[K и \x1b[2K,
    табуляцию и Backspace; прочее управление курсором выбрасывается. Метка REDRAW ставит флаг redraw."""
    TOKEN = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]|\x1b[^\[\]]|\r\n|[\r\n\t\b]|[^\x1b\r\n\t\b]+")
    LIMIT = 5000

    def __init__(self):
        self.lines = [""]
        self.fresh = False  # был \r: следующий текст пишет строку заново
        self.redraw = False
        self.rest = ""

    def feed(self, text):
        text = self.rest + text
        cut_at = text.rfind("\x1b")
        # незаконченная управляющая последовательность в конце куска: дождаться остального
        if cut_at != -1 and not re.match(r"\x1b(\][^\x07]*\x07|\][^\x1b]*\x1b\\|\[[0-9;?]*[A-Za-z]|[^\[\]])", text[cut_at:]):
            text, self.rest = text[:cut_at], text[cut_at:]
        else:
            self.rest = ""
        for tok in self.TOKEN.findall(text):
            if tok in ("\n", "\r\n"):
                self.lines.append("")
                self.fresh = False
            elif tok == "\r":
                self.fresh = True
            elif tok == "\t":
                self.put(" " * (8 - width(self.lines[-1]) % 8))
            elif tok == "\b":
                self.lines[-1] = self.lines[-1][:-1]
            elif tok.startswith("\x1b]"):
                if "mnrh-redraw" in tok:
                    self.redraw = True
            elif tok.startswith("\x1b["):
                if tok.endswith("m"):
                    self.lines[-1] += tok
                elif tok in ("\x1b[K", "\x1b[0K") and self.fresh or tok == "\x1b[2K":
                    self.lines[-1] = ""
                    self.fresh = False
            elif not tok.startswith("\x1b"):
                self.put(tok)
        del self.lines[:-self.LIMIT]

    def put(self, text):
        if self.fresh:
            self.lines[-1] = ""
            self.fresh = False
        self.lines[-1] += text

    def plain(self):
        lines = self.lines[:-1] if self.lines[-1] == "" else self.lines
        return "\n".join(lines)


def run_framed(screen, argv, title, env=None):
    """Запустить argv в рамке на экране screen: вывод с цветами в рамке, меню программы на том же экране
    (MNRH_IN_FRAME=1). Ответ: (код выхода, Output). Экран не закрывается, поэтому ничего не мигает."""
    import fcntl
    import pty
    import struct
    import subprocess

    screen.open()
    out = Output()
    rows, cols = screen_size(screen.fd)
    master, slave = pty.openpty()

    def resize():
        r, c = screen_size(screen.fd)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", max(3, r - 5), max(20, c - 8), 0, 0))
        return r, c

    resize()
    child_env = dict(os.environ if env is None else env, MNRH_IN_FRAME="1")
    # программе нужен обычный терминал: ввод строки, вопросы да или нет; свои меню она включит сама
    termios.tcsetattr(screen.fd, termios.TCSADRAIN, screen.saved)
    # Ctrl+C прерывает программу, а меню остается: себе SIGINT игнорируем, программе возвращаем обычный
    import signal
    old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        proc = subprocess.Popen(argv, stdout=slave, stderr=slave, env=child_env,
                                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    except OSError as e:
        signal.signal(signal.SIGINT, old_int)
        os.close(master)
        os.close(slave)
        out.feed(f"\x1b[31m{e}\x1b[0m\n")
        return 127, out
    shape = (rows, cols)
    draw_output(screen, out, title, running=True)
    while True:
        ready = select.select([master], [], [], 0.2)[0]
        if ready:
            try:
                data = os.read(master, 65536)
            except OSError:
                data = b""
            if not data:
                break
            out.feed(data.decode("utf-8", "replace"))
            out.redraw = False
            draw_output(screen, out, title, running=True)
        elif proc.poll() is not None:
            break
        elif screen_size(screen.fd) != shape:
            shape = resize()
            draw_output(screen, out, title, running=True)
    code = proc.wait()
    signal.signal(signal.SIGINT, old_int)
    while select.select([master], [], [], 0.05)[0]:  # дочитать хвост вывода
        try:
            data = os.read(master, 65536)
        except OSError:
            break
        if not data:
            break
        out.feed(data.decode("utf-8", "replace"))
    os.close(master)
    os.close(slave)
    tty.setcbreak(screen.fd)
    attrs = termios.tcgetattr(screen.fd)
    attrs[3] &= ~termios.ISIG
    termios.tcsetattr(screen.fd, termios.TCSANOW, attrs)
    write_all(screen.fd, HIDE)
    return (128 - code if code < 0 else code), out


def screen_size(fd):
    try:
        cols, rows = os.get_terminal_size(fd)
    except OSError:
        cols, rows = 80, 24
    return rows, cols


def fold(line, size):
    """Разбить строку с цветами на куски по size клеток; цвет переходит на следующий кусок."""
    if width(line) <= size:
        return [line]
    parts, cur, seen, active, i = [], "", 0, [], 0
    while i < len(line):
        m = ANSI.match(line, i)
        if m:
            code = m.group(0)
            active = [] if code in ("\x1b[0m", "\x1b[m") else active + [code]
            cur += code
            i = m.end()
            continue
        w = cells(line[i])
        if seen + w > size:
            parts.append(cur + "\x1b[0m")
            cur, seen = "".join(active), 0
        cur += line[i]
        seen += w
        i += 1
    parts.append(cur)
    return parts


def draw_output(screen, out, title, running, code=0, back=0):
    """Рамка с выводом: последние строки, back строк назад при листании. Курсор в конце вывода, чтобы ввод
    строки и вопросы программы шли на месте."""
    rows, cols = screen_size(screen.fd)
    w, h = max(20, cols - 4), max(5, rows - 3)
    inner = h - 2
    raw = out.lines[:-1] if out.lines[-1] == "" and not running else out.lines
    # широкие строки переносятся, а не обрезаются: таблицы бывают шире окна
    lines = [part for line in raw for part in fold(line, w - 3)]
    end = max(0, len(lines) - back)
    shown = lines[max(0, end - inner):end]
    body = [" " + line for line in shown]
    if running:
        state = tr("выполняется", "running")
        hint = tr("выполняется · Ctrl+C прервать", "running · Ctrl+C to stop")
    else:
        state = (tr("готово", "done") if code == 0 else tr(f"код {code}", f"exit {code}"))
        hint = tr("Enter в меню · ↑↓ листать · q выход", "Enter menu · ↑↓ scroll · q quit")
    head = f" › {title}  ·  {state} "
    frame = (["  " + cut(brand(), cols - 2)] + ["  " + line for line in box(head, body, w, h, True)]
             + ["  " + f"\x1b[2m{cut(hint, cols - 4)}\x1b[0m"])
    text = "\x1b[H" + "\x1b[K\r\n".join(frame) + "\x1b[K\x1b[J"
    if running:
        # курсор на последней строке вывода: там программа спросит и там будет эхо ввода
        row = 3 + max(0, len(shown) - 1)
        col = 4 + min(w - 3, width(shown[-1]) if shown else 0)
        text += f"\x1b[{row};{col}H" + SHOW
    write_all(screen.fd, with_icons(text))


def show_output(screen, out, title, code):
    """Вывод после конца программы: листать, Enter или Esc в меню (True), q или Ctrl+C выход (False)."""
    back = 0
    while True:
        draw_output(screen, out, title, running=False, code=code, back=back)
        rows, _ = screen_size(screen.fd)
        page = max(1, rows - 6)
        keys = PENDING[:] if PENDING else split_keys(read_keys(screen.fd))
        del PENDING[:]
        _, cols = screen_size(screen.fd)
        total = sum(len(fold(line, max(20, cols - 4) - 3)) for line in out.lines)
        for key in keys:
            if key in ("\r", "\n", "\x1b"):
                return True
            if key in ("q", "Q", "й", "Й", "\x03", "\x04"):
                return False
            if key in UP:
                back = min(back + 1, max(0, total - page))
            elif key in DOWN:
                back = max(0, back - 1)
            elif key in ("\x1b[5~", " b"):
                back = min(back + page, max(0, total - page))
            elif key in ("\x1b[6~", " "):
                back = max(0, back - page)
            elif key in HOME:
                back = max(0, total - page)
            elif key in END:
                back = 0


def drive(fd, menu):
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ISIG
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return menu.run()
    except Cancel:
        return None
    finally:
        menu.clear()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        menu.write(SHOW)


def panel(items, title="", default=0, describe=None, heads=("", ""), hint="", fallback=None):
    """Главное меню на весь экран. В маленьком терминале (меньше 60 на 14) fallback. Ответ: индекс или None."""
    if not items or all(key is None for key, _ in items):
        return None
    fd = os.open("/dev/tty", os.O_RDWR)
    big = roomy(fd)
    os.close(fd)
    if not big:
        return fallback() if fallback else pick(items, title=title, default=default)
    with Screen() as screen:
        try:
            return Panel(screen.fd, items, title, default, describe, heads, hint).run()
        except Cancel:
            return None


def heading(title):
    return ANSI.sub("", title).strip().rstrip(":")


def pick(items, title="", label="", start=1, default=0, marked=None, summary=None, esc=None, describe=None):
    """Список с выбором. Если терминал позволяет, на весь экран в рамке, с описанием справа, когда есть describe(i, w).
    С marked это список с отметками, ответ набор индексов."""
    if not items or all(key is None for key, _ in items):
        return None
    fd = os.open("/dev/tty", os.O_RDWR)
    if roomy(fd):
        os.close(fd)
        with Screen() as screen:
            try:
                choice = screen.pick(items, title, default, describe, start, back=esc or tr("выход", "quit"),
                                     marked=marked, summary=summary)
            except Quit:
                choice = None
        if choice is not None and label and marked is None:
            fd = os.open("/dev/tty", os.O_WRONLY)
            os.write(fd, f"{label}: \x1b[1m{items[choice][0]}\x1b[0m\r\n".encode())
            os.close(fd)
        return choice
    menu = Menu(fd, items, title, label, start, default)
    if esc:
        menu.esc = esc
    if marked is not None:
        menu.marks = set(marked)
        menu.summary = summary
    choice = drive(fd, menu)
    if choice is not None and label and marked is None:
        menu.write(f"{label}: \x1b[1m{items[choice][0]}\x1b[0m\r\n")
    os.close(fd)
    return choice


def pick_many(items, title="", marked=(), summary=None):
    return pick(items, title=title, marked=set(marked), summary=summary)


def choice(question, options, default=0, back=None, esc=None):
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
        hint = f"\x1b[2m  ←→ · Enter · Esc — {esc or tr('назад', 'back')}\x1b[0m"
        os.write(fd, f"\r\x1b[K\x1b[1;34m{question}\x1b[0m  {'  '.join(parts)}{hint}".encode())

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
        os.write(fd, f"\r\x1b[K\x1b[1;34m{question}\x1b[0m  {label}\r\n{SHOW}".encode())
        os.close(fd)
    return result


def render_note(note):
    """Описание из третьего поля строки: строки через буквальные \\n, первая жирная, «# заголовок» тусклый."""
    lines = note.split("\\n") if note else []
    out = []
    for k, line in enumerate(lines):
        if k == 0:
            out.append(f"\x1b[1m{line}\x1b[0m")
        elif line.startswith("# "):
            out.append(f"\x1b[2m{line[2:].upper()}\x1b[0m")
        else:
            out.append(line)
    return out


def main():
    p = argparse.ArgumentParser(description="меню со стрелками; пункты «ключ<TAB>текст[<TAB>описание]» из stdin, "
                                            "ответ — индекс с 0")
    p.add_argument("--title", default="")
    p.add_argument("--label", default="")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--default", type=int, default=0)
    a = p.parse_args()

    items, notes = [], []
    for line in sys.stdin.read().splitlines():
        if not line.strip():
            continue
        key, _, rest = line.partition("\t")
        text, _, note = rest.partition("\t")
        items.append((key, text or key))
        notes.append(note)
    if not items:
        return 1
    describe = (lambda i, w: render_note(notes[i])) if any(notes) else None
    choice = pick(items, a.title, a.label, a.start, a.default, describe=describe)
    if choice is None:
        return 130
    print(choice)
    return 0


if __name__ == "__main__":
    sys.exit(main())
