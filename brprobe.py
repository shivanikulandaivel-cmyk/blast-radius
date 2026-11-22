"""Equivalence probe.

For a patch that only changes expressions inside function bodies, rewrite
the patched source so that every changed expression is evaluated both the
new way and the old way, and records whenever the two disagree:

    NEW_EXPR   ->   _br_probe_cmp(ID, MODE, lambda: (NEW_EXPR), lambda: (OLD_EXPR))

The old expression must be side-effect free (checked conservatively), and is
evaluated first, so the program behaves exactly like the patched one. Running
tests on the probed code tells us, besides pass/fail, whether the change ever
made a difference at all.
"""

import ast
import builtins

_PURE_FUNCS = {
    "isinstance", "issubclass", "len", "getattr", "hasattr", "type", "bool", "str",
    "int", "float", "tuple", "frozenset", "any", "all", "min", "max", "abs", "repr",
    "seq_get", "callable", "id",
}
_PURE_METHODS = {
    "get", "startswith", "endswith", "lower", "upper", "strip", "lstrip", "rstrip",
    "isdigit", "isalpha", "isalnum", "isspace", "isupper", "islower", "is_type",
    "is_star", "is_string", "is_number", "is_int", "text", "find", "keys", "values",
    "items", "count", "index", "split", "join", "replace", "format", "casefold",
}
_BANNED = (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await)


def _dump(n):
    return ast.dump(n, annotate_fields=False, include_attributes=False)


def _pure(node):
    for n in ast.walk(node):
        if isinstance(n, _BANNED):
            return False
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                if f.id not in _PURE_FUNCS:
                    return False
            elif isinstance(f, ast.Attribute):
                if f.attr not in _PURE_METHODS:
                    return False
            else:
                return False
    return True


def _lambda_safe(node):
    for n in ast.walk(node):
        if isinstance(n, _BANNED):
            return False
        if isinstance(n, ast.Name) and n.id in ("super", "__class__"):
            return False
        if isinstance(n, (ast.Starred,)):
            return False
    return True


ESC = object()


class _Fail(Exception):
    pass


def _diff(o, n, parent, field, in_func, sites):
    """Collect differing expression sites; ESC asks the caller to treat its
    own node as the site."""
    if _dump(o) == _dump(n):
        return None
    if type(o) is not type(n):
        return ESC
    esc = False
    now_in_func = in_func or isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
    if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # header (name, args, decorators, returns) must be identical
        for f in ("name", "args", "decorator_list", "returns"):
            if _dump_any(getattr(o, f)) != _dump_any(getattr(n, f)):
                raise _Fail("def header changed")
    for f in o._fields:
        if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)) and f != "body":
            continue
        vo, vn = getattr(o, f, None), getattr(n, f, None)
        if isinstance(vo, list) or isinstance(vn, list):
            if not isinstance(vo, list) or not isinstance(vn, list) or len(vo) != len(vn):
                esc = True
                continue
            for a, b in zip(vo, vn):
                if isinstance(a, ast.AST) and isinstance(b, ast.AST):
                    r = _diff(a, b, o, f, now_in_func, sites)
                    if r is ESC:
                        esc = True
                elif a != b:
                    esc = True
        elif isinstance(vo, ast.AST) or isinstance(vn, ast.AST):
            if not (isinstance(vo, ast.AST) and isinstance(vn, ast.AST)):
                esc = True
                continue
            r = _diff(vo, vn, o, f, now_in_func, sites)
            if r is ESC:
                esc = True
        else:
            if vo != vn:
                esc = True
    if not esc:
        return None
    if isinstance(o, ast.expr) and not isinstance(o, (ast.Starred, ast.Slice)):
        if not in_func:
            raise _Fail("change outside a function body")
        if isinstance(getattr(o, "ctx", None), (ast.Store, ast.Del)):
            raise _Fail("assignment target changed")
        if isinstance(parent, (ast.keyword,)) or isinstance(parent, ast.arguments):
            pass
        mode = "eq"
        if field == "test" and isinstance(parent, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            mode = "truth"
        elif field == "ifs" and isinstance(parent, ast.comprehension):
            mode = "truth"
        sites.append((o, n, mode))
        return None
    return ESC


def _dump_any(v):
    if isinstance(v, list):
        return [_dump_any(x) for x in v]
    if isinstance(v, ast.AST):
        return _dump(v)
    return v


def _span(src_bytes, lines_off, node):
    a = lines_off[node.lineno - 1] + node.col_offset
    b = lines_off[node.end_lineno - 1] + node.end_col_offset
    return a, b


def _line_offsets(b):
    offs = [0]
    for i, ch in enumerate(b):
        if ch == 10:
            offs.append(i + 1)
    return offs


def plan(old_src, new_src):
    """Probe sites for a change, or None when the change can't be probed."""
    try:
        ot, nt = ast.parse(old_src), ast.parse(new_src)
    except SyntaxError:
        return None
    sites = []
    try:
        r = _diff(ot, nt, None, None, False, sites)
    except _Fail:
        return None
    except RecursionError:
        return None
    if r is ESC or not sites:
        return None
    ob, nb = old_src.encode("utf-8"), new_src.encode("utf-8")
    oo, no = _line_offsets(ob), _line_offsets(nb)
    out = []
    for o, n, mode in sites:
        if not _pure(o) or not _lambda_safe(o) or not _lambda_safe(n):
            return None
        oa, ob_ = _span(ob, oo, o)
        na, nb_ = _span(nb, no, n)
        out.append({
            "old": ob[oa:ob_].decode("utf-8"),
            "new_span": (na, nb_),
            "mode": mode,
            "line": n.lineno,
        })
    return out


def instrument(new_src, sites, base_id=0):
    """Rewrite new_src so each site is probed. Returns text or None."""
    b = new_src.encode("utf-8")
    order = sorted(range(len(sites)), key=lambda i: -sites[i]["new_span"][0])
    spans = sorted(s["new_span"] for s in sites)
    for x, y in zip(spans, spans[1:]):
        if y[0] < x[1]:
            return None  # nested sites: keep it simple
    for i in order:
        s = sites[i]
        a, e = s["new_span"]
        new_txt = b[a:e].decode("utf-8")
        rep = "_br_probe_cmp(%d, %r, (lambda: (%s\n)), (lambda: (%s\n)))" % (
            base_id + i, s["mode"], new_txt, s["old"])
        b = b[:a] + rep.encode("utf-8") + b[e:]
    text = b.decode("utf-8")
    try:
        ast.parse(text)
    except SyntaxError:
        return None
    return text


# ---------------------------------------------------------------- run time

STATS = {}  # site id -> [hits, divergences]


def _same(mode, a, b):
    if mode == "truth":
        return bool(a) == bool(b)
    if a is b:
        return True
    try:
        r = a == b
        if r is True or r is False:
            return r and type(a) is type(b)
        return bool(r) and type(a) is type(b)
    except Exception:
        return False


def br_cmp(site, mode, new_thunk, old_thunk):
    st = STATS.get(site)
    if st is None:
        st = STATS[site] = [0, 0]
    st[0] += 1
    try:
        ov = old_thunk()
        oexc = None
    except BaseException as e:  # noqa: BLE001
        ov, oexc = None, type(e)
    try:
        nv = new_thunk()
    except BaseException as e:
        if oexc is not type(e):
            st[1] += 1
        raise
    if oexc is not None:
        st[1] += 1
    else:
        try:
            if not _same(mode, nv, ov):
                st[1] += 1
        except Exception:
            st[1] += 1
    return nv


def install():
    builtins._br_probe_cmp = br_cmp
