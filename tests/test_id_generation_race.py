"""Regression test for the ID-generation race in create_issue_with_new_id /
create_project_with_new_id / create_task (task #24). Originally verified with
a 20-thread concurrent stress test showing 4/20 failures before the fix
(5 retries, no jitter) and 0/20 after (25 retries + jitter)."""
import threading


def test_concurrent_issue_creation_never_collides(ws_db):
    errors = []
    created_ids = []
    lock = threading.Lock()

    def worker():
        try:
            iid = ws_db.create_issue_with_new_id(title="race test", state="active", category="other")
            with lock:
                created_ids.append(iid)
        except Exception as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert errors == [], f"FAIL: {len(errors)} threads raised errors under concurrency: {errors[:3]}"
    assert len(created_ids) == 20
    assert len(set(created_ids)) == 20, "FAIL: two threads got the same issue id"
