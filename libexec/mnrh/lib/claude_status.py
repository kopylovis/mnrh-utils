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


def until(ts):
    try:
        left = int(ts) - time.time()
    except (TypeError, ValueError):
        return ""
    if left <= 0:
        return ""
    if left < 86400:
        return time.strftime("до %H:%M", time.localtime(int(ts)))
    return f"{int(left // 86400)} дн"


def claude_line(d):
    parts = []
    model = (d.get("model") or {}).get("display_name")
    if model:
        parts.append(color(model, CYAN))
    ctx = (d.get("context_window") or {}).get("used_percentage")
    if isinstance(ctx, (int, float)):
        parts.append(color(f"контекст {ctx:.0f}%", level(ctx, 60, 80)))
    limits = d.get("rate_limits") or {}
    for key, label in (("five_hour", "5 ч"), ("seven_day", "неделя")):
        lim = limits.get(key) or {}
        pct = lim.get("used_percentage")
        if isinstance(pct, (int, float)):
            when = until(lim.get("resets_at"))
            parts.append(color(f"{label} {pct:.0f}%" + (f" ({when})" if when and pct >= 50 else ""),
                               level(pct, 70, 90)))
    return parts


def work_line(d, path):
    parts = []
    cwd = (d.get("workspace") or {}).get("current_dir") or d.get("cwd") or os.getcwd()
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
        parts.append(color(text, YELLOW if g["ahead"] >= 5 or g["changed"] >= 20 else GREEN))
    m = cached("mac", 5, mac)
    if m.get("total"):
        free = m["total"] - m["used"]
        code = RED if free < GB or m["swap"] > 6 * GB else YELLOW if free < 2 * GB or m["swap"] > 3 * GB else ""
        text = f"память {gb(m['used'])}/{m['total'] / GB:.0f} ГБ"
        if m["swap"] >= 0.5 * GB:
            text += f" · swap {gb(m['swap'])}"
        parts.append(color(text, code))
    if m.get("daemons"):
        parts.append(color(f"Gradle {m['daemons']} ({size(m['daemon_mem'])})",
                           YELLOW if m["daemon_mem"] >= 1.5 * GB else ""))
    if m.get("sims"):
        parts.append(f"симуляторов {m['sims']}")
    if m.get("emulator"):
        parts.append("эмулятор")
    if path:
        sid = d.get("session_id") or os.path.basename(path)
        n = cached("size-" + sid, 30, lambda: session_size(path))
        if n >= 50 * 1048576:
            parts.append(color(f"сессия {size(n)}", level(n, 150 * 1048576, 400 * 1048576)))
    return parts


def main():
    try:
        d = json.load(sys.stdin)
    except ValueError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    sep = color(" · ", DIM)
    lines = [claude_line(d), work_line(d, d.get("transcript_path"))]
    print("\n".join(sep.join(p) for p in lines if p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
