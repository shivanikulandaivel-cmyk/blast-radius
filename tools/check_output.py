"""CI only: the output follows the rules. Usage: check_output.py <tests.json> <commits dir> <output.jsonl>"""
import json, os, sys

tests = json.load(open(sys.argv[1]))["tests"]
valid = {t["number"] for t in tests}
commits = {f[:-6] for f in os.listdir(sys.argv[2]) if f.endswith(".patch")}
seen, errs = set(), []
for n, line in enumerate(open(sys.argv[3]).read().splitlines(), 1):
    try:
        r = json.loads(line)
    except Exception as e:  # noqa: BLE001
        errs.append("line %d not JSON: %r" % (n, e)); continue
    if set(r) != {"commit", "workers"}:
        errs.append("line %d keys %s" % (n, sorted(r)))
    c, ws = r.get("commit"), r.get("workers")
    if c in seen:
        errs.append("duplicate commit " + str(c))
    seen.add(c)
    if not isinstance(ws, list) or len(ws) > 8:
        errs.append("%s: %s workers" % (c, len(ws) if isinstance(ws, list) else ws))
        continue
    flat = [t for w in ws for t in w]
    if len(flat) != len(set(flat)):
        errs.append("%s: duplicate test numbers" % c)
    if set(flat) - valid:
        errs.append("%s: unknown tests" % c)
    if any(not w for w in ws):
        errs.append("%s: empty worker" % c)
if seen != commits:
    errs.append("commits missing %d extra %d" % (len(commits - seen), len(seen - commits)))
size = os.path.getsize(sys.argv[3])
print("OUTPUT CHECK:", "OK" if not errs else "PROBLEMS", "| lines %d | bytes %d | max workers %d" % (
    len(seen), size, max((len(json.loads(l)["workers"]) for l in open(sys.argv[3]) if l.strip()), default=0)))
for e in errs[:20]:
    print(" ", e)
