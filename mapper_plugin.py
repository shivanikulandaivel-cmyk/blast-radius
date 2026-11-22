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
    if _SL["on"]:
        tgt = _SL["slices"].setdefault(_SL["cur"], {}) if _SL["cur"] is not None else _SL["common"]
        ss = tgt.get(rel)
        if ss is None:
            ss = tgt[rel] = set()
        ss.add(line)
    return mon.DISABLE


# ---------------------------------------------------------------- slices
# Big tests run many independent cases: fixture pairs (check_file and the
# annotate tests) and the TPC-H / TPC-DS queries. While such a test runs, the
# lines are also recorded per case, so the selector can run only the cases a
# change reaches. Lines outside any case are "common": touching those means
# the whole test runs.

_SL = {"on": False, "cur": None, "slices": {}, "common": {}, "y": 0, "k": 0, "titles": {}}
SLICE_FILES = ("tests/test_optimizer.py", "tests/test_executor.py")


_SCHEMAS = ()


def _clear_caches():
    """Caches would hide work from every case but the first that needs it."""
    try:
        g = sys.modules.get("sqlglot.generator")
        if g is not None and hasattr(g, "_DISPATCH_CACHE"):
            g._DISPATCH_CACHE.clear()
    except Exception:
        pass
    try:
        for sc in list(_SCHEMAS):
            for name, val in vars(sc).items():
                if name.endswith("_cache") and isinstance(val, dict):
                    val.clear()
    except Exception:
        pass


def _slice_begin(key):
    _SL["cur"] = key
    _clear_caches()
    mon.restart_events()


def _wrap_fixture_pairs(orig):
    def load_sql_fixture_pairs(filename):
        i = 0
        for item in orig(filename):
            i += 1
            if _SL["on"]:
                key = "y%d" % _SL["y"]
                _SL["y"] += 1
                try:
                    meta, sql = item[0], item[1]
                    _SL["titles"][meta.get("title") or f"{i}, {sql}"] = key
                except Exception:
                    pass
                _slice_begin(key)
            yield item
        if _SL["on"]:
            _slice_begin(None)

    load_sql_fixture_pairs._br_wrapped = True
    return load_sql_fixture_pairs


def _install_slicing():
    import contextlib
    import re
    import unittest
    import unittest.case

    import mp_shim

    if getattr(unittest.TestCase.subTest, "_br_wrapped", False):
        return
    orig_sub = unittest.TestCase.subTest
    sentinel = unittest.case._subtest_msg_sentinel
    tpc = re.compile(r"^tpc-(?:h|ds) (\d+)$")

    @contextlib.contextmanager
    def subTest(self, msg=sentinel, **params):
        key = None
        if _SL["on"] and isinstance(msg, str):
            m = msg[len("(execute) "):] if msg.startswith("(execute) ") else msg
            key = _SL["titles"].get(m)
            if key is None:
                mm = tpc.match(m)
                if mm:
                    key = "k%d" % (int(mm.group(1)) - 1)
        prev = _SL["cur"]
        if key is not None:
            _slice_begin(key)
        try:
            with orig_sub(self, msg, **params):
                yield
        finally:
            if key is not None:
                _slice_begin(prev)

    subTest._br_wrapped = True
    unittest.TestCase.subTest = subTest

    class SlicingExecutor(mp_shim.SyncExecutor):
        def submit(self, fn, *args, **kwargs):
            if _SL["on"] and not (_SL["cur"] or "").startswith("y"):
                key = "k%d" % _SL["k"]
                _SL["k"] += 1
                prev = _SL["cur"]
                _slice_begin(key)
                try:
                    return super().submit(fn, *args, **kwargs)
                finally:
                    _slice_begin(prev)
            return super().submit(fn, *args, **kwargs)

    mp_shim.SlicingExecutor = SlicingExecutor

    # schema lookups are cached per MappingSchema; with the cache on, only the
    # first case to need a value would run the code computing it
    try:
        import weakref

        from sqlglot.schema import MappingSchema

        orig_init = MappingSchema.__init__

        def __init__(self, *a, **k):
            orig_init(self, *a, **k)
            try:
                _SCHEMAS.add(self)
            except Exception:
                pass

        MappingSchema.__init__ = __init__
        globals()["_SCHEMAS"] = weakref.WeakSet()
    except Exception as e:  # noqa: BLE001
        print("schema cache patch failed", e, file=sys.stderr)


def _patch_fixture_loaders():
    for name, mod in list(sys.modules.items()):
        if not (name == "tests" or name.startswith("tests.")) or mod is None:
            continue
        f = getattr(mod, "load_sql_fixture_pairs", None)
        if f is not None and not getattr(f, "_br_wrapped", False):
            mod.load_sql_fixture_pairs = _wrap_fixture_pairs(f)


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
    # wrap the class that defines __init__: setting it on a class that only
    # inherits one would skip mixins further along the MRO (e.g. TimeUnit)
    for c in cls.__mro__:
        if "__init__" in c.__dict__:
            cls = c
            break
    if getattr(cls.__init__, "_br_wrapped", False):
        return
    orig = cls.__init__

    def __init__(self, *a, **k):
        _types.add(type(self))
        orig(self, *a, **k)
        d = getattr(self, "dialect", None)
        if d is not None:
            _types.add(type(d))

    __init__._br_wrapped = True
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
    _clear_caches()
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

    _install_slicing()
    mp_shim.patch_modules(mp_shim.SlicingExecutor)
    _patch_fixture_loaders()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    on = item.nodeid.split("::")[0] in SLICE_FILES
    if on:
        _SL.update(on=True, cur=None, slices={}, common={}, y=0, k=0, titles={})
        mon.restart_events()
    try:
        yield
    finally:
        if on:
            _SL["on"] = False
            if len(_SL["slices"]) >= 2:
                keys = sorted(_SL["slices"], key=lambda k: (k[0], int(k[1:])))
                masks = {}
                for i, k in enumerate(keys):
                    bit = 1 << i
                    for f, ls in _SL["slices"][k].items():
                        mf = masks.setdefault(f, {})
                        for ln in ls:
                            mf[ln] = mf.get(ln, 0) | bit
                _results["<slices>:" + item.nodeid] = {
                    "keys": keys,
                    "common": {f: sorted(v) for f, v in _SL["common"].items()},
                    "mask": {f: {str(ln): format(m, "x") for ln, m in d.items()} for f, d in masks.items()},
                }
            _SL.update(slices={}, common={}, titles={})


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
