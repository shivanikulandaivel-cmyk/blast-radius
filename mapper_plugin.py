"""pytest plugin: per-test map of what each test touches in the pinned sqlglot.

Records, per test id:
  lines   {relpath: sorted lines}  executed lines in sqlglot/ and tests/ files
  uses    [module, ...]            modules defining classes in the MRO of every
                                   Dialect/Parser/Generator/Tokenizer/Expression
                                   instance the test created
  opens   [relpath, ...]           repo files the test opened
Collection-time records go under the key "<collect>:<test file>".

Output: $MAP_OUT (json).
"""

import json
import os
import sys

ROOT = os.path.realpath(os.environ.get("MAP_ROOT", os.getcwd())) + os.sep
OUT = os.environ.get("MAP_OUT", "/tmp/map.json")
mon = sys.monitoring
TOOL = mon.COVERAGE_ID

_lines = {}
_types = set()
_opens = set()
_results = {}
_codefile = {}


def _rel(path):
    if not path.startswith(ROOT):
        try:
            path = os.path.realpath(path)
        except Exception:
            return None
        if not path.startswith(ROOT):
            return None
    return path[len(ROOT):]


def _on_line(code, line):
    fn = code.co_filename
    rel = _codefile.get(fn, 0)
    if rel == 0:
        rel = _rel(fn)
        if rel is not None and not (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            rel = None
        _codefile[fn] = rel
    if rel is None:
        return mon.DISABLE
    s = _lines.get(rel)
    if s is None:
        s = _lines[rel] = set()
    s.add(line)
    return mon.DISABLE


def _audit(event, args):
    if event in ("os.listdir", "os.scandir"):
        f = sys._getframe(1)
        while f is not None:
            if "importlib" in f.f_code.co_filename or f.f_code.co_filename.startswith("<frozen"):
                return
            f = f.f_back
        p = args[0] if args else None
        if p is None:
            p = "."
        if isinstance(p, bytes):
            p = p.decode(errors="ignore")
        if isinstance(p, (str, os.PathLike)):
            r = _rel(os.path.abspath(os.fspath(p)))
            if r is not None:
                _opens.add("<dir>:" + r.rstrip(os.sep))
        return
    if event == "open" and args and isinstance(args[0], (str, bytes)):
        p = args[0]
        if isinstance(p, bytes):
            p = p.decode(errors="ignore")
        r = _rel(os.path.abspath(p))
        if r is None or r.endswith(".pyc") or "__pycache__" in r:
            return
        if r.endswith(".py"):
            f = sys._getframe(1)
            while f is not None:
                if "importlib" in f.f_code.co_filename or f.f_code.co_filename.startswith("<frozen"):
                    return
                f = f.f_back
        _opens.add(r)


def _wrap_init(cls):
    orig = cls.__init__

    def __init__(self, *a, **k):
        _types.add(type(self))
        orig(self, *a, **k)
        d = getattr(self, "dialect", None)
        if d is not None:
            _types.add(type(d))

    cls.__init__ = __init__


_wrapped = False


def _install_wrappers():
    global _wrapped
    if _wrapped:
        return
    _wrapped = True
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.parser import Parser
    from sqlglot.generator import Generator
    from sqlglot.tokens import Tokenizer
    from sqlglot.expressions.core import Expression

    for c in (Dialect, Parser, Generator, Tokenizer, Expression):
        _wrap_init(c)
    try:
        from sqlglot.optimizer.annotate_types import TypeAnnotator

        _wrap_init(TypeAnnotator)
    except Exception:
        pass


def _reset():
    _lines.clear()
    _types.clear()
    _opens.clear()
    mon.restart_events()


def _snapshot():
    mods = set()
    for t in _types:
        for c in t.__mro__:
            m = getattr(c, "__module__", None)
            if m and m.startswith("sqlglot"):
                mods.add(m + ":" + getattr(c, "__qualname__", "?"))
    return {
        "lines": {f: sorted(s) for f, s in _lines.items()},
        "uses": sorted(mods),
        "opens": sorted(_opens),
    }


def _preimport():
    """Import every sqlglot module up front so code that runs at import time
    is recorded once, under "<import>", instead of on whichever test happened
    to trigger a lazy import."""
    import importlib
    import pkgutil

    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception:
            pass


def pytest_configure(config):
    mon.use_tool_id(TOOL, "brmap")
    mon.register_callback(TOOL, mon.events.LINE, _on_line)
    mon.set_events(TOOL, mon.events.LINE)
    sys.addaudithook(_audit)
    _reset()
    try:
        _install_wrappers()
        _preimport()
    except Exception as e:  # noqa: BLE001
        print("preimport failed", e, file=sys.stderr)
    _install_wrappers()
    # instances built while the core modules imported (before the wrappers
    # existed) are still alive: count their classes as used at import
    try:
        import gc

        from sqlglot.expressions.core import Expression

        for o in gc.get_objects():
            if isinstance(o, Expression):
                _types.add(type(o))
    except Exception:
        pass
    _results["<import>"] = _snapshot()
    _reset()


def pytest_collect_file(file_path, parent):
    return None


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_pycollect_makemodule(module_path, parent):
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    # module import happens inside this for Module collectors
    is_mod = collector.__class__.__name__ == "Module"
    if is_mod:
        _reset()
    yield
    if is_mod:
        try:
            _install_wrappers()
        except Exception:
            pass
        rel = _rel(str(collector.path))
        _results["<collect>:" + str(rel)] = _snapshot()
        _reset()


def pytest_collection_modifyitems(session, config, items):
    import mp_shim

    mp_shim.patch_modules(mp_shim.SyncExecutor)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    yield
    # class-level setup (setUpClass) runs inside the first test's setup; its
    # effects reach every test of the class, so it is recorded on its own
    _results["<setup>:" + item.nodeid] = _snapshot()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _install_wrappers()
    _reset()
    yield
    _results[item.nodeid] = _snapshot()
    _reset()


def pytest_unconfigure(config):
    mon.set_events(TOOL, 0)
    with open(OUT, "w") as f:
        json.dump(_results, f, separators=(",", ":"))
