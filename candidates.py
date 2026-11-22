"""Candidate selection from the map. Used by the selector at run time."""

import json
import os

from brlib import RefIndex, Scopes, body_attr_changes, changed_old_lines, find_class, import_graph, module_name, parse_patch


class Index:
    """Built once from the map (build time), pickled, loaded at run time.
    Bit i stands for self.ids[i]."""

    def __init__(self, mapdata, root):
        self.root = root
        ids = sorted(k for k in mapdata if not k.startswith("<"))
        self.ids = ids
        id2num = {t: i for i, t in enumerate(ids)}
        self.n = len(ids)
        self.all = (1 << self.n) - 1
        self.file_tests = {}
        for t, i in id2num.items():
            f = t.split("::")[0]
            self.file_tests[f] = self.file_tests.get(f, 0) | (1 << i)
        class_tests = {}
        for t, i in id2num.items():
            owner = t.rsplit("::", 1)[0]
            class_tests[owner] = class_tests.get(owner, 0) | (1 << i)
        self.lines = {}  # file -> {line: bits}
        self.uses = {}
        self.opens = {}
        self.import_exec = {f: set(ls) for f, ls in mapdata.get("<import>", {}).get("lines", {}).items()}
        self.import_uses = set(mapdata.get("<import>", {}).get("uses", []))
        for key, rec in mapdata.items():
            if key == "<import>":
                continue
            if key.startswith("<collect>:"):
                bits = self.file_tests.get(key[len("<collect>:"):], 0)
            elif key.startswith("<setup>:"):
                owner = key[len("<setup>:"):].rsplit("::", 1)[0]
                bits = class_tests.get(owner, 0)
            else:
                bits = 1 << id2num[key]
            if not bits:
                continue
            for f, ls in rec["lines"].items():
                d = self.lines.setdefault(f, {})
                for ln in ls:
                    d[ln] = d.get(ln, 0) | bits
            for m in rec["uses"]:
                self.uses[m] = self.uses.get(m, 0) | bits
            for f in rec["opens"]:
                self.opens[f] = self.opens.get(f, 0) | bits
        self.unmapped = 0
        self.cpu = {}
        self.class_setup = {}
        self.graph = import_graph(root)
        self.rev = {}
        for m, ds in self.graph.items():
            for d in ds:
                self.rev.setdefault(d, set()).add(m)
        self._scopes = {}
        self.refs = RefIndex(root)
        # precompute scopes for every python file so run time needs no ast walk
        for f in list(self.lines):
            if f.endswith(".py"):
                self.scopes(f)

    def scopes(self, path):
        s = self._scopes.get(path)
        if s is None:
            try:
                src = open(os.path.join(self.root, path), encoding="utf-8").read()
            except Exception:
                src = ""
            s = self._scopes[path] = Scopes(src)
        return s

    def range_bits(self, path, a, b):
        d = self.lines.get(path)
        if not d:
            return 0
        bits = 0
        if b - a < len(d):
            for ln in range(a, b + 1):
                v = d.get(ln)
                if v:
                    bits |= v
        else:
            for ln, v in d.items():
                if a <= ln <= b:
                    bits |= v
        return bits

    def range_runtime_bits(self, path, a, b):
        sc = self.scopes(path)
        bits = 0
        for ln, v in self.lines.get(path, {}).items():
            if a <= ln <= b and ln not in sc.import_lines:
                bits |= v
        return bits

    def runtime_bits(self, path):
        """Tests that ran any function body line in path."""
        sc = self.scopes(path)
        d = self.lines.get(path)
        if not d:
            return 0
        bits = 0
        for ln, v in d.items():
            if ln not in sc.import_lines:
                bits |= v
        return bits

    def importtime_bits(self, path):
        mod = module_name(path)
        bits = self.uses.get(mod, 0) | self.runtime_bits(path)
        for imp in self.rev.get(mod, ()):
            bits |= self.uses.get(imp, 0)
            ip = imp.replace(".", "/")
            ipath = ip + ".py" if os.path.exists(os.path.join(self.root, ip + ".py")) else ip + "/__init__.py"
            bits |= self.runtime_bits(ipath)
        return bits

    def class_bits(self, path, clspath):
        mod = module_name(path)
        q = mod + ":" + ".".join(clspath)
        return self.uses.get(q, 0)

    def module_uses(self, mod):
        b = 0
        pre = mod + ":"
        for k, v in self.uses.items():
            if k.startswith(pre):
                b |= v
        return b

    def importtime_bits(self, path):
        mod = module_name(path)
        bits = self.module_uses(mod) | self.runtime_bits(path)
        for imp in self.rev.get(mod, ()):
            bits |= self.module_uses(imp)
        return bits

    def any_line_bits(self, path):
        b = 0
        for v in self.lines.get(path, {}).values():
            b |= v
        return b

    def stmtmap(self, path):
        sm = getattr(self, "_stmtmaps", None)
        if sm is None:
            sm = self._stmtmaps = {}
        m = sm.get(path)
        if m is None:
            import ast
            from brlib import StmtMap

            try:
                m = StmtMap(ast.parse(open(os.path.join(self.root, path), encoding="utf-8").read()))
            except Exception:
                m = False
            sm[path] = m
        return m

    def func_stmt_bits(self, path, fd, bs, end, rngs=None):
        """Tests that ran the statements a patch touches inside one function."""
        from brlib import _is_trivial, touched_items

        if rngs is None:
            rngs = []
        sm = self.stmtmap(path)
        if not sm:
            rngs.append((bs, end))
            return self.range_bits(path, bs, end)
        bits = 0

        def rb(a, b):
            rngs.append((a, b))
            return self.range_bits(path, a, b)

        for it in touched_items(fd):
            if it[0] == "mod":
                ln, old_txt, new_txts = it[1], it[2], it[3]
                if not (bs <= ln <= end):
                    continue
                if _is_trivial(old_txt) and all(_is_trivial(x) for x in new_txts):
                    continue
                r = sm.at(ln)
                if r is None:
                    bits |= rb(bs, end)
                elif r[2] == "defhead":
                    bits |= rb(r[3][0], r[3][1])
                else:
                    bits |= rb(r[0], r[1])
            else:
                a, b, txts = it[1], it[2], it[3]
                if not (bs - 1 <= a <= end or bs <= b <= end + 1):
                    continue
                if all(_is_trivial(x) for x in txts):
                    continue
                r_a = sm.nearest(a, -1, bs, end)
                r_b = sm.nearest(b, 1, bs, end)
                if r_a is None and r_b is None:
                    bits |= rb(bs, end)
                for r in (r_a, r_b):
                    if r is None:
                        continue
                    if r[2] == "defhead":
                        bits |= rb(r[3][0], r[3][1])
                    else:
                        bits |= rb(r[0], r[1])
        return bits

    def refs_bits(self, names):
        bits = 0
        for n in names:
            for f, ln in self.refs.runtime.get(n, ()):
                v = self.lines.get(f, {}).get(ln)
                if v:
                    bits |= v
        return bits

    def attr_refine(self, path, old_src, new_src, clspath):
        """Candidates for a change to plain assignments in a class (clspath) or
        module (clspath None) body: tests that both use the owner and run code
        that reads one of the changed names. None when that can't be decided."""
        import ast

        try:
            ot, nt = ast.parse(old_src), ast.parse(new_src)
        except SyntaxError:
            return None
        if clspath:
            oc, nc = find_class(ot, clspath), find_class(nt, clspath)
            if oc is None or nc is None:
                return None
            names = body_attr_changes(oc.body, nc.body)
        else:
            names = body_attr_changes(ot.body, nt.body)
        if names is None:
            return None
        if not names:
            return 0
        cq = module_name(path) + ":" + ".".join(clspath) if clspath else None
        subs = getattr(self, "subclasses", None)
        rel_filter = None
        if cq is not None and subs is not None and cq in subs:
            fam = subs[cq]
            fam_names = {x.split(":")[1].split(".")[-1] for x in fam}

            def rel_filter(owner, refs):
                # does this import-time statement read the attribute from
                # this class (or a subclass)?
                if owner[1:] == ("<meta>",):
                    return True
                if len(owner) > 1 and module_name(owner[0]) + ":" + ".".join(owner[1:]) in fam:
                    return True
                return bool(refs & fam_names)

        allnames, owners = self.refs.closure(names, rel_filter)
        for n in allnames:
            if n in self.refs.meta or n in self.refs.strings:
                return None
            if n.startswith("__") and n != "__doc__":
                return None
        refb = self.refs_bits(allnames)
        if clspath is None:
            return refb
        own = self.class_bits(path, clspath)
        for o in owners:
            f, cp = o[0], o[1:]
            if not cp:
                return refb  # a module-level value depends on it: no owner filter
            if cp == ("<meta>",):
                # derived by a metaclass: onto the class itself (covered by
                # its own users) or onto the dialect that has this class as
                # its tokenizer / parser / generator
                q = module_name(path) + ":" + ".".join(clspath)
                comp = getattr(self, "component_owners", None)
                if comp is None:
                    mod = module_name(path)
                    own |= self.module_uses(mod)
                    for imp in self.rev.get(mod, ()):
                        own |= self.module_uses(imp)
                else:
                    for d in comp.get(q, ()):
                        own |= self.uses.get(d, 0)
                continue
            own |= self.class_bits(f, cp)
        return refb & own

    def py_changes(self, path, fd):
        from brlib import apply_hunks, ast_changes

        try:
            old = open(os.path.join(self.root, path), encoding="utf-8").read()
            new = apply_hunks(old, fd)
        except Exception:
            return [("module",)], None
        ch = ast_changes(old, new)
        # an encoding cookie in the first two lines changes how bytes decode
        for h in fd.hunks:
            if h[0] <= 2 and any("coding" in b for b in h[4] if b[:1] in "+-"):
                ch.append(("module",))
        return ch, new

    def candidates(self, fds):
        """Returns (bits, notes, flags)."""
        bits = self.unmapped
        notes = []
        flags = set()
        for fd in fds:
            path = fd.old or fd.new
            bits |= self.opens.get(path, 0)
            if fd.old is None or fd.new is None or fd.old != fd.new or fd.binary:
                notes.append("addrm:" + path)
                flags.add("addrm")
                for pth in {fd.old, fd.new} - {None}:
                    # tests that list the directory see the file come or go
                    bits |= self.opens.get("<dir>:" + os.path.dirname(pth), 0)
                    bits |= self.opens.get(pth, 0)
                if fd.old is not None and fd.old.endswith(".py"):
                    if fd.old.startswith("tests/"):
                        bits |= self.file_tests.get(fd.old, 0)
                    else:
                        bits |= self.any_line_bits(fd.old) | self.importtime_bits(fd.old)
                    flags.add("importlevel")
                    flags.add("deepcheck")
                if fd.new is not None and fd.new.endswith(".py"):
                    flags.add("importlevel")
                    flags.add("deepcheck")
                    if fd.new.startswith("tests/") and fd.new in self.file_tests:
                        bits |= self.file_tests[fd.new]
                if fd.old is not None and fd.new is not None and fd.old == fd.new and fd.binary:
                    bits |= self.opens.get(fd.old, 0)
                continue
            if path.endswith(".py") and (path.startswith("sqlglot/") or path.startswith("tests/")):
                changes, new_src = self.py_changes(path, fd)
                try:
                    old_src = open(os.path.join(self.root, path), encoding="utf-8").read()
                except Exception:
                    old_src = None
                for c in changes:
                    kind = c[0]
                    if kind == "same":
                        notes.append("same:" + path)
                    elif kind in ("func", "lambda"):
                        rngs = []
                        if kind == "func":
                            bits |= self.func_stmt_bits(path, fd, c[1][0], c[1][1], rngs)
                        else:
                            bits |= self.range_bits(path, c[1][0], c[1][1])
                        ie = self.import_exec.get(path, ())
                        if any(a <= ln <= b for ln in ie for a, b in rngs):
                            # this code also runs while modules import
                            notes.append("import-exec:%s:%d" % (path, c[1][0]))
                            flags.add("importlevel")
                            flags.add("deepcheck")
                            bits |= self.importtime_bits(path)
                        notes.append("%s:%s:%d-%d" % (kind, path, c[1][0], c[1][1]))
                    elif kind == "class":
                        flags.add("importlevel")
                        if module_name(path) + ":" + ".".join(c[1]) in self.import_uses:
                            # instances of this class are built while modules import
                            flags.add("deepcheck")
                        notes.append("class:%s:%s" % (path, ".".join(c[1])))
                        if path.startswith("tests/"):
                            bits |= self.file_tests.get(path, 0)
                            continue
                        if c[2] is not None and new_src is not None and self.class_bits(path, c[1]):
                            r = self.attr_refine(path, old_src, new_src, c[1])
                            if r is not None:
                                notes.append("attr-refined")
                                bits |= r
                                continue
                        cb = self.class_bits(path, c[1])
                        if c[2] is not None:
                            cb |= self.range_runtime_bits(path, c[2][0], c[2][1])
                        if not self.class_bits(path, c[1]):
                            cb |= self.importtime_bits(path)
                            notes.append("untracked-class")
                        bits |= cb
                    else:  # module or syntax
                        flags.add("importlevel")
                        flags.add("deepcheck")
                        notes.append("%s:%s" % (kind, path))
                        flags.add("importcheck")
                        if path.startswith("tests/"):
                            bits |= self.file_tests.get(path, 0)
                            if path in ("tests/helpers.py", "tests/__init__.py"):
                                bits |= self.all
                            continue
                        if kind == "module" and new_src is not None:
                            r = self.attr_refine(path, old_src, new_src, None)
                            if r is not None:
                                notes.append("attr-refined")
                                bits |= r
                                continue
                        bits |= self.importtime_bits(path)
                        if kind == "syntax":
                            flags.add("syntax")
            else:
                notes.append("data:" + path)
        return bits, notes, flags


def load_components(idx, root):
    """dialect class -> the tokenizer/parser/generator classes it is built
    from (and their bases), inverted: component qualname -> dialect qualnames."""
    import sys

    sys.path.insert(0, root)
    try:
        import importlib
        import pkgutil

        import sqlglot.dialects as dpkg
        from sqlglot.dialects.dialect import Dialect

        for info in pkgutil.iter_modules(dpkg.__path__):
            try:
                importlib.import_module("sqlglot.dialects." + info.name)
            except Exception:
                pass
        comp = {}

        def q(c):
            return "%s:%s" % (c.__module__, c.__qualname__)

        seen = set()
        stack = [Dialect]
        while stack:
            d = stack.pop()
            for sub in d.__subclasses__():
                if sub not in seen:
                    seen.add(sub)
                    stack.append(sub)
        for d in seen | {Dialect}:
            dq = [q(d)]
            for attr in dir(d):
                if not attr.endswith("_class"):
                    continue
                try:
                    k = getattr(d, attr)
                except Exception:
                    continue
                if not isinstance(k, type):
                    continue
                for base in k.__mro__:
                    if base.__module__.startswith("sqlglot"):
                        comp.setdefault(q(base), set()).update(dq)
        idx.component_owners = comp
        # every sqlglot class -> qualnames of itself and all its subclasses
        subs = {}
        for name, mod in list(sys.modules.items()):
            if not name.startswith("sqlglot") or mod is None:
                continue
            stack = [v for v in vars(mod).values() if isinstance(v, type)]
            while stack:
                k = stack.pop()
                if not getattr(k, "__module__", "").startswith("sqlglot"):
                    continue
                kq = q(k)
                if kq in subs and kq in subs.get(kq, ()):
                    continue
                for b in k.__mro__:
                    if getattr(b, "__module__", "").startswith("sqlglot"):
                        subs.setdefault(q(b), set()).add(kq)
                stack.extend(v for v in vars(k).values() if isinstance(v, type))
        idx.subclasses = subs
    finally:
        sys.path.remove(root)


def load_cpu(idx, path):
    """Per-test run cost (ms) as measured at build time, and the one-off cost
    of each test class's setUpClass."""
    import json
    import statistics

    try:
        with open(path) as f:
            cpu = json.load(f)
    except Exception:
        return
    by_owner = {}
    for tid, (su, ca, td) in cpu.items():
        by_owner.setdefault(tid.rsplit("::", 1)[0], []).append((tid, su, ca, td))
    for owner, recs in by_owner.items():
        sus = [r[1] for r in recs]
        med = statistics.median(sus)
        idx.class_setup[owner] = max(0, max(sus) - med)
        for tid, su, ca, td in recs:
            idx.cpu[tid] = med + ca + td


def bits_to_list(bits):
    out = []
    i = 0
    while bits:
        if bits & 1:
            out.append(i)
        bits >>= 1
        i += 1
    return out
