"""Build time: make sure br/maps/map.json.gz and br/maps/cpu.json exist.

Committed files are used as they are. Otherwise the whole suite is run in
4 shards (the build has 4 cores): once traced by mapper_plugin for the map,
once plain under cpu_plugin for what each test costs to run here.
"""
import gzip, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = sys.argv[1] if len(sys.argv) > 1 else "/app/sqlglot"
MAPS = os.path.join(HERE, "maps")
OUT = os.path.join(MAPS, "map.json.gz")
CPU_OUT = os.path.join(MAPS, "cpu.json")
SHARDS = int(os.environ.get("MAP_SHARDS", "4"))
os.makedirs(MAPS, exist_ok=True)

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
env = dict(os.environ, SKIP_INTEGRATION="0", MAP_ROOT=REPO,
           PYTHONPATH=REPO + os.pathsep + HERE, OPENBLAS_NUM_THREADS="1")


def run_shards(plugin, outvar, tag):
    t0 = time.time()
    procs = []
    for i, sh in enumerate(shards):
        e = dict(env, **{outvar: "/tmp/%s%d.json" % (tag, i)})
        log = open("/tmp/%slog%d.txt" % (tag, i), "w")
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", plugin,
             "-o", "console_output_style=classic", "--tb=no"] + sh,
            cwd=REPO, env=e, stdout=log, stderr=subprocess.STDOUT))
    for p in procs:
        p.wait()
    merged = {}
    for i in range(SHARDS):
        try:
            with open("/tmp/%slog%d.txt" % (tag, i)) as f:
                tail = f.read().strip().splitlines()[-1:]
            print(tag, "shard", i, tail, file=sys.stderr)
            merged.update(json.load(open("/tmp/%s%d.json" % (tag, i))))
        except Exception as e:  # noqa: BLE001
            print(tag, "shard", i, "failed:", e, file=sys.stderr)
    print("%s done in %.0fs, %d records" % (tag, time.time() - t0, len(merged)), file=sys.stderr)
    return merged


if not os.path.exists(OUT):
    merged = run_shards("mapper_plugin", "MAP_OUT", "map")
    # anything the trace lost comes from the committed local map
    try:
        with gzip.open(os.path.join(HERE, "map_local.json.gz"), "rt") as f:
            local = json.load(f)
        added = 0
        for k, v in local.items():
            if k not in merged:
                merged[k] = v
                added += 1
        print("filled", added, "records from map_local", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print("no local map:", e, file=sys.stderr)
    with gzip.open(OUT, "wt", compresslevel=6) as f:
        json.dump(merged, f, separators=(",", ":"))
else:
    print("using committed map", OUT, file=sys.stderr)

if not os.path.exists(CPU_OUT):
    cpu = run_shards("cpu_plugin", "CPU_OUT", "cpu")
    with open(CPU_OUT, "w") as f:
        json.dump(cpu, f)
else:
    print("using committed cpu costs", CPU_OUT, file=sys.stderr)
