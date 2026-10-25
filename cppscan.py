"""C++ 헤더를 훑어 클래스 다이어그램 모델(dict)을 만드는 경량 파서.

정확한 C++ 파서가 아니라 중괄호/세미콜론 단위로 문장을 끊어 읽는 휴리스틱임.
목표는 다이어그램에 필요한 정보(클래스, 상속, 멤버, 필드 타입 관계)까지만.
"""
import os
import re
from collections import Counter

HEADER_EXTS = (".h", ".hpp", ".hh", ".hxx", ".inl")
SKIP_DIRS = {".git", ".vs", ".vscode", ".idea", "build", "out", "bin", "obj", "external",
             "third_party", "thirdparty", "vendor", "node_modules", "graphify-out", ".serena", ".codex"}
MODULE_DROP = {"public", "private", "include", "src", "source", "inc"}

ACCESS_RE = re.compile(r"^\s*(public|protected|private)\s*:(?!:)")
MACRO_CALL_RE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*\(")
DEFINE_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)\(([^)]*)\)(.*)$")
CLASS_HEAD_RE = re.compile(
    r"^(class|struct|union)\s+(?:alignas\s*\([^)]*\)\s*)?(?:[A-Z_][A-Z0-9_]*\s+)?"
    r"([A-Za-z_]\w*)\s*(final\s*)?(?::(.*))?$", re.S)
ENUM_HEAD_RE = re.compile(r"^enum\s+(?:class\s+|struct\s+)?([A-Za-z_]\w*)\s*(?::\s*(.+))?$", re.S)
NAMESPACE_RE = re.compile(r"^(?:inline\s+)?namespace\s*([\w:]*)\s*$")
SKIP_STMT_RE = re.compile(r"^(using|typedef|friend|static_assert|template\s*<[^>]*>\s*friend)\b")
IDENT_RE = re.compile(r"[A-Za-z_]\w*")

OWN_WORDS = ("unique_ptr", "UniquePtr", "Owner", "Scope")
REF_WORDS = ("shared_ptr", "weak_ptr", "RefPtr", "ObjectPtr", "WeakPtr", "SharedPtr", "Ref", "Ptr", "optional")
MANY_WORDS = ("vector", "array", "Array", "DynamicArray", "StaticArray", "list", "List", "deque", "map",
              "Map", "set", "Set", "unordered_map", "unordered_set", "HashMap", "HashSet", "span", "Pool")
TYPE_NOISE = {"const", "volatile", "static", "mutable", "constexpr", "inline", "std", "struct", "class",
              "enum", "typename", "unsigned", "signed", "long", "short", "int", "char", "bool", "float",
              "double", "void", "auto", "size_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t", "int8_t",
              "int16_t", "int32_t", "int64_t", "thread_local", "atomic"} | set(OWN_WORDS) | set(REF_WORDS) | set(MANY_WORDS)


def blank_comments(src):
    """주석·문자열·전처리 줄을 같은 길이의 공백으로 치환함 (줄 번호 유지)."""
    out = list(src)
    i, n = 0, len(src)

    def blank(a, b):
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    line_start = True
    while i < n:
        c = src[i]
        if line_start and c in " \t":
            i += 1
            continue
        if line_start and c == "#":
            j = i
            while j < n:
                if src[j] == "\n" and src[j - 1] != "\\":
                    break
                j += 1
            blank(i, j)
            i = j
            continue
        line_start = c == "\n"
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif c == '"' or (c == "'" and not (i > 0 and src[i - 1].isalnum())):
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            blank(i + 1, min(j, n))
            i = j + 1
        else:
            i += 1
    return "".join(out)


def collect_class_macros(src):
    """본문에 class/struct 정의를 품은 function-like 매크로만 모음."""
    macros = {}
    lines = src.split("\n")
    i = 0
    while i < len(lines):
        m = DEFINE_RE.match(lines[i])
        if m:
            body = [m.group(3)]
            while body[-1].rstrip().endswith("\\") and i + 1 < len(lines):
                body[-1] = body[-1].rstrip()[:-1]
                i += 1
                body.append(lines[i])
            text = re.sub(r"//[^\n]*", "", "\n".join(body))
            if re.search(r"\b(class|struct)\s+\w+", text) and "{" in text:
                params = [p.strip() for p in m.group(2).split(",") if p.strip()]
                macros[m.group(1)] = (params, text)
        i += 1
    return macros


def split_top(s, sep=",", angles=True):
    """괄호(angles면 꺾쇠까지) 깊이 0에서만 분리함."""
    opens, closes = ("<([{", ">)]}") if angles else ("([{", ")]}")
    parts, depth, cur = [], 0, []
    for c in s:
        if c in opens:
            depth += 1
        elif c in closes:
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def strip_template_prefix(s):
    s = s.lstrip()
    if not s.startswith("template"):
        return s, ""
    i = s.find("<")
    if i < 0:
        return s, ""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "<":
            depth += 1
        elif s[j] == ">":
            depth -= 1
            if depth == 0:
                return s[j + 1:].lstrip(), re.sub(r"\s+", " ", s[i:j + 1])
    return s, ""


def squash(s):
    return re.sub(r"\s+", " ", s).strip()


def strip_macro_calls(s):
    """문장 앞에 붙은 매크로 호출(GENERATE(X) 등)을 떼어냄."""
    while True:
        m = MACRO_CALL_RE.match(s)
        if not m:
            return s
        depth, j = 0, m.end() - 1
        while j < len(s):
            if s[j] == "(":
                depth += 1
            elif s[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        s = s[j + 1:]


class Scanner:
    def __init__(self, macros):
        self.macros = macros
        self.classes = {}       # qualified -> class dict
        self.namespaces = {}    # qualified -> namespace node dict
        self.ns_has_class = set()

    # ---------------------------------------------------------------- 진입점
    def scan_file(self, path, rel, module):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
        text = blank_comments(raw)
        self.ctx = {"file": rel, "module": module}
        self.parse_scope(text, 0, len(text), [], None, text, None)

    def line_of(self, buf, pos, fixed):
        return fixed if fixed is not None else buf.count("\n", 0, pos) + 1

    # ---------------------------------------------------------------- 스코프 순회
    def parse_scope(self, t, i, end, ns, cls, buf, fixed_line):
        """namespace 또는 class 본문을 문장 단위로 순회함. cls가 있으면 class 본문."""
        access = None
        if cls is not None:
            access = "public" if cls["kind"] == "struct" else "private"
        start = i
        depth = 0
        while i < end:
            c = t[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif depth == 0 and c in ";{}":
                stmt = t[start:i]
                stmt, access = self.eat_access(stmt, access)
                if c == "}":
                    start = i + 1
                    i += 1
                    continue
                stmt_pos = start + (len(t[start:i]) - len(t[start:i].lstrip()))
                line = self.line_of(buf, stmt_pos, fixed_line)
                if c == "{":
                    close = self.match_brace(t, i, end)
                    is_type = self.handle_block(t, stmt, i, close, ns, cls, access, buf, fixed_line, line)
                    i = close + 1
                    # 블록 뒤의 ";" 와 타입 정의 뒤 "} name;" 선언자는 버림
                    j = i
                    while j < end and t[j] not in ";{}":
                        j += 1
                    tail = t[i:j].strip()
                    if j < end and t[j] == ";" and (not tail or (is_type and re.fullmatch(r"[\w\s,*&\[\]]+", tail))):
                        i = j + 1
                    start = i
                    continue
                self.handle_stmt(stmt, ns, cls, access, line)
                start = i + 1
            i += 1

    def eat_access(self, stmt, access):
        while True:
            m = ACCESS_RE.match(stmt)
            if not m:
                return stmt, access
            access = m.group(1)
            stmt = stmt[m.end():]

    def match_brace(self, t, i, end):
        depth = 0
        for j in range(i, end):
            if t[j] == "{":
                depth += 1
            elif t[j] == "}":
                depth -= 1
                if depth == 0:
                    return j
        return end - 1

    # ---------------------------------------------------------------- 블록 { }
    def handle_block(self, t, header, open_i, close_i, ns, cls, access, buf, fixed_line, line):
        h = squash(strip_macro_calls(header))
        if h.startswith('extern') and cls is None:
            self.parse_scope(t, open_i + 1, close_i, ns, None, buf, fixed_line)
            return
        m = NAMESPACE_RE.match(h)
        if m and cls is None:
            names = [x for x in m.group(1).split("::") if x] or ["(anonymous)"]
            self.parse_scope(t, open_i + 1, close_i, ns + names, None, buf, fixed_line)
            return
        body_h, tmpl = strip_template_prefix(h)
        m = CLASS_HEAD_RE.match(body_h)
        if m and "(" not in body_h:
            kind, name, final, bases = m.group(1), m.group(2), m.group(3), m.group(4)
            if kind == "union":
                kind = "struct"
            outer = cls["qname"] if cls else "::".join(ns)
            qname = f"{outer}::{name}" if outer else name
            info = {
                "qname": qname, "name": name, "kind": kind, "ns": list(ns),
                "outer": cls["qname"] if cls else None,
                "module": self.ctx["module"], "file": self.ctx["file"], "line": line,
                "template": tmpl, "final": bool(final),
                "bases": [re.sub(r"^(public|protected|private|virtual)\s+(virtual\s+)?", "", b).strip()
                          for b in split_top(bases or "")],
                "members": [],
            }
            self.classes[qname] = info
            if cls is None:
                self.ns_has_class.add("::".join(ns))
            self.parse_scope(t, open_i + 1, close_i, ns, info, buf, fixed_line)
            return True
        m = ENUM_HEAD_RE.match(body_h)
        if m:
            outer = cls["qname"] if cls else "::".join(ns)
            qname = f"{outer}::{m.group(1)}" if outer else m.group(1)
            values = []
            for v in split_top(t[open_i + 1:close_i], angles=False):
                vn = IDENT_RE.match(v)
                if vn:
                    values.append({"kind": "value", "name": vn.group(0), "sig": squash(v), "access": "public"})
            self.classes[qname] = {
                "qname": qname, "name": m.group(1), "kind": "enum", "ns": list(ns),
                "outer": cls["qname"] if cls else None,
                "module": self.ctx["module"], "file": self.ctx["file"], "line": line,
                "template": "", "final": False, "bases": [], "underlying": squash(m.group(2) or ""),
                "members": values,
            }
            if cls is None:
                self.ns_has_class.add("::".join(ns))
            return True
        # 인라인 함수 본문 -> 선언부만 멤버로 (생성자 초기화 리스트는 잘라냄)
        if cls is not None and "(" in h:
            self.handle_stmt(re.sub(r"\)\s*:(?!:).*$", ")", header, flags=re.S), ns, cls, access, line)
        elif cls is not None:
            self.handle_stmt(header, ns, cls, access, line)  # "T x{...};" / "T x = {...};"
        elif "(" in h and ns:
            self.handle_stmt(header, ns, None, None, line)
        return False

    # ---------------------------------------------------------------- 문장 ;
    def handle_stmt(self, stmt, ns, cls, access, line):
        raw = squash(stmt)
        if not raw:
            return
        mc = MACRO_CALL_RE.match(raw)
        if mc and mc.group(1) in self.macros and cls is None:
            self.expand_macro(raw, mc.group(1), ns, line)
            return
        s = squash(strip_macro_calls(raw))
        if not s or SKIP_STMT_RE.match(s):
            return
        s, tmpl = strip_template_prefix(s)
        if re.match(r"^(class|struct|union|enum)\b[^(]*$", s):
            return  # 전방 선언
        member = self.parse_member(s, cls)
        if member is None:
            return
        if tmpl:
            member["sig"] = f"template{tmpl} {member['sig']}"
        if cls is not None:
            member["access"] = access or "public"
            cls["members"].append(member)
        elif member["kind"] == "method" and ns:
            q = "::".join(ns)
            node = self.namespaces.setdefault(q, {
                "qname": q, "name": ns[-1], "kind": "namespace", "ns": ns[:-1], "outer": None,
                "module": self.ctx["module"], "file": self.ctx["file"], "line": line,
                "template": "", "final": False, "bases": [], "members": []})
            member["access"] = "public"
            if not any(x["sig"] == member["sig"] for x in node["members"]):
                node["members"].append(member)

    def expand_macro(self, raw, name, ns, line):
        params, body = self.macros[name]
        inner = raw[raw.find("(") + 1: raw.rfind(")")]
        args = split_top(inner)
        text = body
        for p, a in zip(params, args):
            text = re.sub(r"##\s*\b%s\b|\b%s\b\s*##|\b%s\b" % (p, p, p), a, text)
        text = text.replace("##", "")
        text = blank_comments(text)
        self.parse_scope(text, 0, len(text), ns, None, text, line)

    def parse_member(self, s, cls):
        s = re.sub(r"\b(override|noexcept(\s*\([^)]*\))?)\b", "", s)
        s = squash(re.sub(r"\[\[[^\]]*\]\]", "", s))
        paren = self.first_top_paren(s)
        eq = self.first_top(s, "=")
        is_method = paren >= 0 and (eq < 0 or paren < eq or re.search(r"\boperator\s*\S*\s*$", s[:paren]))
        if is_method:
            head = s[:paren].rstrip()
            nm = re.search(r"(operator\s*[^\s(]+|operator\s*\(\s*\)|~?[A-Za-z_]\w*)\s*$", head)
            if not nm:
                return None
            name = nm.group(1)
            if cls is None and re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
                return None
            abstract = bool(re.search(r"=\s*0\s*$", s))
            sig = re.sub(r"\s*=\s*(0|default|delete)\s*$", lambda m: " = 0" if m.group(1) == "0" else (" = delete" if m.group(1) == "delete" else ""), s)
            return {"kind": "method", "name": name, "sig": squash(sig),
                    "static": bool(re.match(r"^(static|inline static)\b", s)),
                    "virtual": bool(re.match(r"^(virtual|inline virtual)\b", s)) or abstract,
                    "abstract": abstract}
        if cls is None:
            return None
        decl = s[:eq].strip() if eq >= 0 else s
        decl = re.sub(r"\{.*$", "", decl).strip()
        first = split_top(decl)[0] if split_top(decl) else decl
        nm = re.search(r"([A-Za-z_]\w*)\s*(\[[^\]]*\])*\s*(:\s*\d+)?$", first)
        if not nm or nm.start() == 0:
            return None
        type_part = first[:nm.start()].strip()
        names = [nm.group(1)] + [IDENT_RE.match(x.strip().lstrip("*&")).group(0)
                                 for x in split_top(decl)[1:] if IDENT_RE.match(x.strip().lstrip("*&"))]
        return {"kind": "field", "name": ", ".join(names), "sig": squash(decl), "type": type_part,
                "static": bool(re.match(r"^(static|inline static|static constexpr|constexpr static)\b", s)),
                "virtual": False, "abstract": False}

    def first_top(self, s, ch):
        depth = 0
        for i, c in enumerate(s):
            if c in "<([{":
                depth += 1
            elif c in ">)]}":
                depth = max(0, depth - 1)
            elif c == ch and depth == 0:
                if ch == "=" and (s[i + 1:i + 2] == "=" or s[i - 1:i] in "!<>="):
                    continue
                return i
        return -1

    def first_top_paren(self, s):
        depth = 0
        for i, c in enumerate(s):
            if c == "<":
                depth += 1
            elif c == ">":
                depth = max(0, depth - 1)
            elif c == "(" and depth == 0:
                return i
        return -1


# -------------------------------------------------------------------- 모델 조립
def module_of(rel_dir):
    parts = [p for p in rel_dir.replace("\\", "/").split("/") if p and p.lower() not in MODULE_DROP]
    return "/".join(parts) or "(root)"


def iter_headers(repo, roots):
    bases = [os.path.join(repo, r) for r in roots] if roots else [repo]
    for base in bases:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith("."))
            for fn in sorted(filenames):
                if fn.lower().endswith(HEADER_EXTS):
                    yield os.path.join(dirpath, fn)


def scan_repo(repo, roots=None):
    repo = os.path.abspath(repo)
    files = list(iter_headers(repo, roots or []))
    macros = {}
    for p in files:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            macros.update(collect_class_macros(f.read()))
    sc = Scanner(macros)
    for p in files:
        rel = os.path.relpath(p, repo).replace("\\", "/")
        sc.scan_file(p, rel, module_of(os.path.dirname(rel)))

    nodes = list(sc.classes.values()) + [n for q, n in sc.namespaces.items() if q not in sc.ns_has_class]
    # 가장 흔한 최상위 namespace(wtr 등)는 표시 이름에서 생략
    root_ns = Counter(n["ns"][0] for n in nodes if n["ns"]).most_common(1)
    root_ns = root_ns[0][0] if root_ns else None

    def display(n):
        q = n["qname"].split("::")
        if root_ns and q and q[0] == root_ns and len(q) > 1:
            q = q[1:]
        return "::".join(q)

    classes = {}
    for n in nodes:
        cid = display(n)
        if cid in classes:
            cid = n["qname"]
        members = [{
            "kind": m["kind"], "name": m["name"], "sig": m["sig"], "access": m.get("access", "public"),
            "static": m.get("static", False), "virtual": m.get("virtual", False),
            "abstract": m.get("abstract", False), "type": m.get("type", ""),
            "origin": "code", "removed": False,
        } for m in n["members"]]
        kind = n["kind"]
        if kind == "class" and members and all(m["kind"] != "field" for m in members) \
                and any(m["abstract"] for m in members):
            kind = "interface"
        classes[cid] = {
            "id": cid, "name": n["name"], "kind": kind, "module": n["module"],
            "file": n["file"], "line": n["line"], "template": n["template"],
            "bases": n["bases"], "underlying": n.get("underlying", ""), "outer": None,
            "origin": "code", "removed": False, "note": "", "members": members,
        }
        n["_id"] = cid
    by_q = {n["qname"]: n["_id"] for n in nodes}
    for n in nodes:
        if n.get("outer") in by_q:
            classes[n["_id"]]["outer"] = by_q[n["outer"]]
    return {"classes": classes, "relations": infer_relations(classes)}


def resolve(name, classes, short_index):
    """타입 이름 -> 다이어그램 클래스 id. 못 찾으면 None."""
    name = re.sub(r"<.*$", "", name).strip()
    if name in classes:
        return name
    tail = name.split("::")[-1]
    cands = short_index.get(tail, [])
    if len(cands) == 1:
        return cands[0]
    for c in cands:
        if c.endswith("::" + name) or c == name:
            return c
    return cands[0] if cands else None


def build_short_index(classes):
    idx = {}
    for cid, c in classes.items():
        if not c.get("removed"):
            idx.setdefault(c["name"], []).append(cid)
    return idx


def infer_relations(classes):
    """필드 타입에서 composition/aggregation/association 추정."""
    idx = build_short_index(classes)
    rels, seen = [], set()
    for cid, c in classes.items():
        for m in c["members"]:
            if m["kind"] != "field" or not m.get("type"):
                continue
            t = m["type"]
            words = IDENT_RE.findall(t)
            many = any(w in MANY_WORDS for w in words)
            own = any(w in OWN_WORDS for w in words)
            ref = "*" in t or "&" in t or any(w in REF_WORDS for w in words)
            for w in words:
                if w in TYPE_NOISE:
                    continue
                target = resolve(w, classes, idx)
                if not target or target == cid or classes[target]["kind"] == "enum":
                    continue
                if own:
                    rtype = "composition"
                elif ref:
                    rtype = "association"
                elif many:
                    rtype = "aggregation"
                else:
                    rtype = "composition"
                key = (cid, target, rtype)
                if key in seen:
                    continue
                seen.add(key)
                rels.append({"from": cid, "to": target, "type": rtype,
                             "label": m["name"].split(",")[0] + ("[*]" if many else ""),
                             "origin": "code", "removed": False})
    return rels


if __name__ == "__main__":
    import json
    import sys
    model = scan_repo(sys.argv[1], sys.argv[2:])
    print(json.dumps({"classes": len(model["classes"]), "relations": len(model["relations"])}))
