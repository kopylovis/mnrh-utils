import json
import os
import re
import shlex
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import HOME, read_config, tilde, write_config

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
RULES = os.path.join(ROOT, "share", "mnrh", "humanize.md")
STATE = os.path.join(HOME, ".cache", "mnrh", "humanize")
LOG = os.path.join(HOME, "Library", "Logs", "mnrh-humanize.log")
CLAUDE = os.environ.get("MNRH_CLAUDE_HOME", os.path.join(HOME, ".claude"))
SETTINGS = os.path.join(CLAUDE, "settings.json")
SLASH_FILE = os.path.join(CLAUDE, "commands", "humanize.md")
MNRH = shutil.which("mnrh") or os.path.join(ROOT, "bin", "mnrh")
HOOK_CMD = "claude humanize-hook"
BADGE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"),
                     "promptsill", "badges", "mnrh-humanize.json")
HOOK_EVENTS = [("SessionStart", False), ("UserPromptSubmit", False), ("Stop", True)]
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

REMINDER = ("mnrh humanization включена, правила письма из начала сессии действуют для всего, что ты пишешь людям. "
            "Сразу суть, без вступлений и итогов-пересказов. Конкретные факты вместо значимости и восторгов. "
            "Без \"не только... но и\", троек, \"от... до...\", хвостов \"..., подчеркивая\". Длинное тире только "
            "где его требует грамматика (в английском никогда), пункты списка \"термин — пояснение\" запрещены. "
            "Без жирного, нумерованных жирных и курсивных подзаголовков, \"---\", списков \"Заголовок: текст\" и эмодзи. "
            "Кавычки только прямые \"\", елочки нельзя. Букву е с точками не пиши, только е. Двоеточий избегай. "
            "Текст, который пользователь отправит людям, пиши как сообщение в мессенджере (правило 4.7). "
            "Перед отправкой перечитай по разделу 5.")
OFF_NOTE = "mnrh humanization выключена, правила письма из этой сессии больше не обязательны, пиши как обычно."

S = r"(?:^|(?<=[.!?…]\s)|(?<=\n))"
MARKERS = [
    ("значимость", r"\b(?:игра\w*|сыгра\w*)\s+(?:важн|ключев|значительн|решающ|центральн|огромн|заметн)\w*\s+рол"),
    ("значимость", r"\b(?:служ|выступа|являет|станов|стал)\w*\s+(?:символ|напоминани)"),
    ("значимость", r"\bсимволизиру|\bподч[её]ркива\w*\s+(?:важност|значимост|значени)|\b(?:ключев|поворотн)\w*\s+момент"
                   r"|\bнеизгладим|\b(?:устойчив|длительн)\w*\s+влияни|\bнепоколебим"),
    ("значимость", r"\b(?:play(?:s|ed|ing)?)\s+an?\s+(?:pivotal|crucial|vital|key|significant|important|central)\s+role"
                   r"|\bserves?\s+as\s+an?\s+(?:testament|reminder|symbol)|\ba\s+testament\s+to|\bpivotal\s+moment"
                   r"|\benduring\s+legacy|\bindelible|\bunderscor(?:e|es|ed|ing)\b"),
    ("хвост-деепричастие", r",\s+(?:подч[её]ркивая|демонстрируя|отражая|способствуя|содействуя|обеспечивая|выделяя"
                           r"|символизируя|усиливая|воплощая|знаменуя|укрепляя)\b"),
    ("хвост-деепричастие", r",\s+(?:highlighting|underscoring|showcasing|reflecting|emphasizing|demonstrating"
                           r"|contributing\s+to|ensuring|fostering|symbolizing|solidifying|cementing)\b"),
    ("реклама", r"\bбогат\w*\s+(?:культурн|истори|наслед|традиц)|\bзахватывающ|\bживописн|\bпотрясающ|\bв\s+самом\s+сердце"
                r"|\bможет\s+похвастаться|\bпродолжает\s+(?:очаровывать|процветать|вдохновлять|радовать)|\bвеков\w*\s+наслед"
                r"|\bдостойн\w*\s+(?:посещения|внимания)|\bбесшовн|\bне\s+имеющ\w*\s+аналогов"),
    ("реклама", r"\b(?:vibrant|breathtaking|stunning|nestled|captivating|renowned|seamless(?:ly)?|cutting-edge"
                r"|game[- ]changer)\b|\bin\s+the\s+heart\s+of\b|\bboasts?\b|\brich\s+(?:cultural|history|heritage|tapestry)"
                r"|\bunlock\w*\s+the\s+(?:power|potential)"),
    ("освещение в СМИ", r"\b(?:освещал\w*|писали|упоминал\w*)\s+(?:в|во)\s+(?:нескольких|ряде|ведущих|многочисленных|различных)"
                        r"|\bнезависим\w*\s+освещени|\bfeatured\s+in\s+(?:several|numerous|multiple)"),
    ("дидактика", r"\b(?:важно|стоит|следует|необходимо|нужно|критически\s+важно|крайне\s+важно)\s+"
                  r"(?:отметить|учесть|учитывать|помнить|запомнить|подчеркнуть|упомянуть|понимать)\b|\bобратите\s+внимание"
                  r"|\b(?:значения|результаты|показатели)\s+могут\s+(?:варьироваться|различаться)|\bчтобы\s+избежать\s+путаницы"
                  r"|\bкрайне\s+важно"),
    ("дидактика", r"\bit'?s\s+(?:important|worth|crucial|essential|vital)\s+(?:to\s+(?:note|remember|mention|keep)|noting)"
                  r"|\bkeep\s+in\s+mind\b|\bmay\s+vary\b"),
    ("итог-пересказ", S + r"\s*(?:в\s+заключение|подводя\s+итоги?|вкратце|в\s+целом|итак|резюмируя|таким\s+образом)\b"),
    ("итог-пересказ", S + r"\s*(?:in\s+conclusion|in\s+summary|to\s+sum\s+up|overall,|ultimately,|in\s+essence)"),
    ("итог-пересказ", r"(?m)^\s*(?:#+\s*|\*\*)?(?:заключение|выводы|итоги|подводя\s+итоги|conclusion|summary|key\s+takeaways)"
                      r"\s*(?:\*\*)?:?\s*$"),
    ("проблемы-перспективы", r"\bнесмотря\s+на\s+(?:эти|все|свои|многочисленн|определённ|определенн)"
                             r"|\bсталкива\w*\s+с\s+(?:рядом|определённ\w*|определенн\w*|многочисленн\w*|некоторыми)\s+(?:проблем|трудност|вызов)"
                             r"|\bперспектив\w*\s+на\s+будущее|\bоткрыва\w*\s+новые\s+(?:возможности|горизонты)|\bпрокладыва\w*\s+путь"),
    ("проблемы-перспективы", r"\bfaces?\s+(?:several|numerous|many|a\s+number\s+of)\s+challenges|\bfuture\s+outlook"
                             r"|\bpaves?\s+the\s+way|\bcontinues?\s+to\s+(?:thrive|flourish)"),
    ("чат-бот", r"(?:^|\n)\s*(?:конечно|безусловно|разумеется|отличный\s+вопрос|хороший\s+вопрос|давайте\s+разбер\w*)\b[,!.:]?"),
    ("чат-бот", r"(?:^|\n)\s*(?:certainly|of\s+course|absolutely|sure)[!,.]|\bgreat\s+question\b|\blet'?s\s+dive\s+in\b"),
    ("чат-бот", r"\bнадеюсь,\s+(?:это|эта|мой|я)\s+(?:помог|был|было|информац)|\b(?:дайте|сообщите)\s+(?:мне\s+)?знать"
                r"|\bсообщите\s+мне\b|\bесли\s+(?:у\s+вас\s+)?(?:возникнут|появятся|будут|остались)\s+(?:ещё\s+|еще\s+)?вопросы"
                r"|\bвы\s+(?:абсолютно|совершенно)\s+правы|\bне\s+стесняйтесь|\bболее\s+подробн\w*\s+анализ"),
    ("чат-бот", r"\bi\s+hope\s+this\s+helps|\blet\s+me\s+know\s+if|\bfeel\s+free\s+to|\byou'?re\s+absolutely\s+right"
                r"|\bhappy\s+to\s+help\b"),
    ("дисклеймер", r"\bна\s+момент\s+(?:моего\s+)?(?:обучения|последнего\s+обновления)|\bмо(?:ё|е|его|им)\s+последн\w*\s+обновлени"
                   r"|\bкак\s+(?:языковая\s+модель|ии\b|искусственный\s+интеллект)|\b(?:информаци|сведени|данн)\w*\s+(?:ограничен|скудн|немного)"
                   r"|\bне\s+(?:широко\s+)?задокументирован\w*(?:\s+широко)?|\bв\s+(?:предоставленных|доступных|имеющихся)\s+"
                   r"(?:источниках|результатах)|\bна\s+основе\s+(?:имеющейся|доступной)\s+информации|\bниже\s+представлен"),
    ("дисклеймер", r"\bas\s+of\s+my\s+(?:last|latest|knowledge)|\bas\s+an\s+ai\b|\bbased\s+on\s+(?:the\s+)?available\s+information"
                   r"|\blimited\s+information\s+(?:is\s+)?available|\bhere'?s\s+a\s+(?:comprehensive|detailed)\s+overview"),
    ("обтекаемо", r"\b(?:эксперты|специалисты|аналитики|критики|исследователи)\s+(?:считают|отмечают|утверждают|полагают|сходятся)"
                  r"|\bпо\s+(?:словам|мнению)\s+экспертов|\bотраслев\w*\s+(?:отч[её]т|исследовани)|\bнекоторые\s+(?:критики|эксперты)"
                  r"|\bшироко\s+признан|\bописыва\w*\s+как\b"),
    ("обтекаемо", r"\bexperts\s+(?:say|believe|agree|note)|\bindustry\s+reports|\bsome\s+critics|\bwidely\s+(?:regarded|considered)"
                  r"|\bis\s+described\s+as"),
    ("не только… но и", r"\bне\s+только\b[^.!?\n]{0,150}\bно\s+и\b|\b(?:это|речь\s+ид[её]т|дело)\s+не\s+(?:просто|только)\b"
                        r"|\bне\s+просто\b[^.!?\n]{0,60}[,—–-]\s*(?:а|это)\b"),
    ("не только… но и", r"\bnot\s+only\b[^.!?\n]{0,150}\bbut\s+also\b|\bit'?s\s+not\s+(?:just|only|merely)\b[^.!?\n]{0,80}\bit'?s\b"
                        r"|\bit'?s\s+not\s+about\b[^.!?\n]{0,80}\bit'?s\s+about"),
    ("письмо-шаблон", r"застанет\s+вас\s+в\s+добром\s+здравии|\bя\s+пишу,\s+чтобы|\bhope\s+this\s+(?:e-?mail|message)\s+finds\s+you"
                      r"|\bi\s+am\s+writing\s+to\b"),
    ("канцелярит", r"\bосуществл\w*|\bв\s+рамках\s+данн|\bданн(?:ый|ая|ое|ого|ому|ой|ую|ом)\b|\bв\s+целях\b|\bна\s+сегодняшний\s+день"
                   r"|\bявля\w*\s+неотъемлем|\bв\s+настоящий\s+момент\s+времени"),
    ("слова нейросети", r"\b(?:delv(?:e|es|ed|ing)|tapestry|multifaceted|holistic|synergy|utiliz(?:e|es|ed|ing)|leverag(?:e|es|ed|ing)"
                        r"|facilitat(?:e|es|ed|ing)|embark(?:s|ed|ing)?|realm|meticulous(?:ly)?|intricate)\b"
                        r"|\bnavigat\w*\s+the\s+complexit|\bthe\s+\w+\s+landscape\b"),
    ("UTM-метка", r"utm_source=(?:openai|chatgpt)"),
    ("заглушка", r"\[(?:имя|название|укажите|опишите|вставьте|ваш|ваше|name|insert|your|describe|placeholder)[^\]\n]{0,60}\]"),
    ("жирный заголовок пункта", r"(?m)^\s*(?:[-*•]|\d+[.)])\s+\*\*[^*\n]+\*\*|^\s*\*\*\d+[.)]|\*\*[^*\n]{1,60}:\*\*"),
    ("курсивный подзаголовок", r"(?m)^\s*(?:\*(?!\*)[^*\n]{2,60}\*|_[^_\n]{2,60}_)(?=[\s:(]|$)"),
    ("разделитель ---", r"(?m)^\s*(?:-{3,}|\*{3,}|_{3,})\s*$"),
    ("термин — пояснение в списке", r"(?m)^\s*(?:[-*•]|\d+[.)])\s+[^\n—]{0,60}\s—\s"),
    ("эмодзи", "[\U0001F300-\U0001FAFF☀-⛿✅✔✨❌⭐➡]"),
    ("пафос", r"!(?:\s|$)|(?:^|\n)\s*(?:и\s+вот\s+главное|самое\s+интересное|вот\s+в\s+ч[её]м\s+фишка)\b"),
]
MERISM = re.compile(r"\bот\s+([^\d.!?,;:\n]{2,40}?)\s+до\s+([^\d.!?,;:\n]{2,40}?)(?=[,.;!?\n]|$)", re.I)
COMPILED = [(name, re.compile(rx, re.I)) for name, rx in MARKERS]


def settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def missing_hooks():
    try:
        hooks = settings().get("hooks", {})
    except ValueError:
        return [e for e, _ in HOOK_EVENTS]
    return [event for event, _ in HOOK_EVENTS
            if not any(HOOK_CMD in h.get("command", "") for e in hooks.get(event, []) for h in e.get("hooks", []))]


def install_hooks():
    """Подключает хуки humanize и /humanize, если их нет; остальное в settings.json не трогает."""
    missing = missing_hooks()
    if missing:
        try:
            data = settings()
        except ValueError as e:
            print(f"✗ {tilde(SETTINGS)} не читается как JSON ({e}), хуки не подключил")
            return False
        hooks = data.setdefault("hooks", {})
        for event, run_async in HOOK_EVENTS:
            if event in missing:
                hook = {"type": "command", "command": f"{shlex.quote(MNRH)} {HOOK_CMD}", "timeout": 10}
                if run_async:
                    hook["async"] = True
                hooks.setdefault(event, []).append({"hooks": [hook]})
        os.makedirs(CLAUDE, exist_ok=True)
        tmp = SETTINGS + ".mnrh-tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, SETTINGS)
        print(f"✓ подключил хуки {', '.join(missing)} в {tilde(SETTINGS)}")
    if not os.path.exists(SLASH_FILE):
        import claude_restart
        os.makedirs(os.path.dirname(SLASH_FILE), exist_ok=True)
        with open(SLASH_FILE, "w") as f:
            f.write(claude_restart.EXTRA_SLASH["humanize"])
        print(f"✓ /humanize → {tilde(SLASH_FILE)}")
    return True


def enabled():
    return read_config().get("humanize", "off") == "on"


def sync_badge(on):
    """Метка «humanize» во второй строке promptsill, пока режим включён."""
    try:
        if not on:
            os.remove(BADGE)
        elif not os.path.exists(BADGE):
            os.makedirs(os.path.dirname(BADGE), exist_ok=True)
            with open(BADGE, "w") as f:
                json.dump({"text": "humanize", "color": "green"}, f)
    except OSError:
        pass


def rules_text():
    try:
        with open(RULES) as f:
            return f.read()
    except OSError:
        return ""


def log(line):
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass


def mark(sid, name, value=None):
    if not sid or not UUID_RE.match(sid):
        return None
    path = os.path.join(STATE, f"{sid}.{name}")
    if value is None:
        return path
    try:
        if value is False:
            os.remove(path)
        else:
            os.makedirs(STATE, exist_ok=True)
            with open(path, "w") as f:
                f.write(value)
    except OSError:
        pass
    return path


def read_mark(sid, name):
    path = mark(sid, name)
    try:
        with open(path) as f:
            return f.read()
    except (OSError, TypeError):
        return None


def prose(text):
    """Текст без кода, цитат и строк вывода: их правила не касаются."""
    text = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1[^\n]*$", "", text)
    text = re.sub(r"`[^`\n]*`", "", text)
    text = re.sub(r"(?m)^\s*>.*$", "", text)
    text = re.sub(r"«[^»\n]{0,160}»|“[^”\n]{0,160}”|„[^“”\n]{0,160}[“”]|\"[^\"\n]{0,160}\"", '""', text)
    return re.sub(r"https?://\S+", lambda m: m.group(0) if "utm_source" in m.group(0) else "", text)


def is_english(paragraph):
    latin = len(re.findall(r"[A-Za-z]", paragraph))
    cyr = len(re.findall(r"[А-Яа-яЁё]", paragraph))
    return latin > 3 * max(cyr, 1)


def check(text):
    """[(номер строки, правило, фрагмент)] по явным маркерам."""
    clean = prose(text)
    hits = []
    raw = re.sub(r"`[^`\n]*`", "", re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1[^\n]*$", "", text))
    for name, rx in (("елочки и лапки", r"[«»„“”]"), ("буква е с точками", r"[ёЁ]")):
        found = [m.start() for m in re.finditer(rx, raw)]
        if found:
            hits.append((raw.count("\n", 0, found[0]) + 1, name, f"{len(found)} шт."))
    colons = [m.start() for m in re.finditer(r"[^\s:/\d]:(?=[ \t]+\S)", clean)]
    if len(colons) > 1:
        hits.append((clean.count("\n", 0, colons[0]) + 1, "двоеточия", f"{len(colons)} посреди фраз"))

    def line_of(pos):
        return clean.count("\n", 0, pos) + 1

    for name, rx in COMPILED:
        for m in rx.finditer(clean):
            frag = m.group(0).strip()
            hits.append((line_of(m.start()), name, frag or m.group(0)))
    for m in MERISM.finditer(clean):
        hits.append((line_of(m.start()), "от… до…", m.group(0)))
    pos = 0
    for para in re.split(r"\n\s*\n", clean):
        start = clean.find(para, pos)
        pos = start + len(para)
        n = para.count("—")
        if n and is_english(para):
            hits.append((line_of(start), "тире в английском", f"{n} шт."))
        elif n > 1:
            hits.append((line_of(start), "много тире", f"{n} в абзаце"))
        for hm in re.finditer(r"(?m)^\s*(?:#{1,6}\s+|\*\*)([^\n*#]+?)(?:\*\*)?\s*$", para):
            words = re.findall(r"[A-Za-zА-Яа-яЁё][\wЁё'-]*", hm.group(1))
            if len(words) >= 3 and all(w[0].isupper() for w in words) and not hm.group(1).isupper():
                hits.append((line_of(start + hm.start()), "Заглавные Буквы", hm.group(1).strip()))
    bold = len(re.findall(r"\*\*[^*\n]+\*\*", clean))
    if bold > 2:
        hits.append((1, "много жирного", f"{bold} выделений"))
    return sorted(set(hits))


def last_reply(path):
    """Текст последнего ответа Claude в транскрипте: всё после последнего сообщения пользователя."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - (8 << 20)))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    parts = []
    for raw in reversed(lines):
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        msg = e.get("message") or {}
        content = msg.get("content")
        if e.get("type") == "user" and not e.get("isMeta"):
            if isinstance(content, str) or (isinstance(content, list) and
                                            any(c.get("type") == "text" for c in content if isinstance(c, dict))):
                break
        if e.get("type") == "assistant" and isinstance(content, list) and not e.get("isSidechain"):
            parts += [c.get("text", "") for c in reversed(content) if isinstance(c, dict) and c.get("type") == "text"]
    return "\n\n".join(reversed(parts))


def emit(event, context):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}},
                     ensure_ascii=False))


def hook():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    sid, event = payload.get("session_id"), payload.get("hook_event_name")
    on = enabled()
    if event == "SessionStart":
        sync_badge(on)
        mark(sid, "primed", False)
        if on and rules_text():
            emit(event, rules_text())
            mark(sid, "primed", "1")
    elif event == "UserPromptSubmit":
        primed = read_mark(sid, "primed") is not None
        hits = read_mark(sid, "hits")
        mark(sid, "hits", False)
        if on:
            text = REMINDER if primed else rules_text()
            if hits and primed:
                text += f"\nВ прошлом ответе проверка нашла: {hits}. Не повторяй этого."
            if text:
                emit(event, text)
                mark(sid, "primed", "1")
        elif primed:
            emit(event, OFF_NOTE)
            mark(sid, "primed", False)
    elif event == "Stop" and on and payload.get("transcript_path"):
        found = check(payload.get("last_assistant_message") or last_reply(payload["transcript_path"]))
        if found:
            short = "; ".join(f"{name} («{frag[:40]}»)" for _, name, frag in found[:6])
            mark(sid, "hits", short)
            log(f"{sid[:8] if sid else '?'} {len(found)}: {short}")
    return 0


def current_sid():
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        return sid
    pid = os.environ.get("CLAUDE_PID")
    try:
        with open(os.path.join(HOME, ".claude", "sessions", f"{pid}.json")) as f:
            return json.load(f).get("sessionId")
    except (OSError, ValueError, TypeError):
        return None


def set_state(value, inside):
    write_config(humanize=value)
    sync_badge(value == "on")
    sid = current_sid() if inside else None
    if value == "on":
        install_hooks()
        print("mnrh humanization включена.")
        if inside and rules_text():
            print("Правила ниже действуют в этой сессии с этого ответа:\n")
            print(rules_text())
            mark(sid, "primed", "1")
        else:
            print("Правила придут в каждую сессию Claude Code (новую и уже открытую) со следующим сообщением.")
    else:
        print("mnrh humanization выключена. Правила письма больше не обязательны.")
        mark(sid, "primed", False)


def usage():
    print("mnrh claude humanize               состояние")
    print("mnrh claude humanize on | off      включить или выключить во всех сессиях Claude Code")
    print("mnrh claude humanize switch [on|off]   то же для /humanize; без аргумента переключает")
    print("mnrh claude humanize rules         свод правил, который получает Claude")
    print("mnrh claude humanize check <файл>  найти явные признаки нейросети в тексте (- читает stdin)")
    print("mnrh claude humanize log           что проверка находила в ответах Claude")
    print()
    print("Правила взяты из эссе Википедии «Признаки сгенерированности текста» (ВП:ПРГЕН). Хук SessionStart отдаёт")
    print("их в начале сессии и после сжатия контекста, UserPromptSubmit напоминает о них к каждому сообщению,")
    print("Stop проверяет ответ и сообщает Claude о найденном в следующем сообщении.")


def status():
    print(f"mnrh humanization: {'включена' if enabled() else 'выключена'}. Правила: {tilde(RULES)}")
    missing = missing_hooks()
    if missing:
        print(f"! хуки не подключены ({', '.join(missing)}): Claude правил не получит. "
              f"Подключит mnrh claude humanize on или mnrh claude setup")
    print(f"Переключить: /humanize в Claude Code или mnrh claude humanize on|off. Лог проверок: {tilde(LOG)}")


def main():
    args = sys.argv[1:]
    if args[:1] == ["humanize-hook"]:
        try:
            return hook()
        except Exception as e:
            log(f"error {e!r}")
            return 0
    rest = args[1:]
    if not rest:
        status()
        return 0
    cmd = rest[0]
    if cmd in ("-h", "--help"):
        usage()
        return 0
    if cmd in ("on", "off"):
        set_state(cmd, inside=bool(os.environ.get("CLAUDECODE")))
        return 0
    if cmd == "switch":
        want = rest[1] if len(rest) > 1 else ("off" if enabled() else "on")
        if want in ("status", "?"):
            status()
            return 0
        if want not in ("on", "off"):
            print(f"Не понял «{want}»: on, off или пусто, чтобы переключить.", file=sys.stderr)
            return 2
        set_state(want, inside=bool(os.environ.get("CLAUDECODE")))
        return 0
    if cmd == "rules":
        print(rules_text(), end="")
        return 0
    if cmd == "check" and len(rest) == 2:
        try:
            text = sys.stdin.read() if rest[1] == "-" else open(rest[1], encoding="utf-8").read()
        except OSError as e:
            print(f"Не прочитал {rest[1]}: {e.strerror}", file=sys.stderr)
            return 2
        found = check(text)
        for line, name, frag in found:
            print(f"{line:>4}  {name}: {frag}")
        print("Явных признаков не нашёл." if not found else f"Найдено: {len(found)}.")
        return 1 if found else 0
    if cmd == "log":
        try:
            with open(LOG) as f:
                print("".join(f.readlines()[-30:]), end="")
        except OSError:
            print("Лог пуст.")
        return 0
    usage()
    return 2


if __name__ == "__main__":
    sys.exit(main())
