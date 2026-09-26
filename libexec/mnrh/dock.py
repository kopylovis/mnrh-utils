import os
import plistlib
import subprocess
import sys
from urllib.parse import quote, unquote, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import BAD, CONFIG, OK, WARN, find_apps, has_flag, paint, run

args = sys.argv[1:]
ACTIONS = ("add", "remove", "clean", "undo")
if has_flag(args, "-h", "--help") or (args and args[0] not in ACTIONS):
    print("mnrh dock                  что закреплено в Dock; ✗ — приложения уже нет")
    print("mnrh dock clean            убрать значки удалённых приложений")
    print("mnrh dock add <Приложение>     закрепить (в конец)")
    print("mnrh dock remove <Приложение>  открепить")
    print("mnrh dock undo             отменить последнее изменение (можно повторять, до 10 шагов)")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)

BACKUP = os.path.join(os.path.dirname(CONFIG), "dock-backup.plist")


def export():
    out = subprocess.run(["defaults", "export", "com.apple.dock", "-"], capture_output=True).stdout
    return plistlib.loads(out) if out else {}


def history():
    try:
        with open(BACKUP, "rb") as f:
            return plistlib.load(f).get("history", [])
    except Exception:
        return []


def write_history(items):
    os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
    with open(BACKUP, "wb") as f:
        plistlib.dump({"history": items[-10:]}, f)


def save(data):
    write_history(history() + [export()])
    subprocess.run(["defaults", "import", "com.apple.dock", "-"], input=plistlib.dumps(data), check=True)
    run(["killall", "Dock"])


def tile_path(tile):
    url = tile.get("tile-data", {}).get("file-data", {}).get("_CFURLString", "")
    return unquote(urlparse(url).path).rstrip("/") if url.startswith("file://") else url


def tile_name(tile):
    return tile.get("tile-data", {}).get("file-label") or os.path.basename(tile_path(tile))[:-4]


def status():
    apps = export().get("persistent-apps", [])
    print(paint("Закреплено в Dock:", "1"))
    missing = 0
    for t in apps:
        if t.get("tile-type") == "spacer-tile" or t.get("tile-type") == "small-spacer-tile":
            print(paint("    ── разделитель", "2"))
            continue
        path = tile_path(t)
        ok = not path.startswith("/") or os.path.exists(path)
        missing += not ok
        print(f"  {OK if ok else BAD} {tile_name(t):<28} " + paint(path, "2") + ("" if ok else "  нет на диске"))
    if missing:
        print(f"\n{WARN} Значков удалённых приложений: {missing}" + paint("   -> mnrh dock clean", "2"))


def clean():
    data = export()
    apps = data.get("persistent-apps", [])
    keep = [t for t in apps if not tile_path(t).startswith("/") or os.path.exists(tile_path(t))]
    gone = [tile_name(t) for t in apps if t not in keep]
    if not gone:
        print("В Dock нет значков удалённых приложений.")
        return
    data["persistent-apps"] = keep
    save(data)
    print(f"{OK} Убрано: {', '.join(gone)}. Вернуть: mnrh dock undo")


def pick(query):
    apps = [a for a in find_apps(query)]
    if len(apps) != 1:
        sys.exit(("Подходит несколько: " + ", ".join(os.path.basename(a)[:-4] for a in apps)) if apps
                 else f"Не нашёл приложение «{query}»")
    return apps[0]


def add():
    app = pick(" ".join(args[1:]))
    data = export()
    apps = data.setdefault("persistent-apps", [])
    if any(tile_path(t) == app for t in apps):
        print(f"{os.path.basename(app)[:-4]} уже в Dock.")
        return
    apps.append({"tile-type": "file-tile", "tile-data": {
        "file-label": os.path.basename(app)[:-4],
        "file-data": {"_CFURLString": "file://" + quote(app) + "/", "_CFURLStringType": 15}}})
    save(data)
    print(f"{OK} {os.path.basename(app)[:-4]} закреплён в Dock. Вернуть: mnrh dock undo")


def remove():
    query = " ".join(args[1:]).lower()
    data = export()
    apps = data.get("persistent-apps", [])
    hit = [t for t in apps if query and (query == tile_name(t).lower() or query in tile_name(t).lower())]
    if len(hit) != 1:
        sys.exit(("Подходит несколько: " + ", ".join(tile_name(t) for t in hit)) if hit else f"В Dock нет «{query}»")
    data["persistent-apps"] = [t for t in apps if t is not hit[0]]
    save(data)
    print(f"{OK} {tile_name(hit[0])} откреплён. Вернуть: mnrh dock undo")


def undo():
    items = history()
    if not items:
        sys.exit("Нечего возвращать: Dock через mnrh ещё не меняли.")
    subprocess.run(["defaults", "import", "com.apple.dock", "-"], input=plistlib.dumps(items[-1]), check=True)
    run(["killall", "Dock"])
    write_history(items[:-1])
    left = len(items) - 1
    print(f"{OK} Dock на шаг назад." + (f" Ещё можно вернуть: {left}" if left else ""))


{"status": status, "add": add, "remove": remove, "clean": clean, "undo": undo}[args[0] if args else "status"]()
