"""pytest plugin (build time only): which class settings each test reads.

After every sqlglot module is imported, each UPPER_CASE setting of the
tokenizer / parser / generator / dialect classes is replaced, on the class that
defines it, by a small descriptor that returns the same value and records the
read. Python resolves self.X / cls.X / getattr(obj, "X") through the class
that defines X, so the record names the exact definition a test used (a
subclass that overrides X records its own). Reads are kept per test, per class
setup (applies to every test of the class) and per test-file collection
(applies to every test of the file).
"""

import gzip
import json
import os
import re
import sys

_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_keys = []  # index -> "module:Qualname.ATTR"
_reads = set()
_scope = ["<session>"]
_out = {}
_failed = set()


class _Rec:
    __slots__ = ("v", "i")

    def __init__(self, v, i):
        self.v = v
        self.i = i

    def __get__(self, obj, typ=None):
        _reads.add(self.i)
        return self.v


def _flush():
    if _reads:
        s = _out.setdefault(_scope[0], set())
        s |= _reads
        _reads.clear()


def _set_scope(name):
    _flush()
    _scope[0] = name


def _install():
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
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    seen = set()
    stack = [Tokenizer, Parser, Generator, Dialect]
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        stack.extend(c.__subclasses__())
        if not (getattr(c, "__module__", "") or "").startswith("sqlglot"):
            continue
        for name, v in list(vars(c).items()):
            if not _KEY.match(name) or callable(v) or isinstance(v, (staticmethod, classmethod, property, type)):
                continue
            try:
                setattr(c, name, _Rec(v, len(_keys)))
                _keys.append("%s:%s.%s" % (c.__module__, c.__qualname__, name))
            except Exception:
                pass
    _reads.clear()
    print("reads: %d settings on %d classes" % (len(_keys), len(seen)), file=sys.stderr)


def pytest_configure(config):
    _install()


def pytest_collection_modifyitems(session, config, items):
    try:
        import mp_shim

        mp_shim.patch_modules(mp_shim.SyncExecutor)
    except Exception as e:  # noqa: BLE001
        print("pool patch failed", e, file=sys.stderr)


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    prev = _scope[0]
    if type(collector).__name__ == "Module":
        _set_scope("<collect>:" + collector.nodeid)
    yield
    _set_scope(prev)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _set_scope("<setup>:" + item.nodeid.rsplit("::", 1)[0])
    yield
    _set_scope("<session>")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _set_scope(item.nodeid)
    yield
    _set_scope("<session>")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _set_scope("<setup>:" + item.nodeid.rsplit("::", 1)[0])
    yield
    _set_scope("<session>")


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _failed.add("<collect>:" + report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    _flush()
    out = os.environ.get("READS_OUT")
    if out:
        with open(out, "w") as f:
            json.dump({"keys": _keys, "failed": sorted(_failed),
                       "reads": {k: sorted(v) for k, v in _out.items()}}, f)
