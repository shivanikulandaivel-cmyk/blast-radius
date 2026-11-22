"""Build time: fetch sqlglot at the pinned commit into /app/sqlglot."""
import io, os, sys, tarfile, time, urllib.request
SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = "https://github.com/tobymao/sqlglot/archive/%s.tar.gz" % SHA
dest = sys.argv[1] if len(sys.argv) > 1 else "/app/sqlglot"
for attempt in range(5):
    try:
        data = urllib.request.urlopen(URL, timeout=120).read()
        break
    except Exception as e:
        print("fetch failed:", e, file=sys.stderr)
        time.sleep(5 * (attempt + 1))
else:
    sys.exit(1)
with tarfile.open(fileobj=io.BytesIO(data)) as tf:
    top = tf.getmembers()[0].name.split("/")[0]
    tf.extractall("/tmp/sg", filter="fully_trusted")
os.rename("/tmp/sg/" + top, dest)
print("sqlglot at", dest, file=sys.stderr)
