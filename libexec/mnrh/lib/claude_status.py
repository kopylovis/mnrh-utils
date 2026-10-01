import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import HOME

CACHE = os.path.join(HOME, ".cache", "mnrh", "statusline")
GB = 1073741824
DIM, YELLOW, RED, GREEN, CYAN = "2", "33", "31", "32", "36"


def color(text, code):
    return f"\033[{code}m{text}\033[0m" if code else text


def level(value, warn, bad):
    return RED if value >= bad else YELLOW if value >= warn else ""


def run(cmd, timeout=2):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def cached(name, ttl, fn):
    path = os.path.join(CACHE, re.sub(r"[^\w.-]", "_", name)[-120:] + ".json")
    try:
        if time.time() - os.path.getmtime(path) < ttl:
            with open(path) as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    value = fn()
    try:
        os.makedirs(CACHE, exist_ok=True)
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(value, f)
        os.replace(tmp, path)
    except OSError:
        pass
    return value


def mac():
    vm = run(["vm_stat"])
    page = re.search(r"page size of (\d+)", vm)
    page = int(page.group(1)) if page else 16384

    def pages(name):
        m = re.search(r"^" + re.escape(name) + r":\s+(\d+)", vm, re.M)
        return int(m.group(1)) if m else 0

    sysctl = run(["sysctl", "-n", "hw.memsize", "vm.swapusage"]).splitlines()
    total = int(sysctl[0]) if sysctl and sysctl[0].isdigit() else 0
    swap = re.search(r"used = ([\d.]+)M", sysctl[1] if len(sysctl) > 1 else "")
    used = (pages("Anonymous pages") - pages("Pages purgeable") + pages("Pages wired down")
            + pages("Pages occupied by compressor")) * page
    daemons = daemon_mem = sims = 0
    emulator = False
    for line in run(["ps", "-axo", "rss=,command="]).splitlines():
        rss, _, cmd = line.strip().partition(" ")
        if re.search(r"GradleDaemon|KotlinCompileDaemon|kotlin-daemon", cmd):
            daemons += 1
            daemon_mem += int(rss) * 1024 if rss.isdigit() else 0
        elif "launchd_sim" in cmd:
            sims += 1
        elif re.search(r"/qemu-system-\w+", cmd):
            emulator = True
    return {"total": total, "used": used, "swap": float(swap.group(1)) * 1048576 if swap else 0,
            "daemons": daemons, "daemon_mem": daemon_mem, "sims": sims, "emulator": emulator}


def git(cwd):
    out = run(["git", "-C", cwd, "status", "--porcelain=v2", "--branch"], timeout=3)
    if not out:
        return None
    info = {"branch": None, "ahead": 0, "behind": 0, "upstream": False, "changed": 0}
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            info["branch"] = line.split(" ", 2)[2]
        elif line.startswith("# branch.upstream "):
            info["upstream"] = True
        elif line.startswith("# branch.ab "):
            m = re.match(r"# branch\.ab \+(\d+) -(\d+)", line)
            if m:
                info["ahead"], info["behind"] = int(m.group(1)), int(m.group(2))
        elif line and not line.startswith("#"):
            info["changed"] += 1
    return info


DEV_PORTS = [("next", 3000), ("nuxt", 3000), ("astro", 4321), ("@angular/core", 4200), ("react-scripts", 3000),
             ("gatsby", 8000), ("@remix-run/dev", 3000), ("expo", 8081), ("react-native", 8081),
             ("@react-router/dev", 5173), ("vite", 5173), ("webpack-dev-server", 8080)]
SERVER_NAMES = ["vite", "next", "nuxt", "astro", "ng", "react-scripts", "react-router", "remix", "gatsby", "webpack",
                "expo", "metro", "storybook", "wrangler", "vercel", "netlify", "parcel", "esbuild", "bun", "deno"]
WEB_DIRS = ["", "web", "frontend", "client", "site", "app"]


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_text(path, limit=200000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def project(cwd):
    root = run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], timeout=2).strip() or cwd
    kinds, ports, node_dir = set(), [], None
    try:
        names = set(os.listdir(root))
    except OSError:
        names = set()
    gradle = " ".join(read_text(os.path.join(root, n)) for n in
                      ("settings.gradle.kts", "settings.gradle", "build.gradle.kts", "build.gradle",
                       "gradle/libs.versions.toml") if os.path.exists(os.path.join(root, n)))
    if gradle:
        kinds.add("mobile" if re.search(r"com\.android|androidApplication|android\s*\{|kotlin\.multiplatform", gradle)
                  else "jvm")
    if "pubspec.yaml" in names or "iosApp" in names or any(n.endswith((".xcodeproj", ".xcworkspace")) for n in names):
        kinds.add("mobile")
    for sub in WEB_DIRS:
        pkg_path = os.path.join(root, sub, "package.json")
        if not os.path.isfile(pkg_path):
            continue
        pkg = read_json(pkg_path)
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        if not deps and not pkg.get("scripts"):
            continue
        kinds.add("web")
        node_dir = node_dir or os.path.join(root, sub)
        if "react-native" in deps or "expo" in deps:
            kinds.add("mobile")
        scripts = " ".join(str(v) for k, v in (pkg.get("scripts") or {}).items() if k in ("dev", "start", "serve"))
        m = re.search(r"(?:--port[ =]|-p\s+|PORT=)(\d{2,5})", scripts)
        if m:
            ports.append(int(m.group(1)))
        else:
            ports += [port for dep, port in DEV_PORTS if dep in deps][:1]
    return {"root": root, "kinds": sorted(kinds), "ports": sorted(set(ports)), "node_dir": node_dir}


def listeners():
    out, pid, found = run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pn"], timeout=3), None, {}
    for line in out.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("n") and pid:
            m = re.search(r":(\d+)$", line)
            if m:
                found.setdefault(pid, set()).add(int(m.group(1)))
    if not found:
        return []
    cwds, cur = {}, None
    for line in run(["lsof", "-a", "-d", "cwd", "-p", ",".join(found), "-Fn"], timeout=3).splitlines():
        if line.startswith("p"):
            cur = line[1:]
        elif line.startswith("n") and cur:
            cwds[cur] = line[1:]
    cmds = {}
    for line in run(["ps", "-o", "pid=,command=", "-p", ",".join(found)]).splitlines():
        pid, _, cmd = line.strip().partition(" ")
        cmds[pid] = cmd
    return [{"pid": pid, "ports": sorted(ports), "cwd": cwds.get(pid, ""), "name": server_name(cmds.get(pid, ""))}
            for pid, ports in found.items()]


def server_name(cmd):
    low = cmd.lower()
    for name in SERVER_NAMES:
        if re.search(r"(^|[/\s@])" + re.escape(name) + r"([/\s.-]|$)", low):
            return name
    exe = os.path.basename(cmd.split(" ")[0]) if cmd else "?"
    return exe


def node_check(node_dir):
    want, source = None, None
    for name in (".nvmrc", ".node-version"):
        text = read_text(os.path.join(node_dir, name)).strip()
        m = re.match(r"^v?(\d+)", text)
        if m:
            want, source = ("=", int(m.group(1))), name
            break
    if not want:
        engines = str((read_json(os.path.join(node_dir, "package.json")).get("engines") or {}).get("node") or "")
        m = re.search(r"(>=|\^|~)?\s*v?(\d+)", engines)
        if m:
            want, source = (">=" if m.group(1) == ">=" else "=", int(m.group(2))), "engines"
    if not want:
        return None
    m = re.match(r"v(\d+)", run(["node", "--version"]).strip())
    if not m:
        return {"text": f"нет node, нужна {want[1]} ({source})"}
    have = int(m.group(1))
    ok = have >= want[1] if want[0] == ">=" else have == want[1]
    if ok:
        return None
    need = f"{'≥' if want[0] == '>=' else ''}{want[1]}"
    return {"text": f"node {have}, нужна {need} ({source})"}


def session_size(path):
    total = 0
    try:
        total = os.path.getsize(path)
    except OSError:
        return 0
    extra = path[:-6] if path.endswith(".jsonl") else ""
    if extra and os.path.isdir(extra):
        for root, _, files in os.walk(extra):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
    return total


def gb(n):
    return f"{n / GB:.1f}"


def size(n):
    return f"{n / GB:.1f} ГБ" if n >= GB else f"{n / 1048576:.0f} МБ"


DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def until(ts):
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    left = ts - time.time()
    if left <= 0:
        return ""
    at = time.localtime(ts)
    if left < 3600:
        return f"через {max(1, int(left // 60))} мин"
    if time.strftime("%Y%m%d", at) == time.strftime("%Y%m%d"):
        return time.strftime("в %H:%M", at)
    return f"{DAYS[at.tm_wday]} {time.strftime('%H:%M', at)}"


def bar(pct, width=5):
    filled = min(width, int(round(pct / 100 * width)))
    return "▰" * filled + "▱" * (width - filled)


def claude_line(d):
    parts = []
    model = (d.get("model") or {}).get("display_name")
    if model:
        parts.append((4, color(model, CYAN)))
    ctx = (d.get("context_window") or {}).get("used_percentage")
    if isinstance(ctx, (int, float)):
        parts.append((1, color(f"контекст {ctx:.0f}%", level(ctx, 60, 80))))
    limits = d.get("rate_limits") or {}
    for key, label in (("five_hour", "лимит 5 ч"), ("seven_day", "неделя")):
        lim = limits.get(key) or {}
        pct = lim.get("used_percentage")
        if isinstance(pct, (int, float)):
            when = until(lim.get("resets_at"))
            text = f"{label} {bar(pct)} {pct:.0f}%" if key == "five_hour" else f"{label} {pct:.0f}%"
            if when and (key == "five_hour" or pct >= 50):
                text += f", сброс {when}"
            code = level(pct, 70, 90)
            parts.append((1 if key == "five_hour" or code else 3, color(text, code)))
    return parts


def dev_parts(proj):
    parts = []
    root = proj["root"]
    servers = cached("listeners", 5, listeners)
    mine = [s for s in servers if s["cwd"] == root or s["cwd"].startswith(root.rstrip("/") + "/")]
    for s in mine:
        parts.append((1, color(f"{s['name']} :{','.join(map(str, s['ports'][:2]))}", GREEN)))
    busy = {p for s in mine for p in s["ports"]}
    for port in proj["ports"]:
        if port in busy:
            continue
        other = next((s for s in servers if port in s["ports"]), None)
        if other:
            owner = os.path.basename(other["cwd"].rstrip("/")) if other["cwd"] not in ("", "/") else other["name"]
            parts.append((1, color(f":{port} занят: {owner}", YELLOW)))
    return parts


def work_line(d, path):
    parts = []
    cwd = (d.get("workspace") or {}).get("current_dir") or d.get("cwd") or os.getcwd()
    proj = cached("project-" + cwd, 30, lambda: project(cwd))
    kinds = set(proj["kinds"])
    g = cached("git-" + cwd, 3, lambda: git(cwd))
    if g and g.get("branch"):
        text = g["branch"]
        if g["ahead"]:
            text += f" ↑{g['ahead']}"
        if g["behind"]:
            text += f" ↓{g['behind']}"
        if not g["upstream"] and g["branch"] != "(detached)":
            text += " (не запушена)"
        if g["changed"]:
            text += f" ±{g['changed']}"
        parts.append((1, color(text, YELLOW if g["ahead"] >= 5 or g["changed"] >= 20 else GREEN)))
    if "web" in kinds:
        parts += dev_parts(proj)
        if proj.get("node_dir"):
            nc = cached("node-" + proj["node_dir"], 60, lambda: node_check(proj["node_dir"]))
            if nc:
                parts.append((1, color(nc["text"], YELLOW)))
    m = cached("mac", 5, mac)
    tight = False
    if m.get("total"):
        free = m["total"] - m["used"]
        code = RED if free < GB or m["swap"] > 6 * GB else YELLOW if free < 2 * GB or m["swap"] > 3 * GB else ""
        tight = bool(code)
        parts.append((2 if code else 3, color(f"память {gb(m['used'])}/{m['total'] / GB:.0f} ГБ", code)))
        if m["swap"] >= 0.5 * GB:
            parts.append((2 if code else 5, color(f"swap {gb(m['swap'])}", code)))
    builds = kinds & {"mobile", "jvm"}
    if m.get("daemons") and (builds or tight or m["daemon_mem"] >= 1.5 * GB):
        heavy = m["daemon_mem"] >= 1.5 * GB
        parts.append((2 if heavy else 3, color(f"Gradle {m['daemons']} ({size(m['daemon_mem'])})", YELLOW if heavy else "")))
    if "mobile" in kinds or tight:
        if m.get("sims"):
            parts.append((3, f"симуляторов {m['sims']}"))
        if m.get("emulator"):
            parts.append((3, "эмулятор"))
    if path:
        sid = d.get("session_id") or os.path.basename(path)
        n = cached("size-" + sid, 30, lambda: session_size(path))
        if n >= 50 * 1048576:
            code = level(n, 150 * 1048576, 400 * 1048576)
            parts.append((3 if code == RED else 5, color(f"файл сессии {size(n)}", code)))
    return parts


def main():
    try:
        d = json.load(sys.stdin)
    except ValueError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    try:
        cols = int(os.environ.get("COLUMNS") or 0)
    except ValueError:
        cols = 0
    lines = [fit(p, cols) for p in (claude_line(d), work_line(d, d.get("transcript_path")))]
    print("\n".join(line for line in lines if line))
    return 0


ANSI = re.compile(r"\033\[[0-9;]*m")
SEP = " · "


def fit(parts, cols):
    parts = list(parts)
    width = lambda ps: sum(len(ANSI.sub("", t)) for _, t in ps) + len(SEP) * max(0, len(ps) - 1)
    limit = cols - 4 if cols else 0
    while limit and len(parts) > 1 and width(parts) > limit:
        worst = max(p for p, _ in parts)
        if worst <= 1:
            break
        idx = max(i for i, (p, _) in enumerate(parts) if p == worst)
        parts.pop(idx)
    return color(SEP, DIM).join(t for _, t in parts)


if __name__ == "__main__":
    sys.exit(main())
