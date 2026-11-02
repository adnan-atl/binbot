"""httpx must not log request URLs at INFO: some providers take the API key in the query string."""
import importlib
import logging


def test_api_quietens_httpx():
    importlib.import_module("app.main")
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_worker_quietens_httpx():
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    importlib.reload(importlib.import_module("app.workers.main"))
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING
