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


def process_memory():
    units = {"B": 1, "K": 1024, "M": 1048576, "G": 1073741824}
    mem, started = {}, False
    for line in run(["top", "-l", "1", "-stats", "pid,mem"]).splitlines():
        if line.startswith("PID"):
            started = True
            continue
        cols = line.split()
        if started and len(cols) >= 2:
            m = re.match(r"([\d.]+)([BKMG])", cols[1])
            if m:
                mem[cols[0]] = float(m.group(1)) * units[m.group(2)]
    return mem


def process_commands():
    cmds = {}
    for line in run(["ps", "-axo", "pid=,command="]).splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2:
            cmds[parts[0]] = parts[1]
    return cmds


def simulator_memory():
    mem = process_memory()
    return sum(mem.get(pid, 0) for pid, cmd in process_commands().items()
               if "/CoreSimulator/" in cmd or "CoreSimulator.framework" in cmd)


def confirm(question, assume_yes):
    import sys as _sys
    if assume_yes:
        return True
    if not _sys.stdin.isatty():
        print("Не терминал, подтвердить нельзя. Повтори с -y")
        _sys.exit(2)
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes", "д", "да")


def gradle_build_dirs(project):
    found = []
    for root, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".idea", "Pods")]
        if "build" in dirs and ({"build.gradle", "build.gradle.kts"} & set(files)):
            found.append(os.path.join(root, "build"))
        dirs[:] = [d for d in dirs if d not in ("build", ".gradle")]
    pg = os.path.join(project, ".gradle")
    if os.path.isdir(pg):
        found.append(pg)
    return found


def remove_under_home(path):
    import shutil as _shutil
    if not path.startswith(HOME + os.sep) or path.rstrip("/") == HOME:
        return
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    else:
        _shutil.rmtree(path, ignore_errors=True)
