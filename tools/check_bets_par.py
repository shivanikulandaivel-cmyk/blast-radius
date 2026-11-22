"""CI only: for every commit where the probe left tests out, run those tests
on the plainly patched code. Any failure there would have been a miss."""
import json, os, subprocess, sys, time
sys.path.insert(0, "/app/br")
os.environ.setdefault("BR_REPO", "/app/sqlglot")
import selector  # noqa: E402
from brlib import apply_hunks, parse_patch  # noqa: E402

cdir, bets_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
bets = json.load(open(bets_path))
report = {}
from concurrent.futures import ThreadPoolExecutor


def one(item):
    cid, b = item
    fds = parse_patch(open(os.path.join(cdir, cid + ".patch")).read())
    changes = {}
    for fd in fds:
        changes[fd.new] = apply_hunks(open(os.path.join("/app/sqlglot", fd.old)).read(), fd)
    root = "/tmp/cb/" + cid
    selector.build_overlay(root, changes)
    res = "/tmp/cb/" + cid + ".json"
    t0 = time.time()
    subprocess.run([sys.executable, "/app/br/runner.py", root, res, "600"] + b["bet"],
                   env=dict(os.environ, PYTHONPATH="/app/br", SKIP_INTEGRATION="0"),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        r = json.load(open(res))
        failed = r["failed"]
        unseen = [t for t in b["bet"] if t not in r["seen"]]
    except Exception as e:  # noqa: BLE001
        failed, unseen = ["<no result: %r>" % (e,)], []
    subprocess.run(["rm", "-rf", root])
    return cid, {"left_out": len(b["bet"]), "failed": failed, "unseen": unseen,
                 "secs": round(time.time() - t0, 1)}


with ThreadPoolExecutor(4) as ex:
    for cid, r in ex.map(one, [(c, b) for c, b in sorted(bets.items()) if b["left_out"]]):
        report[cid] = r
        print(cid, r, flush=True)
json.dump(report, open(out_path, "w"), indent=1)
bad = [c for c, r in report.items() if r["failed"]]
print("BETS CHECKED:", len(report), "WRONG:", len(bad), bad)
