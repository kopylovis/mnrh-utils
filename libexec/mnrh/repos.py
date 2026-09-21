import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import BAD, DEV, OK, WARN, has_flag, paint, projects, run, tilde

args = sys.argv[1:]
if has_flag(args, "-h", "--help"):
    print("mnrh repos           состояние всех git-проектов в ~/Developer")
    print("mnrh repos --fetch   сначала подтянуть свежие данные со всех remote")
    sys.exit(0)
fetch = has_flag(args, "--fetch")


def git(repo, *cmd, timeout=30):
    return run(["git", "-C", repo, *cmd], timeout=timeout).strip()


def count(repo, *rev):
    out = git(repo, "rev-list", "--count", *rev)
    return int(out) if out.isdigit() else 0


def inspect(repo):
    if fetch:
        git(repo, "fetch", "--all", "--prune", "--quiet", timeout=120)
    branch = git(repo, "branch", "--show-current") or "(detached)"
    upstream = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    problems, warnings = [], []

    if not upstream and branch != "(detached)":
        candidates = git(repo, "for-each-ref", "--format=%(refname:short)", f"refs/remotes/*/{branch}").splitlines()
        if candidates:
            upstream = candidates[0]
            warnings.append(f"upstream не настроен: git branch -u {upstream}")

    if not git(repo, "remote"):
        warnings.append("нет remote")
    elif not upstream:
        local_only = count(repo, "HEAD", "--not", "--remotes")
        if local_only:
            problems.append(f"ветки нет на remote, {local_only} коммитов только локально")
        else:
            warnings.append("ветки нет на remote, но все её коммиты уже там")
    else:
        ahead = count(repo, f"{upstream}..HEAD")
        behind = count(repo, f"HEAD..{upstream}")
        if ahead:
            warnings.append(f"{ahead} не отправлено")
        if behind:
            warnings.append(f"отстаёт на {behind}")

    status = git(repo, "status", "--porcelain").splitlines()
    changed = sum(1 for s in status if not s.startswith("??"))
    untracked = sum(1 for s in status if s.startswith("??"))
    if changed:
        warnings.append(f"изменено файлов: {changed}")
    if untracked:
        warnings.append(f"неотслеживаемых: {untracked}")

    stashes = len(git(repo, "stash", "list").splitlines())
    if stashes:
        warnings.append(f"stash: {stashes}")

    others = []
    for b in git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines():
        if b == branch:
            continue
        n = count(repo, b, "--not", "--remotes")
        if n:
            others.append(f"{b} ({n})")

    last = git(repo, "log", "-1", "--format=%cr")
    return os.path.basename(repo), branch, problems, warnings, others, last


repos = projects()
if not repos:
    print(f"В {tilde(DEV)} нет git-репозиториев.")
    sys.exit(0)

with ThreadPoolExecutor(max_workers=8) as pool:
    results = list(pool.map(inspect, repos))

width = max(len(r[0]) for r in results)
bwidth = min(max(len(r[1]) for r in results), 34)
total_bad = total_warn = 0
for name, branch, problems, warnings, others, last in results:
    total_bad += bool(problems)
    total_warn += bool(warnings) and not problems
    mark = BAD if problems else (WARN if warnings else OK)
    state = "; ".join(problems + warnings) or "всё отправлено, дерево чистое"
    b = branch if len(branch) <= bwidth else branch[: bwidth - 1] + "…"
    print(f"{mark} {name:<{width}}  {b:<{bwidth}}  {state}" + paint(f"   {last}", "2"))
    if others:
        print(paint(f"  {'':<{width}}  ещё только локально: {', '.join(others)}", "2"))

print()
print(f"Проектов: {len(results)}, требуют внимания: {total_bad + total_warn}")
if not fetch:
    print(paint("Сверено с последним fetch. Свежие данные: mnrh repos --fetch", "2"))
