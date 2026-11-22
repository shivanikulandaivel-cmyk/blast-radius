"""CI only: ground truth for a set of patches. For each patch, run the WHOLE
suite on the patched code (in-process pools, like the selector) and record
which tests fail. Then score the selector's output for the same patches.
Usage: truth.py <data dir> <selector output.jsonl> <out.json>"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/app/br")
os.environ.setdefault("BR_REPO", "/app/sqlglot")
import selector  # noqa: E402
from brlib import apply_hunks, parse_patch  # noqa: E402

data, sel_out, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
tests = json.load(open(os.path.join(data, "tests.json")))["tests"]
ids = [t["id"] for t in tests]
num = {t["id"]: t["number"] for t in tests}
dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
cdir = os.path.join(data, "commits")
cids = sorted(f[:-6] for f in os.listdir(cdir) if f.endswith(".patch"))


def one(cid):
    t0 = time.time()
    try:
        fds = parse_patch(open(os.path.join(cdir, cid + ".patch")).read())
        changes = {}
        for fd in fds:
            old = open(os.path.join("/app/sqlglot", fd.old)).read() if fd.old else None
            changes[fd.new or fd.old] = apply_hunks(old, fd) if fd.new else None
        root = "/tmp/truth/" + cid
        selector.build_overlay(root, changes)
        res = root + ".json"
        argf = root + ".ids"
        subprocess.run([sys.executable, "/app/br/runner.py", root, res, "60"] + ids,
                       env=dict(os.environ, PYTHONPATH="/app/br", SKIP_INTEGRATION="0"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
        r = json.load(open(res))
        failing = sorted(set(r["failed"]) | (set(ids) - set(r["seen"])))
        subprocess.run(["rm", "-rf", root])
        return cid, {"failing": [num[i] for i in failing], "secs": round(time.time() - t0)}
    except Exception as e:  # noqa: BLE001
        return cid, {"error": repr(e)[:200], "secs": round(time.time() - t0)}


with ThreadPoolExecutor(4) as ex:
    truth = dict(ex.map(one, cids))
for c in cids:
    print(c, {k: (v if k != "failing" else len(v)) for k, v in truth[c].items()}, flush=True)

sel = {}
for line in open(sel_out):
    r = json.loads(line)
    sel[r["commit"]] = r["workers"]
total = 0
oracle = 0
report = {}
for c in cids:
    ws = sel.get(c, [])
    chosen = {t for w in ws for t in w}
    cost = (max(sum(dur[t] for t in w) for w in ws) + 2000 * len(ws)) if ws else 0
    tr = truth[c].get("failing")
    miss = tr is not None and bool(set(tr) - chosen)
    orc = (max(dur[t] for t in tr) + 2000) if tr else 0
    total += cost + (300000 if miss else 0)
    oracle += orc
    report[c] = {"cost": cost / 1000, "oracle": orc / 1000, "miss": miss,
                 "n_fail": len(tr) if tr is not None else None, "n_sel": len(chosen)}
    print(c, report[c], flush=True)
print("TOTAL %.1f  ORACLE %.1f  MISSES %d" % (total / 1000, oracle / 1000, sum(r["miss"] for r in report.values())))
json.dump({"truth": truth, "report": report}, open(out_path, "w"), indent=1)
