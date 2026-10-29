"""Fake `glab api PATH [--hostname H] [--paginate]` serving canned JSON from $FAKE_GLAB_DIR.

  merge_requests?scope=all...          -> queue.json
  projects/P/merge_requests/I/approvals    -> appr_P_I.json   (default: nobody approved)
  projects/P/merge_requests/I/discussions  -> disc_P_I.json   (default: [])
  user                                 -> user.json
A file named fail_<name> makes that lookup exit 1, to simulate a GitLab outage.
"""
import json
import os
import re
import sys
from pathlib import Path

fake = Path(os.environ["FAKE_GLAB_DIR"])
path = sys.argv[2]  # argv: api PATH --hostname H [--paginate]
with (fake / "calls.log").open("a") as f:
    f.write(path + "\n")

if "-X" in sys.argv:
    method = sys.argv[sys.argv.index("-X") + 1]
    body = Path(sys.argv[sys.argv.index("--input") + 1]).read_text()
    with (fake / "writes.jsonl").open("a") as f:
        f.write(json.dumps({"method": method, "path": path, "body": json.loads(body)}) + "\n")
    print("{}")
    sys.exit(0)

m = re.match(r"projects/(\d+)/merge_requests/(\d+)/(approvals|discussions)$", path.split("?")[0])
dn = re.match(r"projects/(\d+)/merge_requests/(\d+)/draft_notes$", path)
d = re.match(r"projects/(\d+)/merge_requests/(\d+)/discussions/(\w+)$", path)
r = re.match(r"projects/(\d+)/merge_requests/(\d+)$", path)
if dn:
    name, default = f"drafts_{dn[1]}_{dn[2]}.json", "[]"
elif d:
    name, default = f"thread_{d[3]}.json", "null"
elif r:
    name, default = f"mr_{r[1]}_{r[2]}.json", "null"
elif path.startswith("merge_requests?"):
    name, default = "queue.json", "[]"
elif m:
    name = f"{'appr' if m[3] == 'approvals' else 'disc'}_{m[1]}_{m[2]}.json"
    default = '{"approved_by": []}' if m[3] == "approvals" else "[]"
elif path == "user":
    name, default = "user.json", '{"username": "me"}'
else:
    sys.exit(f"fake glab: unhandled {path}")

if (fake / f"fail_{name}").exists():
    print("500 Internal Server Error", file=sys.stderr)
    sys.exit(1)
f = fake / name
print(f.read_text() if f.exists() else default)
