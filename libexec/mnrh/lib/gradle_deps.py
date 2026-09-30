import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from mnrhlib import HOME, projects_dir, tilde

CACHE = os.path.join(HOME, ".cache", "mnrh", "maven")
TTL = 6 * 3600
GOOGLE = "https://dl.google.com/dl/android/maven2"
CENTRAL = "https://repo1.maven.org/maven2"
PORTAL = "https://plugins.gradle.org/m2"
GOOGLE_GROUPS = ("androidx.", "android.arch.", "com.android.", "com.google.android.", "com.google.firebase",
                 "com.google.gms", "com.google.mlkit", "com.google.ar", "com.google.testing.platform")
PRE = re.compile(r"(alpha|beta|rc|cr|eap|dev|snapshot|preview|pre|milestone|m\d+$|b\d+$)", re.I)
RANK = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "m": 1, "milestone": 1, "rc": 2, "cr": 2, "preview": 0,
        "pre": 0, "dev": -1, "eap": -1, "snapshot": -2}


def version_key(v):
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", v.strip())
    if not m:
        return ((0,), -3, (), v)
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums = nums + (0,) * (4 - len(nums))
    rest = m.group(2).lstrip(".-_+")
    q = re.match(r"^([A-Za-z]+)[.-]?(\d*)", rest)
    if q and q.group(1).lower() in RANK:
        return (nums, RANK[q.group(1).lower()], tuple(int(x) for x in re.findall(r"\d+", rest)), v)
    return (nums, 3, tuple(int(x) for x in re.findall(r"\d+", rest)), v)


def flavor(v):
    rest = re.sub(r"^[\d.]+", "", v).lower()
    rest = re.sub(r"(alpha|beta|rc|cr|eap|dev|snapshot|preview|pre|milestone)[\d.]*|\bm\d+|\bb\d+", "", rest)
    return re.sub(r"[^a-z]", "", rest)


def is_stable(v):
    return not PRE.search(re.sub(r"^[\d.]+", "", v))


def fetch(url):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest() + ".xml")
    try:
        if time.time() - os.path.getmtime(path) < TTL:
            with open(path) as f:
                return f.read() or None
    except OSError:
        pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mnrh-deps"})
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        text = "" if e.code == 404 else None
    except (urllib.error.URLError, OSError, ValueError):
        text = None
    if text is not None:
        with open(path, "w") as f:
            f.write(text)
    return text or None


def metadata_urls(group, artifact, plugin=False):
    path = f"{group.replace('.', '/')}/{artifact}/maven-metadata.xml"
    google = group.startswith(GOOGLE_GROUPS) or group in ("com.google.gms.google-services",)
    order = [GOOGLE, CENTRAL] if google else [CENTRAL, GOOGLE]
    if plugin:
        order = ([GOOGLE] if google else []) + [PORTAL, CENTRAL]
    return [f"{base}/{path}" for base in order]


def versions_of(group, artifact, plugin=False):
    for url in metadata_urls(group, artifact, plugin):
        text = fetch(url)
        if text:
            found = re.findall(r"<version>([^<]+)</version>", text)
            if found:
                return found, url.split("/maven2")[0].split("/m2")[0]
    return [], None


def strip_comment(line):
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == "#" and not quoted:
            return line[:i]
    return line


def parse_catalog(path):
    sections, current = {"versions": {}, "libraries": {}, "plugins": {}}, None
    buf = ""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    for raw in lines:
        line = strip_comment(raw).strip()
        if not buf and (not line or line.startswith("#")):
            continue
        m = re.match(r"^\[([\w-]+)\]$", line)
        if m and not buf:
            current = m.group(1)
            continue
        buf = (buf + " " + line).strip()
        if buf.count("{") > buf.count("}"):
            continue
        entry, buf = buf, ""
        if current not in sections:
            continue
        m = re.match(r'^"?([\w.-]+)"?\s*=\s*(.+)$', entry)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value.startswith('"'):
            sections[current][key] = value.strip('"')
            continue
        fields = dict(re.findall(r'([\w.]+)\s*=\s*"([^"]*)"', value))
        sections[current][key] = fields
    return sections


def coordinates(sections):
    refs = {}
    direct = []
    for kind in ("libraries", "plugins"):
        for alias, spec in sections[kind].items():
            if isinstance(spec, str):
                parts = spec.split(":")
                if kind == "libraries" and len(parts) == 3:
                    direct.append((alias, parts[0], parts[1], parts[2], False))
                elif kind == "plugins" and len(parts) == 2:
                    direct.append((alias, parts[0], parts[0] + ".gradle.plugin", parts[1], True))
                continue
            if kind == "libraries":
                module = spec.get("module") or (f"{spec.get('group')}:{spec.get('name')}" if spec.get("group") else "")
                if ":" not in module:
                    continue
                group, artifact = module.split(":", 1)
                plugin = False
            else:
                group = spec.get("id")
                if not group:
                    continue
                artifact, plugin = group + ".gradle.plugin", True
            if spec.get("version.ref"):
                refs.setdefault(spec["version.ref"], []).append((alias, group, artifact, plugin))
            elif spec.get("version"):
                direct.append((alias, group, artifact, spec["version"], plugin))
    return refs, direct


def wrapper_version(project):
    path = os.path.join(project, "gradle", "wrapper", "gradle-wrapper.properties")
    try:
        with open(path) as f:
            m = re.search(r"gradle-([\d.]+(?:-[\w.]+)?)-(bin|all)\.zip", f.read())
            return m.group(1) if m else None
    except OSError:
        return None


def latest_gradle():
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "gradle-current.json")
    try:
        if time.time() - os.path.getmtime(path) < TTL:
            with open(path) as f:
                return json.load(f).get("version")
    except (OSError, ValueError):
        pass
    try:
        with urllib.request.urlopen("https://services.gradle.org/versions/current", timeout=15) as r:
            data = json.loads(r.read())
        with open(path, "w") as f:
            json.dump(data, f)
        return data.get("version")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def change_kind(cur, new):
    a, b = version_key(cur)[0], version_key(new)[0]
    if a[0] != b[0]:
        return "мажорная"
    if a[1] != b[1]:
        return "минорная"
    return "патч" if a[:3] != b[:3] else "сборка"


def check_one(key, current, targets):
    alias, group, artifact, plugin = targets[0]
    found, repo = versions_of(group, artifact, plugin)
    if not found:
        return {"key": key, "current": current, "error": f"{group}:{artifact} нет в Google Maven, Maven Central и Plugin Portal (свой репозиторий?)",
                "aliases": [t[0] for t in targets]}
    cur_key = version_key(current)
    found = [v for v in found if flavor(v) == flavor(current)] or found
    stable = [v for v in found if is_stable(v)]
    newest_stable = max(stable, key=version_key) if stable else None
    newest_any = max(found, key=version_key)
    out = {"key": key, "current": current, "module": f"{group}:{artifact.replace('.gradle.plugin', '')}",
           "aliases": [t[0] for t in targets], "plugin": plugin}
    if newest_stable and version_key(newest_stable) > cur_key:
        out["stable"] = newest_stable
        out["kind"] = change_kind(current, newest_stable)
    if newest_any != newest_stable and version_key(newest_any) > max(cur_key, version_key(newest_stable or "0")):
        out["pre"] = newest_any
    return out


def check_project(project, include_pre=False):
    catalog = os.path.join(project, "gradle", "libs.versions.toml")
    if not os.path.isfile(catalog):
        return None
    sections = parse_catalog(catalog)
    refs, direct = coordinates(sections)
    jobs = []
    for key, targets in refs.items():
        current = sections["versions"].get(key)
        if isinstance(current, dict):
            current = current.get("strictly") or current.get("require") or current.get("prefer")
        if current:
            jobs.append((key, current, targets))
    for alias, group, artifact, version, plugin in direct:
        jobs.append((alias, version, [(alias, group, artifact, plugin)]))
    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(lambda j: check_one(*j), jobs))
    gradle_now, gradle_new = wrapper_version(project), latest_gradle()
    return {"project": project, "results": results, "gradle": (gradle_now, gradle_new), "pre": include_pre}


def notes(report):
    by_key = {r["key"]: r for r in report["results"]}
    kotlin = next((r for r in report["results"] if r.get("module", "").startswith("org.jetbrains.kotlin.")
                   and r.get("plugin")), None)
    out = []
    ksp = next((r for r in report["results"] if "devtools.ksp" in r.get("module", "")), None)
    if kotlin and kotlin.get("stable") and ksp:
        out.append("Kotlin и KSP обновляй вместе: у KSP должна быть сборка под новую версию Kotlin.")
    agp = next((r for r in report["results"] if r.get("module", "").startswith("com.android.")
                and r.get("plugin") and r.get("stable")), None)
    if agp and agp.get("kind") in ("мажорная", "минорная"):
        out.append(f"AGP {agp['stable']} может требовать более новый Gradle — проверь таблицу совместимости AGP и Gradle.")
    cmp = next((r for r in report["results"] if r.get("module", "").startswith("org.jetbrains.compose")
                and r.get("stable")), None)
    if cmp and kotlin:
        out.append("Compose Multiplatform привязан к версии Kotlin: смотри таблицу совместимости JetBrains.")
    return out


def format_report(report, include_pre=False):
    project = report["project"]
    rows = [r for r in report["results"] if r.get("stable") or (include_pre and r.get("pre")) or r.get("error")]
    lines = [f"{os.path.basename(project)} ({tilde(project)}/gradle/libs.versions.toml): проверено "
             f"{len(report['results'])}, есть новее: {sum(1 for r in report['results'] if r.get('stable'))}"]
    now, new = report["gradle"]
    if now and new and version_key(new) > version_key(now):
        lines.append(f"  Gradle wrapper {now} → {new} ({change_kind(now, new)})")
    order = {"мажорная": 0, "минорная": 1, "патч": 2, "сборка": 3}
    rows.sort(key=lambda r: (0 if r.get("error") else 1, order.get(r.get("kind"), 4), r["key"]))
    for r in rows:
        if r.get("error"):
            lines.append(f"  ? {r['key']} {r['current']}: {r['error']}")
            continue
        target = r.get("stable") or r.get("pre")
        kind = r.get("kind") or "предварительная"
        extra = f", предварительная {r['pre']}" if include_pre and r.get("pre") and r.get("stable") else ""
        used = ", ".join(r["aliases"][:3]) + (f" и ещё {len(r['aliases']) - 3}" if len(r["aliases"]) > 3 else "")
        lines.append(f"  {r['key']}: {r['current']} → {target} ({kind}{extra}) — {used}")
    if len(rows) == 0:
        lines.append("  всё свежее")
    for n in notes(report):
        lines.append("  ! " + n)
    return "\n".join(lines)


def find_projects(query=None):
    root = projects_dir()
    if query:
        path = os.path.abspath(os.path.expanduser(query))
        if os.path.isdir(path):
            return [path]
        names = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))) if os.path.isdir(root) else []
        hits = [d for d in names if d.lower() == query.lower()] or [d for d in names if d.lower().startswith(query.lower())]
        return [os.path.join(root, d) for d in hits]
    if not os.path.isdir(root):
        return []
    return [os.path.join(root, d) for d in sorted(os.listdir(root))
            if os.path.isfile(os.path.join(root, d, "gradle", "libs.versions.toml"))]


def report_text(query=None, cwd=None, include_pre=False):
    if query:
        targets = find_projects(query)
        if not targets:
            return f"Проекта «{query}» нет в {tilde(projects_dir())}.", True
    elif cwd and os.path.isfile(os.path.join(cwd, "gradle", "libs.versions.toml")):
        targets = [cwd]
    else:
        targets = find_projects()
    reports = [check_project(p, include_pre) for p in targets]
    reports = [r for r in reports if r]
    if not reports:
        return "Нет gradle/libs.versions.toml ни в одном из проектов.", False
    return "\n\n".join(format_report(r, include_pre) for r in reports), False


TOOL = {
    "name": "deps_outdated",
    "description": (
        "Какие зависимости Gradle устарели: разбирает gradle/libs.versions.toml проекта (Android, KMP, Ktor), "
        "сверяет версии с Google Maven, Maven Central и Gradle Plugin Portal и версию Gradle wrapper. "
        "Показывает, что обновилось и насколько (мажорная, минорная, патч), какие библиотеки на этой версии и "
        "что нужно обновлять вместе (Kotlin и KSP, AGP и Gradle, Compose Multiplatform и Kotlin). "
        "Без project — проект текущей сессии, а если в нём нет каталога, все проекты. Сам ничего не меняет."),
    "annotations": {"readOnlyHint": True},
    "inputSchema": {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "имя папки проекта или путь; по умолчанию текущий"},
            "prerelease": {"type": "boolean", "description": "показывать и alpha/beta/rc"},
        },
    },
}


def handle(a, cwd=None):
    return report_text(a.get("project") or None, cwd or os.getcwd(), bool(a.get("prerelease")))
