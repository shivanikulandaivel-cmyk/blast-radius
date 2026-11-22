"""CI only: re-check every "passed, leave it out" decision of the selector.
For each commit, the tests the selector ran and saw pass (and did not hand
over) are run again on the patched code, whole (no case slicing), in a fresh
session with nothing else in it. A test that fails here would have been a miss
if the full run agrees.
Usage: audit.py <data dir> <run log json> <out json>"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/app/br")
os.environ.setdefault("BR_REPO", "/app/sqlglot")
import selector  # noqa: E402
from brlib import apply_hunks, parse_patch  # noqa: E402

data, run_log, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
rl = json.load(open(run_log))
cdir = os.path.join(data, "commits")


def one(cid):
    t0 = time.time()
    r = rl[cid]
    out = set(r["out"])
    failed = set(r["failed"])
    seen = set(r["seen"])
    passed = [t for t in r["run"] if t in seen and t not in failed and t not in out]
    if not passed:
        return cid, {"checked": 0}
    # did anything fail before this test in the same session?
    after_fail = []
    hit = False
    for t in r["run"]:
        if t in failed:
            hit = True
        elif hit and t in passed:
            after_fail.append(t)
    try:
        fds = parse_patch(open(os.path.join(cdir, cid + ".patch")).read())
        changes = {}
        for fd in fds:
            old = open(os.path.join("/app/sqlglot", fd.old)).read() if fd.old else None
            changes[fd.new or fd.old] = apply_hunks(old, fd) if fd.new else None
        root = "/tmp/audit/" + cid
        selector.build_overlay(root, changes)
        res = root + ".json"
        subprocess.run([sys.executable, "/app/br/runner.py", root, res, "60"] + passed,
                       env=dict(os.environ, PYTHONPATH="/app/br", SKIP_INTEGRATION="0"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
        rr = json.load(open(res))
        bad = sorted(set(rr["failed"]) | (set(passed) - set(rr["seen"])))
        subprocess.run(["rm", "-rf", root])
        return cid, {"checked": len(passed), "bad": bad,
                     "bad_sliced": [b for b in bad if b in r["slices"]],
                     "bad_after_fail": [b for b in bad if b in after_fail],
                     "slices": {b: r["slices"][b][0] for b in bad if b in r["slices"]},
                     "secs": round(time.time() - t0)}
    except Exception as e:  # noqa: BLE001
        return cid, {"error": repr(e)[:200], "secs": round(time.time() - t0)}


with ThreadPoolExecutor(4) as ex:
    rep = dict(ex.map(one, sorted(rl)))
nb = 0
for c in sorted(rep):
    v = rep[c]
    if v.get("bad") or v.get("error"):
        nb += 1
        print("MISMATCH", c, v, flush=True)
print("AUDITED %d commits, %d tests re-run, %d commits with mismatches" % (
    len(rep), sum(v.get("checked", 0) for v in rep.values()), nb))
json.dump(rep, open(out_path, "w"), indent=1)
