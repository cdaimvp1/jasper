"""Helper script spawned as a real separate OS process by
test_multiprocess_concurrency.py - not importable as a normal test module
(no test_ prefix), just a subprocess target."""
import json
import os
import sys
import time

BODY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BODY)

os.environ["TEAM_DATA_DIR"] = sys.argv[2]
os.environ["TEAM_WORKSPACE_ROOT"] = sys.argv[2]
os.environ["TEAM_CONFIG_DIR"] = sys.argv[2]

import workgraph_store as ws
import bus

worker_id = sys.argv[1]
duration_s = float(sys.argv[3])
ws.init_workgraph()
bus.init_bus()

created_issues = []
errors = []
t_end = time.time() + duration_s

while time.time() < t_end:
    try:
        iid = ws.create_issue_with_new_id(
            title=f"stress-{worker_id}-{len(created_issues)}", state="active", category="other")
        created_issues.append(iid)
        bus.emit_event(source="stress", kind="stress.write", actor=worker_id, target=iid, payload={})
    except Exception as e:
        errors.append(repr(e))

print(json.dumps({"worker_id": worker_id, "created": created_issues, "errors": errors}))
