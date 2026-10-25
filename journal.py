"""날짜별 일지: git 코드 변경 + 다이어그램 설계 기록 + 메모 + AI 요약.

- 코드 변경은 저장하지 않고 매번 git에서 읽음 (읽기 전용 명령만 사용).
- 설계 기록/메모/요약만 data/<repo>.journal.json 에 저장함.
"""
import datetime
import json
import os
import subprocess

MAX_PATCH_LINES = 700
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "code_changes": {"type": "array", "items": {"type": "string"}},
        "planned": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "code_changes", "planned", "next_steps"],
    "additionalProperties": False,
}


# ==================================================================== 저장
def path_for(data_path):
    return data_path[:-5] + ".journal.json"


def load(data_path):
    p = path_for(data_path)
    if not os.path.exists(p):
        return {"entries": [], "summaries": {}}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save(data_path, j):
    p = path_for(data_path)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, indent=1)
    os.replace(p + ".tmp", p)


def add(data_path, kind, source, text, classes=(), items=()):
    """kind: design | code | note, source: manual | ai | rescan | user"""
    j = load(data_path)
    now = datetime.datetime.now()
    j["entries"].append({"at": now.isoformat(timespec="seconds"), "date": now.date().isoformat(),
                         "kind": kind, "source": source, "text": text,
                         "classes": sorted(set(classes))[:40], "items": list(items)[:80]})
    save(data_path, j)


def remove_note(data_path, at, text):
    """사용자 메모 하나를 지움 (작성 시각 + 내용으로 찾음). 지웠으면 True."""
    j = load(data_path)
    for i, e in enumerate(j["entries"]):
        if e["kind"] == "note" and e["at"] == at and e["text"] == text:
            del j["entries"][i]
            save(data_path, j)
            return True
    return False


# ==================================================================== git (읽기 전용)
def git(repo, *args, timeout=20):
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def commits(repo, roots, since_days):
    out = git(repo, "log", f"--since={since_days}.days", "--date=short",
              "--pretty=format:%x1e%H%x1f%ad%x1f%an%x1f%s", "--name-status", "--", *(roots or ["."]))
    if out is None:
        return None
    result = []
    for block in out.split("\x1e"):
        block = block.strip("\n")
        if not block:
            continue
        head, *files = block.split("\n")
        h, date, author, subject = (head.split("\x1f") + ["", "", "", ""])[:4]
        fl = []
        for line in files:
            parts = line.split("\t")
            if len(parts) >= 2:
                fl.append({"status": parts[0][:1], "path": parts[-1]})
        result.append({"hash": h, "short": h[:7], "date": date, "author": author, "subject": subject, "files": fl})
    return result


def uncommitted(repo, roots):
    out = git(repo, "status", "--porcelain=v1", "--", *(roots or ["."]))
    if out is None:
        return []
    files = []
    for line in out.split("\n"):
        if len(line) > 3:
            st = line[:2].strip() or "M"
            files.append({"status": "A" if st == "??" else st[:1], "path": line[3:].strip().strip('"')})
    return files


def file_class_index(d):
    """파일 경로/파일 이름(확장자 제외) -> 클래스 id. .cpp는 같은 이름 헤더의 클래스로 연결함."""
    by_path, by_stem = {}, {}
    for cid, c in d["classes"].items():
        if c.get("file"):
            by_path.setdefault(c["file"], []).append(cid)
            stem = os.path.splitext(os.path.basename(c["file"]))[0]
            by_stem.setdefault(stem, []).append(cid)
    return by_path, by_stem


def classes_of(files, index):
    by_path, by_stem = index
    out = []
    for f in files:
        hit = by_path.get(f["path"]) or by_stem.get(os.path.splitext(os.path.basename(f["path"]))[0]) or []
        out += hit
    return sorted(set(out))


def build(repo, roots, d, data_path, days):
    """최근 days일의 날짜별 일지를 만듦 (최신 날짜 먼저)."""
    j = load(data_path)
    today = datetime.date.today()
    since = (today - datetime.timedelta(days=days - 1)).isoformat()
    index = file_class_index(d)
    cs = commits(repo, roots, days)
    byday = {}

    def day(date):
        return byday.setdefault(date, {"date": date, "commits": [], "uncommitted": [], "entries": [], "notes": [],
                                       "summary": j["summaries"].get(date)})

    for c in cs or []:
        c["classes"] = classes_of(c["files"], index)
        day(c["date"])["commits"].append(c)
    unc = uncommitted(repo, roots) if cs is not None else []
    if unc:
        day(today.isoformat())["uncommitted"] = [{**f, "classes": classes_of([f], index)} for f in unc]
    for e in j["entries"]:
        if e["date"] >= since:
            (day(e["date"])["notes"] if e["kind"] == "note" else day(e["date"])["entries"]).append(e)
    for date, s in j["summaries"].items():
        if date >= since:
            day(date)
    return {"git": cs is not None, "days": sorted(byday.values(), key=lambda x: x["date"], reverse=True)}


# ==================================================================== AI 요약
def pending_plan(d):
    """현재 다이어그램의 '바꿀 예정' 항목 (설계 추가 / 코드와 다르게 수정 / 삭제 예정 / 플래그)."""
    lines = []
    for c in d["classes"].values():
        tags = []
        if c["origin"] == "design":
            tags.append("new class")
        if c.get("removed"):
            tags.append("planned removal")
        if c.get("flags"):
            tags.append("flags=" + ",".join(c["flags"]))
        if c.get("basesOrigin") == "design" and c["origin"] == "code":
            tags.append(f"bases {c.get('origBases')} -> {c['bases']}")
        mem = []
        for m in c["members"]:
            t = []
            if m["origin"] == "design":
                t.append("added")
            if m.get("edited"):
                t.append(f"edited from `{m.get('origSig')}`")
            if m.get("removed"):
                t.append("planned removal")
            if m.get("flags"):
                t.append("flags=" + ",".join(m["flags"]))
            if t and c["origin"] == "code":
                mem.append(f"    - `{m['sig']}`: {'; '.join(t)}")
        if tags or mem:
            lines.append(f"- {c['id']}" + (f" ({'; '.join(tags)})" if tags else ""))
            lines += mem
    return "\n".join(lines[:200]) or "(none)"


def summary_prompt(repo, roots, d, data_path, date, lang):
    info = build(repo, roots, d, data_path, max(1, (datetime.date.today() - datetime.date.fromisoformat(date)).days + 1))
    dd = next((x for x in info["days"] if x["date"] == date), None) or {"commits": [], "uncommitted": [], "entries": [], "notes": []}
    parts = [f"Repository: {os.path.basename(repo)}. Journal date: {date}."]
    budget = MAX_PATCH_LINES
    if dd["commits"]:
        for c in dd["commits"]:
            files = ", ".join(f"{f['status']} {f['path']}" for f in c["files"][:30])
            parts.append(f"Commit {c['short']}: {c['subject']}\nFiles: {files}\nClasses: {', '.join(c['classes'][:30]) or '-'}")
            if budget > 0:
                patch = git(repo, "show", "--format=", "--unified=2", c["hash"], "--", *(roots or ["."])) or ""
                lines = patch.split("\n")[:min(budget, 300)]
                budget -= len(lines)
                parts.append("```diff\n" + "\n".join(lines) + "\n```")
    else:
        parts.append("No commits on this date.")
    if dd["uncommitted"]:
        parts.append("Uncommitted working-tree changes: " + ", ".join(f["path"] for f in dd["uncommitted"]))
        if budget > 0:
            patch = git(repo, "diff", "HEAD", "--unified=2", "--", *(roots or ["."])) or ""
            parts.append("```diff\n" + "\n".join(patch.split("\n")[:budget]) + "\n```")
    if dd["entries"]:
        parts.append("Diagram design log for this date (planned changes recorded in the class diagram):\n"
                     + "\n".join(f"- [{e['source']}] {e['text']}" + (": " + "; ".join(e['items'][:12]) if e['items'] else "")
                                 for e in dd["entries"][-80:]))
    if dd["notes"]:
        parts.append("User notes:\n" + "\n".join(f"- {n['text']}" for n in dd["notes"]))
    if date == datetime.date.today().isoformat():
        parts.append("Current pending plan in the diagram (not yet in code):\n" + pending_plan(d))
    lang_name = {"ko": "Korean", "en": "English"}.get(lang, "Korean")
    parts.append(
        f"Write a development journal entry for this date in {lang_name}.\n"
        "- summary: 1-2 sentences on what the day was about.\n"
        "- code_changes: what actually changed in code, at class/module level (from commits and uncommitted diffs).\n"
        "- planned: design changes recorded in the diagram that are not (yet) in code.\n"
        "- next_steps: 1-4 concrete follow-ups suggested by the gap between code and plan.\n"
        "Use empty arrays when there is nothing for a section. Mention class names exactly.")
    return "\n\n".join(parts)


def save_summary(data_path, date, summary):
    j = load(data_path)
    j["summaries"][date] = summary
    save(data_path, j)


# ==================================================================== 재스캔 diff
def code_diff(cur, new, member_key):
    """이전 다이어그램의 code 항목과 새 스캔을 비교해 코드에서 바뀐 것을 문장으로 돌려줌."""
    items, classes = [], set()
    old = {cid: c for cid, c in cur["classes"].items() if c["origin"] == "code"}
    nc = new["classes"]
    for cid in sorted(nc.keys() - old.keys()):
        planned = cur["classes"].get(cid, {}).get("origin") == "design"
        items.append(f"+ class {cid}" + (" (설계대로 구현됨)" if planned else ""))
        classes.add(cid)
    for cid in sorted(old.keys() - nc.keys()):
        items.append(f"- class {cid}")
        classes.add(cid)
    for cid in sorted(nc.keys() & old.keys()):
        o, n = old[cid], nc[cid]
        old_keys = {(m["kind"], m.get("origSig", m["sig"])) for m in o["members"] if m["origin"] == "code"}
        new_keys = {member_key(m) for m in n["members"]}
        planned = {member_key(m) for m in o["members"] if m["origin"] == "design" or m.get("edited")}
        for k in sorted(new_keys - old_keys):
            items.append(f"+ {cid}: {k[1]}" + (" (설계대로 구현됨)" if k in planned else ""))
            classes.add(cid)
        for k in sorted(old_keys - new_keys):
            items.append(f"- {cid}: {k[1]}")
            classes.add(cid)
        if o.get("origBases", o["bases"]) != n["bases"]:
            items.append(f"~ {cid} 상속: {', '.join(o.get('origBases', o['bases'])) or '없음'} → {', '.join(n['bases']) or '없음'}")
            classes.add(cid)
    return items, classes
