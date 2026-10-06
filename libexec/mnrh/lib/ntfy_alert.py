import json
import os
import subprocess
import urllib.error
import urllib.request

from mnrhlib import HOME, read_config, tossy_path

LEGACY_CONFIGS = (os.path.join(HOME, ".config", "tossling", "config.json"), os.path.join(HOME, ".config", "tossy", "config.json"), os.path.join(HOME, ".config", "mnrh", "clip.json"))
DEFAULT_TOPIC = "mac"
CLAUDE_TOPIC = "claude"


class AlertError(Exception):
    pass


def tossy_config():
    path = tossy_path()
    if path:
        try:
            r = subprocess.run([path, "config", "--json"], capture_output=True, text=True, timeout=15)
            return json.loads(r.stdout or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {}
    for legacy in LEGACY_CONFIGS:
        try:
            with open(legacy) as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return {}


def endpoint():
    conf = tossy_config()
    server, token = conf.get("server", "").rstrip("/"), conf.get("token", "")
    return (server, token) if server and token else None


def configured():
    return endpoint() is not None


def phone_mode():
    value = read_config().get("notify_phone", "off")
    return value if value in ("away", "always") else "off"


def publish(topic, message, title="", priority=3, tags=None, click=None, markdown=False):
    target = endpoint()
    if not target:
        raise AlertError("Tossy не настроен: tossy setup")
    server, token = target
    payload = {"topic": topic, "message": message or title or "—", "priority": int(priority)}
    if title:
        payload["title"] = title
    if tags:
        payload["tags"] = list(tags)
    if click:
        payload["click"] = click
    if markdown:
        payload["markdown"] = True
    req = urllib.request.Request(server, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace").strip()
        if e.code in (401, 403):
            raise AlertError(f"сервер не пускает в канал {topic}: нужен доступ на запись (HTTP {e.code})")
        raise AlertError(f"HTTP {e.code} {detail}".strip())
    except (urllib.error.URLError, OSError) as e:
        raise AlertError(f"нет связи с сервером: {getattr(e, 'reason', e)}")
