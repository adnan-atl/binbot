"""Worker process: runs the monitor jobs on a schedule. `python -m app.workers.main`."""
import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from app.core.db import session_factory
from app.services import daily_report
from app.services.reminders import purge_unrecognised, send_fallbacks, share_nudges, signup_nudges
from app.services.retention import purge_inactive
from app.workers import monitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full request URLs at INFO, API keys in query strings included: warnings only.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)
log = logging.getLogger("worker")


def _with_db(fn):
    def run():
        db = session_factory()()
        try:
            log.info("%s -> %s", fn.__name__, fn(db))
        except Exception:
            log.exception("%s failed", fn.__name__)
        finally:
            db.close()
    return run


def main():
    from app.core.errors import init_errors

    init_errors("worker")
    sched = BlockingScheduler(timezone="Europe/London")
    sched.add_job(_with_db(monitor.dispatch), "interval", minutes=1, id="dispatch")
    sched.add_job(_with_db(monitor.refresh_stale), "interval", hours=1, id="refresh")
    sched.add_job(_with_db(monitor.health_check), "cron", hour=4, minute=15, id="health")
    sched.add_job(_with_db(monitor.resolve_disputes), "cron", hour=4, minute=45, id="disputes")
    sched.add_job(_with_db(purge_unrecognised), "cron", hour=5, minute=5, id="purge")
    # The day's growth report to #binjamin-analytics, as the day ends (London)
    sched.add_job(_with_db(daily_report.run), "cron", hour=0, minute=0, id="daily_report",
                  misfire_grace_time=3600, coalesce=True)  # still sent if the worker was busy
    sched.add_job(_with_db(purge_inactive), "cron", hour=5, minute=10, id="retention")
    # Sunday evening: people are messaging and planning the week (services/reminders.share_nudges)
    sched.add_job(_with_db(share_nudges), "cron", day_of_week="sun", hour=19, minute=30, id="share")
    sched.add_job(_with_db(signup_nudges), "interval", minutes=3, id="signup-nudge")
    sched.add_job(_with_db(send_fallbacks), "interval", seconds=15, id="fallbacks")
    log.info("worker up")
    sched.start()


if __name__ == "__main__":
    main()
