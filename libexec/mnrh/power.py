import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import BAD, OK, WARN, has_flag, paint, run

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or args:
    print("mnrh power   питание и нагрев: зарядник и сколько он даёт, сколько тянет Mac сейчас,")
    print("             троттлинг, здоровье батареи, кто грузит процессор")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)


def battery():
    out = run(["ioreg", "-rn", "AppleSmartBattery", "-w0"])

    def num(key):
        m = re.search(rf'"{key}" = (-?\d+)', out)
        return int(m.group(1)) if m else None

    def sub(key, field):
        m = re.search(rf'"{key}" = \{{[^\n]*?"{field}"=(-?\d+)', out)
        return int(m.group(1)) if m else None

    name = re.search(r'"AdapterDetails" = \{[^\n]*?"Name"="([^"]+)"', out)
    return {
        "present": bool(out),
        "percent": num("CurrentCapacity"),
        "raw_max": num("AppleRawMaxCapacity"),
        "design": num("DesignCapacity"),
        "cycles": num("CycleCount"),
        "temp": (num("Temperature") or 0) / 100,
        "voltage": num("Voltage"),
        "amperage": num("InstantAmperage") if num("InstantAmperage") is not None else num("Amperage"),
        "external": re.search(r'"ExternalConnected" = Yes', out) is not None,
        "charging": re.search(r'"IsCharging" = Yes', out) is not None,
        "full": re.search(r'"FullyCharged" = Yes', out) is not None,
        "adapter_w": sub("AdapterDetails", "Watts"),
        "adapter_name": name.group(1) if name else None,
        "load_mw": sub("PowerTelemetryData", "SystemLoad"),
        "thermal_limited": sub("ChargerData", "TimeChargingThermallyLimited"),
    }


def section(title):
    print(f"\n{paint(title, '1')}")


def power(b):
    section("Питание")
    load_w = (b["load_mw"] or 0) / 1000
    if b["external"]:
        adapter = f"{b['adapter_name'] or 'зарядка'}, отдаёт {b['adapter_w']} Вт" if b["adapter_w"] else "от сети"
        print(f"  {OK} {adapter}")
        if b["adapter_w"] and load_w:
            share = load_w / b["adapter_w"]
            level = BAD if share > 0.95 else WARN if share > 0.8 else OK
            print(f"  {level} Mac сейчас тянет {load_w:.0f} Вт — {share:.0%} от зарядки"
                  + (". Под сборкой батарея будет разряжаться даже от сети" if share > 0.95 else ""))
        if b["adapter_w"] and b["adapter_w"] < 60:
            print(f"  {WARN} Зарядка слабая для M1 Pro под нагрузкой (нужно 67–96 Вт): сборки могут замедляться")
    else:
        drain = abs((b["voltage"] or 0) * (b["amperage"] or 0)) / 1e6
        print(f"  {WARN} От батареи" + (f", расход {drain:.0f} Вт" if drain else ""))
    lpm = re.search(r"lowpowermode\s+(\d)", run(["pmset", "-g"]))
    if lpm and lpm.group(1) == "1":
        print(f"  {WARN} Включён режим энергосбережения: процессор работает медленнее")


def thermal(b):
    section("Нагрев")
    therm = run(["pmset", "-g", "therm"])
    limit = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", therm)
    warn = re.search(r"thermal warning level (?:set to|of) (\d+)", therm)
    if limit and int(limit.group(1)) < 100:
        print(f"  {BAD} Троттлинг: процессор ограничен до {limit.group(1)}% скорости")
    elif warn and warn.group(1) != "0":
        print(f"  {WARN} macOS отметила перегрев (уровень {warn.group(1)})")
    else:
        print(f"  {OK} Троттлинга нет")
    if b["present"]:
        t = b["temp"]
        print(f"  {WARN if t >= 40 else OK} Батарея {t:.0f} °C" + (" — горячая, зарядка может замедлиться" if t >= 40 else ""))
    top = run(["top", "-l", "2", "-s", "1", "-n", "6", "-o", "cpu", "-stats", "cpu,command"], timeout=15)
    rows = []
    started = 0
    for line in top.splitlines():
        if line.startswith("%CPU"):
            started += 1
            continue
        if started == 2 and line.strip():
            cpu, _, cmd = line.strip().partition(" ")
            try:
                if float(cpu) >= 10 and cmd.strip() != "top":
                    rows.append(f"{cmd.strip()} {float(cpu):.0f}%")
            except ValueError:
                pass
    if rows:
        print(f"  Грузят процессор: {', '.join(rows[:5])}")


def health(b):
    if not b["present"]:
        return
    section("Батарея")
    state = "заряжается" if b["charging"] else "заряжена" if b["full"] else "не заряжается" if b["external"] else "разряжается"
    print(f"  {b['percent']}%, {state}")
    if b["raw_max"] and b["design"]:
        h = b["raw_max"] / b["design"]
        level = BAD if h < 0.8 else WARN if h < 0.85 else OK
        print(f"  {level} Ёмкость {h:.0%} от новой ({b['raw_max']} из {b['design']} мА·ч)"
              + (" — ниже 80%, Apple считает это поводом для замены" if h < 0.8 else ""))
    if b["cycles"] is not None:
        level = WARN if b["cycles"] >= 900 else OK
        print(f"  {level} Циклов {b['cycles']} из 1000, на которые рассчитана батарея")
    cond = re.search(r"Condition: (.+)", run(["system_profiler", "SPPowerDataType"], timeout=20))
    if cond and cond.group(1).strip() != "Normal":
        print(f"  {WARN} Состояние по оценке macOS: {cond.group(1).strip()}")
    if b["external"] and b["percent"] and b["percent"] >= 95 and not b["charging"]:
        print(paint("  Mac всё время на зарядке: ограничение заряда до 80% (Настройки → Аккумулятор →"
                    " Оптимизированная зарядка, или batt/AlDente) продлит жизнь батарее.", "2"))


b = battery()
power(b)
thermal(b)
health(b)
