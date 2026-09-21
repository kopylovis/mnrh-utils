import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import confirm, du_bytes, has_flag, human, paint, run, simulator_memory, version_key

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or (args and args[0] not in ("stop", "clean")):
    print("mnrh sim                     запущенные симуляторы, устройства, runtime")
    print("mnrh sim stop                остановить все симуляторы и показать, сколько памяти вернулось")
    print("mnrh sim clean               удалить устройства без runtime")
    print("mnrh sim clean --runtimes    ещё runtime, которые не запускались 60 дней (кроме новейшего)")
    print("                             без терминала нужен -y")
    sys.exit(0)
cmd = args[0] if args else "status"
assume_yes = has_flag(args, "-y", "--yes")


def ago(stamp):
    if not stamp:
        return "ни разу", None
    days = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).days
    return ("сегодня" if days == 0 else f"{days} дн. назад"), days


def load():
    devices = json.loads(run(["xcrun", "simctl", "list", "devices", "-j"]) or "{}").get("devices", {})
    runtimes = json.loads(run(["xcrun", "simctl", "runtime", "list", "-j"]) or "{}")
    return devices, runtimes


def old_runtimes(runtimes):
    by_platform = {}
    for rid, r in runtimes.items():
        by_platform.setdefault(r.get("platformIdentifier", ""), []).append((rid, r))
    old = []
    for rts in by_platform.values():
        newest = max(rts, key=lambda x: version_key(x[1].get("version", "0")))[0]
        for rid, r in rts:
            _, days = ago(r.get("lastUsedAt"))
            if rid != newest and (days is None or days > 60):
                old.append((rid, r))
    return old


def status():
    devices, runtimes = load()
    flat = [(rt, d) for rt, lst in devices.items() for d in lst]
    booted = [(rt, d) for rt, d in flat if d.get("state") == "Booted"]
    if booted:
        print(paint("Запущены:", "1"))
        for rt, d in booted:
            kind = d.get("deviceTypeIdentifier", "").split(".")[-1].replace("-", " ")
            print(f"  {d['name']} ({kind}, {rt.split('.')[-1].replace('-', ' ', 1).replace('-', '.')})")
        print(f"  память всех симуляторов: {human(simulator_memory())}" + paint("   -> mnrh sim stop", "2"))
    else:
        print("Запущенных симуляторов нет.")

    unavailable = [d for _, d in flat if not d.get("isAvailable", True)]
    data = sum(d.get("dataPathSize", 0) for _, d in flat)
    print(f"\n{paint('Устройства:', '1')} {len(flat)}, данные на диске {human(data)}"
          + (f", без runtime: {len(unavailable)}" + paint("   -> mnrh sim clean", "2") if unavailable else ""))
    for rt, d in sorted(flat, key=lambda x: -x[1].get("dataPathSize", 0)):
        when, _ = ago(d.get("lastBootedAt"))
        print(f"  {human(d.get('dataPathSize', 0)):>9}  {d['name']:<24} запуск: {when}")

    if runtimes:
        print(f"\n{paint('Runtime:', '1')}")
        by_rt = {}
        for rt, d in flat:
            by_rt[rt] = by_rt.get(rt, 0) + 1
        old_ids = {rid for rid, _ in old_runtimes(runtimes)}
        for rid, r in sorted(runtimes.items(), key=lambda x: version_key(x[1].get("version", "0")), reverse=True):
            when, _ = ago(r.get("lastUsedAt"))
            used_by = by_rt.get(r.get("runtimeIdentifier", ""), 0)
            mark = paint("   -> mnrh sim clean --runtimes", "2") if rid in old_ids else ""
            print(f"  {human(r.get('sizeBytes', 0)):>9}  iOS {r.get('version', '?'):<10} запуск: {when:<13} устройств: {used_by}{mark}")


def stop():
    before = simulator_memory()
    run(["xcrun", "simctl", "shutdown", "all"], timeout=120)
    time.sleep(3)
    after = simulator_memory()
    print(f"Симуляторы остановлены. Память симуляторов: {human(before)} -> {human(after)}")


def clean():
    devices, runtimes = load()
    unavailable = [d for lst in devices.values() for d in lst if not d.get("isAvailable", True)]
    if unavailable:
        run(["xcrun", "simctl", "delete", "unavailable"], timeout=120)
        print(f"Удалено устройств без runtime: {len(unavailable)}")
    else:
        print("Устройств без runtime нет.")
    if not has_flag(args, "--runtimes"):
        return
    old = old_runtimes(runtimes)
    if not old:
        print("Старых runtime нет: всё либо новейшее, либо запускалось за последние 60 дней.")
        return
    total = sum(r.get("sizeBytes", 0) for _, r in old)
    for _, r in old:
        when, _ = ago(r.get("lastUsedAt"))
        print(f"  {human(r.get('sizeBytes', 0)):>9}  iOS {r.get('version')}  запуск: {when}")
    if not confirm(f"Удалить эти runtime, {human(total)}?", assume_yes):
        print("Отменено.")
        return
    for rid, r in old:
        run(["xcrun", "simctl", "runtime", "delete", rid], timeout=300)
        print(f"  удалён iOS {r.get('version')}")
    print("macOS освобождает место в фоне, свободное пространство вырастет через минуту-две.")


{"status": status, "stop": stop, "clean": clean}[cmd]()
