"""Child process for a proxied scrape (adapters/ukbcd._fetch_via_proxy): reads the spec and the
home as JSON on stdin, runs the scraper with HTTP(S)_PROXY already set in its environment, and
prints the result (or the error) as one line of JSON."""
import json
import sys

from app.adapters.base import AdapterError
from app.adapters.ukbcd import UKBCDAdapter
from app.scraper.wire import error_to_json, lookup_from_json, result_to_json, spec_from_json


def main() -> None:
    body = json.loads(sys.stdin.read())
    spec, lookup = spec_from_json(body["spec"]), lookup_from_json(body["lookup"])
    try:
        out = result_to_json(UKBCDAdapter(spec)._fetch_direct(lookup))
    except AdapterError as e:
        out = error_to_json(e)
    except Exception as e:  # report it like any scraper failure
        out = error_to_json(AdapterError(f"{spec.module}: {type(e).__name__}: {e}"))
    print(json.dumps(out))


if __name__ == "__main__":
    main()
