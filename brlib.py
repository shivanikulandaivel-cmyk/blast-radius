"""Shared pieces: patch parsing/applying, scope analysis, candidate selection."""

import ast
import os
import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FileDiff:
    def __init__(self):
        self.old = None  # path relative to repo, None if new file
        self.new = None  # path relative to repo, None if deleted
        self.hunks = []  # (old_start, old_len, new_start, new_len, [lines])
        self.binary = False

    @property
    def path(self):
        return self.old or self.new


def _strip(p):
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return p


def parse_patch(text):
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = FileDiff()
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
            if m:
                cur.old, cur.new = m.group(1), m.group(2)
            i += 1
            continue
        if ln.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = FileDiff()
                files.append(cur)
            cur.old = _strip(ln[4:])
            cur.new = _strip(lines[i + 1][4:])
            i += 2
            continue
        if cur is not None and (ln.startswith("new file mode")):
            cur.old = None
        elif cur is not None and ln.startswith("deleted file mode"):
            cur.new = None
        elif cur is not None and ln.startswith("rename from "):
            cur.old = ln[len("rename from "):]
        elif cur is not None and ln.startswith("rename to "):
            cur.new = ln[len("rename to "):]
        elif cur is not None and (ln.startswith("Binary files") or ln.startswith("GIT binary patch")):
            cur.binary = True
        m = HUNK.match(ln)
        if m and cur is not None:
            os_, ol, ns, nl = m.groups()
            os_, ns = int(os_), int(ns)
            ol = 1 if ol is None else int(ol)
            nl = 1 if nl is None else int(nl)
            body = []
            i += 1
            ro, rn = ol, nl
            while i < n and (ro > 0 or rn > 0):
                b = lines[i]
                if b.startswith("\\"):
                    body.append(b)
                    i += 1
                    continue
                tag = b[:1] if b else " "
                if tag == " ":
                    ro -= 1
                    rn -= 1
                elif tag == "-":
                    ro -= 1
                elif tag == "+":
                    rn -= 1
                else:
                    break
                body.append(b if b else " ")
                i += 1
            while i < n and lines[i].startswith("\\"):
                body.append(lines[i])
                i += 1
            cur.hunks.append((os_, ol, ns, nl, body))
            continue
        i += 1
    return [f for f in files if f.old or f.new]


def apply_hunks(old_text, fd):
    """Apply a FileDiff's hunks to old_text (str or None). Returns new text."""
    if old_text is None:
        old_text = ""
    old = old_text.split("\n")
    # keep track of trailing newline: split gives '' at end if text ends with \n
    ends_nl = old_text.endswith("\n")
    if ends_nl:
        old = old[:-1]
    if old_text == "":
        old = []
    out = []
    pos = 0  # index into old (0-based)
    new_ends_nl = ends_nl if old else True
    for os_, ol, ns, nl, body in fd.hunks:
        start = os_ - 1 if ol > 0 else os_  # for ol==0, insert after line os_
        if start < pos:
            raise ValueError("overlapping hunks")
        out.extend(old[pos:start])
        pos = start
        prev = None
        for b in body:
            if b.startswith("\\"):
                if prev == "+":
                    new_ends_nl = False
                elif prev == " ":
                    new_ends_nl = False
                continue
            tag, content = b[0], b[1:]
            if tag == " ":
                if pos < len(old) and old[pos] != content:
                    raise ValueError("context mismatch at %d" % (pos + 1))
                out.append(content)
                pos += 1
            elif tag == "-":
                if pos < len(old) and old[pos] != content:
                    raise ValueError("delete mismatch at %d" % (pos + 1))
                pos += 1
            elif tag == "+":
                out.append(content)
            prev = tag
        if prev == "+" or prev == " ":
            pass
    out.extend(old[pos:])
    # "\ No newline" after a '-' line means the old file lacked one; new keeps default
    text = "\n".join(out)
    if out and new_ends_nl:
        text += "\n"
    return text


def changed_old_lines(fd):
    """Old-file line numbers touched: removed lines, and for pure insertions the
    lines on both sides of the insertion point."""
    touched = set()
    for os_, ol, ns, nl, body in fd.hunks:
        o = os_ if ol > 0 else os_ + 1  # next old line number
        last_ctx = os_ - 1 if ol > 0 else os_
        for b in body:
            if b.startswith("\\"):
                continue
            tag = b[0]
            if tag == " ":
                last_ctx = o
                o += 1
            elif tag == "-":
                touched.add(o)
                last_ctx = o
                o += 1
            elif tag == "+":
                touched.add(("ins", last_ctx, o))
    return touched


def added_text(fd):
    return [b[1:] for h in fd.hunks for b in h[4] if b.startswith("+")]


def removed_text(fd):
    return [b[1:] for h in fd.hunks for b in h[4] if b.startswith("-")]


# ---------------------------------------------------------------- scopes


class Scopes:
    """For a python source: function body ranges and the import-time lines."""

    def __init__(self, src):
        self.ok = True
        try:
            tree = ast.parse(src)
        except SyntaxError:
            self.ok = False
            self.funcs = []
            self.classes = []
            return
        self.funcs = []  # (body_start, end, node_start)
        self.classes = []  # (start, end, name)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bs = node.body[0].lineno
                self.funcs.append((bs, node.end_lineno, node.lineno, node.name))
            elif isinstance(node, ast.Lambda):
                self.funcs.append((node.body.lineno, node.body.end_lineno, node.lineno, "<lambda>"))
            elif isinstance(node, ast.ClassDef):
                self.classes.append((node.lineno, node.end_lineno, node.name))
        # lines whose statement belongs to module or class body (import-time)
        self.import_lines = set()

        def visit_body(stmts):
            for s in stmts:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # decorators, def line and signature defaults run at import
                    start = min([d.lineno for d in s.decorator_list] + [s.lineno])
                    self.import_lines.update(range(start, s.body[0].lineno))
                elif isinstance(s, ast.ClassDef):
                    start = min([d.lineno for d in s.decorator_list] + [s.lineno])
                    self.import_lines.update(range(start, s.body[0].lineno))
                    visit_body(s.body)
                else:
                    self.import_lines.update(range(s.lineno, s.end_lineno + 1))
                    # compound statements at import level (if/try/with) may hold defs
                    for attr in ("body", "orelse", "finalbody", "handlers"):
                        sub = getattr(s, attr, None)
                        if isinstance(sub, list) and sub and isinstance(sub[0], ast.AST):
                            pass

        visit_body(tree.body)

    def innermost_func(self, line):
        best = None
        for bs, end, st, name in self.funcs:
            if st <= line <= end and (bs <= line or st == line):
                if best is None or (end - bs) < (best[1] - best[0]):
                    best = (bs, end, st, name)
        return best

    def class_at(self, line):
        best = None
        for st, end, name in self.classes:
            if st <= line <= end:
                if best is None or (end - st) < (best[1] - best[0]):
                    best = (st, end, name)
        return best


def module_name(path):
    if not path.endswith(".py"):
        return None
    m = path[:-3].replace("/", ".")
    if m.endswith(".__init__"):
        m = m[: -len(".__init__")]
    return m


def import_graph(root, pkg="sqlglot"):
    """module -> set of sqlglot modules it imports (static, any level)."""
    graph = {}
    base = os.path.join(root, pkg)
    for dp, dn, fn in os.walk(base):
        for f in fn:
            if not f.endswith(".py"):
                continue
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, root)
            mod = module_name(rel)
            try:
                tree = ast.parse(open(full, encoding="utf-8").read())
            except Exception:
                continue
            deps = set()
            is_pkg = f == "__init__.py"
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.startswith(pkg):
                            deps.add(a.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        parts = mod.split(".")
                        if not is_pkg:
                            parts = parts[:-1]
                        parts = parts[: len(parts) - (node.level - 1)]
                        b = ".".join(parts + ([node.module] if node.module else []))
                    else:
                        b = node.module or ""
                    if b.startswith(pkg):
                        deps.add(b)
                        for a in node.names:
                            deps.add(b + "." + a.name)
            graph[mod] = deps
    mods = set(graph)
    return {m: {d for d in ds if d in mods and d != m} for m, ds in graph.items()}


def importers_closure(graph, mod):
    rev = {}
    for m, ds in graph.items():
        for d in ds:
            rev.setdefault(d, set()).add(m)
    seen = {mod}
    stack = [mod]
    while stack:
        x = stack.pop()
        for y in rev.get(x, ()):
            if y not in seen:
                seen.add(y)
                stack.append(y)
    return seen


# ---------------------------------------------------------------- AST diff


def _strip_dump(node):
    return ast.dump(node, annotate_fields=False, include_attributes=False)


class _Units:
    """Split a module into units: functions (incl. nested), lambdas, class
    skeletons and the module skeleton. Each unit has a key, a fingerprint and
    an old line range."""

    def __init__(self, tree):
        self.units = {}  # key -> (fingerprint, (start, end), kind, owner_class)
        self._walk_body(tree, (), None)

    def _key(self, path):
        k = path
        n = 0
        while (k + (n,)) in self.units:
            n += 1
        return k + (n,)

    def _walk_body(self, node, path, cls):
        # skeleton of node: functions/classes replaced by headers
        for child in ast.iter_child_nodes(node):
            pass
        skel = self._skeleton(node, path, cls)
        kind = "module" if isinstance(node, ast.Module) else "class"
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", 10**9)
        self.units[self._key(path + (kind,))] = (skel, (start, end), kind, cls)

    def _skeleton(self, node, path, cls):
        units = self

        class T(ast.NodeTransformer):
            def visit_FunctionDef(self, n):
                return self._func(n)

            def visit_AsyncFunctionDef(self, n):
                return self._func(n)

            def _func(self, n):
                qual = path + (n.name,)
                fp = _strip_dump(n)
                units.units[units._key(qual + ("func",))] = (
                    fp,
                    (n.body[0].lineno, n.end_lineno),
                    "func",
                    cls,
                )
                n2 = ast.FunctionDef(
                    name=n.name,
                    args=self.visit(n.args),
                    body=[ast.Pass()],
                    decorator_list=[self.visit(d) for d in n.decorator_list],
                    returns=None,
                    type_comment=None,
                    type_params=[],
                )
                return n2

            def visit_Lambda(self, n):
                fp = _strip_dump(n)
                units.units[units._key(path + ("lambda",))] = (
                    fp,
                    (n.lineno, n.end_lineno),
                    "lambda",
                    cls,
                )
                return ast.Constant(value="<lambda>")

            def visit_ClassDef(self, n):
                units._walk_body(n, path + (n.name,), path + (n.name,))
                return ast.ClassDef(
                    name=n.name,
                    bases=[self.visit(b) for b in n.bases],
                    keywords=[self.visit(k) for k in n.keywords],
                    body=[ast.Pass()],
                    decorator_list=[self.visit(d) for d in n.decorator_list],
                    type_params=[],
                )

        import copy

        node = copy.deepcopy(node)
        t = T()
        if isinstance(node, ast.Module):
            body = [t.visit(s) for s in node.body]
        else:
            body = [t.visit(s) for s in node.body]
            # bases/decorators are part of parent skeleton
        return "\n".join(_strip_dump(b) for b in body)


def ast_changes(old_src, new_src):
    """Return a list of changes between two python sources:
    ("same",) | ("syntax",) | ("func", (s, e), cls) | ("lambda", (s, e), cls)
    | ("class", clspath, (s, e)) | ("module",)"""
    try:
        old = ast.parse(old_src)
    except SyntaxError:
        return [("module",)]
    try:
        new = ast.parse(new_src)
    except (SyntaxError, ValueError):
        return [("syntax",)]
    if _strip_dump(old) == _strip_dump(new):
        return [("same",)]
    uo = _Units(old).units
    un = _Units(new).units
    out = []
    for k, (fp, rng, kind, cls) in uo.items():
        other = un.get(k)
        if other is not None and other[0] == fp:
            continue
        if other is None:
            # unit vanished: its owner changed shape, which shows as a skeleton diff
            pass
        if kind in ("func", "lambda"):
            out.append((kind, rng, cls))
        elif kind == "class":
            out.append(("class", cls, rng))
        else:
            out.append(("module",))
    for k, (fp, rng, kind, cls) in un.items():
        if k not in uo:
            # new unit: owner skeleton changed already when it's a def; lambdas
            # appear via skeleton too
            if kind == "class":
                out.append(("class", cls, None))
    return out


# ---------------------------------------------------------------- attribute-level analysis

_META_FUNCS = {"__new__", "__init_subclass__", "__set_name__", "__class_getitem__", "__prepare__"}


def _assign_names(stmt):
    """Names bound by a simple class/module-level assignment, else None."""
    if isinstance(stmt, ast.Assign):
        out = []
        for t in stmt.targets:
            if isinstance(t, ast.Name):
                out.append(t.id)
            elif isinstance(t, (ast.Tuple, ast.List)) and all(isinstance(e, ast.Name) for e in t.elts):
                out.extend(e.id for e in t.elts)
            else:
                return None
        return out
    if isinstance(stmt, (ast.AnnAssign, ast.AugAssign)) and isinstance(stmt.target, ast.Name):
        return [stmt.target.id]
    return None


def body_attr_changes(old_body, new_body):
    """Compare two class/module bodies, ignoring function and class defs
    (those are their own units). Returns the set of names whose binding
    changed, or None when the change is not a plain assignment change."""

    def split(body):
        assigns = {}
        other = []
        for i, s in enumerate(body):
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                hdr = [_strip_dump(d) for d in s.decorator_list] + [_strip_dump(s.args)]
                if s.returns is not None:
                    hdr.append(_strip_dump(s.returns))
                other.append(("def", s.name, tuple(hdr)))
                continue
            if isinstance(s, ast.ClassDef):
                hdr = [_strip_dump(d) for d in s.decorator_list + s.bases + s.keywords]
                other.append(("class", s.name, tuple(hdr)))
                continue
            if i == 0 and isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), ast.Constant) and isinstance(s.value.value, str):
                assigns.setdefault("__doc__", []).append(_strip_dump(s))
                continue
            names = _assign_names(s)
            if names is None:
                other.append(_strip_dump(s))
                continue
            d = _strip_dump(s)
            for n in names:
                assigns.setdefault(n, []).append(d)
        return assigns, other

    oa, oo = split(old_body)
    na, no = split(new_body)
    if oo != no:
        return None
    changed = set()
    for n in set(oa) | set(na):
        if oa.get(n) != na.get(n):
            changed.add(n)
    return changed


def find_class(tree, clspath):
    body = tree.body
    node = None
    for name in clspath:
        node = None
        for s in body:
            if isinstance(s, ast.ClassDef) and s.name == name:
                node = s
                break
        if node is None:
            return None
        body = node.body
    return node


class RefIndex:
    """Where every identifier is read, across sqlglot and its tests."""

    def __init__(self, root, dirs=("sqlglot", "tests")):
        self.runtime = {}  # name -> set((file, line)) read inside functions/lambdas
        self.importtime = []  # (owner, targets, refs) for module/class level statements
        self.meta = set()  # names read by metaclass / __init_subclass__ code
        self.strings = set()  # identifier-like string constants
        for d in dirs:
            for dp, dn, fn in os.walk(os.path.join(root, d)):
                dn[:] = [x for x in dn if x != "fixtures" and x != "__pycache__"]
                for f in fn:
                    if f.endswith(".py"):
                        full = os.path.join(dp, f)
                        rel = os.path.relpath(full, root)
                        try:
                            tree = ast.parse(open(full, encoding="utf-8").read())
                        except Exception:
                            continue
                        self._scan(tree, rel)

    def _scan(self, tree, rel):
        runtime = self.runtime
        meta = self.meta
        strings = self.strings

        def refs_of(node, inside_fn, is_meta):
            for n in ast.walk(node):
                name = None
                if isinstance(n, ast.Name):
                    name = n.id
                elif isinstance(n, ast.Attribute):
                    name = n.attr
                elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
                    strings.add(n.value)
                if name is not None and inside_fn:
                    runtime.setdefault(name, set()).add((rel, n.lineno))
                    if is_meta:
                        meta.add(name)

        def visit_fn(fn, is_meta):
            if is_meta:
                self._meta_fn(fn, rel)
                return
            for s in fn.body:
                refs_of(s, True, False)

        def visit_body(body, owner, cls_is_meta):
            for s in body:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit_fn(s, cls_is_meta or s.name in _META_FUNCS)
                    # defaults/decorators are import time
                    for e in list(s.decorator_list) + list(s.args.defaults) + [x for x in s.args.kw_defaults if x]:
                        self._imp(e, owner, [])
                elif isinstance(s, ast.ClassDef):
                    is_meta = any(isinstance(b, ast.Name) and b.id == "type" for b in s.bases)
                    visit_body(s.body, owner + (s.name,), is_meta)
                else:
                    names = _assign_names(s) or []
                    self._imp(s, owner, names)

        self._file = rel
        visit_body(tree.body, (), False)

    def _meta_fn(self, fn, rel):
        """Model what metaclass / __init_subclass__ code derives from class
        attributes: each assignment's targets depend on the names it reads
        (and on the conditions it sits under). Anything else it reads is
        marked as meta, which turns refinement off for that name."""
        local = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        if fn.args.vararg:
            local.add(fn.args.vararg.arg)
        if fn.args.kwarg:
            local.add(fn.args.kwarg.arg)
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                local.add(n.id)
            elif isinstance(n, ast.arg):
                local.add(n.arg)
        tag = "<l:%s:%d>" % (rel, fn.lineno)

        def m(name):
            return tag + name if name in local else name

        def reads(node):
            out = set()
            for n in ast.walk(node):
                if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Store):
                    out.add(m(n.id))
                elif isinstance(n, ast.Attribute) and not isinstance(n.ctx, ast.Store):
                    out.add(n.attr)
                elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
                    self.strings.add(n.value)
            return out

        def targets_of(t):
            out = set()
            extra = set()
            if isinstance(t, ast.Name):
                out.add(m(t.id))
            elif isinstance(t, ast.Attribute):
                out.add(t.attr)
                extra |= reads(t.value)
            elif isinstance(t, ast.Subscript):
                o, e = targets_of(t.value)
                out |= o
                extra |= e | reads(t.slice)
            elif isinstance(t, (ast.Tuple, ast.List)):
                for e in t.elts:
                    o, x = targets_of(e)
                    out |= o
                    extra |= x
            elif isinstance(t, ast.Starred):
                return targets_of(t.value)
            return out, extra

        owner = (rel, "<meta>")

        def stmts(body, ctx):
            for st in body:
                if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                    outs, extra = set(), set()
                    for t in tg:
                        o, e = targets_of(t)
                        outs |= o
                        extra |= e
                    r = (reads(st.value) if st.value is not None else set()) | extra | ctx
                    if isinstance(st, ast.AugAssign):
                        r |= outs
                    self.importtime.append((owner, tuple(outs), frozenset(r)))
                elif isinstance(st, (ast.If, ast.While)):
                    c2 = ctx | reads(st.test)
                    stmts(st.body, c2)
                    stmts(st.orelse, c2)
                elif isinstance(st, (ast.For, ast.AsyncFor)):
                    r = reads(st.iter) | ctx
                    o, e = targets_of(st.target)
                    self.importtime.append((owner, tuple(o), frozenset(r | e)))
                    stmts(st.body, ctx | r)
                    stmts(st.orelse, ctx | r)
                elif isinstance(st, (ast.With, ast.AsyncWith)):
                    r = set()
                    for it in st.items:
                        r |= reads(it.context_expr)
                    stmts(st.body, ctx | r)
                elif isinstance(st, ast.Try):
                    stmts(st.body, ctx)
                    for h in st.handlers:
                        stmts(h.body, ctx)
                    stmts(st.orelse, ctx)
                    stmts(st.finalbody, ctx)
                elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
                    call = st.value
                    f = call.func
                    r = set()
                    for a in call.args:
                        r |= reads(a)
                    for k in call.keywords:
                        r |= reads(k.value)
                    if isinstance(f, ast.Name) and f.id == "setattr" and len(call.args) >= 2 \
                            and isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str):
                        self.importtime.append((owner, (call.args[1].value,), frozenset(r | ctx)))
                    elif isinstance(f, ast.Attribute):
                        # x.Y.update(...), x.Y.append(...): Y is mutated
                        o, e = targets_of(f.value)
                        self.importtime.append((owner, tuple(o), frozenset(r | e | ctx | reads(f.value))))
                    else:
                        self.meta.update(r | ctx)
                elif isinstance(st, (ast.Return, ast.Pass, ast.Expr)):
                    if isinstance(st, ast.Expr):
                        self.meta.update(reads(st.value))
                    continue
                elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    self.meta.update(reads(st))
                else:
                    self.meta.update(reads(st) | ctx)

        stmts(fn.body, set())

    def _imp(self, stmt, owner, targets):
        refs = set()
        rel = self._file
        stack = [stmt]
        while stack:
            n = stack.pop()
            if isinstance(n, ast.Lambda):
                # lambda bodies run later, when called
                for m in ast.walk(n.body):
                    nm = m.id if isinstance(m, ast.Name) else (m.attr if isinstance(m, ast.Attribute) else None)
                    if nm is not None:
                        self.runtime.setdefault(nm, set()).add((rel, m.lineno))
                continue
            if isinstance(n, ast.Name):
                if not isinstance(n.ctx, ast.Store):
                    refs.add(n.id)
            elif isinstance(n, ast.Attribute):
                if not isinstance(n.ctx, ast.Store):
                    refs.add(n.attr)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
                self.strings.add(n.value)
            stack.extend(ast.iter_child_nodes(n))
        if targets or refs:
            self.importtime.append(((rel,) + owner, tuple(targets), frozenset(refs)))

    def closure(self, names, relevant=None):
        """Names whose import-time value depends on any of names, with their
        owners. relevant(owner, refs) can drop statements that read a name
        from an unrelated class."""
        names = set(names)
        first = set(names)
        owners = set()
        changed = True
        while changed:
            changed = False
            for owner, targets, refs in self.importtime:
                hit = refs & names
                if hit:
                    if relevant is not None and hit <= first and not relevant(owner, refs):
                        continue
                    owners.add(owner)
                    for t in targets:
                        if t not in names:
                            names.add(t)
                            changed = True
        return names, owners


# ---------------------------------------------------------------- statement-level ranges


def touched_items(fd):
    """[("mod", old_line, old_text, new_texts) | ("ins", a, b, new_texts)]
    Removed lines are "mod"; additions with no removal are "ins" between old
    lines a and b."""
    items = []
    for os_, ol, ns, nl, body in fd.hunks:
        o = os_ if ol > 0 else os_ + 1
        last = os_ - 1 if ol > 0 else os_
        pending_add = []
        removed_run = []
        def flush():
            nonlocal pending_add, removed_run
            if removed_run:
                for ln, txt in removed_run:
                    items.append(("mod", ln, txt, list(pending_add)))
            elif pending_add:
                items.append(("ins", last, o, list(pending_add)))
            pending_add = []
            removed_run = []
        for b in body:
            if b.startswith("\\"):
                continue
            tag, txt = b[0], b[1:]
            if tag == " ":
                flush()
                last = o
                o += 1
            elif tag == "-":
                if pending_add and not removed_run:
                    # additions before removals in the same run
                    pass
                removed_run.append((o, txt))
                o += 1
            elif tag == "+":
                pending_add.append(txt)
        flush()
    return items


def _is_trivial(txt):
    s = txt.strip()
    return s == "" or s.startswith("#")


class StmtMap:
    """For each line inside a function: the line range of the innermost
    statement (or compound-statement header) that holds it."""

    def __init__(self, tree):
        self.ranges = []  # (start, end, kind) kind: "stmt" | "header" | "defhead"
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                self.ranges.append((start, node.body[0].lineno - 1 if node.body[0].lineno > node.lineno else node.lineno, "defhead", (node.body[0].lineno, node.end_lineno)))
                self._body(node.body)

    def _body(self, stmts):
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # handled by the walk (defs) / import-time (classes)
            subs = []
            for attr in ("body", "orelse", "finalbody"):
                v = getattr(s, attr, None)
                if isinstance(v, list) and v and isinstance(v[0], ast.stmt):
                    subs.append(v)
            handlers = getattr(s, "handlers", None) or []
            cases = getattr(s, "cases", None) or []
            if subs or handlers or cases:
                first = min(x[0].lineno for x in subs) if subs else s.end_lineno
                self.ranges.append((s.lineno, max(s.lineno, first - 1), "header", None))
                for v in subs:
                    self._body(v)
                for h in handlers:
                    self.ranges.append((h.lineno, max(h.lineno, h.body[0].lineno - 1), "header", None))
                    self._body(h.body)
                for c in cases:
                    self.ranges.append((c.pattern.lineno, max(c.pattern.lineno, c.body[0].lineno - 1), "header", None))
                    self._body(c.body)
                # orelse/finally keyword lines: attach to the header
            else:
                self.ranges.append((s.lineno, s.end_lineno, "stmt", None))

    def at(self, line):
        best = None
        for r in self.ranges:
            if r[0] <= line <= r[1]:
                if best is None or (r[1] - r[0]) < (best[1] - best[0]):
                    best = r
        return best

    def nearest(self, line, direction, lo, hi):
        ln = line
        while lo <= ln <= hi:
            r = self.at(ln)
            if r is not None:
                return r
            ln += direction
        return None
