import glob
import json
import os
import re
import shlex
from datetime import datetime

from mnrhlib import HOME, tilde

CLAUDE = os.environ.get("MNRH_CLAUDE_HOME", os.path.join(HOME, ".claude"))
PROJECTS = os.path.join(CLAUDE, "projects")
INDEX = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"), "mnrh", "sessions-index")
VERSION = 2
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
LOCAL = ("<command-name>", "<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>",
         "<task-notification>")
ROLES = {"user": "ты", "claude": "claude", "tool": "команда", "summary": "сводка"}
TOOL_CAP = 400
READ_BUDGET = 40000


def index_path(sid):
    return os.path.join(INDEX, sid + ".json")


def drop(sid):
    try:
        os.remove(index_path(sid))
    except OSError:
        pass


def sessions():
    out = {}
    for path in glob.glob(os.path.join(glob.escape(PROJECTS), "*", "*.jsonl")):
        sid = os.path.basename(path)[:-6]
        if UUID.match(sid) and os.path.isfile(path):
            out[sid] = path
    return out


def tool_line(block):
    name = block.get("name") or "?"
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    if name == "Bash":
        text = "$ " + str(inp.get("command") or "")
    else:
        arg = next((str(inp[k]) for k in ("file_path", "path", "pattern", "url", "query", "description", "prompt")
                    if inp.get(k)), "")
        text = f"{name} {arg}".strip()
    return text[:TOOL_CAP]


def extract(o):
    kind = o.get("type")
    msg = o.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    out = []
    if kind == "user":
        if o.get("isMeta"):
            return out
        role = "summary" if o.get("isCompactSummary") else "user"
        texts = [content] if isinstance(content, str) else \
            [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
        for t in texts:
            if not isinstance(t, str) or any(m in t for m in LOCAL):
                continue
            t = REMINDER.sub("", t).strip()
            if t:
                out.append((role, t))
    elif kind == "assistant" and isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and (b.get("text") or "").strip():
                out.append(("claude", b["text"].strip()))
            elif b.get("type") == "tool_use":
                out.append(("tool", tool_line(b)))
    return out


def load_index(sid):
    try:
        with open(index_path(sid), encoding="utf-8") as f:
            data = json.load(f)
        return data if data.get("v") == VERSION else None
    except (OSError, ValueError):
        return None


def save_index(sid, data):
    os.makedirs(INDEX, mode=0o700, exist_ok=True)
    tmp = f"{index_path(sid)}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, index_path(sid))


def update(sid, path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    data = load_index(sid)
    if not data or data.get("path") != path or data.get("ino") != st.st_ino or data.get("offset", 0) > st.st_size:
        data = {"v": VERSION, "path": path, "ino": st.st_ino, "offset": 0, "cwd": None,
                "custom": None, "ai": None, "prompt": None, "first": None, "last": None, "msgs": []}
    if data["offset"] == st.st_size:
        return data
    with open(path, "rb") as f:
        f.seek(data["offset"])
        chunk = f.read(st.st_size - data["offset"])
    end = chunk.rfind(b"\n") + 1
    for raw in chunk[:end].split(b"\n"):
        if not raw.strip():
            continue
        try:
            o = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(o, dict):
            continue
        kind = o.get("type")
        if kind == "custom-title" and o.get("customTitle"):
            data["custom"] = o["customTitle"]
        elif kind == "ai-title" and o.get("aiTitle"):
            data["ai"] = o["aiTitle"]
        elif kind == "last-prompt" and o.get("lastPrompt"):
            data["prompt"] = o["lastPrompt"]
        if data["cwd"] is None and o.get("cwd"):
            data["cwd"] = o["cwd"]
        ts = o.get("timestamp") if isinstance(o.get("timestamp"), str) else None
        for role, text in extract(o):
            data["msgs"].append([role, ts, text])
            if ts:
                data["first"] = data["first"] or ts
                data["last"] = ts
    data["offset"] += end
    if end:
        save_index(sid, data)
    return data


def refresh():
    found = sessions()
    out = {}
    for sid, path in found.items():
        data = update(sid, path)
        if data:
            out[sid] = data
    for f in glob.glob(os.path.join(glob.escape(INDEX), "*.json")):
        if os.path.basename(f)[:-5] not in found:
            try:
                os.remove(f)
            except OSError:
                pass
    return out


def title(sid, data):
    try:
        with open(os.path.join(os.path.dirname(data["path"]), sid, "custom-title.json")) as f:
            custom = json.load(f).get("customTitle")
            if custom:
                return custom
    except (OSError, ValueError, AttributeError):
        pass
    return data.get("custom") or data.get("ai") or data.get("prompt") or "без названия"


def folder(data):
    from mnrhlib import DEV
    cwd = home_of(data)
    if cwd.startswith(DEV + "/") and "/" not in cwd[len(DEV) + 1:]:
        return cwd[len(DEV) + 1:]
    return tilde(cwd) if cwd else os.path.basename(os.path.dirname(data["path"]))


_HOMES = {}


def home_of(data):
    pdir = os.path.basename(os.path.dirname(data["path"]))
    if pdir not in _HOMES:
        from claude_sessions import decode
        _HOMES[pdir] = decode(pdir)
    return _HOMES[pdir] or data.get("cwd") or ""


def when(ts, year=False):
    if not ts:
        return "?"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ts[:16]
    return dt.strftime("%d.%m.%Y %H:%M" if year else "%d.%m %H:%M")


def age_days(ts):
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return (datetime.now(dt.tzinfo) - dt).total_seconds() / 86400


def terms_of(query):
    try:
        parts = shlex.split(query)
    except ValueError:
        parts = query.split()
    return [p.lower() for p in parts if p.strip()]


def snippet(text, terms, width=320):
    flat = " ".join(text.split())
    low = flat.lower()
    pos = min((low.find(t) for t in terms if t in low), default=0)
    start = max(0, pos - width // 3)
    piece = flat[start:start + width]
    return ("…" if start else "") + piece + ("…" if start + width < len(flat) else "")


def matches_project(data, project):
    p = project.lower().strip()
    cwd = home_of(data).lower()
    return p in (os.path.basename(cwd), cwd) or os.path.basename(cwd).startswith(p) or \
        (p in ("~", "root", "home") and cwd == HOME.lower())


def search(query, project=None, days=None, roles=None, limit=8, exclude=None):
    terms = terms_of(query)
    if not terms:
        return "Пустой запрос: дай хотя бы одно слово.", True
    idx = refresh()
    exclude = set(exclude or [])
    hits, partial, seen_terms = [], [], set()
    for sid, data in idx.items():
        if sid in exclude or (project and not matches_project(data, project)):
            continue
        if days and (age_days(data.get("last")) or 0) > days:
            continue
        full, half = [], []
        for i, (role, ts, text) in enumerate(data["msgs"]):
            if roles and role not in roles:
                continue
            low = text.lower()
            got = [t for t in terms if t in low]
            seen_terms.update(got)
            if len(got) == len(terms):
                full.append(i)
            elif len(got) * 2 >= len(terms) and len(terms) > 1:
                half.append((len(got), i))
        if full:
            hits.append((sid, data, full))
        elif half:
            partial.append((sid, data, [i for _, i in sorted(half, reverse=True)]))

    def last_ts(entry):
        sid, data, found = entry
        return max((data["msgs"][i][1] or "" for i in found), default="")

    exact = bool(hits)
    chosen = sorted(hits or partial, key=last_ts, reverse=True)
    scope = (f" в проекте «{project}»" if project else "") + (f" за {days} дн." if days else "")
    if not chosen:
        missing = [t for t in terms if t not in seen_terms]
        text = f"По запросу «{query}»{scope} ничего не нашёл (просмотрено сессий: {len(idx)})."
        if missing:
            text += " Нигде не встречается: " + ", ".join(missing) + ". Попробуй основу слова (подпис, а не подписью)."
        return text, False
    lines = [f"Сессий с совпадениями{scope}: {len(chosen)}"
             f"{'' if exact else ' (только частичные: не все слова в одном сообщении)'}. "
             f"Показаны {min(limit, len(chosen))}, свежие сверху."]
    for sid, data, found in chosen[:limit]:
        lines.append("")
        lines.append(f"■ {sid[:8]}  {folder(data)}  {when(data.get('last'))}  «{title(sid, data)}»  "
                     f"совпадений: {len(found)}")
        for i in found[:3]:
            role, ts, text = data["msgs"][i]
            lines.append(f"  #{i} [{ROLES.get(role, role)} {when(ts)}] {snippet(text, terms)}")
        if len(found) > 3:
            more = ", ".join(f"#{i}" for i in found[3:18])
            lines.append(f"  ещё: {more}{' …' if len(found) > 18 else ''}")
    lines.append("")
    lines.append("Прочитать место целиком: session_read с id и at=<номер после #>.")
    return "\n".join(lines), False


def resolve(ref, idx):
    ref = (ref or "").strip().lower()
    if not ref:
        return None, "Нужен id сессии (первые 8 символов из session_search)."
    hits = [sid for sid in idx if sid.startswith(ref)]
    if not hits:
        hits = [sid for sid, d in idx.items() if ref in title(sid, d).lower()]
    if len(hits) == 1:
        return hits[0], None
    if hits:
        return None, f"«{ref}» подходит к {len(hits)} сессиям, дай больше символов id."
    return None, f"Сессии «{ref}» нет."


def read(ref, at=None, query=None, before=2, after=8, max_chars=2000, full=False):
    idx = refresh()
    sid, err = resolve(ref, idx)
    if err:
        return err, True
    data = idx[sid]
    msgs = data["msgs"]
    total = len(msgs)
    head = (f"Сессия {sid}  «{title(sid, data)}»\nпапка: {tilde(home_of(data) or '?')} · "
            f"{when(data.get('first'), True)} — {when(data.get('last'), True)} · сообщений: {total}")
    if not total:
        return head + "\nВ ней нет ни одного сообщения.", False
    note = ""
    if query:
        terms = terms_of(query)
        found = [i for i, (_, _, t) in enumerate(msgs) if all(x in t.lower() for x in terms)]
        if not found:
            return head + f"\n«{query}» в этой сессии не встречается.", False
        at = found[0] if at is None else at
        if len(found) > 1:
            note = "Совпадения в сессии: " + ", ".join(f"#{i}" for i in found[:30]) + (" …" if len(found) > 30 else "")
    if at is None:
        at = 0
    at = int(at)
    if at < 0:
        at = max(0, total + at)
    at = min(at, total - 1)
    before, after = max(0, int(before)), max(0, int(after))
    lo, hi = max(0, at - before), min(total, at + after + 1)
    lines = [head]
    if note:
        lines.append(note)
    if lo:
        lines.append(f"… раньше ещё {lo} (at={max(0, lo - 1)})")
    budget = READ_BUDGET
    shown_to = lo
    for i in range(lo, hi):
        role, ts, text = msgs[i]
        cap = len(text) if full else max_chars
        body = text if len(text) <= cap else text[:cap] + f"\n… (+{len(text) - cap} симв., целиком: at={i} full=true)"
        block = f"\n#{i} [{ROLES.get(role, role)} {when(ts)}]{' ◀' if i == at else ''}\n{body}"
        if budget - len(block) < 0 and i > at:
            break
        budget -= len(block)
        lines.append(block)
        shown_to = i + 1
    if shown_to < total:
        lines.append(f"\n… дальше ещё {total - shown_to} (at={shown_to})")
    return "\n".join(lines), False


SEARCH_TOOL = {
    "name": "session_search",
    "description": (
        "Поиск по всем прошлым сессиям Claude Code на этом Mac (все проекты): запросы пользователя, твои ответы, "
        "выполненные команды и сводки после /compact. Используй, когда пользователь ссылается на прошлую работу "
        "(«как мы делали…», «в прошлый раз», «в сессии sightra»), или когда решение, скорее всего, уже "
        "находили раньше. Все слова должны встретиться в одном сообщении, регистр не важен, слово ищется как "
        "подстрока — давай основы слов (подпис, keystore, fastlane). Фразу — в кавычках. Текущая сессия "
        "не ищется. Дальше открывай найденное через session_read."),
    "annotations": {"readOnlyHint": True},
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "слова через пробел, фраза в кавычках"},
            "project": {"type": "string", "description": "только в этом проекте: имя папки или его начало, «~» — домашний каталог"},
            "days": {"type": "integer", "description": "только сессии, в которых что-то было за последние N дней"},
            "roles": {"type": "array", "items": {"type": "string", "enum": list(ROLES)},
                      "description": "где искать: user — запросы, claude — ответы, tool — команды, summary — сводки; по умолчанию везде"},
            "limit": {"type": "integer", "description": "сколько сессий показать, по умолчанию 8"},
        },
        "required": ["query"],
    },
}

READ_TOOL = {
    "name": "session_read",
    "description": (
        "Прочитать кусок прошлой сессии Claude Code: сообщения вокруг номера из session_search. "
        "Без at и query — начало сессии, at=-10 — последние сообщения. Длинные сообщения обрезаются, "
        "full=true — целиком."),
    "annotations": {"readOnlyHint": True},
    "inputSchema": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "id сессии или его начало (8 символов из session_search)"},
            "at": {"type": "integer", "description": "номер сообщения (#N из session_search); отрицательный — с конца"},
            "query": {"type": "string", "description": "перейти к первому сообщению с этими словами в этой сессии"},
            "before": {"type": "integer", "description": "сколько сообщений до, по умолчанию 2"},
            "after": {"type": "integer", "description": "сколько сообщений после, по умолчанию 8"},
            "full": {"type": "boolean", "description": "не обрезать длинные сообщения"},
        },
        "required": ["id"],
    },
}

TOOLS = [SEARCH_TOOL, READ_TOOL]


def handle(name, a, current=None):
    if name == "session_search":
        roles = [r for r in a.get("roles") or [] if r in ROLES] or None
        return search(str(a.get("query") or ""), project=a.get("project") or None, days=a.get("days") or None,
                      roles=roles, limit=int(a.get("limit") or 8), exclude=[current] if current else None)
    return read(a.get("id"), at=a.get("at"), query=a.get("query") or None,
                before=a.get("before", 2), after=a.get("after", 8), full=bool(a.get("full")))
