"""Blast Radius selector.

For each commit:
  1. parse the patch and diff old/new ASTs to find what really changed
  2. pick candidate tests from a map built on the pinned commit (lines run,
     classes used, files opened, per test)
  3. run the cheap candidates on a patched overlay of sqlglot and keep only
     the ones that fail; hand the expensive ones straight to workers
  4. split what is left over up to 8 workers (LPT), picking the worker count
     with the lowest cost
stdout carries only the answer lines; everything else goes to stderr.
"""

import json
import os
import pickle
import resource
import shutil
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from brlib import apply_hunks, parse_patch  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
REPO = os.environ.get("BR_REPO", "/app/sqlglot")
INDEX = os.environ.get("BR_INDEX", os.path.join(HERE, "index.pkl"))
WORK = os.environ.get("BR_WORK", "/tmp/br")
BUDGET = float(os.environ.get("BR_BUDGET", "0"))  # wall seconds; 0 = derive
WORKER_COST = 2000  # ms
MAX_WORKERS = 8
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
NO_RUN = os.environ.get("BR_NO_RUN") == "1"
RISK = os.environ.get("BR_RISK", "1") == "1"
RISK_MS = int(os.environ.get("BR_RISK_MS", "5000"))
RISK_MIN_EVIDENCE = int(os.environ.get("BR_RISK_MIN", "3"))

T0 = time.monotonic()
_printed = False


def log(*a):
    print("[sel %.1fs]" % (time.monotonic() - T0), *a, file=sys.stderr, flush=True)


# ------------------------------------------------------------ scheduling


def lpt(tests, dur, k):
    loads = [0] * k
    ws = [[] for _ in range(k)]
    for t in sorted(tests, key=lambda t: -dur[t]):
        i = loads.index(min(loads))
        ws[i].append(t)
        loads[i] += dur[t]
    return ws, max(loads)


def schedule(tests, dur):
    """Best split of tests over 1..8 workers. Returns (workers, cost_ms)."""
    tests = list(tests)
    if not tests:
        return [], 0
    best = None
    for k in range(1, min(MAX_WORKERS, len(tests)) + 1):
        ws, mk = lpt(tests, dur, k)
        c = mk + WORKER_COST * k
        if best is None or c < best[1]:
            best = (ws, c)
    return best


# ------------------------------------------------------------ overlay


def build_overlay(root, changes):
    """Mirror REPO under root: real dirs, symlinked files, patched files real.
    changes: {relpath: text or None (deleted)}"""
    skip_top = {".git"}
    for dp, dns, fns in os.walk(REPO):
        rel = os.path.relpath(dp, REPO)
        if rel == ".":
            dns[:] = [d for d in dns if d not in skip_top]
            tdir = root
        else:
            tdir = os.path.join(root, rel)
        os.makedirs(tdir, exist_ok=True)
        for fn in fns:
            r = fn if rel == "." else os.path.join(rel, fn)
            if r in changes:
                continue
            src = os.path.join(dp, fn)
            if fn.endswith(".py") and (r.startswith("tests" + os.sep) or r.startswith("sqlglotc" + os.sep)):
                # real copies: some tests resolve their own path
                shutil.copy2(src, os.path.join(tdir, fn))
            else:
                os.symlink(src, os.path.join(tdir, fn))
    for r, text in changes.items():
        if text is None:
            continue
        p = os.path.join(root, r)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)


# ------------------------------------------------------------ child runs


class Job:
    def __init__(self, cid, changes, nodeids, timeout, hard, check_imports=(), collect_all=False, later=()):
        self.later = list(later)
        self.check_imports = check_imports if check_imports == "ALL" else list(check_imports)
        self.collect_all = collect_all
        self.cid = cid
        self.changes = changes
        self.nodeids = nodeids
        self.timeout = timeout
        self.hard = hard
        self.pid = None
        self.started = None
        self.root = os.path.join(WORK, cid)
        self.out = os.path.join(WORK, cid + ".json")
        self.result = None
        self.cpu = 0.0


def start_job(job):
    shutil.rmtree(job.root, ignore_errors=True)
    build_overlay(job.root, job.changes)
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.setpgid(0, 0)
            import runner

            runner.run_tests(job.root, job.nodeids, job.out, job.timeout,
                             job.check_imports, job.collect_all, job.later)
        except BaseException:  # noqa: BLE001
            import traceback

            traceback.print_exc(file=sys.stderr)
            code = 1
        finally:
            sys.stderr.flush()
            os._exit(code)
    job.pid = pid
    job.started = time.monotonic()


def reap(job, status, ru):
    job.cpu = ru.ru_utime + ru.ru_stime if ru else 0.0
    try:
        with open(job.out) as f:
            job.result = json.load(f)
    except Exception:
        job.result = None
    shutil.rmtree(job.root, ignore_errors=True)
    for p in (job.out, job.out + ".args"):
        try:
            os.remove(p)
        except OSError:
            pass


def run_jobs(jobs, deadline):
    pending = list(jobs)
    running = {}
    while pending or running:
        while pending and len(running) < PARALLEL:
            if time.monotonic() > deadline:
                for j in pending:
                    j.result = None
                pending = []
                break
            j = pending.pop(0)
            try:
                start_job(j)
                running[j.pid] = j
            except Exception as e:  # noqa: BLE001
                log("start failed", j.cid, e)
                j.result = None
        if not running:
            break
        try:
            pid, status, ru = os.wait4(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            now = time.monotonic()
            for j in list(running.values()):
                if now - j.started > j.hard or now > deadline + 5:
                    log("killing", j.cid)
                    try:
                        os.killpg(j.pid, signal.SIGKILL)
                    except Exception:
                        try:
                            os.kill(j.pid, signal.SIGKILL)
                        except Exception:
                            pass
            time.sleep(0.005)
            continue
        j = running.pop(pid, None)
        if j is None:
            continue
        # also collect any grandchildren left in the group
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            pass
        reap(j, status, ru)
        log("ran", j.cid, len(j.nodeids), "tests cpu %.2fs" % j.cpu,
            "failed", len(j.result["failed"]) if j.result else "?")


# ------------------------------------------------------------ main


def main():
    tests = json.loads(open(os.path.join(DATA, "tests.json")).read())["tests"]
    num = {t["id"]: t["number"] for t in tests}
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    ids = {t["number"]: t["id"] for t in tests}
    cdir = os.path.join(DATA, "commits")
    commits = sorted(f[:-6] for f in os.listdir(cdir) if f.endswith(".patch"))
    budget = BUDGET or (180.0 if len(commits) <= 34 else 270.0) * (len(commits) / (30 if len(commits) <= 34 else 45))
    budget = max(60.0, min(budget, 170.0 if len(commits) <= 34 else 255.0))
    run_deadline = T0 + budget * 0.55

    import candidates  # noqa: F401  (classes for unpickling)

    with open(INDEX, "rb") as f:
        idx = pickle.load(f)
    idx.root = REPO
    pos2num = {}
    for i, tid in enumerate(idx.ids):
        n = num.get(tid)
        if n is not None:
            pos2num[i] = n
    unmapped = [n for tid, n in num.items() if tid not in set(idx.ids)]
    log("loaded index", len(idx.ids), "tests; unmapped", len(unmapped))

    def to_nums(bits):
        out = []
        i = 0
        while bits:
            low = bits & -bits
            i = low.bit_length() - 1
            n = pos2num.get(i)
            if n is not None:
                out.append(n)
            bits ^= low
        return out

    plans = {}
    jobs = []
    for cid in commits:
        try:
            text = open(os.path.join(cdir, cid + ".patch"), encoding="utf-8", errors="replace").read()
            if "\r\n" in text:
                text = text.replace("\r\n", "\n")
            fds = parse_patch(text)
            if not fds and text.strip():
                raise ValueError("patch not understood")
            bits, notes, flags = idx.candidates(fds)
            cand = set(to_nums(bits)) | set(unmapped)
            changes = {}
            ok = True
            for fd in fds:
                if fd.binary:
                    ok = False
                    continue
                if fd.new is None:
                    changes[fd.old] = None
                    continue
                old = None
                if fd.old is not None:
                    try:
                        old = open(os.path.join(REPO, fd.old), encoding="utf-8", newline="").read()
                    except Exception:
                        old = None
                try:
                    changes[fd.new] = apply_hunks(old, fd)
                except Exception as e:  # noqa: BLE001
                    log(cid, "apply failed", fd.path, e)
                    ok = False
                if fd.old and fd.old != fd.new:
                    changes[fd.old] = None
        except Exception as e:  # noqa: BLE001
            log(cid, "analysis failed", repr(e))
            cand = set(dur)
            changes, ok, notes, flags = {}, False, ["error"], set()
        if "syntax" in flags:
            # a module that cannot be compiled fails every test that imports it
            plans[cid] = {"out": cand, "run": []}
            log(cid, "syntax error -> output", len(cand))
            continue
        # very expensive tests (recorded >= RISK_MS): when enough cheaper
        # candidates exist, run those first; if they all pass, leave the
        # expensive ones out, otherwise hand them over
        gamble = []
        if RISK and ok:
            heavy = [t for t in cand if dur[t] >= RISK_MS]
            cheap_n = len(cand) - len(heavy)
            if heavy and cheap_n >= RISK_MIN_EVIDENCE:
                gamble = heavy
                cand = set(cand) - set(heavy)
        # choose which candidates to run and which to hand over directly
        order = sorted(cand, key=lambda t: -dur[t])
        h = 300  # ms of fork/overhead per run

        def runcost(t):
            c = idx.cpu.get(ids[t])
            return c if c is not None else dur[t] * 1.2 + 5

        rc = {t: runcost(t) for t in order}
        owner = {t: ids[t].rsplit("::", 1)[0] for t in order}
        best = None
        # splits: the first j (longest) tests are handed over, the rest run
        js = list(range(min(len(order), 60) + 1))
        if len(order) not in js:
            js.append(len(order))
        for j in js:
            head = order[:j]
            tail = order[j:]
            if tail:
                if NO_RUN or not ok:
                    continue
                owners = {owner[t] for t in tail}
                run_cost = h + sum(rc[t] for t in tail) + sum(idx.class_setup.get(o, 0) for o in owners)
            else:
                run_cost = 0
            _, sc = schedule(head, dur)
            c = run_cost + sc
            if best is None or c < best[0]:
                best = (c, j)
        j = best[1]
        out, run = order[:j], order[j:]
        plans[cid] = {"out": set(out), "run": run, "gamble": gamble}
        if gamble and not run:
            # nothing cheap to run means no evidence: hand them over
            plans[cid]["out"] |= set(gamble)
            plans[cid]["gamble"] = gamble = []
        log(cid, "cand", len(cand), "out", len(out), "run", len(run), "est %.1fs" % (best[0] / 1000), notes[:4])
        mods = []
        if "importlevel" in flags or "addrm" in flags:
            mods = [p[:-3].replace("/", ".").replace(".__init__", "") for p in changes
                    if p.startswith("sqlglot/") and p.endswith(".py") and changes[p] is not None]
        plans[cid]["fds"] = fds
        if ok and (run or mods):
            maxc = max([rc[t] for t in run] or [0])
            timeout = max(10.0, maxc / 1000 * 4 + 5)
            hard = 40 + sum(rc[t] for t in run) / 1000 * 3
            deep = "deepcheck" in flags or "addrm" in flags
            # expensive tests run last, and only if the cheap ones all passed:
            # a cheap failure means the expensive ones likely fail too, and
            # handing them over is cheaper than running them to find out
            first = [ids[t] for t in run if rc[t] < 1000]
            later = [ids[t] for t in run if rc[t] >= 1000]
            if not first:
                first, later = later, []
            jobs.append(Job(cid, changes, first, timeout, hard,
                            check_imports="ALL" if deep else mods,
                            collect_all=deep or "importcheck" in flags, later=later))

    if jobs:
        # preload heavy third-party modules once; children inherit them
        os.environ["SKIP_INTEGRATION"] = "0"
        heavy_files = ("tests/test_executor.py", "tests/test_optimizer.py")
        need_heavy = any(n.startswith(heavy_files) for jb in jobs for n in jb.nodeids + jb.later)
        mods_pre = ["pytest", "pytest_timeout", "pytz", "dateutil"]
        if need_heavy:
            mods_pre += ["numpy", "pandas", "duckdb"]
        for m in mods_pre:
            try:
                __import__(m)
            except Exception:
                pass
        os.makedirs(WORK, exist_ok=True)
        import gc

        gc.collect()
        gc.freeze()  # children share the parent's heap without touching it
        run_jobs(jobs, run_deadline)
        for j in jobs:
            p = plans[j.cid]
            res = j.result
            if res is None:
                # no usable result: hand every planned test over
                p["out"] |= set(num[i] for i in j.nodeids + j.later)
                p["out"] |= set(p.get("gamble") or [])
                continue
            if res.get("import_errors"):
                # a module no longer imports: everything that uses it fails
                broad = 0
                for fd in plans[j.cid].get("fds", []):
                    pth = fd.old or fd.new
                    if pth.endswith(".py") and fd.old:
                        broad |= idx.importtime_bits(pth) | idx.any_line_bits(pth)
                for m, _err in res["import_errors"]:
                    mp = m.replace(".", "/")
                    for pth in (mp + ".py", mp + "/__init__.py"):
                        if os.path.exists(os.path.join(REPO, pth)):
                            broad |= idx.importtime_bits(pth) | idx.any_line_bits(pth)
                p["out"] |= set(to_nums(broad))
                log(j.cid, "import errors", res["import_errors"][:2], "-> +", len(to_nums(broad)))
            failed = set(res["failed"])
            seen = set(res["seen"])
            g = p.get("gamble") or []
            if g:
                cheap_seen = [t for t in j.nodeids if t in seen]
                if failed or res.get("collect_errors") or res.get("import_errors") \
                        or len(cheap_seen) < len(j.nodeids):
                    p["out"] |= set(g)
                else:
                    log(j.cid, "skipping", len(g), "expensive tests: all cheaper ones passed")
            for tid in j.nodeids + j.later:
                if tid in failed or tid not in seen:
                    p["out"].add(num[tid])
            if res.get("collect_errors"):
                # a test file failed to import: all its tests fail
                files = {ce.split("::")[0] for ce in res["collect_errors"]}
                for t in tests:
                    if t["file"] in files:
                        p["out"].add(t["number"])
                log(j.cid, "collect errors in", len(files), "files")

    total = 0
    lines = []
    for cid in commits:
        ws, c = schedule(plans[cid]["out"], dur)
        total += c
        lines.append(json.dumps({"commit": cid, "workers": ws}, separators=(",", ":")))
    global _printed
    _printed = True
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    sys.stdout.flush()
    ru = resource.getrusage(resource.RUSAGE_SELF)
    rc = resource.getrusage(resource.RUSAGE_CHILDREN)
    log("done: simulated %.1fs, cpu self %.1fs children %.1fs" % (
        total / 1000, ru.ru_utime + ru.ru_stime, rc.ru_utime + rc.ru_stime))


def fallback():
    """Something broke: run everything, split well. Never miss."""
    tests = json.loads(open(os.path.join(DATA, "tests.json")).read())["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    ws, _ = schedule(list(dur), dur)
    cdir = os.path.join(DATA, "commits")
    for f in sorted(os.listdir(cdir)):
        if f.endswith(".patch"):
            sys.stdout.write(json.dumps({"commit": f[:-6], "workers": ws}, separators=(",", ":")) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001
        import traceback

        traceback.print_exc(file=sys.stderr)
        if not _printed:
            fallback()
