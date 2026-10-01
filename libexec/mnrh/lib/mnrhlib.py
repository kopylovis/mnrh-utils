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


CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "mnrh", "config")
DEFAULT_DEV = os.path.join(HOME, "Developer")


def read_config():
    conf = {}
    try:
        with open(CONFIG) as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep:
                    conf[key.strip()] = value.strip()
    except OSError:
        pass
    return conf


def write_config(**values):
    conf = read_config()
    conf.update(values)
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        for key, value in conf.items():
            f.write(f"{key}={value}\n")


def projects_dir():
    path = os.environ.get("MNRH_PROJECTS") or read_config().get("projects") or DEFAULT_DEV
    return os.path.abspath(os.path.expanduser(path))


DEV = projects_dir()


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
    try:
        from menu import choice
        return choice(question, [("yes", "Да", "yд"), ("no", "Нет", "nн")], default=1, back="no", esc="нет") == "yes"
    except OSError:
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


def adb_path():
    import shutil as _shutil
    for sdk in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                os.path.join(HOME, "Library", "Android", "sdk")):
        if sdk and os.access(os.path.join(sdk, "platform-tools", "adb"), os.X_OK):
            return os.path.join(sdk, "platform-tools", "adb")
    return _shutil.which("adb")


APP_DIRS = ["/Applications", os.path.join(HOME, "Applications"), "/Applications/Utilities"]


def bundle_info(app):
    """(bundle id, имя) из Info.plist приложения."""
    import plistlib as _plistlib
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            info = _plistlib.load(f)
    except Exception:
        return None, None
    return info.get("CFBundleIdentifier"), info.get("CFBundleName") or os.path.basename(app)[:-4]


def find_apps(query):
    """Приложения по имени: точное совпадение, иначе все, где имя начинается с запроса или содержит его."""
    import glob as _glob
    q = query.lower().removesuffix(".app")
    if os.path.isdir(query) and query.endswith(".app"):
        return [os.path.abspath(query)]
    candidates = []
    for d in APP_DIRS + ["/System/Applications", "/System/Applications/Utilities", "/System/Library/CoreServices"]:
        for app in _glob.glob(os.path.join(d, "*.app")):
            name = os.path.basename(app)[:-4].lower()
            if name == q:
                return [app]
            if q in name:
                candidates.append(app)
    return candidates


def login_path():
    """PATH, как в новой вкладке Терминала: login и interactive zsh."""
    env = {"HOME": HOME, "TERM": "xterm", "SHELL": "/bin/zsh"}
    out = run(["/bin/zsh", "-l", "-i", "-c", "echo $PATH"], timeout=20, env=env).strip().splitlines()
    return out[-1].split(":") if out else []


def git_versions():
    """[(путь, версия)] всех git, которыми пользуются терминал, IDE и Sourcetree; первый — тот, что в PATH."""
    found, seen = [], set()
    candidates = [os.path.join(d, "git") for d in login_path()]
    candidates += ["/usr/bin/git", "/Applications/Sourcetree.app/Contents/Resources/git_local/bin/git"]
    for path in candidates:
        if not os.access(path, os.X_OK) or os.path.isdir(path):
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        m = re.search(r"git version (\d+(?:\.\d+)+)", run([path, "--version"]))
        if m:
            found.append((path, m.group(1)))
    return found


def project_root(cwd=None):
    cwd = os.path.abspath(cwd or os.getcwd())
    out = run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], timeout=5).strip()
    return out or cwd


def find_project(query=None, cwd=None):
    if not query:
        return project_root(cwd), None
    if query in ("~", "root", "home"):
        return HOME, None
    path = os.path.abspath(os.path.expanduser(query))
    if os.path.isdir(path) and ("/" in query or query.startswith(("~", "."))):
        return project_root(path), None
    root = projects_dir()
    names = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)) and not d.startswith(".")) \
        if os.path.isdir(root) else []
    hits = [d for d in names if d.lower() == query.lower()] or \
        [d for d in names if d.lower().startswith(query.lower())]
    if len(hits) == 1:
        return os.path.join(root, hits[0]), None
    if hits:
        return None, f"«{query}» подходит к нескольким проектам: {', '.join(hits)}"
    return None, f"проекта «{query}» нет в {tilde(root)}"
