"""Reminders due at the same time go out in parallel, and one failed send doesn't stop the rest."""
import threading
import time

from app.providers import messaging


class SlowProvider:
    name = "meta"

    def __init__(self):
        self.lock, self.live, self.peak = threading.Lock(), 0, 0

    def send_template(self, to_phone, name, lang, params, buttons):
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
        time.sleep(0.1)
        with self.lock:
            self.live -= 1
        if to_phone.endswith("13"):
            raise TimeoutError("read timed out")
        return messaging.SendResult(ok=True, message_id=f"wamid.{to_phone[-2:]}")


def test_batch_sends_in_parallel_and_keeps_order(db, monkeypatch):
    p = SlowProvider()
    monkeypatch.setattr(messaging, "get_provider", lambda: p)
    jobs = [(f"+4477009001{i:02d}", ["Recycling", "tomorrow", "Wed 4 Nov"], f"body {i}") for i in range(40)]
    start = time.monotonic()
    rows = messaging.send_template_batch(db, "bin_reminder", jobs, kind="reminder", buttons=True)
    assert time.monotonic() - start < 1.5  # 40 one-by-one would take 4s
    assert p.peak > 1 and p.peak <= messaging.SEND_CONCURRENCY
    assert [r.to_phone for r in rows] == [j[0] for j in jobs]
    assert [r.body for r in rows] == [j[2] for j in jobs]
    failed = [r for r in rows if r.error]
    assert [r.to_phone for r in failed] == ["+447700900113"] and "timed out" in failed[0].error
    assert rows[0].provider_message_id == "wamid.00"
