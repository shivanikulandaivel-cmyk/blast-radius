"""Run selected sqlglot tests on a patched overlay and report which failed.

Used two ways:
  * imported by selector.py, which forks a child per commit (run_in_child)
  * as a script: python runner.py <overlay_root> <out.json> <timeout> <nodeid>...
"""

import json
import os
import sys


class _Collector:
    def pytest_collection_modifyitems(self, session, config, items):
        import mp_shim

        mp_shim.patch_modules(mp_shim.ForkPool if os.environ.get("BR_POOL") == "fork" else mp_shim.SyncExecutor)

    def __init__(self):
        self.failed = set()
        self.seen = set()
        self.collect_errors = []

    def pytest_runtest_logreport(self, report):
        nid = report.nodeid
        self.seen.add(nid)
        if report.failed:
            self.failed.add(nid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collect_errors.append(report.nodeid)


def run_tests(root, nodeids, out_path, timeout, check_imports=(), collect_all=False, later=()):
    """Run nodeids; then, only if none of them failed, run the (expensive)
    tests in later. Tests not run are left out of "seen"."""
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    sys.dont_write_bytecode = True
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    import importlib

    import_errors = []
    if check_imports == "ALL":
        import pkgutil

        check_imports = []
        base = os.path.join(root, "sqlglot")
        for dp, dn, fn in os.walk(base):
            dn[:] = [d for d in dn if d != "__pycache__"]
            for f in fn:
                if f.endswith(".py") and f != "__main__.py":
                    rel = os.path.relpath(os.path.join(dp, f), root)[:-3].replace(os.sep, ".")
                    if rel.endswith(".__init__"):
                        rel = rel[: -len(".__init__")]
                    check_imports.append(rel)
        check_imports.sort()
    for m in check_imports:
        try:
            importlib.import_module(m)
        except BaseException as e:  # noqa: BLE001
            import_errors.append([m, repr(e)[:200]])
    import pytest

    col = _Collector()
    args = [
        "-q",
        "-p", "no:cacheprovider",
        "-p", "no:randomly",
        "--assert=plain",
        "--rootdir", root,
        "-o", "console_output_style=classic",
        "--no-header",
        "--tb=no",
    ]
    try:
        import pytest_timeout  # noqa: F401

        args += ["--timeout", str(timeout), "--timeout-method", "signal"]
    except Exception:
        pass
    # pass ids relative to root
    argfile = os.path.join(os.path.dirname(out_path), os.path.basename(out_path) + ".args")
    with open(argfile, "w") as f:
        f.write("\n".join(nodeids))
    args.append("@" + argfile)
    rc = None
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved = os.dup(1)
    os.dup2(devnull, 1)  # keep stdout clean: it is the answer channel
    try:
        rc = pytest.main(args, plugins=[col]) if nodeids else "skipped"
        if later and not col.failed and not col.collect_errors:
            with open(argfile, "w") as f:
                f.write("\n".join(later))
            rc = (rc, pytest.main(args, plugins=[col]))
    except BaseException as e:  # noqa: BLE001
        rc = "exc:%r" % (e,)
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
    if collect_all:
        # a module-level change can break the import of any test file
        col2 = _Collector()
        os.dup2(devnull, 1)
        try:
            pytest.main(["-q", "--collect-only", "-p", "no:cacheprovider", "--rootdir", root,
                         "--no-header", "--tb=no", os.path.join(root, "tests")], plugins=[col2])
        except BaseException:  # noqa: BLE001
            pass
        finally:
            sys.stdout.flush()
            os.dup2(saved, 1)
        col.collect_errors.extend(col2.collect_errors)
    with open(out_path, "w") as f:
        json.dump(
            {
                "failed": sorted(col.failed),
                "seen": sorted(col.seen),
                "collect_errors": col.collect_errors,
                "import_errors": import_errors,
                "rc": str(rc),
            },
            f,
        )


if __name__ == "__main__":
    root, out, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])
    run_tests(root, sys.argv[4:], out, timeout)
