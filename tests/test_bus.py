"""Regression test for bus.py's busy_timeout (task #27) - bus.db is written
by every cohort worker's OWN process, not just threads in one process; the
default busy_timeout of 0 meant two workers posting at the same instant would
raise 'database is locked' immediately instead of one briefly waiting."""


def test_busy_timeout_is_set_on_every_connection(bus_db):
    conn = bus_db._connect()
    row = conn.execute("PRAGMA busy_timeout").fetchone()
    conn.close()
    assert row[0] >= 5000
