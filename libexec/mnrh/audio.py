import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import BAD, OK, has_flag, paint, run
from swiftagent import SwiftAgent

args = sys.argv[1:]
ACTIONS = ("out", "in", "keep-mic")
if has_flag(args, "-h", "--help") or (args and args[0] not in ACTIONS):
    print("mnrh audio                  звуковые устройства: куда идёт звук, откуда берётся микрофон")
    print("mnrh audio out <название>   переключить выход (достаточно части названия: mnrh audio out buds)")
    print("mnrh audio in <название>    переключить микрофон")
    print("mnrh audio keep-mic on      Bluetooth-наушники не забирают микрофон: иначе macOS включает режим")
    print("                            гарнитуры, и звук в них становится глухим и моно. off — выключить")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)

agent = SwiftAgent("audio", "mnrh audio", "com.mnrh.audio")
KIND = {"builtin": "встроенное", "bluetooth": "Bluetooth", "usb": "USB", "virtual": "виртуальное",
        "display": "монитор", "airplay": "AirPlay", "other": ""}


def devices():
    agent.build()
    items = []
    for line in run([agent.bin, "--list"]).splitlines():
        parts = line.split("\t")
        if len(parts) >= 6:
            uid, name, kind, ins, outs, marks = parts[:6]
            items.append({"uid": uid, "name": name, "kind": kind, "in": int(ins), "out": int(outs),
                          "default_in": "in" in marks, "default_out": "out" in marks})
    return items


def show(items, key, title):
    print(paint(title, "1"))
    for d in items:
        if d[key]:
            mark = paint("▶", "32") if d["default_" + key] else " "
            print(f"  {mark} {d['name']:<34} {paint(KIND.get(d['kind'], ''), '2')}")


def status():
    items = devices()
    show(items, "out", "Выход (звук):")
    print()
    show(items, "in", "Вход (микрофон):")
    s = agent.state()
    print()
    if s:
        print(f"{OK} Микрофон держится встроенным, возвращал его: {s.get('restored', 0)} раз")
    else:
        print(paint("Bluetooth-наушники могут забрать микрофон и испортить звук   -> mnrh audio keep-mic on", "2"))


def switch(key):
    query = " ".join(a for a in args[1:] if not a.startswith("-")).lower()
    if not query:
        sys.exit(f"mnrh audio {args[0]} <часть названия>")
    items = [d for d in devices() if d[key]]
    found = [d for d in items if query in d["name"].lower() or query == d["uid"].lower()]
    if len(found) != 1:
        names = ", ".join(d["name"] for d in (found or items))
        sys.exit(("Подходит несколько: " if found else "Нет такого. Есть: ") + names)
    d = found[0]
    ok = run([agent.bin, "--set-output" if key == "out" else "--set-input", d["uid"]]) == "" and \
        any(x["uid"] == d["uid"] and x["default_" + key] for x in devices())
    print(f"{OK} {'Звук' if key == 'out' else 'Микрофон'}: {d['name']}" if ok else f"{BAD} Не переключилось на {d['name']}")


def keep_mic():
    if len(args) < 2 or args[1] not in ("on", "off"):
        sys.exit("mnrh audio keep-mic on|off")
    if args[1] == "off":
        agent.uninstall()
        print("Микрофон больше не удерживается: macOS снова решает сама.")
        return
    agent.build()
    agent.write_plist(["--keep-mic"])
    if agent.start():
        print(f"{OK} Bluetooth-наушники больше не забирают микрофон, работает и после перезагрузки.")
        print(paint("  Для звонка через микрофон наушников: mnrh audio keep-mic off", "2"))
    else:
        print(f"{BAD} Помощник не запустился. Лог: {agent.log}")


{"status": status, "out": lambda: switch("out"), "in": lambda: switch("in"),
 "keep-mic": keep_mic}[args[0] if args else "status"]()
