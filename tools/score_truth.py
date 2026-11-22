"""Score a selector output against stored ground truth (tools/truth.json)."""
import json, sys
tests_path, truth_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
tests = json.load(open(tests_path))["tests"]
dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
truth = json.load(open(truth_path))["truth"]
sel = {}
for line in open(out_path):
    r = json.loads(line)
    sel[r["commit"]] = r["workers"]
total = misses = 0
rows = []
for c, tr in sorted(truth.items()):
    ws = sel.get(c, [])
    chosen = {t for w in ws for t in w}
    cost = (max(sum(dur[t] for t in w) for w in ws) + 2000 * len(ws)) if ws else 0
    miss = bool(set(tr["failing"]) - chosen)
    misses += miss
    total += cost + (300000 if miss else 0)
    orc = (max(dur[t] for t in tr["failing"]) + 2000) if tr["failing"] else 0
    if cost - orc > 500 or miss:
        rows.append("%s cost %.1f oracle %.1f%s" % (c, cost / 1000, orc / 1000, " MISS" if miss else ""))
print("\n".join(rows))
print("SIMULATED %.1f MISSES %d" % (total / 1000, misses))
