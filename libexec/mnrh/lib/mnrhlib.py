import os
import re
import subprocess
import sys

HOME = os.path.expanduser("~")
TTY = sys.stdout.isatty()


def run(cmd, timeout=30, env=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
        return ""


def paint(text, code):
    return f"\033[{code}m{text}\033[0m" if TTY else text


OK = paint("✓", "32")
WARN = paint("!", "33")
BAD = paint("✗", "31")
DIM = "2"


def human(nbytes):
    gb = nbytes / 1073741824
    if gb >= 1:
        return f"{gb:.1f} ГБ"
    return f"{nbytes / 1048576:.0f} МБ"


def du_bytes(path):
    out = run(["du", "-sk", "--", path], timeout=300)
    m = re.match(r"(\d+)", out)
    return int(m.group(1)) * 1024 if m else 0


def tilde(path):
    return path.replace(HOME, "~", 1) if path.startswith(HOME) else path


def has_flag(args, *names):
    return any(a in args for a in names)
