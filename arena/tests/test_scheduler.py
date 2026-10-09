"""Phase 5: the scheduler's timing rules (pure function, fake clock)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from arena.prices import ET
from arena.scheduler import MAX_ATTEMPTS, RETRY_AFTER, due_jobs

UTC = ZoneInfo("UTC")

# A slice of the real NYSE 2024 calendar: Good Friday (Mar 29) closed, July 3
# half day, DST starts Mar 10.
CAL = {d: "16:00" for d in [
    "2024-03-07", "2024-03-08", "2024-03-11", "2024-03-12",
    "2024-03-25", "2024-03-26", "2024-03-27", "2024-03-28", "2024-04-01",
    "2024-07-01", "2024-07-02", "2024-07-05",
]}
CAL["2024-07-03"] = "13:00"


def at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=ET)


def names(now, history=None, started=True, cal=CAL):
    return [(j.name, j.key) for j in due_jobs(at(now), cal, history or {}, started)]


def test_execute_runs_at_0950_eastern_only_on_trading_days():
    friday_done = {("daily", "2024-03-08"): [{"exit_code": 0, "started_at": at("2024-03-08 16:45")}]}
    assert names("2024-03-11 09:49", friday_done) == []
    assert names("2024-03-11 09:50", friday_done) == [("execute", "2024-03-11")]
    assert not any(j == "execute" for j, _ in names("2024-03-09 09:50"))   # Saturday


def test_dst_is_handled_by_the_eastern_clock():
    # Clocks went forward on Sun Mar 10 2024. 09:50 Eastern was 14:50 UTC on
    # Friday (EST) but 13:50 UTC on Monday (EDT).
    def utc_names(y, m, d, hh, mm):
        return [(j.name, j.key) for j in due_jobs(datetime(y, m, d, hh, mm, tzinfo=UTC), CAL, {}, True)]
    assert ("execute", "2024-03-08") not in utc_names(2024, 3, 8, 14, 49)
    assert ("execute", "2024-03-08") in utc_names(2024, 3, 8, 14, 50)
    assert ("execute", "2024-03-11") not in utc_names(2024, 3, 11, 13, 49)
    assert ("execute", "2024-03-11") in utc_names(2024, 3, 11, 13, 50)


def test_daily_runs_45_minutes_after_the_close_including_half_days():
    assert ("daily", "2024-03-11") not in names("2024-03-11 16:30")
    assert ("daily", "2024-03-11") in names("2024-03-11 16:45")
    assert ("daily", "2024-07-03") in names("2024-07-03 13:45")      # 1 pm close
    assert ("execute", "2024-07-03") not in names("2024-07-03 13:45")


def test_decide_runs_the_morning_after_the_weeks_last_close():
    assert names("2024-03-09 09:59") == [("daily", "2024-03-08")]   # catching up on Friday's daily
    assert ("decide", "2024-03-08") in names("2024-03-09 10:00")
    assert ("decide", "2024-03-08") in names("2024-03-10 20:00")     # still due on Sunday
    assert ("decide", "2024-03-08") not in names("2024-03-11 09:30")  # too late once Monday opens


def test_holiday_week_decides_after_thursday():
    # Good Friday: the week's last trading day is Thursday Mar 28.
    assert ("decide", "2024-03-28") in names("2024-03-29 10:00")
    assert ("execute", "2024-03-29") not in names("2024-03-29 10:00")


def test_successful_jobs_never_rerun_and_failures_retry_a_few_times():
    ok = {("execute", "2024-03-11"): [{"exit_code": 0, "started_at": at("2024-03-11 09:50")}]}
    assert ("execute", "2024-03-11") not in names("2024-03-11 10:30", ok)
    failed = {("execute", "2024-03-11"): [{"exit_code": 1, "started_at": at("2024-03-11 09:50")}]}
    assert ("execute", "2024-03-11") not in names("2024-03-11 10:00", failed)       # wait before retrying
    assert ("execute", "2024-03-11") in names(str(at("2024-03-11 09:50") + RETRY_AFTER)[:16], failed)
    gave_up = {("execute", "2024-03-11"): [{"exit_code": 1, "started_at": at("2024-03-11 09:50")}] * MAX_ATTEMPTS}
    assert ("execute", "2024-03-11") not in names("2024-03-11 15:00", gave_up)


def test_catch_up_after_downtime_runs_jobs_in_a_safe_order():
    # Scheduler was off from Friday afternoon until Monday 11:00.
    jobs = names("2024-03-11 11:00")
    assert jobs[:2] == [("daily", "2024-03-08"), ("execute", "2024-03-11")]


def test_nothing_but_calendar_upkeep_before_kickoff():
    assert names("2024-03-11 09:50", started=False) == []
    assert names("2024-03-11 09:50", started=False, cal={"2024-03-01": "16:00"}) == [("update-calendar", "missing-2024-03-11")]
    sunday = names("2024-03-10 12:00")
    assert ("update-calendar", "2024-W10") in sunday


def test_catch_up_does_not_replay_old_days():
    stale = {"2024-03-01": "16:00", "2024-03-11": "16:00"}
    assert ("daily", "2024-03-01") not in names("2024-03-11 08:00", cal=stale)   # 10 days back: too old


def test_jobs_run_against_the_schedulers_database():
    from arena.scheduler import Job
    argv = Job("daily", "2024-03-11", "2024-03-11").argv("D:/league/arena.db")
    assert argv[-4:] == ["--db", "D:/league/arena.db", "--date", "2024-03-11"]


def test_only_one_scheduler_per_league(tmp_path):
    import subprocess
    import sys
    from arena.scheduler import AlreadyRunning, single_instance_lock
    db = tmp_path / "arena.db"
    lock = single_instance_lock(db)
    # A second process must be refused while the first holds the lock.
    code = ("import sys; from arena.scheduler import single_instance_lock, AlreadyRunning\n"
            "try:\n    single_instance_lock(sys.argv[1]); print('GOT LOCK')\n"
            "except AlreadyRunning: print('REFUSED')")
    out = subprocess.run([sys.executable, "-c", code, str(db)], capture_output=True, text=True).stdout
    assert "REFUSED" in out
    lock.close()
    out = subprocess.run([sys.executable, "-c", code, str(db)], capture_output=True, text=True).stdout
    assert "GOT LOCK" in out          # released when the holder lets go
