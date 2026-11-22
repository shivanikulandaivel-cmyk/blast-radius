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
import brprobe  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
REPO = os.environ.get("BR_REPO", "/app/sqlglot")
INDEX = os.environ.get("BR_INDEX", os.path.join(HERE, "index.pkl"))
WORK = os.environ.get("BR_WORK", "/tmp/br")
BUDGET = float(os.environ.get("BR_BUDGET", "0"))  # wall seconds; 0 = derive
WORKER_COST = 2000  # ms
MAX_WORKERS = 8
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
NO_RUN = os.environ.get("BR_NO_RUN") == "1"
RISK = os.environ.get("BR_RISK", "0") == "1"
PROBE = os.environ.get("BR_PROBE", "1") == "1"
PROBE_MIN_HITS = int(os.environ.get("BR_PROBE_MIN_HITS", "10"))
PROBE_HEAVY_MS = int(os.environ.get("BR_PROBE_HEAVY_MS", "5000"))
PROBE_HEAVY_DUR = int(os.environ.get("BR_PROBE_HEAVY_DUR", "2000"))
PROBE_EV_BUDGET = int(os.environ.get("BR_PROBE_EV_BUDGET", "4000"))
FIXTURE_DIRECT = os.environ.get("BR_FIXTURE_DIRECT", "1") == "1"
FIXTURE_DIRECT_MS = int(os.environ.get("BR_FIXTURE_DIRECT_MS", "4000"))
PROBE_MIN_EVIDENCE = int(os.environ.get("BR_PROBE_MIN_EVIDENCE", "3"))
CPU_FACTOR = float(os.environ.get("BR_CPU_FACTOR", "2.5"))
PLAN_SCALE = float(os.environ.get("BR_PLAN_SCALE", "1.0"))
RUN_CAP_MS = float(os.environ.get("BR_RUN_CAP_MS", "1e12"))
NO_LATER = os.environ.get("BR_NO_LATER", "0") == "1"
BIG_GAMBLE = os.environ.get("BR_BIG_GAMBLE", "0") == "1"
BIG_GAMBLE_MS = int(os.environ.get("BR_BIG_GAMBLE_MS", "2000"))
BIG_GAMBLE_EV = int(os.environ.get("BR_BIG_GAMBLE_EV", "40"))
BIG_GAMBLE_BUDGET = int(os.environ.get("BR_BIG_GAMBLE_BUDGET", "10000"))
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
    def __init__(self, cid, changes, nodeids, timeout, hard, check_imports=(), collect_all=False, later=(),
                 probe_sites=0):
        self.probe_sites = probe_sites
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
                             job.check_imports, job.collect_all, job.later,
                             slices=getattr(job, "slices", None) or {},
                             stoppable=getattr(job, "stoppable", None) or (),
                             probe=bool(job.probe_sites))
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
            "failed", len(j.result["failed"]) if j.result else "?",
            "| raw %.2fs files %d later %d deep %d" % (getattr(j, "raw", 0) / 1000, getattr(j, "nfiles", 0),
                                                       len(j.later), int(getattr(j, "deep", False))))


# ------------------------------------------------------------ attribute diff

ATTRDIFF = os.environ.get("BR_ATTRDIFF", "1") == "1"
SLICING = os.environ.get("BR_SLICING", "1") == "1"
_ATTR_BASE = None


def attr_diff_bits(cid, changes, idx):
    """Import the patched code in a fresh process and diff its fingerprints
    against the original's. Bits of affected tests, or None when unsure."""
    global _ATTR_BASE
    import subprocess

    import attrdiff

    if _ATTR_BASE is None:
        with open(os.path.join(HERE, "maps", "attrfp.json")) as f:
            _ATTR_BASE = json.load(f)["fp"]
    root = os.path.join(WORK, "ad_" + cid)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    build_overlay(root, changes)
    out = root + ".json"
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "attrdiff.py"), root, out],
                       env=dict(os.environ, PYTHONHASHSEED="0", SKIP_INTEGRATION="0"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, cwd=WORK)
        with open(out) as f:
            res = json.load(f)
    except Exception as e:  # noqa: BLE001
        log(cid, "attrdiff run failed", repr(e))
        return None
    finally:
        shutil.rmtree(root, ignore_errors=True)
        try:
            os.remove(out)
        except OSError:
            pass
    if res.get("errors"):
        return None
    entries = attrdiff.diff(_ATTR_BASE, res["fp"])
    if not entries:
        return None  # nothing visible changed: keep the broad answer
    log(cid, "attrdiff changed:", [e[1].split(":")[-1] + "." + e[2] for e in entries[:6]], len(entries))
    return idx.diff_bits(entries)


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
        # import-time changes: compare what the patched code actually builds
        # (class attributes, module globals) with the original, and keep only
        # tests that read a value that really changed
        if ATTRDIFF and ok and "importlevel" in flags and "syntax" not in flags and "addrm" not in flags \
                and all(fd.path.startswith("sqlglot/") and fd.path.endswith(".py") for fd in fds):
            try:
                base_bits, imp_bits = idx.last_split
                extra = set(to_nums(imp_bits & ~base_bits))
                if extra:
                    _, extra_cost = schedule(extra, dur)
                    if extra_cost > 4000:
                        db = attr_diff_bits(cid, changes, idx)
                        if db is not None:
                            # both answers are safe on their own: keep what both select
                            newc = set(to_nums(base_bits | (db & imp_bits))) | set(unmapped)
                            log(cid, "attrdiff: %d -> %d candidates" % (len(cand), len(newc)))
                            cand = newc
            except Exception as e:  # noqa: BLE001
                log(cid, "attrdiff failed", repr(e))
        # big tests recorded case by case: when the change only reaches some
        # cases, run just those (exact: the other cases run no changed code)
        slice_f = {}
        slice_frac = {}
        if SLICING and ok and "importlevel" not in flags and "addrm" not in flags and "syntax" not in flags \
                and fds and all(fd.path.startswith("sqlglot/") and fd.path.endswith(".py")
                                and fd.path in idx.last_body_only for fd in fds):
            for t in cand:
                r = idx.slice_select(ids[t], idx.last_ranges)
                if r is not None:
                    slice_f[ids[t]] = [r[0], r[1]]
                    slice_frac[t] = r[2]
            if slice_f:
                log(cid, "slices:", {k.rsplit("::", 1)[-1]: "%d" % len(v[1]) for k, v in slice_f.items()})
        if "syntax" in flags:
            # a module that cannot be compiled fails every test that imports it
            plans[cid] = {"out": cand, "run": []}
            log(cid, "syntax error -> output", len(cand))
            continue
        # very expensive tests (recorded >= RISK_MS): when enough cheaper
        # candidates exist, run those first; if they all pass, leave the
        # expensive ones out, otherwise hand them over
        gamble = []
        evidence = []
        if RISK and ok and not NO_RUN:
            heavy = [t for t in cand if dur[t] >= RISK_MS]
            if heavy:
                def _rc(t):
                    c = idx.cpu.get(ids[t])
                    return c if c is not None else dur[t] * 1.2 + 5
                ev_all = sorted((t for t in cand if t not in heavy and _rc(t) <= 1000), key=_rc)
                ev, tot = [], 0
                for t in ev_all:
                    if len(ev) >= 12 or (tot + _rc(t) > 1500 and len(ev) >= RISK_MIN_EVIDENCE):
                        break
                    ev.append(t)
                    tot += _rc(t)
                if len(ev) >= RISK_MIN_EVIDENCE:
                    gamble = heavy
                    evidence = ev
                    cand = set(cand) - set(heavy) - set(ev)
        data_only = bool(fds) and all(not (fd.path or "").endswith(".py") for fd in fds)
        if data_only:
            # only edits of real content: comment or blank lines in a fixture
            # are usually harmless, and running the test is the cheap check
            def _content(line):
                # what the fixture helpers keep ("#" lines are settings)
                return line != "" and not line.startswith("--")

            data_only = any(_content(b[1:]) for fd in fds for h in fd.hunks for b in h[4]
                            if b[:1] in "+-")
        # choose which candidates to run and which to hand over directly
        order = sorted(cand, key=lambda t: -dur[t])
        h = 400  # ms of fork/overhead per run

        def runcost(t):
            # build-time measurements run warm; a fresh child pays imports,
            # lazy dialect setup and caches again: about 2.5x in practice
            c = idx.cpu.get(ids[t])
            c = (c if c is not None else dur[t] * 1.2) * CPU_FACTOR + 5
            if t in slice_frac:
                c = c * slice_frac[t] + 150
            return c

        rc = {t: runcost(t) for t in list(order) + evidence}
        # planning view of the run cost: PLAN_SCALE < 1 favours running over
        # handing over; tests above RUN_CAP_MS (measured CPU) are never run
        def plancost(t):
            raw = idx.cpu.get(ids[t])
            if raw is not None and raw > RUN_CAP_MS and t not in slice_frac:
                return float("inf")
            return rc[t] * PLAN_SCALE
        owner = {t: ids[t].rsplit("::", 1)[0] for t in list(order) + evidence}
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
                run_cost = h * PLAN_SCALE + sum(plancost(t) for t in tail) + \
                    sum(idx.class_setup.get(o, 0) for o in owners) * PLAN_SCALE
            else:
                run_cost = 0
            _, sc = schedule(head, dur)
            c = run_cost + sc
            if best is None or c < best[0]:
                best = (c, j)
        j = best[1]
        if data_only and FIXTURE_DIRECT and order:
            # a changed fixture almost always breaks the tests that read it
            # (all 10 such samples did): confirming that costs more than it saves
            _, direct = schedule(order, dur)
            if direct <= FIXTURE_DIRECT_MS:
                j = len(order)
        out, run = order[:j], order[j:] + evidence
        plans[cid] = {"out": set(out), "run": run, "gamble": gamble}
        probe_sites = 0
        ev_stop = []
        if PROBE and ok and out and not NO_RUN and "importlevel" not in flags and "addrm" not in flags \
                and all(fd.path.startswith("sqlglot/") and fd.path.endswith(".py") for fd in fds):
            probed = {}
            for fd in fds:
                if fd.old is None or changes.get(fd.new) is None:
                    probed = None
                    break
                try:
                    old_src = open(os.path.join(REPO, fd.old), encoding="utf-8", newline="").read()
                except Exception:
                    probed = None
                    break
                if old_src == changes[fd.new]:
                    continue
                sites = brprobe.plan(old_src, changes[fd.new])
                text = brprobe.instrument(changes[fd.new], sites, base_id=probe_sites) if sites else None
                if text is None:
                    probed = None
                    break
                probed[fd.new] = text
                probe_sites += len(sites)
            if probed and probe_sites:
                # bet only on tests that are very expensive to run and to hand
                # over. A small sample of the cheapest related tests runs on the
                # probed code; the bet stands only if nothing fails and the
                # changed code ran many times without ever computing a
                # different value. Everything else keeps its normal plan.
                allc = set(out) | set(run)
                bet = {t for t in allc if rc[t] >= PROBE_HEAVY_MS and dur[t] >= PROBE_HEAVY_DUR}
                if bet:
                    pool = sorted((t for t in allc if t not in bet and rc[t] <= 2500), key=lambda t: rc[t])
                    in_run = [t for t in pool if t in run]
                    ev_extra, tot = [], sum(rc[t] for t in in_run)
                    for t in pool:
                        if t in run:
                            continue
                        if len(in_run) + len(ev_extra) >= 25 or (tot + rc[t] > PROBE_EV_BUDGET and len(in_run) + len(ev_extra) >= 3):
                            break
                        ev_extra.append(t)
                        tot += rc[t]
                    new_out = set(out) - bet - set(ev_extra)
                    new_run = [t for t in run if t not in bet] + ev_extra
                    _, c_with = schedule(set(out), dur)
                    _, c_without = schedule(new_out, dur)
                    gain = c_with - c_without
                    cost_ev = sum(rc[t] for t in ev_extra) + (0 if [t for t in run if t not in bet] else h)
                    if len(new_run) >= 3 and gain > cost_ev:
                        ev_stop = list(ev_extra)
                        out, run = sorted(new_out), new_run
                        plans[cid] = {"out": set(out), "run": run, "gamble": [], "bet": bet}
                        changes = dict(changes)
                        changes.update(probed)
                        log(cid, "probe", probe_sites, "sites; betting", len(bet), "heavy tests on", len(run),
                            "(gain %.1fs, evidence %.1fs)" % (gain / 1000, cost_ev / 1000))
                    else:
                        probe_sites = 0
                else:
                    probe_sites = 0
            else:
                probe_sites = 0
        # Option B: when a lot would be handed over, run the cheapest of those
        # tests as evidence; if every test that runs passes, leave the rest
        # out. A bet: a failure among the rest would be a miss.
        if BIG_GAMBLE and ok and not NO_RUN and out and not gamble and "syntax" not in flags \
                and "importlevel" not in flags and "addrm" not in flags:
            _, out_cost = schedule(set(out), dur)
            if out_cost >= BIG_GAMBLE_MS:
                pool = sorted((t for t in out if rc[t] <= 2500), key=lambda t: rc[t])
                ev2, tot = [], 0
                for t in pool:
                    if len(ev2) >= BIG_GAMBLE_EV or (tot + rc[t] > BIG_GAMBLE_BUDGET and len(ev2) >= 3):
                        break
                    ev2.append(t)
                    tot += rc[t]
                if len(ev2) + len([t for t in run]) >= 3 and ev2:
                    gamble = [t for t in out if t not in set(ev2)]
                    run = list(run) + ev2
                    out = []
                    p0 = plans[cid]
                    p0["out"] = set()
                    p0["run"] = run
                    p0["gamble"] = gamble
                    log(cid, "big gamble: %d tests (%.1fs) on %d evidence" % (len(gamble), out_cost / 1000, len(ev2)))
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
            # a test that hangs fails (timeout) either way: stop it early and
            # hand it over instead of burning CPU waiting
            maxc = max([rc[t] + idx.class_setup.get(owner[t], 0) for t in run] or [0])
            timeout = max(2.0, maxc / 1000 * 3 + 1)
            hard = 4 + sum(rc[t] for t in run) / 1000 * 2.5
            if "deepcheck" in flags or "addrm" in flags:
                hard += 4
            deep = "deepcheck" in flags or "addrm" in flags
            # expensive tests run last, and only if the cheap ones all passed:
            # a cheap failure means the expensive ones likely fail too, and
            # handing them over is cheaper than running them to find out
            # extra evidence goes last so it can stop early without costing
            # the tests that were going to run anyway
            evs = set(ev_stop)
            run = [t for t in run if t not in evs] + [t for t in run if t in evs]
            first = [ids[t] for t in run if rc[t] < 1000 or NO_LATER]
            later = [ids[t] for t in run if rc[t] >= 1000 and not NO_LATER]
            if not first:
                first, later = later, []
            raw = sum((idx.cpu.get(ids[t]) or dur[t]) for t in run) + \
                sum(idx.class_setup.get(o, 0) for o in {owner[t] for t in run})
            nfiles = len({ids[t].split("::")[0] for t in run})
            jobs.append(Job(cid, changes, first, timeout, hard,
                            check_imports="ALL" if deep else mods,
                            collect_all=deep or "importcheck" in flags, later=later,
                            probe_sites=probe_sites))
            jobs[-1].slices = {k: v for k, v in slice_f.items() if k in set(first) | set(later)}
            jobs[-1].stoppable = [ids[t] for t in ev_stop]
            jobs[-1].raw = raw
            jobs[-1].nfiles = nfiles
            jobs[-1].deep = deep

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
                p["out"] |= set(p.get("bet") or [])
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
            bet = p.get("bet")
            if bet:
                pr = res.get("probe") or {}
                clean = (not failed and not res.get("collect_errors") and not res.get("import_errors")
                         and all(t in seen for t in j.nodeids + j.later))
                hits_ok = all(pr.get(str(i), [0, 0])[0] >= PROBE_MIN_HITS for i in range(j.probe_sites))
                div = sum(v[1] for v in pr.values())
                if clean and hits_ok and div == 0:
                    log(j.cid, "probe clean", pr, "-> leaving out", len(bet))
                else:
                    log(j.cid, "probe", pr, "clean" if clean else "fail", "-> handing over", len(bet))
                    p["out"] |= set(bet)
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

    bet_log = os.environ.get("BR_BET_LOG")
    if bet_log:
        try:
            with open(bet_log, "w") as f:
                json.dump({cid: {"bet": sorted(ids[t] for t in (p.get("bet") or [])),
                                 "left_out": bool(p.get("bet")) and not (set(p.get("bet")) & p["out"])}
                           for cid, p in plans.items()}, f)
        except Exception as e:  # noqa: BLE001
            log("bet log failed", e)
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
