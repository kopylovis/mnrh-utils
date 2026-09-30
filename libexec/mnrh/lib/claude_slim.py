import glob
import gzip
import json
import os
import re
import shutil
import time
import uuid

from mnrhlib import HOME, tilde

BACKUPS = os.path.join(HOME, ".cache", "mnrh", "slim-backup")
BACKUP_DAYS = 14
TEXT_LIMIT = 8000
HEAD, TAIL = 3000, 1500
PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
MARK = "mnrh slim"


class SlimError(Exception):
    pass


def kb(n):
    return f"{n / 1048576:.1f} МБ" if n >= 1048576 else f"{max(1, n // 1024)} КБ"


def scan(path):
    info = []
    with open(path, "rb") as f:
        for raw in f:
            item = {"len": len(raw)}
            try:
                o = json.loads(raw)
            except ValueError:
                info.append(item)
                continue
            if not isinstance(o, dict):
                info.append(item)
                continue
            item.update(uuid=o.get("uuid"), parent=o.get("parentUuid"), logical=o.get("logicalParentUuid"),
                        type=o.get("type"), side=bool(o.get("isSidechain")))
            if o.get("type") == "system" and o.get("subtype") == "compact_boundary":
                meta = o.get("compactMetadata") or {}
                keep = set((meta.get("preservedMessages") or {}).get("allUuids") or [])
                keep |= set((meta.get("preservedMessages") or {}).get("uuids") or [])
                seg = meta.get("preservedSegment") or {}
                keep |= {seg.get(k) for k in ("headUuid", "anchorUuid", "tailUuid") if seg.get(k)}
                item["boundary"] = True
                item["preserved"] = keep
            info.append(item)
    return info


def protection(info, keep):
    bounds = [i for i, it in enumerate(info) if it.get("boundary")]
    if len(bounds) < keep:
        return None
    cut = bounds[-keep]
    guarded = set()
    for i in bounds[-keep:]:
        guarded |= info[i]["preserved"]
    by_uuid = {it["uuid"]: it for it in info if it.get("uuid")}
    leaf = next((it for it in reversed(info) if it.get("uuid") and not it.get("side")), None)
    crossed, node, seen = 0, leaf, set()
    while node and node["uuid"] not in seen:
        seen.add(node["uuid"])
        guarded.add(node["uuid"])
        if node.get("boundary"):
            crossed += 1
            if crossed >= keep:
                break
            nxt = node.get("logical")
        else:
            nxt = node.get("parent")
        node = by_uuid.get(nxt)
    guarded.discard(None)
    flags = [i >= cut or (it.get("uuid") in guarded) for i, it in enumerate(info)]
    return {"cut": cut, "flags": flags, "bounds": len(bounds)}


def placeholder(kind, size):
    return f"[{kind} {kb(size)} удалён: {MARK}]"


def cut_text(text, stats):
    if len(text) <= TEXT_LIMIT or MARK in text[-200:]:
        return text
    stats["texts"] += 1
    return text[:HEAD] + f"\n… [обрезано {len(text) - HEAD - TAIL} символов: {MARK}] …\n" + text[-TAIL:]


def image_size(block):
    src = block.get("source") or {}
    return len(src.get("data") or "") if isinstance(src, dict) else 0


def slim_blocks(blocks, stats):
    out = []
    for b in blocks:
        if not isinstance(b, dict):
            out.append(b)
        elif b.get("type") == "image" and image_size(b) > 2048:
            stats["images"] += 1
            stats["image_bytes"] += image_size(b)
            out.append({"type": "text", "text": placeholder("скриншот", image_size(b) * 3 // 4)})
        elif b.get("type") == "text" and isinstance(b.get("text"), str):
            out.append(dict(b, text=cut_text(b["text"], stats)))
        elif b.get("type") == "tool_result":
            c = b.get("content")
            if isinstance(c, list):
                out.append(dict(b, content=slim_blocks(c, stats)))
            elif isinstance(c, str):
                out.append(dict(b, content=cut_text(c, stats)))
            else:
                out.append(b)
        else:
            out.append(b)
    return out


def slim_ui(v, stats, key=None):
    if isinstance(v, dict):
        if isinstance(v.get("base64"), str) and len(v["base64"]) > 2048:
            stats["images"] += 1
            stats["image_bytes"] += len(v["base64"])
            return dict(v, base64=PIXEL, type="image/png") if "type" in v else dict(v, base64=PIXEL)
        src = v.get("source")
        if v.get("type") == "image" and isinstance(src, dict) and isinstance(src.get("data"), str) \
                and len(src["data"]) > 2048:
            stats["images"] += 1
            stats["image_bytes"] += len(src["data"])
            return dict(v, source=dict(src, data=PIXEL, media_type="image/png"))
        return {k: (x if k in ("structuredPatch", "oldString", "newString") else slim_ui(x, stats, k))
                for k, x in v.items()}
    if isinstance(v, list):
        return [slim_ui(x, stats, key) for x in v]
    if isinstance(v, str) and len(v) > TEXT_LIMIT:
        return cut_text(v, stats)
    return v


def slim_line(o, stats):
    kind = o.get("type")
    if kind == "user":
        msg = o.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            o = dict(o, message=dict(msg, content=slim_blocks(msg["content"], stats)))
        elif isinstance(msg, dict) and isinstance(msg.get("content"), str) and not o.get("isCompactSummary"):
            o = dict(o, message=dict(msg, content=cut_text(msg["content"], stats)))
        if "toolUseResult" in o:
            o = dict(o, toolUseResult=slim_ui(o["toolUseResult"], stats))
    elif kind == "attachment" and isinstance(o.get("attachment"), dict):
        att = o["attachment"]
        if isinstance(att.get("prompt"), list):
            o = dict(o, attachment=dict(att, prompt=slim_blocks(att["prompt"], stats)))
    return o


def dump(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def write_slim(src, dst, info, prot, rename=None):
    stats = {"images": 0, "image_bytes": 0, "texts": 0, "changed": 0}
    sid_re = re.compile(rb'"sessionId":"' + re.escape(rename[0].encode()) + rb'"') if rename else None
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        for i, raw in enumerate(fi):
            if sid_re:
                raw = sid_re.sub(b'"sessionId":"' + rename[1].encode() + b'"', raw)
            if prot["flags"][i] or info[i].get("type") not in ("user", "attachment"):
                fo.write(raw)
                continue
            try:
                o = json.loads(raw)
            except ValueError:
                fo.write(raw)
                continue
            new = slim_line(o, stats)
            if new is o or dump(new) == dump(o):
                fo.write(raw)
                continue
            stats["changed"] += 1
            fo.write(dump(new).encode("utf-8") + (b"\n" if raw.endswith(b"\n") else b""))
    return stats


def verify(src, dst, info, prot, keep, rename=None):
    info2 = scan(dst)
    if len(info2) != len(info):
        raise SlimError(f"число строк изменилось: {len(info)} → {len(info2)}")
    prot2 = protection(info2, keep)
    if not prot2 or prot2["flags"] != prot["flags"]:
        raise SlimError("после сжатия иначе определяется то, что Claude Code отправит модели")
    back = (re.compile(rb'"sessionId":"' + re.escape(rename[1].encode()) + rb'"'),
            b'"sessionId":"' + rename[0].encode() + b'"') if rename else None
    with open(src, "rb") as fa, open(dst, "rb") as fb:
        for i, (a, b) in enumerate(zip(fa, fb)):
            if back:
                b = back[0].sub(back[1], b)
            if a == b:
                continue
            if prot["flags"][i]:
                raise SlimError(f"строка {i + 1} попадает в контекст модели, но изменилась")
            try:
                oa, ob = json.loads(a), json.loads(b)
            except ValueError:
                raise SlimError(f"строка {i + 1} перестала читаться как JSON")
            for k in ("uuid", "parentUuid", "logicalParentUuid", "type", "timestamp", "isSidechain", "sessionId"):
                if oa.get(k) != ob.get(k):
                    raise SlimError(f"в строке {i + 1} изменилось поле {k}")
            if set(oa) != set(ob):
                raise SlimError(f"в строке {i + 1} изменился набор полей")
    return True


def plan(path, keep=2):
    info = scan(path)
    prot = protection(info, keep)
    if not prot:
        return info, None, None
    tmp = path + ".mnrh-slim-probe"
    try:
        stats = write_slim(path, tmp, info, prot)
        stats["after"] = os.path.getsize(tmp)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    stats["before"] = os.path.getsize(path)
    stats["guarded"] = sum(1 for f in prot["flags"] if f)
    stats["bounds"] = prot["bounds"]
    return info, prot, stats


def prune_backups():
    cutoff = time.time() - BACKUP_DAYS * 86400
    for f in glob.glob(os.path.join(BACKUPS, "*")):
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
        except OSError:
            pass


def backup(path, sid, slim_size):
    os.makedirs(BACKUPS, mode=0o700, exist_ok=True)
    prune_backups()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    gz = os.path.join(BACKUPS, f"{sid}.{stamp}.jsonl.gz")
    with open(path, "rb") as fi, gzip.open(gz, "wb", compresslevel=1) as fo:
        shutil.copyfileobj(fi, fo, 4 << 20)
    os.chmod(gz, 0o600)
    with open(gz[:-9] + ".json", "w") as f:
        json.dump({"sid": sid, "path": path, "orig_size": os.path.getsize(path), "slim_size": slim_size,
                   "at": time.time()}, f)
    return gz


def apply(path, sid, keep=2):
    info = scan(path)
    prot = protection(info, keep)
    if not prot:
        raise SlimError(f"в сессии меньше {keep} сжатий /compact, сжимать нечего")
    tmp = path + ".mnrh-slim"
    try:
        stats = write_slim(path, tmp, info, prot)
        verify(path, tmp, info, prot, keep)
        shutil.copystat(path, tmp)
        stats["before"] = os.path.getsize(path)
        stats["after"] = os.path.getsize(tmp)
        stats["backup"] = backup(path, sid, stats["after"])
        if os.path.getsize(path) != stats["before"]:
            raise SlimError("сессия изменилась во время сжатия, повтори")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return stats


def copy(path, sid, title, keep=2):
    info = scan(path)
    prot = protection(info, keep)
    if not prot:
        raise SlimError(f"в сессии меньше {keep} сжатий /compact, сжимать нечего")
    new_sid = str(uuid.uuid4())
    dst = os.path.join(os.path.dirname(path), new_sid + ".jsonl")
    tmp = dst + ".mnrh-slim"
    try:
        stats = write_slim(path, tmp, info, prot, rename=(sid, new_sid))
        verify(path, tmp, info, prot, keep, rename=(sid, new_sid))
        with open(tmp, "ab") as f:
            f.write(dump({"type": "custom-title", "customTitle": f"{title} (сжатая копия)",
                          "sessionId": new_sid}).encode() + b"\n")
        shutil.copymode(path, tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    stats["before"] = os.path.getsize(path)
    stats["after"] = os.path.getsize(dst)
    stats["new_sid"] = new_sid
    return stats


def backups_of(sid):
    return sorted(glob.glob(os.path.join(BACKUPS, f"{sid}.*.jsonl.gz")))


def undo(path, sid):
    found = backups_of(sid)
    if not found:
        raise SlimError(f"резервной копии нет в {tilde(BACKUPS)} (хранятся {BACKUP_DAYS} дней)")
    gz = found[-1]
    with open(gz[:-9] + ".json") as f:
        meta = json.load(f)
    size = os.path.getsize(path)
    if size < meta["slim_size"]:
        raise SlimError("сессию после сжатия переписали (перенос или другая правка), вернуть автоматически нельзя; "
                        f"оригинал лежит в {tilde(gz)}")
    tmp = path + ".mnrh-undo"
    with gzip.open(gz, "rb") as fi, open(tmp, "wb") as fo:
        shutil.copyfileobj(fi, fo, 4 << 20)
        with open(path, "rb") as cur:
            cur.seek(meta["slim_size"])
            shutil.copyfileobj(cur, fo, 4 << 20)
    shutil.copystat(path, tmp)
    os.replace(tmp, path)
    os.remove(gz)
    os.remove(gz[:-9] + ".json")
    return size - meta["slim_size"]
