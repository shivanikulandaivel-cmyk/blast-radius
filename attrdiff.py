"""Fingerprints of what sqlglot builds while it imports.

For every sqlglot class: each attribute in its own __dict__; for every
sqlglot module: each global that is not a class or a module. Values are
reduced to a short hash that is stable across processes (sets are sorted,
functions are compared by their code, defaults and closure cells, objects
without a useful repr by their type and __dict__).

Comparing the fingerprints of the original and the patched code says which
class attributes and module globals a change really altered, including the
ones derived by metaclasses and __init_subclass__.
"""

import hashlib
import importlib
import os
import pkgutil
import sys
import types

_MAXD = 6


def _fp(v, depth=0, seen=None):
    if depth > _MAXD:
        return "<deep>"
    if seen is None:
        seen = set()
    if v is None or isinstance(v, (bool, int, float, complex, str, bytes)):
        return repr(v)
    vid = id(v)
    if vid in seen:
        return "<cycle>"
    if isinstance(v, type):
        return "class:%s.%s" % (getattr(v, "__module__", "?"), getattr(v, "__qualname__", "?"))
    if isinstance(v, types.ModuleType):
        return "module:" + v.__name__
    seen = seen | {vid}
    if isinstance(v, (list, tuple)):
        parts = [_fp(x, depth + 1, seen) for x in v]
        if parts and all(isinstance(x, type) for x in v):
            parts.sort()  # tuple(set_of_classes) has no stable order
        return "%s[%s]" % (type(v).__name__, ",".join(parts))
    if isinstance(v, (set, frozenset)):
        return "%s{%s}" % (type(v).__name__, ",".join(sorted(_fp(x, depth + 1, seen) for x in v)))
    if isinstance(v, dict):
        # sorted: dicts built by iterating sets of classes have no stable order
        items = sorted((_fp(k, depth + 1, seen), _fp(x, depth + 1, seen)) for k, x in v.items())
        return "dict{%s}" % ",".join("%s:%s" % kv for kv in items)
    if isinstance(v, (staticmethod, classmethod)):
        return type(v).__name__ + "(" + _fp(v.__func__, depth + 1, seen) + ")"
    if isinstance(v, property):
        return "property(%s,%s,%s)" % tuple(_fp(f, depth + 1, seen) for f in (v.fget, v.fset, v.fdel))
    if isinstance(v, types.FunctionType):
        return "fn(%s|%s|%s|%s)" % (
            _code(v.__code__, depth + 1, seen),
            _fp(v.__defaults__, depth + 1, seen),
            _fp(v.__kwdefaults__, depth + 1, seen),
            ",".join(_fp(_cell(c), depth + 1, seen) for c in (v.__closure__ or ())),
        )
    if isinstance(v, types.MethodType):
        return "method(" + _fp(v.__func__, depth + 1, seen) + ")"
    if isinstance(v, (types.BuiltinFunctionType, types.BuiltinMethodType)):
        return "builtin:" + getattr(v, "__qualname__", repr(v))
    if isinstance(v, types.CodeType):
        return _code(v, depth + 1, seen)
    cls = type(v)
    r = None
    if cls.__repr__ is not object.__repr__:
        try:
            r = repr(v)
        except Exception:
            r = None
        if r is not None and " at 0x" in r:
            r = None
    if r is not None:
        return "%s:%s" % (cls.__qualname__, r)
    d = getattr(v, "__dict__", None)
    if isinstance(d, dict):
        return "obj:%s.%s%s" % (cls.__module__, cls.__qualname__, _fp(d, depth + 1, seen))
    return "obj:%s.%s" % (cls.__module__, cls.__qualname__)


def _cell(c):
    try:
        return c.cell_contents
    except ValueError:
        return "<empty>"


def _code(co, depth, seen):
    consts = ",".join(_code(c, depth + 1, seen) if isinstance(c, types.CodeType) else _fp(c, depth + 1, seen)
                      for c in co.co_consts)
    return "code(%s|%s|%s|%s)" % (co.co_code.hex(), consts, co.co_names, co.co_varnames)


ROOT = None


def _h(s, v=None):
    if ROOT:
        s = s.replace(ROOT, "<root>")
    tag = "f" if isinstance(v, (types.FunctionType, staticmethod, classmethod, property, types.MethodType)) else "v"
    return tag + hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()


def import_all(pkg="sqlglot"):
    mod = importlib.import_module(pkg)
    errors = []
    for info in pkgutil.walk_packages(mod.__path__, pkg + "."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except BaseException as e:  # noqa: BLE001
            errors.append([info.name, repr(e)[:200]])
    return errors


def fingerprints(pkg="sqlglot"):
    """{"C:<module>:<qualname>": {attr: hash}, "M:<module>": {name: hash}}"""
    out = {}
    seen_cls = set()
    for name, m in sorted(sys.modules.items()):
        if m is None or not (name == pkg or name.startswith(pkg + ".")):
            continue
        g = {}
        stack = []
        for k, v in list(vars(m).items()):
            if isinstance(v, type):
                if getattr(v, "__module__", None) == name:
                    stack.append(v)
                continue
            if isinstance(v, types.ModuleType) or k.startswith("__"):
                continue
            try:
                g[k] = _h(_fp(v), v)
            except Exception:
                g[k] = "err"
        out["M:" + name] = g
        while stack:
            c = stack.pop()
            if id(c) in seen_cls:
                continue
            seen_cls.add(id(c))
            a = {}
            for k, v in list(vars(c).items()):
                if k in ("__dict__", "__weakref__", "__module__", "__qualname__", "__doc__"):
                    continue
                if isinstance(v, type):
                    if getattr(v, "__module__", None) == name:
                        stack.append(v)
                    a[k] = _h(_fp(v))
                    continue
                try:
                    a[k] = _h(_fp(v), v)
                except Exception:
                    a[k] = "err"
            # the class's bases are part of what it is
            a["<bases>"] = _h(",".join(_fp(b) for b in c.__bases__))
            out["C:%s:%s" % (name, c.__qualname__)] = a
    return out


def diff(base, new):
    """[(kind, owner, attr, is_func)] for every changed, added or removed entry."""
    changed = []
    for key in set(base) | set(new):
        b, n = base.get(key), new.get(key)
        kind, owner = key[0], key[2:]
        if b is None or n is None:
            changed.append((kind, owner, "*", False))
            continue
        for attr in set(b) | set(n):
            x, y = b.get(attr), n.get(attr)
            if x != y:
                is_func = all(v is None or v.startswith("f") for v in (x, y))
                changed.append((kind, owner, attr, is_func))
    return sorted(changed)


if __name__ == "__main__":
    # python attrdiff.py <repo root> <out.json>
    import json

    root, out = sys.argv[1], sys.argv[2]
    ROOT = os.path.abspath(root)
    sys.path.insert(0, root)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    errs = import_all()
    fp = fingerprints()
    with open(out, "w") as f:
        json.dump({"errors": errs, "fp": fp}, f, separators=(",", ":"))
    print("fingerprinted %d owners, %d import errors" % (len(fp), len(errs)), file=sys.stderr)
