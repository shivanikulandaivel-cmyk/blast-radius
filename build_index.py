"""Build time: map.json(.gz) + pinned sqlglot -> index.pkl"""
import gzip, json, pickle, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from candidates import Index
src, root, out = sys.argv[1], sys.argv[2], sys.argv[3]
op = gzip.open if src.endswith(".gz") else open
with op(src, "rt") as f:
    m = json.load(f)
idx = Index(m, root)
from candidates import load_components, load_cpu
load_components(idx, root)
print("component owners:", len(getattr(idx, "component_owners", {})), file=sys.stderr)
cpu_path = os.path.join(os.path.dirname(src), "cpu.json")
load_cpu(idx, cpu_path)
print("cpu costs for %d tests" % len(idx.cpu), file=sys.stderr)
with open(out, "wb") as f:
    pickle.dump(idx, f, protocol=pickle.HIGHEST_PROTOCOL)
print("index: %d tests, %d files" % (idx.n, len(idx.lines)), file=sys.stderr)
