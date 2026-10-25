"""클래스 다이어그램 툴 로컬 서버.

    python server.py [--port 8765]

- 다이어그램 JSON은 이 폴더의 data/ 에만 저장함. 대상 repo에는 아무것도 쓰지 않음.
- 최초 열기 때만 헤더를 파싱하고, 이후 AI 대화는 다이어그램만 수정함 (소스는 읽기 전용).
"""
import argparse
import copy
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import anthropic
except ImportError:  # Claude Code(구독) 방식만 쓸 때는 SDK가 없어도 됨
    anthropic = None

import cppscan
import journal

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
SOURCE_EXTS = (".h", ".hpp", ".hh", ".hxx", ".inl", ".c", ".cc", ".cpp", ".cxx", ".glsl", ".hlsl")
RELATION_TYPES = ("composition", "aggregation", "association", "dependency")
MAX_UNDO = 30

lock = threading.Lock()
sessions = {}  # repo -> {"messages": [...], "undo": [...], "note": str, "system": str}


# ==================================================================== 저장소
def norm_repo(repo):
    return os.path.normcase(os.path.abspath(os.path.expanduser(repo.strip().strip('"'))))


def data_path(repo):
    h = hashlib.sha1(repo.encode("utf-8")).hexdigest()[:8]
    return os.path.join(DATA_DIR, f"{os.path.basename(repo) or 'repo'}-{h}.json")


def load(repo):
    p = data_path(repo)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save(repo, d):
    os.makedirs(DATA_DIR, exist_ok=True)
    p = data_path(repo)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(p + ".tmp", p)


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def fresh_scan(repo, roots):
    m = cppscan.scan_repo(repo, roots)
    return {"version": 1, "repo": repo, "roots": roots, "scannedAt": now(),
            "classes": m["classes"], "relations": m["relations"]}


def member_key(m):
    return (m["kind"], m["sig"])


def rel_key(r):
    return (r["from"], r["to"], r["type"], r.get("label", ""))


def merge_rescan(cur, new):
    """코드에서 다시 읽은 결과에 기존 design 항목·삭제 표시·위치를 얹음."""
    out = copy.deepcopy(new)
    for cid, nc in out["classes"].items():
        old = cur["classes"].get(cid)
        if not old:
            continue
        for k in ("x", "y", "note", "role", "flags", "memos"):
            if k in old:
                nc[k] = old[k]
        if old["origin"] == "code":
            nc["removed"] = old.get("removed", False)
        if old.get("basesOrigin") == "design":
            nc["bases"], nc["basesOrigin"] = old["bases"], "design"
        if "origBases" in old and nc.get("basesOrigin") == "design":
            nc["origBases"] = old["origBases"]
        new_keys = {member_key(m) for m in nc["members"]}
        removed = {member_key(m) for m in old["members"] if m["origin"] == "code" and m.get("removed")}
        flags = {member_key(m): m["flags"] for m in old["members"] if m["origin"] == "code" and m.get("flags")}
        # 직접 수정한 code 멤버는 원래 시그니처로 찾아 수정본으로 바꿔 끼움 (코드가 이미 수정본과 같아졌으면 그대로 둠)
        edited = {(m["kind"], m["origSig"]): m for m in old["members"] if m["origin"] == "code" and m.get("edited")}
        for i, m in enumerate(nc["members"]):
            m["removed"] = member_key(m) in removed
            if member_key(m) in flags:
                m["flags"] = flags[member_key(m)]
            e = edited.get(member_key(m))
            if e and member_key(e) not in new_keys:
                nc["members"][i] = e
        nc["members"] += [m for m in old["members"] if m["origin"] == "design" and member_key(m) not in new_keys]
    for cid, old in cur["classes"].items():
        if cid not in out["classes"] and old["origin"] == "design":
            out["classes"][cid] = old
    code_keys = {rel_key(r) for r in out["relations"]}
    removed = {rel_key(r) for r in cur["relations"] if r["origin"] == "code" and r.get("removed")}
    for r in out["relations"]:
        r["removed"] = rel_key(r) in removed
    out["relations"] += [r for r in cur["relations"]
                         if r["origin"] == "design" and rel_key(r) not in code_keys
                         and r["from"] in out["classes"] and r["to"] in out["classes"]]
    return out


# ==================================================================== AI 도구
TOOLS = [
    {"name": "list_classes",
     "description": "List diagram classes. Without arguments returns class ids grouped by module. "
                    "With module and/or query, returns one detailed line per matching class "
                    "(kind, bases, member counts, origin, removed).",
     "input_schema": {"type": "object", "properties": {
         "module": {"type": "string", "description": "Module name prefix, e.g. 'Engine/RHI'."},
         "query": {"type": "string", "description": "Case-insensitive substring of the class id."}}}},
    {"name": "get_class",
     "description": "Full detail of one class: file, bases, every member signature with access/origin/removed, "
                    "and incoming/outgoing relations.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "read_source",
     "description": "Read a source file of the repository (read-only). Path is relative to the repo root. "
                    "Returns at most 400 lines with line numbers.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
         "required": ["path"]}},
    {"name": "search_source",
     "description": "Regex search over the repository's source files (read-only). Returns up to 60 'path:line: text' hits.",
     "input_schema": {"type": "object", "properties": {
         "pattern": {"type": "string"},
         "path_prefix": {"type": "string", "description": "Optional repo-relative directory prefix."}},
         "required": ["pattern"]}},
    {"name": "upsert_class",
     "description": "Create a design class, or update an existing class's kind/module/bases/template/note. "
                    "new_id renames a design class (and its references). Code classes cannot be renamed.",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "Class id as shown in the diagram, e.g. 'VulkanBuffer' or 'Generation::Component'."},
         "kind": {"type": "string", "enum": ["class", "struct", "interface", "enum", "namespace"]},
         "module": {"type": "string"},
         "bases": {"type": "array", "items": {"type": "string"}, "description": "Full replacement of the base list."},
         "template": {"type": "string", "description": "e.g. '<typename T>'"},
         "note": {"type": "string", "description": "Short design note shown on the class."},
         "new_id": {"type": "string"}},
         "required": ["id"]}},
    {"name": "remove_class",
     "description": "Remove a class. Design classes are deleted; code classes are marked as planned removal.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "add_members",
     "description": "Add members to a class. sig is a C++ declaration as written in a header without the trailing ';' "
                    "(e.g. 'void Submit(const RHICommandList& list)', 'Memory::RefPtr<RHIBuffer> m_buffer'). "
                    "For enums use kind 'value' with sig like 'eFoo = 1'.",
     "input_schema": {"type": "object", "properties": {
         "class_id": {"type": "string"},
         "members": {"type": "array", "items": {"type": "object", "properties": {
             "sig": {"type": "string"},
             "kind": {"type": "string", "enum": ["field", "method", "value"]},
             "access": {"type": "string", "enum": ["public", "protected", "private"]}},
             "required": ["sig"]}}},
         "required": ["class_id", "members"]}},
    {"name": "edit_member",
     "description": "Change a member's signature and/or access. On a code member this marks the old one as planned removal "
                    "and adds the new design member.",
     "input_schema": {"type": "object", "properties": {
         "class_id": {"type": "string"}, "sig": {"type": "string", "description": "Current signature or member name."},
         "new_sig": {"type": "string"}, "access": {"type": "string", "enum": ["public", "protected", "private"]}},
         "required": ["class_id", "sig"]}},
    {"name": "remove_members",
     "description": "Remove members by signature or name. Design members are deleted; code members are marked as planned removal.",
     "input_schema": {"type": "object", "properties": {
         "class_id": {"type": "string"}, "sigs": {"type": "array", "items": {"type": "string"}}},
         "required": ["class_id", "sigs"]}},
    {"name": "restore",
     "description": "Undo a planned removal on a code class, or on one of its members when sig is given.",
     "input_schema": {"type": "object", "properties": {"class_id": {"type": "string"}, "sig": {"type": "string"}},
                      "required": ["class_id"]}},
    {"name": "add_relation",
     "description": "Add a non-inheritance relation (inheritance is set via upsert_class bases). "
                    "composition/aggregation: 'from' is the whole that owns/holds 'to'. association/dependency: 'from' uses 'to'.",
     "input_schema": {"type": "object", "properties": {
         "from": {"type": "string"}, "to": {"type": "string"},
         "type": {"type": "string", "enum": list(RELATION_TYPES)}, "label": {"type": "string"}},
         "required": ["from", "to", "type"]}},
    {"name": "remove_relation",
     "description": "Remove relations between two classes (optionally only of one type).",
     "input_schema": {"type": "object", "properties": {
         "from": {"type": "string"}, "to": {"type": "string"},
         "type": {"type": "string", "enum": list(RELATION_TYPES)}},
         "required": ["from", "to"]}},
]


class ToolError(Exception):
    pass


class DiagramTools:
    def __init__(self, repo, d):
        self.repo, self.d = repo, d
        self.ops, self.touched = [], set()

    @property
    def classes(self):
        return self.d["classes"]

    def cls(self, cid):
        c = self.classes.get(cid)
        if c is None:
            idx = cppscan.build_short_index(self.classes)
            hit = cppscan.resolve(cid, self.classes, idx)
            c = self.classes.get(hit) if hit else None
        if c is None:
            raise ToolError(f"class '{cid}' not found. Use list_classes to find the id.")
        return c

    def log(self, cid, text):
        self.touched.add(cid)
        self.ops.append({"class": cid, "text": text})

    def run(self, name, args):
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            raise ToolError(f"unknown tool {name}")
        return fn(**args)

    # ---------------------------------------------------------- 조회
    def t_list_classes(self, module=None, query=None):
        rows = sorted(self.classes.values(), key=lambda c: (c["module"], c["id"]))
        if module:
            rows = [c for c in rows if c["module"].startswith(module)]
        if query:
            rows = [c for c in rows if query.lower() in c["id"].lower()]
        if not module and not query:
            groups = {}
            for c in rows:
                groups.setdefault(c["module"], []).append(c["id"] + ("*" if c["origin"] == "design" else ""))
            return "\n".join(f"[{m}] " + ", ".join(ids) for m, ids in groups.items()) + "\n(* = design)"
        if not rows:
            return "no match"
        out = []
        for c in rows[:200]:
            nf = sum(m["kind"] == "field" for m in c["members"])
            nm = len(c["members"]) - nf
            flags = (" design" if c["origin"] == "design" else "") + (" REMOVED" if c.get("removed") else "") \
                + (f" flags={','.join(c['flags'])}" if c.get("flags") else "") \
                + (" member-flags" if any(m.get("flags") for m in c["members"]) else "")
            bases = f" : {', '.join(c['bases'])}" if c["bases"] else ""
            out.append(f"{c['id']} [{c['kind']}] {c['module']}{bases} fields={nf} methods={nm}{flags}")
        return "\n".join(out) + ("\n(truncated)" if len(rows) > 200 else "")

    def t_get_class(self, id):
        c = self.cls(id)
        lines = [f"{c['id']} [{c['kind']}{' ' + c['template'] if c.get('template') else ''}] module={c['module']} origin={c['origin']}"
                 + (" REMOVED" if c.get("removed") else "")]
        if c.get("file"):
            lines.append(f"file: {c['file']}:{c.get('line', '')}")
        if c["bases"]:
            lines.append("bases: " + ", ".join(c["bases"]))
        if c.get("note"):
            lines.append("note: " + c["note"])
        if c.get("role"):
            lines.append("inferred role: " + c["role"]["summary"])
        if c.get("flags"):
            lines.append("user flags: " + ", ".join(c["flags"]))
        for memo in c.get("memos", []):
            lines.append(f"user memo ({memo['at'][:10]}): " + memo["text"].replace("\n", " / ")[:300])
        for m in c["members"]:
            tag = (" [design]" if m["origin"] == "design" else "") + (" [REMOVED]" if m.get("removed") else "") \
                + (f" [flags: {', '.join(m['flags'])}]" if m.get("flags") else "") \
                + (f" [edited, code has: {m.get('origAccess', m['access'])} {m.get('origSig', m['sig'])}]" if m.get("edited") else "")
            lines.append(f"  {m['access']:<9} {m['kind']:<6} {m['sig']}{tag}")
        for r in self.d["relations"]:
            if c["id"] in (r["from"], r["to"]):
                tag = (" [design]" if r["origin"] == "design" else "") + (" [REMOVED]" if r.get("removed") else "")
                lines.append(f"  rel: {r['from']} -{r['type']}-> {r['to']} ({r.get('label', '')}){tag}")
        return "\n".join(lines)

    def safe_path(self, rel):
        p = os.path.realpath(os.path.join(self.repo, rel))
        if os.path.commonpath([p, os.path.realpath(self.repo)]) != os.path.realpath(self.repo):
            raise ToolError("path is outside the repository")
        if not os.path.isfile(p):
            raise ToolError(f"no such file: {rel}")
        return p

    def t_read_source(self, path, start_line=1, end_line=None):
        with open(self.safe_path(path), "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
        start = max(1, start_line or 1)
        end = min(len(lines), end_line or start + 399, start + 399)
        body = "\n".join(f"{i:5}  {lines[i - 1]}" for i in range(start, end + 1))
        return f"{path} ({len(lines)} lines)\n{body}"

    def t_search_source(self, pattern, path_prefix=""):
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"bad regex: {e}")
        base = self.repo
        hits = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d.lower() not in cppscan.SKIP_DIRS and not d.startswith("."))
            for fn in sorted(filenames):
                if not fn.lower().endswith(SOURCE_EXTS):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), base).replace("\\", "/")
                if path_prefix and not rel.startswith(path_prefix):
                    continue
                with open(os.path.join(dirpath, fn), "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                            if len(hits) >= 60:
                                return "\n".join(hits) + "\n(truncated)"
        return "\n".join(hits) or "no match"

    # ---------------------------------------------------------- 수정
    def t_upsert_class(self, id, kind=None, module=None, bases=None, template=None, note=None, new_id=None):
        c = self.classes.get(id)
        if c is None:
            c = {"id": id, "name": id.split("::")[-1], "kind": kind or "class", "module": module or "(design)",
                 "file": "", "line": 0, "template": template or "", "bases": bases or [], "underlying": "",
                 "outer": None, "origin": "design", "removed": False, "note": note or "", "members": []}
            self.classes[id] = c
            self.log(id, f"+ {c['kind']} {id}")
            return f"created design class {id}"
        changed = []
        for k, v in (("kind", kind), ("module", module), ("template", template), ("note", note)):
            if v is not None and c.get(k) != v:
                c[k] = v
                changed.append(k)
        if bases is not None and bases != c["bases"]:
            if c["origin"] == "code":
                orig = c.setdefault("origBases", list(c["bases"]))
                if bases == orig:
                    c.pop("origBases"); c.pop("basesOrigin", None)
                else:
                    c["basesOrigin"] = "design"
            c["bases"] = bases
            changed.append("bases")
        if new_id and new_id != id:
            if c["origin"] == "code":
                raise ToolError("code classes cannot be renamed; add a design class and mark the code class removed instead")
            if new_id in self.classes:
                raise ToolError(f"{new_id} already exists")
            del self.classes[id]
            c["id"], c["name"] = new_id, new_id.split("::")[-1]
            self.classes[new_id] = c
            for r in self.d["relations"]:
                r["from"] = new_id if r["from"] == id else r["from"]
                r["to"] = new_id if r["to"] == id else r["to"]
            for o in self.classes.values():
                o["bases"] = [new_id if b == id else b for b in o["bases"]]
            changed.append(f"renamed -> {new_id}")
            id = new_id
        if changed:
            self.log(id, f"~ {id} ({', '.join(changed)})")
        return f"updated {id}: {', '.join(changed) or 'no change'}"

    # 사용자 메모 (인스펙터·메모 배지에서만 사용, AI 도구 목록에는 없음)
    def t_add_memo(self, class_id, text):
        c = self.cls(class_id)
        text = (text or "").strip()
        if not text:
            raise ToolError("메모가 비어 있음")
        c.setdefault("memos", []).append({"id": uuid.uuid4().hex[:8], "text": text[:4000], "at": now()})
        self.log(c["id"], f"+ {c['id']} 메모")
        return "added memo"

    def t_remove_memo(self, class_id, memo_id):
        c = self.cls(class_id)
        memos = c.get("memos", [])
        if not any(m["id"] == memo_id for m in memos):
            raise ToolError("메모를 찾을 수 없음")
        c["memos"] = [m for m in memos if m["id"] != memo_id]
        if not c["memos"]:
            c.pop("memos")
        self.log(c["id"], f"- {c['id']} 메모")
        return "removed memo"

    def t_remove_class(self, id):
        c = self.cls(id)
        cid = c["id"]
        if c["origin"] == "design":
            del self.classes[cid]
            self.d["relations"] = [r for r in self.d["relations"] if cid not in (r["from"], r["to"])]
            self.touched.discard(cid)
            self.ops.append({"class": None, "text": f"- {cid}"})
            return f"deleted design class {cid}"
        c["removed"] = True
        self.log(cid, f"- {cid} (planned removal)")
        return f"marked code class {cid} as planned removal"

    def make_member(self, spec, cls):
        sig = spec["sig"].strip().rstrip(";").strip()
        kind = spec.get("kind")
        if cls["kind"] == "enum" or kind == "value":
            name = (cppscan.IDENT_RE.match(sig) or [sig])[0]
            m = {"kind": "value", "name": name, "sig": sig, "type": "", "static": False, "virtual": False, "abstract": False}
        else:
            sc = cppscan.Scanner({})
            parsed = sc.parse_member(cppscan.squash(sig), {"kind": "class"})
            if parsed is None:
                raise ToolError(f"cannot parse member signature: {sig}")
            m = {k: parsed.get(k, "") for k in ("kind", "name", "sig", "type")}
            m.update(static=parsed["static"], virtual=parsed["virtual"], abstract=parsed["abstract"])
            if kind in ("field", "method"):
                m["kind"] = kind
        m.update(access=spec.get("access") or ("public" if cls["kind"] in ("struct", "namespace", "interface") or m["kind"] == "method" else "private"),
                 origin="design", removed=False)
        return m

    def find_member(self, c, sig):
        s = cppscan.squash(sig.rstrip(";"))
        for m in c["members"]:
            if m["sig"] == s:
                return m
        named = [m for m in c["members"] if m["name"] == s and not m.get("removed")]
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            raise ToolError(f"'{sig}' is ambiguous: " + " | ".join(m["sig"] for m in named))
        raise ToolError(f"member '{sig}' not found in {c['id']}")

    def sync_field_relations(self, c):
        """design 필드 타입에서 관계를 추론해 design 관계로 추가함."""
        have = {rel_key(r) for r in self.d["relations"]}
        design_fields = {m["name"].split(",")[0] for m in c["members"] if m["origin"] == "design" and m["kind"] == "field"}
        for r in cppscan.infer_relations({**self.classes, c["id"]: c}):
            if r["from"] == c["id"] and r["label"].replace("[*]", "") in design_fields and rel_key(r) not in have:
                r["origin"] = "design"
                self.d["relations"].append(r)

    def t_add_members(self, class_id, members):
        c = self.cls(class_id)
        added = []
        for spec in members:
            m = self.make_member(spec, c)
            if any(member_key(x) == member_key(m) and not x.get("removed") for x in c["members"]):
                continue
            c["members"].append(m)
            added.append(m["sig"])
            self.log(c["id"], f"+ {c['id']}::{m['name']}")
        self.sync_field_relations(c)
        return f"added {len(added)} member(s) to {c['id']}" + ("" if len(added) == len(members) else " (duplicates skipped)")

    def drop_member(self, c, m):
        if m["origin"] == "design":
            c["members"].remove(m)
            self.d["relations"] = [r for r in self.d["relations"]
                                   if not (r["origin"] == "design" and r["from"] == c["id"]
                                           and r["label"].replace("[*]", "") == m["name"].split(",")[0])]
        else:
            m["removed"] = True
            for r in self.d["relations"]:
                if r["from"] == c["id"] and r["label"].replace("[*]", "") == m["name"].split(",")[0]:
                    r["removed"] = True

    def t_edit_member(self, class_id, sig, new_sig=None, access=None):
        c = self.cls(class_id)
        m = self.find_member(c, sig)
        if m["origin"] == "design":
            if new_sig:
                nm = self.make_member({"sig": new_sig, "kind": m["kind"], "access": access or m["access"]}, c)
                m.update({k: nm[k] for k in ("kind", "name", "sig", "type", "static", "virtual", "abstract")})
            if access:
                m["access"] = access
        else:
            # code 멤버는 원래 시그니처를 기억한 채 수정본으로 표시함 (재스캔 병합과 '수정됨' 색 표시에 사용)
            m.setdefault("origSig", m["sig"])
            m.setdefault("origAccess", m["access"])
            if new_sig:
                nm = self.make_member({"sig": new_sig, "kind": m["kind"], "access": access or m["access"]}, c)
                m.update({k: nm[k] for k in ("kind", "name", "sig", "type", "static", "virtual", "abstract")})
            if access:
                m["access"] = access
            m["edited"] = m["sig"] != m["origSig"] or m["access"] != m["origAccess"]
            if not m["edited"]:
                for k in ("origSig", "origAccess", "edited"):
                    m.pop(k, None)
        self.sync_field_relations(c)
        self.log(c["id"], f"~ {c['id']}::{m['name']}")
        return f"edited {c['id']}::{m['name']}"

    def t_remove_members(self, class_id, sigs):
        c = self.cls(class_id)
        done = []
        for s in sigs:
            m = self.find_member(c, s)
            self.drop_member(c, m)
            done.append(m["name"])
            self.log(c["id"], f"- {c['id']}::{m['name']}")
        return f"removed {', '.join(done)} from {c['id']}"

    def t_restore(self, class_id, sig=None):
        c = self.cls(class_id)
        if sig:
            m = next((x for x in c["members"] if x.get("removed") and (x["sig"] == cppscan.squash(sig) or x["name"] == sig)), None)
            if m is None:
                raise ToolError(f"no removed member '{sig}' in {c['id']}")
            m["removed"] = False
            for r in self.d["relations"]:
                if r["from"] == c["id"] and r["label"].replace("[*]", "") == m["name"].split(",")[0]:
                    r["removed"] = False
        else:
            c["removed"] = False
        self.log(c["id"], f"↺ {c['id']}" + (f"::{sig}" if sig else ""))
        return "restored"

    def t_add_relation(self, type, label="", **kw):
        a, b = self.cls(kw["from"])["id"], self.cls(kw["to"])["id"]
        if type not in RELATION_TYPES:
            raise ToolError(f"type must be one of {RELATION_TYPES}")
        r = {"from": a, "to": b, "type": type, "label": label or "", "origin": "design", "removed": False}
        if rel_key(r) in {rel_key(x) for x in self.d["relations"]}:
            return "relation already exists"
        self.d["relations"].append(r)
        self.log(a, f"+ {a} -{type}-> {b}")
        self.touched.add(b)
        return "added relation"

    def t_remove_relation(self, type=None, **kw):
        a, b = self.cls(kw["from"])["id"], self.cls(kw["to"])["id"]
        n, keep = 0, []
        for r in self.d["relations"]:
            if r["from"] == a and r["to"] == b and (type is None or r["type"] == type) and not r.get("removed"):
                n += 1
                if r["origin"] == "code":
                    r["removed"] = True
                    keep.append(r)
            else:
                keep.append(r)
        self.d["relations"] = keep
        if n:
            self.log(a, f"- {a} -> {b}")
        return f"removed {n} relation(s)"


SYSTEM_TEMPLATE = """You are a software design assistant working on a UML class diagram of the C++ repository "{repo_name}".

The diagram is the design space; the source code is read-only. You never modify code. Entries parsed from code have origin=code; anything you add has origin=design. Removing a code entry only marks it as a planned removal (shown struck through), so the diagram doubles as a diff of the intended design against the current code. The user can also tag classes and members by hand with flags (added / modified / review) to mark intent; when the user refers to flagged items, find them with get_class.

Work with the tools: inspect with list_classes / get_class before editing, and use read_source / search_source when real code context helps you design accurately (signatures, ownership, naming). Write member signatures exactly as they would appear in a header of this codebase, following its naming conventions (prefixes, smart pointer types, namespaces) as you observe them.

Keep replies short: what you changed and any design decision the user should confirm. Reply in the user's language.

Modules at session start (module: class count):
{modules}
"""


def build_system(d):
    counts = {}
    for c in d["classes"].values():
        counts[c["module"]] = counts.get(c["module"], 0) + 1
    mods = "\n".join(f"- {m}: {n}" for m, n in sorted(counts.items()))
    return SYSTEM_TEMPLATE.format(repo_name=os.path.basename(d["repo"]), modules=mods)


def chat(repo, text, api_key, model, backend):
    if backend == "claude-code":
        return chat_cc(repo, text, model)
    with lock:
        return chat_api(repo, text, api_key, model)


def chat_api(repo, text, api_key, model):
    d = load(repo)
    if d is None:
        raise ToolError("diagram not opened yet")
    if not api_key and not os.environ.get("ANTHROPIC_API_KEY"):
        raise ToolError("API 키가 없음. ⚙ 설정에 키를 넣거나 서버 실행 전에 ANTHROPIC_API_KEY를 설정할 것")
    s = sessions.setdefault(repo, {"messages": [], "undo": [], "note": ""})
    if not s["messages"]:
        s["system"] = build_system(d)
    s["undo"].append(copy.deepcopy({"classes": d["classes"], "relations": d["relations"]}))
    del s["undo"][:-MAX_UNDO]

    content = (s["note"] + "\n\n" if s["note"] else "") + text
    base_len = len(s["messages"])
    s["messages"].append({"role": "user", "content": content})

    tools = DiagramTools(repo, d)
    client = api_client(api_key)
    extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model in FALLBACK_MODELS else {}
    reply, stop = [], None
    try:
        stop = run_turns(client, model, s, tools, reply, extra)
        s["note"] = ""
        if tools.ops:
            journal.add(data_path(repo), "design", "ai", "AI 요청: " + text.strip()[:200],
                        classes=tools.touched, items=[o["text"] for o in tools.ops])
    except Exception:
        # 응답을 하나도 못 받았으면 이번 요청은 이력에서 뺌 (모델이 본 적 없는 메시지라 안전함)
        if len(s["messages"]) == base_len + 1:
            del s["messages"][base_len:]
        raise
    finally:
        # API가 중간에 실패해도 이미 적용된 편집은 저장함 (대화 이력과 일치시키기 위해)
        if tools.ops:
            save(repo, d)
        else:
            s["undo"].pop()

    if stop == "refusal":
        reply.append("(모델이 요청을 거절함)")
    elif stop == "max_tokens":
        reply.append("(응답 길이 한도에 도달함)")
    elif stop == "loop_limit":
        reply.append("(도구 호출 반복 한도에 도달해 중단함)")
    return {"reply": "\n\n".join(reply), "ops": tools.ops, "touched": sorted(tools.touched),
            "diagram": d, "canUndo": bool(s["undo"])}


def run_turns(client, model, s, tools, reply, extra):
    stop = None
    for _ in range(40):
        resp = client.beta.messages.create(
            model=model, max_tokens=16000, system=s["system"], tools=TOOLS, messages=s["messages"],
            thinking={"type": "adaptive"}, output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"}, **extra)
        s["messages"].append({"role": "assistant", "content": resp.content})
        reply += [b.text for b in resp.content if b.type == "text" and b.text.strip()]
        stop = resp.stop_reason
        if stop == "pause_turn":
            continue
        if stop != "tool_use":
            break
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            try:
                out = tools.run(b.name, dict(b.input or {}))
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
            except (ToolError, TypeError, KeyError) as e:
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"Error: {e}", "is_error": True})
        s["messages"].append({"role": "user", "content": results})
    else:
        return "loop_limit"
    return stop


# ==================================================================== Claude Code(구독) 백엔드
# API 키 대신 이 PC에 로그인된 Claude Code CLI를 `claude -p`로 실행함.
# --restricted: 명령 실행 도구 제거 + 설정 파일 무시 + 파일 도구는 작업 폴더 안으로 제한 + 쓰기는 승인 불가
CLAUDE_BIN = shutil.which("claude")
PORT = 8765
CC_TIMEOUT = 900
CC_SYSTEM_EXTRA = (
    "\n\nTools in this environment: the diagram tools are mcp__diagram__* (list_classes, get_class, upsert_class, "
    "remove_class, add_members, edit_member, remove_members, restore, add_relation, remove_relation). Read source code "
    "with Read, Grep and Glob only; the working directory is the repository root. Source files are read-only.")


def claude_status():
    if not CLAUDE_BIN:
        return {"available": False}
    try:
        v = subprocess.run([CLAUDE_BIN, "--version"], capture_output=True, encoding="utf-8", errors="replace", timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
    return {"available": bool(v), "version": v}


def run_claude(prompt, args, cwd):
    if not CLAUDE_BIN:
        raise ToolError("claude CLI를 찾을 수 없음 (Claude Code 설치·로그인 필요)")
    try:
        r = subprocess.run([CLAUDE_BIN, "-p", "--output-format", "json", *args], input=prompt, capture_output=True,
                           encoding="utf-8", errors="replace", cwd=cwd, timeout=CC_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ToolError("Claude Code 응답 시간 초과")
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise ToolError("Claude Code 실행 실패: " + (r.stderr or r.stdout).strip()[:300])
    if out.get("is_error"):
        raise ToolError("Claude Code 오류: " + str(out.get("result") or out.get("subtype"))[:300])
    return out


def cc_structured(system, prompt, schema, model):
    """도구 없이 한 번 실행하고 --json-schema로 검증된 결과를 돌려줌."""
    args = ["--json-schema", json.dumps(schema), "--tools", "", "--no-session-persistence", "--restricted",
            "--strict-mcp-config", "--system-prompt", system]
    if model:
        args += ["--model", model]
    out = run_claude(prompt, args, tempfile.gettempdir())
    result = out.get("structured_output")
    if not isinstance(result, dict):
        raise ToolError("Claude Code 응답에서 결과를 찾지 못함")
    return result, next(iter(out.get("modelUsage") or {}), model)


def chat_cc(repo, text, model):
    with lock:
        d = load(repo)
        if d is None:
            raise ToolError("diagram not opened yet")
        s = sessions.setdefault(repo, {"messages": [], "undo": [], "note": ""})
        if not s.get("cc_session"):
            s["cc_system"] = build_system(d) + CC_SYSTEM_EXTRA
        s["undo"].append(copy.deepcopy({"classes": d["classes"], "relations": d["relations"]}))
        del s["undo"][:-MAX_UNDO]
        s["cc_ops"], s["cc_touched"] = [], set()
        content = (s["note"] + "\n\n" if s["note"] else "") + text
        cfg = os.path.join(DATA_DIR, "mcp-diagram.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"diagram": {"command": sys.executable, "args": [
                os.path.join(HERE, "mcp_diagram.py"), "--port", str(PORT), "--repo", repo]}}}, f)
    args = ["--restricted", "--strict-mcp-config", "--mcp-config", cfg, "--tools", "Read,Grep,Glob",
            "--allowedTools", "Read,Grep,Glob,mcp__diagram", "--system-prompt", s["cc_system"]]
    if model:
        args += ["--model", model]
    if s.get("cc_session"):
        args += ["--resume", s["cc_session"]]
    else:
        s["cc_session_new"] = str(uuid.uuid4())
        args += ["--session-id", s["cc_session_new"]]
    # 도구 호출은 mcp_diagram.py -> /api/tool 로 들어오므로 여기서는 lock을 잡지 않음
    try:
        out = run_claude(content, args, repo)
        s["note"] = ""
        s["cc_session"] = out.get("session_id") or s.get("cc_session_new")
    finally:
        with lock:
            ops, touched = s.get("cc_ops", []), set(s.get("cc_touched", set()))
            if not ops:
                s["undo"].pop()
    if ops:
        journal.add(data_path(repo), "design", "ai", "AI 요청: " + text.strip()[:200], classes=touched,
                    items=[o["text"] for o in ops])
    reply = out.get("result") or ""
    denied = {p.get("tool_name") for p in out.get("permission_denials") or []}
    if denied:
        reply += "\n\n(허용되지 않은 도구 요청은 거부됨: " + ", ".join(sorted(n for n in denied if n)) + ")"
    with lock:
        d = load(repo)
    return {"reply": reply, "ops": ops, "touched": sorted(touched), "diagram": d, "canUndo": bool(s["undo"])}


def open_in_editor(repo, rel, line):
    p = os.path.realpath(os.path.join(repo, rel))
    if os.path.commonpath([p, os.path.realpath(repo)]) != os.path.realpath(repo) or not os.path.isfile(p):
        raise ToolError(f"repo 안의 파일이 아님: {rel}")
    code = shutil.which("code")
    try:
        if code:
            subprocess.Popen([code, "-g", f"{p}:{max(1, int(line or 1))}"])
            return "vscode"
    except OSError:
        pass
    os.startfile(p)
    return "default"


# ==================================================================== 역할 추론
ROLE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "responsibilities": {"type": "array", "items": {"type": "string"}},
        "collaborators": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "why": {"type": "string"}},
            "required": ["name", "why"], "additionalProperties": False}},
        "notes": {"type": "string"},
    },
    "required": ["summary", "responsibilities", "collaborators", "notes"],
    "additionalProperties": False,
}
ROLE_SYSTEM = "You explain the role of C++ classes for a class diagram viewer. Ground every statement in the code you are given."
ROLE_LANG = {"ko": "Korean", "en": "English"}
MAX_CTX_LINES = 300


def class_block(repo, c):
    """헤더에서 클래스 선언 본문만 잘라냄 (주석 포함 원문)."""
    p = os.path.join(repo, c["file"])
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    blank = cppscan.blank_comments(raw)
    lines = raw.split("\n")
    start = sum(len(l) + 1 for l in lines[:max(0, c["line"] - 1)])
    # 선언 바로 위 주석도 근거가 되므로 몇 줄 앞에서 시작
    head = sum(len(l) + 1 for l in lines[:max(0, c["line"] - 6)])
    open_i = blank.find("{", start)
    if open_i < 0 or c["kind"] == "namespace":
        return "\n".join(lines[max(0, c["line"] - 6): c["line"] + MAX_CTX_LINES])
    end = cppscan.Scanner({}).match_brace(blank, open_i, len(blank))
    text = raw[head:end + 1].split("\n")
    return "\n".join(text[:MAX_CTX_LINES]) + ("\n// ... (truncated)" if len(text) > MAX_CTX_LINES else "")


def find_impl(repo, c):
    """헤더와 이름이 같은 .cpp를 찾아 클래스 이름이 나오는 부분 위주로 돌려줌."""
    stem = os.path.splitext(os.path.basename(c["file"]))[0]
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d.lower() not in cppscan.SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if os.path.splitext(fn)[0] == stem and fn.lower().endswith((".cpp", ".cc", ".cxx")):
                p = os.path.join(dirpath, fn)
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().split("\n")
                rel = os.path.relpath(p, repo).replace("\\", "/")
                key = c["name"] + "::"
                hits = [i for i, l in enumerate(lines) if key in l]
                if hits:
                    lines = lines[max(0, hits[0] - 2):]
                body = "\n".join(lines[:MAX_CTX_LINES])
                return rel, body + ("\n// ... (truncated)" if len(lines) > MAX_CTX_LINES else "")
    return None, None


def role_prompt(repo, d, c, lang):
    idx = cppscan.build_short_index(d["classes"])
    derived = [o["id"] for o in d["classes"].values()
               if any(cppscan.resolve(b, d["classes"], idx) == c["id"] for b in o["bases"])]
    rels = [f"{r['from']} -{r['type']}-> {r['to']} ({r.get('label', '')})" for r in d["relations"]
            if c["id"] in (r["from"], r["to"])]
    parts = [f"Repository: {os.path.basename(repo)}",
             f"Class: {c['id']} [{c['kind']}] in module {c['module']}",
             f"Bases: {', '.join(c['bases']) or '-'}",
             f"Derived classes: {', '.join(derived[:30]) or '-'}",
             "Relations:\n" + ("\n".join(rels[:30]) or "-")]
    if c["origin"] == "design" or not c.get("file"):
        parts.append("This class is a planned design that does not exist in code yet. Members:\n"
                     + "\n".join(f"{m['access']} {m['sig']}" for m in c["members"])
                     + (f"\nDesign note: {c['note']}" if c.get("note") else ""))
    else:
        parts.append(f"Declaration ({c['file']}):\n```cpp\n{class_block(repo, c)}\n```")
        rel, impl = find_impl(repo, c)
        if impl:
            parts.append(f"Implementation excerpt ({rel}):\n```cpp\n{impl}\n```")
    parts.append(
        f"Describe what this class is for in the engine, written in {ROLE_LANG.get(lang, 'Korean')}.\n"
        "- summary: one sentence naming its role (what it is responsible for, not a list of members).\n"
        "- responsibilities: 2-5 short items.\n"
        "- collaborators: up to 5 related classes from the context above and why they interact.\n"
        "- notes: anything uncertain or notable (threading, ownership, lifetime); empty string if nothing.")
    return "\n\n".join(parts)


def api_client(api_key):
    if anthropic is None:
        raise ToolError("API 키 방식에는 anthropic 패키지가 필요함: pip install -r requirements.txt (또는 AI 연결을 Claude Code로)")
    return anthropic.Anthropic(api_key=api_key or None)


def structured_call(system, prompt, schema, api_key, model, backend):
    """역할 추론·일지 요약 공용: 백엔드(Claude Code 구독 / API 키)에 맞게 JSON 결과를 받아옴."""
    if backend == "claude-code":
        return cc_structured(system, prompt, schema, model)
    if not api_key and not os.environ.get("ANTHROPIC_API_KEY"):
        raise ToolError("API 키가 없음. ⚙ 설정에 키를 넣거나 AI 연결을 Claude Code로 바꿀 것")
    client = api_client(api_key)
    extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model in FALLBACK_MODELS else {}
    resp = client.beta.messages.create(
        model=model, max_tokens=8000, system=system, messages=[{"role": "user", "content": prompt}],
        thinking={"type": "adaptive"},
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}}, **extra)
    if resp.stop_reason == "refusal":
        raise ToolError("모델이 요청을 거절함")
    try:
        return json.loads(next((b.text for b in resp.content if b.type == "text"), "")), resp.model
    except json.JSONDecodeError:
        raise ToolError("응답을 해석하지 못함 (응답이 잘렸을 수 있음)")


def infer_role(repo, cid, lang, api_key, model, backend):
    with lock:
        d = load(repo)
        if d is None or cid not in d["classes"]:
            raise ToolError(f"class '{cid}' not found")
        prompt = role_prompt(repo, d, d["classes"][cid], lang)
    role, used = structured_call(ROLE_SYSTEM, prompt, ROLE_SCHEMA, api_key, model, backend)
    role.update(lang=lang, model=used, at=now())
    # API 호출 동안 다른 변경이 있었을 수 있으므로 다시 읽고 role만 기록함
    with lock:
        d = load(repo)
        if cid in d["classes"]:
            d["classes"][cid]["role"] = role
            save(repo, d)
    return role


# ==================================================================== 일지 요약
JOURNAL_SYSTEM = ("You write concise development journal entries for a C++ game engine, "
                  "grounded only in the given commits, diffs and design log.")


def summarize_day(repo, date, lang, api_key, model, backend):
    datetime.date.fromisoformat(date)  # 형식 검증
    with lock:
        d = load(repo)
        if d is None:
            raise ToolError("열린 다이어그램이 없음")
        prompt = journal.summary_prompt(repo, d.get("roots", []), d, data_path(repo), date, lang)
    summary, used = structured_call(JOURNAL_SYSTEM, prompt, journal.SUMMARY_SCHEMA, api_key, model, backend)
    summary.update(lang=lang, model=used, at=now())
    with lock:
        journal.save_summary(data_path(repo), date, summary)
    return summary


# ==================================================================== 수동 플래그
USER_FLAGS = ("added", "modified", "review")
MANUAL_OPS = {"upsert_class", "remove_class", "add_members", "edit_member", "remove_members", "restore",
              "add_relation", "remove_relation", "add_memo", "remove_memo"}


def set_flags(d, cid, sig, state):
    """사용자가 드래그로 단 플래그를 그대로 기록함. state = {"removed": bool, "flags": [...]} (절대값이라 되돌리기도 같은 호출)."""
    c = d["classes"].get(cid)
    if c is None:
        raise ToolError(f"class '{cid}' not found")
    flags = [f for f in state.get("flags", []) if f in USER_FLAGS]
    removed = bool(state.get("removed"))
    if sig is None:
        c["flags"], c["removed"] = flags, removed
        return
    m = next((x for x in c["members"] if x["sig"] == sig), None)
    if m is None:
        raise ToolError(f"member '{sig}' not found in {cid}")
    m["flags"], m["removed"] = flags, removed
    label = m["name"].split(",")[0]
    for r in d["relations"]:
        if r["from"] == cid and r["label"].replace("[*]", "") == label:
            r["removed"] = removed


# ==================================================================== 폴더 선택 / 저장 파일 가져오기
# 브라우저는 폴더의 실제 경로를 알려주지 않으므로, 서버(로컬)가 OS 폴더 선택 창을 띄워 경로를 받음.
# tkinter는 HTTP 처리 스레드가 아닌 별도 프로세스에서 실행함
PICK_FOLDER_SCRIPT = r'''
import sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True); root.update()
p = filedialog.askdirectory(initialdir=sys.argv[1] or None, title="스캔할 repo 폴더 선택", mustexist=True, parent=root)
root.destroy()
sys.stdout.write(p or "")
'''


def pick_folder(initial):
    try:
        r = subprocess.run([sys.executable, "-c", PICK_FOLDER_SCRIPT, initial or ""], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=900, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except (OSError, subprocess.TimeoutExpired):
        raise ToolError("폴더 선택 창을 띄우지 못함 (경로를 직접 입력할 것)")
    path = r.stdout.strip()
    return os.path.normpath(path) if path else ""


def import_bundle(req):
    """Ctrl+S로 저장한 묶음(JSON)을 열어 이 PC의 작업 데이터로 씀."""
    b = req.get("bundle") or {}
    if b.get("format") != "class-diagram-bundle" or not isinstance(b.get("diagram"), dict):
        raise ToolError("ClassDiagram 저장 파일(JSON)이 아님")
    repo = norm_repo(req.get("repo") or b.get("repo") or "")
    if not os.path.isdir(repo):
        return 409, {"needRepo": True, "savedRepo": b.get("repo")}
    incoming = copy.deepcopy(b["diagram"])
    incoming["repo"] = repo
    with lock:
        cur = load(repo)
        mode = req.get("mode")
        if cur is not None and cur != incoming and mode is None:
            return 409, {"conflict": True, "repo": repo, "localScannedAt": cur.get("scannedAt"), "fileSavedAt": b.get("savedAt")}
        replaced = cur is None or mode == "replace"
        if replaced:
            save(repo, incoming)
            if isinstance(b.get("journal"), dict):
                journal.save(data_path(repo), b["journal"])
            sessions.pop(repo, None)
        d = incoming if replaced else cur
    return 200, {"diagram": d, "repo": repo, "replaced": replaced, "canUndo": False}


# ==================================================================== HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        # 다른 사이트가 폼(text/plain)으로 로컬 서버를 조작하지 못하게 JSON 요청만 받음 (JSON은 교차 출처면 사전 요청으로 막힘)
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.send_json(415, {"error": "application/json 요청만 받음"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            route = self.path.rstrip("/")
            # repo가 아직 없거나(폴더 선택) 이 PC에 없을 수 있는(저장 파일 열기) 요청은 repo 확인 전에 처리
            if route == "/api/pick_folder":
                return self.send_json(200, {"path": pick_folder(req.get("initial", ""))})
            if route == "/api/import":
                return self.send_json(*import_bundle(req))
            repo = norm_repo(req.get("repo", ""))
            if not os.path.isdir(repo):
                return self.send_json(400, {"error": f"repo 경로가 없음: {repo}"})
            roots = [r.strip().strip("/\\") for r in req.get("roots", []) if r.strip()]
            backend = req.get("backend") or "api"
            # AI 호출이 걸리는 요청은 전역 lock 밖에서 처리 (Claude Code 채팅은 도구 호출이 /api/tool로 다시 들어옴)
            if route == "/api/role":
                role = infer_role(repo, req["id"], req.get("lang", "ko"), req.get("apiKey"), req.get("model") or DEFAULT_MODEL, backend)
                return self.send_json(200, {"role": role})
            if route == "/api/journal_summary":
                return self.send_json(200, {"summary": summarize_day(
                    repo, req["date"], req.get("lang", "ko"), req.get("apiKey"), req.get("model") or DEFAULT_MODEL, backend)})
            if route == "/api/chat":
                return self.send_json(200, chat(repo, req["message"], req.get("apiKey"), req.get("model") or DEFAULT_MODEL, backend))
            if route == "/api/claude_status":
                return self.send_json(200, claude_status())
            if route == "/api/open_file":
                return self.send_json(200, {"opened": open_in_editor(repo, req["path"], req.get("line"))})
            if route == "/api/journal":
                with lock:
                    d = load(repo)
                if d is None:
                    return self.send_json(400, {"error": "열린 다이어그램이 없음"})
                days = max(1, min(365, int(req.get("days", 14))))
                return self.send_json(200, journal.build(repo, d.get("roots", []), d, data_path(repo), days))
            with lock:
                if route == "/api/open":
                    d = load(repo)
                    created = d is None
                    if created:
                        d = fresh_scan(repo, roots)
                        save(repo, d)
                    s = sessions.get(repo, {})
                    return self.send_json(200, {"diagram": d, "created": created, "canUndo": bool(s.get("undo"))})
                if route == "/api/rescan":
                    cur = load(repo)
                    new = fresh_scan(repo, roots or (cur or {}).get("roots", []))
                    d = merge_rescan(cur, new) if cur else new
                    save(repo, d)
                    if cur:
                        items, cls = journal.code_diff(cur, new, member_key)
                        if items:
                            journal.add(data_path(repo), "code", "rescan", f"재스캔: 코드에서 {len(items)}건 바뀜",
                                        classes=cls, items=items)
                    return self.send_json(200, {"diagram": d})
                if route == "/api/positions":
                    d = load(repo)
                    if d is None:
                        return self.send_json(400, {"error": "열린 다이어그램이 없음"})
                    for cid, (x, y) in req.get("positions", {}).items():
                        if cid in d["classes"]:
                            d["classes"][cid]["x"], d["classes"][cid]["y"] = round(x), round(y)
                    save(repo, d)
                    return self.send_json(200, {"ok": True})
                if route == "/api/flag":
                    d = load(repo)
                    if d is None:
                        return self.send_json(400, {"error": "열린 다이어그램이 없음"})
                    set_flags(d, req["id"], req.get("sig"), req["state"])
                    save(repo, d)
                    if req.get("label"):
                        journal.add(data_path(repo), "design", "manual", req["label"], classes=[req["id"]])
                    return self.send_json(200, {"class": d["classes"][req["id"]], "relations": d["relations"]})
                if route == "/api/edit":
                    # 인스펙터에서 직접 하는 편집. AI와 같은 도구를 쓰고, 되돌리기용 전/후 상태를 돌려줌
                    if req.get("op") not in MANUAL_OPS:
                        return self.send_json(400, {"error": f"허용되지 않는 편집: {req.get('op')}"})
                    d = load(repo)
                    if d is None:
                        return self.send_json(400, {"error": "열린 다이어그램이 없음"})
                    before = copy.deepcopy(d)
                    result = DiagramTools(repo, d).run(req["op"], req.get("args", {}))
                    ids = {cid for cid in set(before["classes"]) | set(d["classes"])
                           if before["classes"].get(cid) != d["classes"].get(cid)}
                    rel_changed = before["relations"] != d["relations"]
                    save(repo, d)
                    if ids or rel_changed:
                        journal.add(data_path(repo), "design", "manual", req.get("label") or req["op"], classes=ids,
                                    items=[result] if isinstance(result, str) else [])
                    pack = lambda src: {"classes": {cid: src["classes"].get(cid) for cid in ids},
                                        "relations": src["relations"] if rel_changed else None}
                    return self.send_json(200, {"result": result, "before": pack(before), "after": pack(d)})
                if route == "/api/restore_state":
                    d = load(repo)
                    if d is None:
                        return self.send_json(400, {"error": "열린 다이어그램이 없음"})
                    for cid, obj in req.get("classes", {}).items():
                        cur = d["classes"].get(cid)
                        if obj is None:
                            d["classes"].pop(cid, None)
                            continue
                        obj = {k: v for k, v in obj.items() if not k.startswith("_")}
                        if cur is not None and "x" in cur:
                            obj["x"], obj["y"] = cur["x"], cur["y"]
                        d["classes"][cid] = obj
                    if req.get("relations") is not None:
                        d["relations"] = req["relations"]
                    save(repo, d)
                    if req.get("label"):
                        journal.add(data_path(repo), "design", "manual", req["label"], classes=list(req.get("classes", {})))
                    return self.send_json(200, {"classes": {cid: d["classes"].get(cid) for cid in req.get("classes", {})},
                                                "relations": d["relations"]})
                if route == "/api/undo":
                    s = sessions.get(repo)
                    if not s or not s["undo"]:
                        return self.send_json(400, {"error": "되돌릴 변경이 없음"})
                    d = load(repo)
                    snap = s["undo"].pop()
                    # 위치와 사용자 플래그는 AI 변경이 아니므로 현재 값을 유지함
                    keep = {cid: (c.get("x"), c.get("y"), c.get("flags"), {m["sig"]: m.get("flags") for m in c["members"]})
                            for cid, c in d["classes"].items()}
                    d["classes"], d["relations"] = snap["classes"], snap["relations"]
                    for cid, (x, y, flags, mflags) in keep.items():
                        c = d["classes"].get(cid)
                        if c is None:
                            continue
                        if x is not None:
                            c["x"], c["y"] = x, y
                        if flags is not None:
                            c["flags"] = flags
                        for m in c["members"]:
                            if mflags.get(m["sig"]) is not None:
                                m["flags"] = mflags[m["sig"]]
                    save(repo, d)
                    s["note"] = "[Note: the user undid your most recent diagram changes. Re-check the diagram state before relying on it.]"
                    journal.add(data_path(repo), "design", "ai", "AI 변경 되돌림")
                    return self.send_json(200, {"diagram": d, "canUndo": bool(s["undo"])})
                if route == "/api/journal_note":
                    text = (req.get("text") or "").strip()
                    if not text:
                        return self.send_json(400, {"error": "메모가 비어 있음"})
                    journal.add(data_path(repo), "note", "user", text[:2000])
                    return self.send_json(200, {"ok": True})
                if route == "/api/journal_note_delete":
                    if not journal.remove_note(data_path(repo), req.get("at"), req.get("text")):
                        return self.send_json(404, {"error": "메모를 찾을 수 없음 (이미 지워졌을 수 있음)"})
                    return self.send_json(200, {"ok": True})
                if route == "/api/reset_chat":
                    sessions.pop(repo, None)
                    return self.send_json(200, {"ok": True})
                if route == "/api/tool_list":
                    return self.send_json(200, {"tools": TOOLS})
                if route == "/api/tool":
                    # Claude Code 채팅 중 mcp_diagram.py가 보내는 다이어그램 도구 호출
                    d = load(repo)
                    if d is None:
                        return self.send_json(400, {"error": "열린 다이어그램이 없음"})
                    s = sessions.setdefault(repo, {"messages": [], "undo": [], "note": ""})
                    t = DiagramTools(repo, d)
                    try:
                        text, err = t.run(req.get("name"), req.get("args") or {}), False
                    except (ToolError, TypeError, KeyError) as e:
                        text, err = f"Error: {e}", True
                    if t.ops:
                        save(repo, d)
                        s.setdefault("cc_ops", []).extend(t.ops)
                        s.setdefault("cc_touched", set()).update(t.touched)
                    return self.send_json(200, {"text": text, "isError": err})
                if route == "/api/bundle":
                    d = load(repo)
                    if d is None:
                        return self.send_json(400, {"error": "열린 다이어그램이 없음"})
                    return self.send_json(200, {"format": "class-diagram-bundle", "version": 1, "savedAt": now(),
                                                "repo": d["repo"], "diagram": d, "journal": journal.load(data_path(repo))})
            self.send_json(404, {"error": "unknown route"})
        except ToolError as e:
            self.send_json(400, {"error": str(e)})
        except Exception as e:
            if anthropic is not None and isinstance(e, anthropic.APIError):
                return self.send_json(*api_error_response(e))
            self.send_json(500, {"error": f"{type(e).__name__}: {e}"})  # 로컬 툴이라 원인을 그대로 보여줌


def api_error_response(e):
    if isinstance(e, anthropic.AuthenticationError):
        return 401, {"error": "API 키 인증 실패. 키를 확인하거나 ANTHROPIC_API_KEY를 설정할 것"}
    if isinstance(e, anthropic.RateLimitError):
        return 429, {"error": "rate limit. 잠시 후 다시 시도"}
    if isinstance(e, anthropic.APIStatusError):
        if "credit balance" in str(e.message):
            return 402, {"error": "API 크레딧이 부족함. Console(platform.claude.com) → Plans & Billing에서 크레딧을 충전할 것"}
        return 502, {"error": f"API 오류 {e.status_code}: {e.message}"}
    if isinstance(e, anthropic.APIConnectionError):
        return 502, {"error": "API 연결 실패 (네트워크 확인)"}
    return 502, {"error": f"API 오류: {e}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    global PORT
    PORT = args.port
    url = f"http://127.0.0.1:{args.port}/"
    # Windows에서는 SO_REUSEADDR이면 같은 포트에 서버가 중복으로 떠서 요청이 옛 서버로 갈 수 있으므로 끔
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        print(f"포트 {args.port}가 이미 사용 중임. 이미 실행 중인 서버가 있으면 그 창을 쓰거나 Ctrl+C로 끈 뒤 다시 실행할 것.\n"
              f"(다른 포트로 띄우려면: python server.py --port {args.port + 1})")
        if not args.no_browser:
            webbrowser.open(url)
        sys.exit(1)
    print(f"Class Diagram tool: {url}")
    if not args.no_browser:
        webbrowser.open(url)
    srv.serve_forever()


if __name__ == "__main__":
    main()
