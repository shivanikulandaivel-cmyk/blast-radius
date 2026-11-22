"""pytest plugin (build time): CPU seconds each test costs when run the way
the selector runs it (pools replaced by the in-process executor).
Writes {nodeid: [setup, call, teardown]} in ms to $CPU_OUT."""

import json
import os
import time

import pytest

OUT = os.environ.get("CPU_OUT", "/tmp/cpu.json")
_res = {}


def pytest_collection_modifyitems(session, config, items):
    import mp_shim

    mp_shim.patch_modules(mp_shim.SyncExecutor)


def _phase(item, i):
    t0 = time.process_time()
    yield
    rec = _res.setdefault(item.nodeid, [0, 0, 0])
    rec[i] += round((time.process_time() - t0) * 1000)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    yield from _phase(item, 0)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    yield from _phase(item, 1)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    yield from _phase(item, 2)


def pytest_unconfigure(config):
    with open(OUT, "w") as f:
        json.dump(_res, f)
