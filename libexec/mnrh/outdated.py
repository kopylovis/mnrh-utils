import glob
import os
import plistlib
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import HOME, OK, WARN, gradle_versions_in_use, has_flag, paint, run, version_key

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or args:
    print("mnrh outdated   что пора обновить: macOS и программы Apple, Homebrew, Android Studio и SDK,")
    print("                Gradle в проектах. Только показывает, ничего не ставит (нужна сеть, до 30 секунд)")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)

SDK = os.environ.get("ANDROID_HOME") or os.path.join(HOME, "Library", "Android", "sdk")
STUDIO = "/Applications/Android Studio.app"


def macos():
    out = run(["softwareupdate", "-l"], timeout=90)
    items = re.findall(r"Title: ([^,]+), Version: ([^,]+),.*?(Action: restart)?,?\s*$", out, re.M)
    return [(t.strip(), v.strip(), bool(r)) for t, v, r in items]


def brew():
    if not shutil.which("brew"):
        return None
    env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1")
    r = subprocess.run(["brew", "outdated", "--verbose"], capture_output=True, text=True, env=env, timeout=120)
    return [l for l in r.stdout.splitlines() if l.strip()]


def android():
    info = {}
    try:
        with open(os.path.join(STUDIO, "Contents", "Info.plist"), "rb") as f:
            p = plistlib.load(f)
        info["studio"] = p.get("CFBundleShortVersionString")
    except OSError:
        pass
    managers = sorted(glob.glob(os.path.join(SDK, "cmdline-tools", "*", "bin", "sdkmanager")))
    latest = [m for m in managers if "/latest/" in m]
    manager = (latest or managers or [None])[0]
    info["tools"] = os.path.basename(os.path.dirname(os.path.dirname(manager))) if manager else None
    if manager:
        env = dict(os.environ)
        jbr = os.path.join(STUDIO, "Contents", "jbr", "Contents", "Home")
        if not env.get("JAVA_HOME") and os.path.isdir(jbr):
            env["JAVA_HOME"] = jbr
        try:
            out = subprocess.run([manager, "--list", "--newer"], capture_output=True, text=True,
                                 env=env, timeout=120).stdout
        except subprocess.TimeoutExpired:
            out = ""
        info["old_tools"] = "only understands SDK XML versions up to" in out
        updates = out.split("Available Updates:", 1)[1] if "Available Updates:" in out else ""
        info["updates"] = [re.sub(r"\s*\|\s*", " ", l).strip() for l in updates.splitlines()
                           if "|" in l and not l.strip().startswith(("ID", "--", "Path"))]
    return info


def gradle_latest():
    out = run(["curl", "-sS", "--max-time", "10", "https://services.gradle.org/versions/current"])
    m = re.search(r'"version"\s*:\s*"([^"]+)"', out)
    return m.group(1) if m else None


def section(title):
    print(f"\n{paint(title, '1')}")


print("Проверяю обновления, это до 30 секунд...", end="", flush=True)
with ThreadPoolExecutor(max_workers=4) as pool:
    f_mac, f_brew, f_android, f_gradle = (pool.submit(macos), pool.submit(brew), pool.submit(android),
                                          pool.submit(gradle_latest))
    mac, formulae, droid, gradle_now = f_mac.result(), f_brew.result(), f_android.result(), f_gradle.result()
print("\r" + " " * 45 + "\r", end="")

section("macOS и программы Apple")
if not mac:
    print(f"  {OK} Обновлений нет")
for title, version, restart in mac:
    name = title if title.endswith(version) else f"{title} {version}"
    print(f"  {WARN} {name}" + paint("  (с перезагрузкой)" if restart else "", "2"))
if mac:
    print(paint("  Поставить: Настройки → Основные → Обновление ПО", "2"))

if formulae is not None:
    section("Homebrew")
    if not formulae:
        print(f"  {OK} Всё свежее")
    else:
        names = [l.split()[0] for l in formulae]
        print(f"  {WARN} Устарело пакетов: {len(names)}")
        print(paint("  " + ", ".join(names[:25]) + (" …" if len(names) > 25 else ""), "2"))
        print(paint("  Обновить: brew upgrade (а сначала посмотреть: brew outdated)", "2"))

section("Android")
if droid.get("studio"):
    print(f"  Android Studio {droid['studio']}" + paint("  (обновляется из самой студии: Help → Check for Updates)", "2"))
if droid.get("tools") is None:
    print(f"  {WARN} Нет command-line tools: SDK Manager → SDK Tools → Android SDK Command-line Tools (latest)")
elif droid.get("old_tools") or droid.get("tools") != "latest":
    print(f"  {WARN} Command-line tools устарели ({droid['tools']}): не понимают новый формат SDK.")
    print(paint("  Поставить свежие: SDK Manager → SDK Tools → Android SDK Command-line Tools (latest)", "2"))
if droid.get("updates"):
    print(f"  {WARN} Обновления SDK: {len(droid['updates'])}")
    for u in droid["updates"][:10]:
        print(paint(f"    {u}", "2"))
elif droid.get("tools") == "latest" and not droid.get("old_tools"):
    print(f"  {OK} SDK свежий")

section("Gradle в проектах")
used = gradle_versions_in_use()
if not used:
    print("  Проектов с Gradle wrapper нет.")
for v in sorted(used, key=version_key, reverse=True):
    behind = gradle_now and version_key(v) < version_key(gradle_now)
    mark = WARN if behind else OK
    print(f"  {mark} {v:<8} {', '.join(sorted(used[v]))}")
if gradle_now and any(version_key(v) < version_key(gradle_now) for v in used):
    print(paint(f"  Последний Gradle {gradle_now}. Обновить проект: ./gradlew wrapper --gradle-version {gradle_now}", "2"))
