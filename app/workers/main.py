"""Worker process: runs the monitor jobs on a schedule. `python -m app.workers.main`."""
import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from app.core.db import session_factory
from app.workers import monitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
    sched = BlockingScheduler(timezone="Europe/London")
    sched.add_job(_with_db(monitor.dispatch), "interval", minutes=1, id="dispatch")
    sched.add_job(_with_db(monitor.refresh_stale), "interval", hours=1, id="refresh")
    sched.add_job(_with_db(monitor.health_check), "cron", hour=4, minute=15, id="health")
    sched.add_job(_with_db(monitor.resolve_disputes), "cron", hour=4, minute=45, id="disputes")
    log.info("worker up")
    sched.start()


if __name__ == "__main__":
    main()
