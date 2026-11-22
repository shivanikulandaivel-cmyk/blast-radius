"""Stand-ins for the ProcessPoolExecutor that sqlglot's tests create.

SyncExecutor runs each task in this process but sends arguments and results
through pickle, as a process boundary would. The mapper uses it so the code
the pool runs is traced and attributed to the right test.

fork_pool keeps a real pool (one forked worker), used at run time.
"""

import concurrent.futures as cf
import multiprocessing
import pickle
import sys


def _rt(x):
    return pickle.loads(pickle.dumps(x))


class SyncExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._init = initializer
        self._initargs = initargs
        self._ready = False

    def submit(self, fn, *args, **kwargs):
        f = cf.Future()
        try:
            if self._init is not None and not self._ready:
                self._ready = True
                self._init(*_rt(self._initargs))
            a, k = _rt((args, kwargs))
            f.set_result(_rt(fn(*a, **k)))
        except BaseException as e:  # noqa: BLE001
            try:
                e = _rt(e)
            except Exception:
                pass
            f.set_exception(e)
        return f

    def map(self, fn, *its, **kw):
        return [self.submit(fn, *a).result() for a in zip(*its)]

    def shutdown(self, wait=True, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ForkPool(cf.ProcessPoolExecutor):
    def __init__(self, max_workers=None, mp_context=None, **kw):
        super().__init__(max_workers=1, mp_context=multiprocessing.get_context("fork"), **kw)


def patch_modules(replacement):
    for name, mod in list(sys.modules.items()):
        if not (name == "tests" or name.startswith("tests.")) or mod is None:
            continue
        if getattr(mod, "ProcessPoolExecutor", None) is cf.ProcessPoolExecutor:
            mod.ProcessPoolExecutor = replacement
