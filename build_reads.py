"""Build time: make sure br/maps/reads.json.gz exists (which class settings
each test reads, see reads_plugin.py). A committed file is used as it is;
otherwise the suite runs once in 4 shards under reads_plugin."""
import gzip, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = sys.argv[1] if len(sys.argv) > 1 else "/app/sqlglot"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "maps", "reads.json.gz")
SHARDS = int(os.environ.get("MAP_SHARDS", "4"))

if os.path.exists(OUT) and not os.environ.get("READS_FORCE"):
    sys.exit(0)
durs = json.load(open(os.path.join(HERE, "durations.json")))
per_file = {}
for tid, s in durs.items():
    f = tid.split("::")[0]
    per_file[f] = per_file.get(f, 0) + s
files = sorted(
    {os.path.relpath(os.path.join(dp, f), REPO)
     for dp, _, fs in os.walk(os.path.join(REPO, "tests"))
     for f in fs if f.startswith("test_") and f.endswith(".py")},
    key=lambda f: -per_file.get(f, 0.05))
shards = [[] for _ in range(SHARDS)]
loads = [0.0] * SHARDS
for f in files:
    i = loads.index(min(loads))
    shards[i].append(f)
    loads[i] += per_file.get(f, 0.05)
env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=REPO + os.pathsep + HERE, OPENBLAS_NUM_THREADS="1")
t0 = time.time()
procs = []
for i, sh in enumerate(shards):
    e = dict(env, READS_OUT="/tmp/reads%d.json" % i)
    log = open("/tmp/readslog%d.txt" % i, "w")
    procs.append(subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "reads_plugin",
         "-o", "console_output_style=classic", "--tb=no"] + sh,
        cwd=REPO, env=e, stdout=log, stderr=subprocess.STDOUT))
for p in procs:
    p.wait()
keys, kidx, tests, failed, ok = [], {}, {}, set(), True
for i in range(SHARDS):
    try:
        tail = open("/tmp/readslog%d.txt" % i).read().strip().splitlines()[-1:]
        print("reads shard", i, tail, file=sys.stderr)
        d = json.load(open("/tmp/reads%d.json" % i))
    except Exception as e:  # noqa: BLE001
        print("reads shard", i, "failed:", e, file=sys.stderr)
        ok = False
        continue
    failed |= set(d["failed"])
    for scope, idxs in d["reads"].items():
        s = tests.setdefault(scope, set())
        for j in idxs:
            k = d["keys"][j]
            if k not in kidx:
                kidx[k] = len(keys)
                keys.append(k)
            s.add(kidx[k])
    for k in d["keys"]:
        if k not in kidx:
            kidx[k] = len(keys)
            keys.append(k)
if not ok:
    sys.exit(0)  # no file: the selector keeps its usual analysis
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with gzip.open(OUT, "wt") as f:
    json.dump({"keys": keys, "failed": sorted(failed),
               "reads": {k: sorted(v) for k, v in tests.items()}}, f, separators=(",", ":"))
print("reads done in %.0fs: %d settings, %d scopes, %d failed, %d bytes" % (
    time.time() - t0, len(keys), len(tests), len(failed), os.path.getsize(OUT)), file=sys.stderr)
