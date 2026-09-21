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


DEV = os.path.join(HOME, "Developer")


def projects():
    if not os.path.isdir(DEV):
        return []
    return sorted(os.path.join(DEV, d) for d in os.listdir(DEV)
                  if os.path.isdir(os.path.join(DEV, d, ".git")))


def gradle_versions_in_use():
    used = {}
    for proj in projects():
        props = os.path.join(proj, "gradle", "wrapper", "gradle-wrapper.properties")
        try:
            text = open(props, encoding="utf-8").read()
        except OSError:
            continue
        m = re.search(r"gradle-([\d.]+)-(bin|all)\.zip", text)
        if m:
            used.setdefault(m.group(1), []).append(os.path.basename(proj))
    return used


def version_key(name):
    return [int(n) for n in re.findall(r"\d+", name)]
