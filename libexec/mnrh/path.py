import glob
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import (BAD, CONFIG, HOME, OK, WARN, confirm, du_bytes, has_flag, human, login_path, paint, projects,
                     projects_dir, run, tilde, version_key)

args = sys.argv[1:]
FLAGS = ("-a", "--all", "-n", "--dry-run", "-y", "--yes")
if has_flag(args, "-h", "--help") or [a for a in args if a not in FLAGS + ("clean",)]:
    print("mnrh path            мусор в PATH и лишние копии git, java, python, node, ruby, go…")
    print("                     ничего не меняет, только показывает, что и где убрать")
    print("mnrh path -a         весь PATH по порядку и все копии, включая системные")
    print("mnrh path clean      убрать безопасное: мёртвые строки в файлах оболочки, битые ярлыки,")
    print("                     сломанный MacPorts, старый go, лишнюю JDK той же версии, openssl@1.1")
    print("                     файлы оболочки — с бэкапом, остальное — в Корзину. -n показать план, -y без вопроса")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)
ALL = has_flag(args, "-a", "--all")
OLD_BREW = (("openssl@1.1", "не поддерживается с 2023"), ("python@3.9", "устарел"),
            ("python@3.10", "устарел"), ("node@16", "устарел"), ("node@18", "устарел"))
BREW = "/opt/homebrew/bin/brew"

RC = ["~/.zshenv", "~/.zprofile", "~/.zshrc", "~/.zlogin", "~/.bash_profile", "~/.bashrc", "~/.profile"]
SYSTEM_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/System/", "/Library/Apple/", "/var/run/com.apple.")
# инструмент -> аргументы, которые печатают версию
TOOLS = {
    "git": ["--version"], "java": ["-version"], "javac": ["-version"], "python3": ["--version"],
    "python": ["--version"], "pip3": ["--version"], "node": ["--version"], "npm": ["--version"],
    "ruby": ["--version"], "gem": ["--version"], "bundle": ["--version"], "pod": ["--version"],
    "go": ["version"], "openssl": ["version"], "adb": ["version"], "kotlin": ["-version"],
    "gradle": ["--version"], "make": ["--version"], "cmake": ["--version"], "curl": ["--version"],
}
issues = 0


def say(mark, text, hint=""):
    global issues
    issues += mark is not OK
    print(f"  {mark} {text}" + (paint(f"   -> {hint}", "2") if hint else ""))


def is_system(path):
    return path.startswith(SYSTEM_DIRS)


def expand(token):
    token = token.strip("\"'")
    token = re.sub(r"^(\$HOME|\$\{HOME\}|~)(?=/|$)", HOME, token)
    return token if "$" not in token and "`" not in token else None


def rc_files():
    return [(p, os.path.expanduser(p)) for p in RC if os.path.isfile(os.path.expanduser(p))]


def rc_lines():
    for short, path in rc_files():
        try:
            lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            if line.strip() and not line.lstrip().startswith("#"):
                yield short, n, line


def where_added(directory):
    """Где папку добавляют в PATH: файл оболочки или /etc/paths(.d)."""
    forms = {directory, tilde(directory), directory.replace(HOME, "$HOME", 1)}
    for short, n, line in rc_lines():
        if any(f in line for f in forms if f):
            return f"{short}:{n}"
    for f in ["/etc/paths"] + sorted(glob.glob("/etc/paths.d/*")):
        try:
            if directory in open(f).read().split():
                return f
        except OSError:
            pass
    return ""


# ---------- PATH ----------

def check_path(path):
    print(paint("PATH (как в новой вкладке Терминала)", "1"))
    seen = {}
    for i, d in enumerate(path, 1):
        seen.setdefault(os.path.realpath(d), []).append(i)
    zshenv_path = any("PATH" in line for short, _, line in rc_lines() if short == "~/.zshenv")
    shown = False
    for i, d in enumerate(path, 1):
        dup = seen[os.path.realpath(d)]
        if not os.path.isdir(d):
            if d.startswith("/var/run/com.apple."):
                continue  # служебные папки cryptex: появляются только при необходимости
            hint = "zsh кладёт её в PATH по умолчанию; уходит, если не менять PATH в ~/.zshenv" \
                if d == "/usr/ucb" and zshenv_path else f"убрать строку: {where_added(d) or 'не нашёл где'}"
            say(BAD, f"{i:>2}. {tilde(d)} — такой папки нет", hint)
            shown = True
        elif len(dup) > 1 and dup[0] != i:
            say(WARN, f"{i:>2}. {tilde(d)} — повтор, уже есть под №{dup[0]}", where_added(d))
            shown = True
        elif ALL:
            print(f"    {i:>2}. {tilde(d)}" + paint(f"   {where_added(d)}", "2"))
    if not shown and not ALL:
        say(OK, f"{len(path)} папок, все есть, повторов нет")


# ---------- файлы оболочки ----------

def check_rc():
    print(paint("\nФайлы оболочки", "1"))
    zsh = os.environ.get("SHELL", "/bin/zsh").endswith("zsh")
    found = False
    for short, path in rc_files():
        bash = short in ("~/.bash_profile", "~/.bashrc", "~/.profile")
        note = " (bash — вы им не пользуетесь)" if bash and zsh else ""
        counts = {}
        for s, n, line in rc_lines():
            if s != short:
                continue
            key = line.strip()
            if key in counts and ("PATH" in key or key.startswith((".", "source", "[", "eval"))):
                say(WARN, f"{short}:{n} повтор строки {counts[key]}: {key[:70]}{note}")
                found = True
            counts.setdefault(key, n)
            for dead in dead_paths(line):
                say(WARN, f"{short}:{n} ссылается на {tilde(dead)}, а его нет{note}", "строку можно удалить")
                found = True
    for f in sorted(glob.glob("/etc/paths.d/*")):
        for d in open(f).read().split():
            if not os.path.exists(d) and not d.startswith("/var/run/com.apple."):
                say(WARN, f"{f}: {d} — папки нет", f"sudo rm {f}")
                found = True
    if not found:
        say(OK, "мёртвых путей и повторов нет")


def dead_paths(line):
    targets = []
    m = re.search(r"(?:^|[;&|]\s*|\bthen\s+)(?:source|\.)\s+(\S+)", line)
    if m:
        targets.append(m.group(1))
    m = re.search(r"\[\[?\s+-[sfdex]\s+(\S+)\s+\]", line)
    if m:
        targets.append(m.group(1))
    m = re.search(r"\bPATH=(\"[^\"]*\"|'[^']*'|\S+)", line)
    if m:
        targets += [t for t in m.group(1).strip("\"'").split(":") if "PATH" not in t]
    dead = []
    for t in targets:
        p = expand(t)
        if p and p.startswith("/") and not os.path.exists(p) and p not in dead:
            dead.append(p)
    return dead


# ---------- битые ярлыки и скрипты ----------

def dead_programs(path):
    """[(файл, что не так)] битых ярлыков и скриптов без интерпретатора в своих папках PATH."""
    found = []
    for d in dict.fromkeys(path):
        if is_system(d) or not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            f = os.path.join(d, name)
            if os.path.islink(f) and not os.path.exists(f):
                found.append((f, f"ярлык на {tilde(os.readlink(f))}, программы нет"))
                continue
            interp = shebang(f)
            if interp and not os.path.exists(interp):
                found.append((f, f"нет интерпретатора {tilde(interp)}"))
    return found


def check_links(path):
    print(paint("\nНе запускаются", "1"))
    dead = dead_programs(path)
    for f, why in dead:
        say(BAD, f"{tilde(f)} — {why}")
    if not dead:
        say(OK, "битых ярлыков и скриптов без интерпретатора нет")


def shebang(f):
    try:
        if not os.path.isfile(f) or os.path.islink(f):
            return None
        with open(f, "rb") as fh:
            head = fh.read(256)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    interp = head[2:].split(b"\n", 1)[0].decode(errors="replace").split()
    return interp[0] if interp and not interp[0].startswith("/usr/bin/env") else None


# ---------- копии инструментов ----------

def version(path, flags):
    try:
        r = subprocess.run([path] + flags, capture_output=True, text=True, timeout=15,
                           env={"HOME": HOME, "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"})
    except subprocess.TimeoutExpired:
        return "?"
    except OSError:
        return "не запускается"
    text = r.stdout + r.stderr
    m = re.search(r"\d+\.\d+(?:\.\d+)?(?:[._]\d+)?[a-z]?", text)
    if m:
        return m.group(0)
    return "не запускается" if r.returncode else "?"


def copies(path):
    found = {}
    for tool in TOOLS:
        seen = set()
        for d in path:
            f = os.path.join(d, tool)
            if os.path.isfile(f) and os.access(f, os.X_OK) and os.path.realpath(f) not in seen:
                seen.add(os.path.realpath(f))
                found.setdefault(tool, []).append(f)
    jobs = [(t, f) for t, fs in found.items() for f in fs]
    with ThreadPoolExecutor(8) as pool:
        vers = dict(zip(jobs, pool.map(lambda j: version(j[1], TOOLS[j[0]]), jobs)))
    return found, vers


def check_copies(path):
    print(paint("\nНесколько копий одной программы", "1"))
    found, vers = copies(path)
    shown = False
    for tool, files in found.items():
        own = [f for f in files if not is_system(f)]
        if len(own) < 2 and not ALL:
            continue
        if len(files) < 2:
            continue
        shown = True
        print(f"  {WARN if len(own) > 1 else ' '} {paint(tool, '1')}")
        for i, f in enumerate(files):
            v = vers[(tool, f)]
            real = os.path.realpath(f)
            origin = paint(f" ({tilde(real)})", "2") if real != f and not is_system(f) else ""
            mark = paint("▶ работает эта", "32") if i == 0 else paint("перекрыта", "2")
            vtext = paint(v, "31") if v == "не запускается" else v
            print(f"      {tilde(f):<52} {vtext:<14} {mark}{origin}")
    if not shown:
        say(OK, "у каждой программы одна копия (кроме системных)")
    else:
        global issues
        issues += 1
        print(paint("    «перекрыта» — лежит дальше в PATH и не вызывается; обычно её можно удалить", "2"))


# ---------- версии вне PATH ----------

def list_jdks():
    jdks = []
    for line in run(["/usr/libexec/java_home", "-V"]).splitlines() + \
            subprocess.run(["/usr/libexec/java_home", "-V"], capture_output=True, text=True).stderr.splitlines():
        m = re.match(r"\s+([\d.]+)\S* \(\w+\) \"([^\"]+)\" - \"([^\"]+)\" (\S.*)", line)
        if m:
            jdks.append((m.group(1), m.group(3), m.group(4)))
    for cellar in sorted(glob.glob("/opt/homebrew/Cellar/openjdk*/*")):
        jdks.append((os.path.basename(cellar), "Homebrew " + os.path.basename(os.path.dirname(cellar)), cellar))
    for jdk in sorted(glob.glob(os.path.join(HOME, ".gradle/jdks/*"))):
        if os.path.isdir(jdk):
            jdks.append((re.sub(r"\D*(\d+).*", r"\1", os.path.basename(jdk)), "скачал Gradle", jdk))
    return jdks


def spare_jdks(jdks):
    """JDK, у которых есть другая той же версии, выбранная java_home: их можно убрать."""
    spare = []
    for v, name, home in jdks:
        if not home.startswith("/Library/Java/JavaVirtualMachines/") or "JetBrains" in name:
            continue
        major = v.split(".")[0]
        chosen = run(["/usr/libexec/java_home", "-v", major]).strip()
        if chosen and chosen != home and chosen.startswith("/Library/Java/"):
            spare.append((v, name, home.rsplit("/Contents/Home", 1)[0]))
    return spare


def macports_broken():
    if not os.path.isdir("/opt/local"):
        return None
    if not os.path.exists("/opt/local/bin/port"):
        return True
    r = subprocess.run(["/opt/local/bin/port", "version"], capture_output=True, text=True)
    return "mismatch" in (r.stdout + r.stderr) or r.returncode != 0


def brew_leftovers():
    if not os.path.exists(BREW):
        return []
    installed = set(run([BREW, "list", "--formula", "-1"]).split())
    etc = os.path.join(os.path.dirname(os.path.dirname(BREW)), "etc")
    return [os.path.join(etc, old) for old, _ in OLD_BREW
            if old not in installed and os.path.isdir(os.path.join(etc, old))]


def old_brew():
    if not os.path.exists(BREW):
        return []
    installed = set(run([BREW, "list", "--formula", "-1"]).split())
    return [(old, why, run([BREW, "uses", "--installed", old]).split()) for old, why in OLD_BREW if old in installed]


def check_versions():
    print(paint("\nУстановленные версии", "1"))
    jdks = list_jdks()
    if jdks:
        majors = {}
        for v, _, p in jdks:
            majors.setdefault(v.split(".")[0], []).append(p)
        print(f"    Java: {len(jdks)}")
        for v, name, p in jdks:
            dup = len(majors[v.split('.')[0]]) > 1 and "Gradle" not in name and "JetBrains" not in name
            print(f"    {WARN if dup else ' '} {v:<10} {name[:30]:<30} " + paint(tilde(p), "2"))
        if any(len(ps) > 1 for ps in majors.values()):
            print(paint("      ! — две JDK одной версии, одна лишняя", "2"))

    managers = [
        ("node (nvm)", "~/.nvm/versions/node/*", None),
        ("python (pyenv)", "~/.pyenv/versions/*", "pyenv init"),
        ("ruby (rbenv)", "~/.rbenv/versions/*", "rbenv init"),
        ("flutter (fvm)", "~/fvm/versions/*", None),
    ]
    rc_text = "\n".join(line for _, _, line in rc_lines())
    for title, pattern, init in managers:
        vs = sorted(glob.glob(os.path.expanduser(pattern)))
        if not vs:
            continue
        size = sum(du_bytes(v) for v in vs)
        names = ", ".join(os.path.basename(v) for v in vs)
        if init and init not in rc_text:
            say(WARN, f"{title}: {names} ({human(size)})", f"{init.split()[0]} не подключён в оболочке — не используется")
        else:
            print(f"    {title}: {names} " + paint(f"({human(size)})", "2"))

    if os.path.isfile("/usr/local/go/bin/go"):
        v = open("/usr/local/go/VERSION").readline().strip() if os.path.exists("/usr/local/go/VERSION") else "?"
        brew = os.path.exists("/opt/homebrew/bin/go")
        say(WARN if brew else OK, f"go из установщика с go.dev: {v}, /usr/local/go",
            "есть ещё go из Homebrew; старый: sudo rm -rf /usr/local/go /etc/paths.d/go" if brew else "")

    broken = macports_broken()
    if broken is not None:
        size = human(du_bytes("/opt/local"))
        if broken:
            say(BAD, f"MacPorts в /opt/local ({size}) не работает — поставлен под другую версию macOS",
                "убрать /opt/local из PATH (~/.zprofile) и удалить: sudo rm -rf /opt/local")
        else:
            say(WARN, f"MacPorts в /opt/local ({size}) рядом с Homebrew", "две системы пакетов путают PATH")

    for old, why, used in old_brew():
        say(WARN, f"Homebrew: {old} — {why}", f"нужен для: {', '.join(used)}" if used else f"brew uninstall {old}")


# ---------- лишние версии, копии и архивы ----------

MANAGERS = {
    "node": ("nvm", "~/.nvm/versions/node", "~/.nvm/alias/default", (".nvmrc", ".node-version"), "nodejs"),
    "ruby": ("rbenv", "~/.rbenv/versions", "~/.rbenv/version", (".ruby-version",), "ruby"),
    "python": ("pyenv", "~/.pyenv/versions", "~/.pyenv/version", (".python-version",), "python"),
}
PIN_DIRS = ("", "web", "frontend", "client", "app", "site", "iosApp", "fastlane", "server", "backend")
BREW_KEEP = ("git", "node", "ruby", "openjdk", "python@", "openssl@3", "gradle", "kotlin")


def read_first(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
    except OSError:
        return []


def pins(kind):
    manager, _, glob_file, names, tool_name = MANAGERS[kind]
    found = []
    for root in projects() + [projects_dir(), HOME]:
        for sub in PIN_DIRS:
            base = os.path.join(root, sub)
            for n in names:
                lines = read_first(os.path.join(base, n))
                if lines:
                    found.append((lines[0].split()[0], tilde(os.path.join(base, n))))
            for line in read_first(os.path.join(base, ".tool-versions")):
                parts = line.split()
                if len(parts) > 1 and parts[0] == tool_name:
                    found.append((parts[1], tilde(os.path.join(base, ".tool-versions"))))
    if kind != "python" or hooked(manager):
        for line in read_first(os.path.expanduser(glob_file)):
            found.append((line.split()[0], "глобальная"))
    return found


def matching(installed, spec):
    spec = spec.strip().lstrip("v")
    if spec in ("system",):
        return []
    if not re.match(r"^\d+(\.\d+)*$", spec):
        exact = [v for v in installed if v.lstrip("v") == spec]
        return exact or None
    return [v for v in installed if v.lstrip("v") == spec or v.lstrip("v").startswith(spec + ".")]


def hooked(manager):
    rc_text = "\n".join(line for _, _, line in rc_lines())
    return manager in rc_text


def spare_versions(kind):
    manager, vdir, _, _, _ = MANAGERS[kind]
    vdir = os.path.expanduser(vdir)
    try:
        installed = sorted((d for d in os.listdir(vdir) if os.path.isdir(os.path.join(vdir, d))
                            and not os.path.islink(os.path.join(vdir, d))), key=version_key)
    except OSError:
        return [], None
    if not installed:
        return [], None
    found = pins(kind)
    keep, why = set(), {}
    for spec, src in found:
        m = matching(installed, spec)
        if m is None:
            return [], f"{src}: «{spec}» — не разобрать, версии {manager} не трогаю"
        keep.update(m)
        for v in m:
            why.setdefault(v, src)
    if kind == "python" and not hooked("pyenv") and not keep:
        return installed, "pyenv не подключён в оболочке и ни один проект не просит Python через него"
    if not keep:
        keep.add(installed[-1])
    return [v for v in installed if v not in keep], None


def kept_versions(kind, spare):
    vdir = os.path.expanduser(MANAGERS[kind][1])
    try:
        return sorted((d for d in os.listdir(vdir) if os.path.isdir(os.path.join(vdir, d)) and d not in spare),
                      key=version_key) or ["—"]
    except OSError:
        return ["—"]


def formula_bins(formula):
    bins = set()
    for d in glob.glob(f"/opt/homebrew/Cellar/{glob.escape(formula)}/*/bin"):
        bins.update(os.listdir(d))
    return bins


def first_in_path(path, name):
    for d in path:
        f = os.path.join(d, name)
        if os.path.isfile(f) and os.access(f, os.X_OK):
            return f
    return None


def shadowed_brew(path):
    if not os.path.exists(BREW):
        return []
    found, _ = copies(path)
    out = []
    for tool, files in found.items():
        for f in files[1:]:
            m = re.match(r"/opt/homebrew/Cellar/([^/]+)/", os.path.realpath(f))
            if not m or m.group(1).startswith(BREW_KEEP) or m.group(1) in [o for o, _ in out]:
                continue
            formula = m.group(1)
            bins = formula_bins(formula)
            winners = {b: first_in_path(path, b) for b in bins}
            if not bins or any(w and os.path.realpath(w).startswith(f"/opt/homebrew/Cellar/{formula}/")
                               for w in winners.values()):
                continue
            if run([BREW, "uses", "--installed", formula]).split():
                continue
            winner = tilde(files[0])
            out.append((formula, f"{tool} работает из {winner}, копия из Homebrew перекрыта и ни от чего не зависит"))
    return out


def stale_gem_stubs():
    shims = os.path.expanduser("~/.rbenv/shims")
    out = []
    if not os.path.isdir(shims):
        return out
    for f in sorted(glob.glob("/usr/local/bin/*")):
        name = os.path.basename(f)
        if os.path.islink(f) or not os.path.isfile(f) or not os.path.exists(os.path.join(shims, name)):
            continue
        try:
            with open(f, "rb") as fh:
                head = fh.read(400).decode("utf-8", "replace")
        except OSError:
            continue
        if head.startswith("#!") and ("/usr/bin/ruby" in head.splitlines()[0] or "Ruby.framework" in head.splitlines()[0]) \
                and ("RubyGems" in head or "Gem" in head):
            out.append(f)
    return out


def jdk_archives():
    d = os.path.expanduser("~/.gradle/jdks")
    if not os.path.isdir(d):
        return []
    dirs = [n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n))]
    out = []
    for n in os.listdir(d):
        if not re.search(r"\.(tar\.gz|zip)$", n):
            continue
        m = re.search(r"(?:JDK|jdk|jre)[-_]?(\d+)", n)
        major = m.group(1) if m else None
        if major and any(re.search(rf"(^|[-_]){major}([-_.]|$)", x) for x in dirs):
            out.append(os.path.join(d, n))
    return out


def spare_plan(path):
    plan = []
    for kind, (manager, vdir, _, _, _) in MANAGERS.items():
        spare, note = spare_versions(kind)
        if not spare:
            continue
        vdir = os.path.expanduser(vdir)
        size = sum(du_bytes(os.path.join(vdir, v)) for v in spare)
        text = (f"{manager}: {', '.join(spare)} ({human(size)}) — "
                + (note or f"не указаны ни в одном проекте и не по умолчанию; остаются: {', '.join(kept_versions(kind, spare))}"))
        plan.append((text, lambda vdir=vdir, spare=spare: all(trash(os.path.join(vdir, v)) for v in spare)))
    for formula, why in shadowed_brew(path):
        plan.append((f"Homebrew: {formula} — {why}", lambda f=formula: subprocess.run([BREW, "uninstall", f]).returncode == 0))
    for f in stale_gem_stubs():
        plan.append((f"{f} — старый скрипт gem для системного Ruby, работает {os.path.basename(f)} из rbenv",
                     lambda f=f: trash(f)))
    for f in jdk_archives():
        plan.append((f"{tilde(f)} ({human(os.path.getsize(f))}) — архив JDK, уже распакован Gradle",
                     lambda f=f: trash(f) and all(trash(x) for x in glob.glob(glob.escape(f) + ".lock"))))
    return plan


def check_spare(path):
    print(paint("\nЛишнее", "1"))
    plan = spare_plan(path)
    if not plan:
        say(OK, "лишних версий, перекрытых копий и архивов нет")
        return
    global issues
    issues += 1
    for text, _ in plan:
        say(WARN, text)


# ---------- чистка ----------

def rc_edits(path):
    """{файл: (строки без мусора, [что убрано])} и строки PATH из ~/.zshenv, которые надо перенести."""
    macports = macports_broken()
    move_zshenv = "/usr/ucb" in path
    edits, moved = {}, []
    for short, full in rc_files():
        lines = open(full, encoding="utf-8", errors="replace").read().split("\n")
        drop, seen = set(), {}
        for i, line in enumerate(lines):
            key = line.strip()
            if not key or key.startswith("#"):
                continue
            dead = dead_paths(line)
            dup = key in seen and ("PATH" in key or key.startswith((".", "source", "[", "eval")))
            if dead and all_dead(line) or dup or (macports and "/opt/local" in line):
                drop.add(i)
            elif short == "~/.zshenv" and move_zshenv and re.search(r"\bPATH=", line):
                drop.add(i)
                moved.append(line)
            seen.setdefault(key, i)
        # комментарий прямо над убранной строкой («# Add RVM to PATH…», «# MacPorts») — тоже
        for i in sorted(drop):
            j = i - 1
            while j >= 0 and lines[j].lstrip().startswith("#") and j not in drop:
                drop.add(j)
                j -= 1
        if drop:
            kept = [line for i, line in enumerate(lines) if i not in drop]
            text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip("\n")
            gone = [lines[i].strip() for i in sorted(drop) if not lines[i].lstrip().startswith("#")]
            edits[full] = (text + "\n" if text else "", gone)
    return edits, moved


def all_dead(line):
    """Строка ссылается только на то, чего нет (в строке PATH не осталось ни одной живой папки)."""
    m = re.search(r"\bPATH=(\"[^\"]*\"|'[^']*'|\S+)", line)
    if not m:
        return True
    parts = [expand(t) for t in m.group(1).strip("\"'").split(":") if "PATH" not in t]
    return all(p and not os.path.exists(p) for p in parts)


def trash(target):
    dest = os.path.join(HOME, ".Trash", os.path.basename(target.rstrip("/")))
    if os.path.lexists(dest):
        dest += time.strftime(" %H-%M-%S")
    cmd = ["mv", target, dest]
    mine = os.access(os.path.dirname(target.rstrip("/")) or "/", os.W_OK) and \
        (os.path.islink(target) or not os.path.isdir(target) or os.access(target, os.W_OK))
    if mine and subprocess.run(cmd, stderr=subprocess.DEVNULL).returncode == 0:
        return True
    return subprocess.run(["sudo"] + cmd).returncode == 0


def clean(path):
    plan = []  # (текст, действие)
    edits, moved = rc_edits(path)
    backup = os.path.join(os.path.dirname(CONFIG), "backup", "path-" + time.strftime("%Y%m%d-%H%M%S"))

    def write_rc():
        os.makedirs(backup, exist_ok=True)
        zshrc = os.path.join(HOME, ".zshrc")
        touched = set(edits) | ({zshrc} if moved else set())
        for f in touched:
            shutil.copy2(f, backup)
        for f, (text, _) in edits.items():
            if text:
                open(f, "w").write(text)
            else:
                trash(f)
        if moved:
            with open(zshrc, "a") as fh:
                fh.write("\n# перенесено из ~/.zshenv (mnrh path clean)\n" + "\n".join(moved) + "\n")
        return True

    for f, (text, gone) in edits.items():
        gone = [g for g in gone if g not in [m.strip() for m in moved]]
        if not gone:
            continue
        what = "файл станет пустым — в Корзину" if not text else f"убрать строк: {len(gone)}"
        plan.append((f"{tilde(f)}: {what}" + "".join(f"\n        {g if len(g) <= 100 else g[:99] + '…'}"
                                                    for g in gone), None))
    if moved:
        zshenv = os.path.join(HOME, ".zshenv")
        empty = zshenv in edits and not edits[zshenv][0]
        plan.append(("перенести из ~/.zshenv в ~/.zshrc" + (", ~/.zshenv станет пустым — в Корзину" if empty else "") +
                     "".join(f"\n        {m.strip()}" for m in moved) +
                     paint("\n        из-за раннего PATH в него попадает несуществующий /usr/ucb", "2"), None))
    if edits or moved:
        plan.append((f"бэкап файлов оболочки: {tilde(backup)}", write_rc))

    for f, why in dead_programs(path):
        plan.append((f"{tilde(f)} — {why}", lambda f=f: trash(f)))
    if os.path.isfile("/usr/local/go/bin/go") and os.path.exists("/opt/homebrew/bin/go"):
        for f in ("/usr/local/go", "/etc/paths.d/go"):
            if os.path.exists(f):
                plan.append((f"{f} — старый go, работает go из Homebrew", lambda f=f: trash(f)))
    if macports_broken():
        plan.append((f"MacPorts не работает на этой macOS: /opt/local ({human(du_bytes('/opt/local'))})", macports_remove))
    elif os.path.isdir("/Applications/MacPorts") and not os.path.isdir("/opt/local"):
        plan.append(("/Applications/MacPorts — остаток удалённого MacPorts", macports_remove))
    for v, name, home in spare_jdks(list_jdks()):
        plan.append((f"{tilde(home)} — {name if v in name else name + ' ' + v}, для Java {v.split('.')[0]} выбирается другая JDK",
                     lambda h=home: trash(h)))
    for old, why, used in old_brew():
        if not used:
            plan.append((f"Homebrew: {old} — {why}, ни от чего не зависит (и его настройки в etc)",
                         lambda o=old: brew_uninstall(o)))
    for d in brew_leftovers():
        plan.append((f"{d} — настройки уже удалённой формулы {os.path.basename(d)}", lambda d=d: trash(d)))
    plan += spare_plan(path)

    if not plan:
        print(f"{OK} Чистить нечего.")
        return
    print(paint("Что будет сделано:", "1"))
    for text, _ in plan:
        print(f"  • {text}")
    print(paint("\nВсё удаляемое — в Корзину, файлы оболочки — с бэкапом. Для системных папок спросит пароль.", "2"))
    if has_flag(args, "-n", "--dry-run"):
        print(paint("Это план (-n), ничего не менял.", "2"))
        return
    if not confirm("Выполнить?", has_flag(args, "-y", "--yes")):
        print("Отменено.")
        return
    failed = 0
    for text, action in plan:
        if action and not action():
            failed += 1
            print(f"{BAD} не вышло: {text.splitlines()[0]}")
    print(f"\n{OK} Готово." if not failed else f"\n{WARN} Готово, кроме {failed}.")
    print(paint("Открой новую вкладку Терминала, чтобы PATH обновился. Проверить: mnrh path", "2"))


def brew_uninstall(name):
    if subprocess.run([BREW, "uninstall", name]).returncode != 0:
        return False
    etc = os.path.join(os.path.dirname(os.path.dirname(BREW)), "etc", name)
    return trash(etc) if os.path.isdir(etc) else True


def macports_remove():
    for plist in glob.glob("/Library/LaunchDaemons/org.macports.*.plist"):
        subprocess.run(["sudo", "launchctl", "bootout", "system", plist], capture_output=True)
        trash(plist)
    ok = trash("/opt/local") if os.path.isdir("/opt/local") else True
    if os.path.isdir("/Applications/MacPorts"):
        ok = trash("/Applications/MacPorts") and ok
    return ok


path = login_path()
if not path:
    sys.exit("Не получилось узнать PATH из zsh.")
if "clean" in args:
    clean(path)
    sys.exit(0)
check_path(path)
check_rc()
check_links(path)
check_copies(path)
check_versions()
check_spare(path)
print()
print(f"{OK} Всё чисто." if not issues else paint("Ничего не менял. Убрать безопасное: mnrh path clean", "2"))
