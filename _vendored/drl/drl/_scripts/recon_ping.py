"""Step 0 reachability ping -the cheapest pre-wave gate.

A 30-second check before dispatching a 17-agent /dl system wave. Confirms
the source homepage actually serves real marketing content, not a site-down
failover page, an LLM stub, a regional geo-gate, or a 5xx.

The Patagonia outage that burned 12 agents would have cost 1 inline call
with this script.

## Run command

    python -m _scripts.recon_ping https://x.com
    python -m _scripts.recon_ping https://x.com --quiet  # only exit code
    python -m _scripts.recon_ping --inner https://x.com/pricing https://x.com

## Exit codes

- 0 = homepage live, real marketing content
- 1 = unreachable, stub, or failover detected
- 2 = bad arguments

Throwaway: no. Quality floor applies.
"""
from __future__ import annotations

import argparse
import sys

from _scripts.recon import probe


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Returns shell exit code."""
    ap = argparse.ArgumentParser(
        description="Step 0 reachability ping for /dl system waves.",
    )
    ap.add_argument("url", help="Homepage URL to probe.")
    ap.add_argument("--inner", action="append", default=[],
                    help="Additional inner URL to probe (repeatable). "
                         "All probes must pass for exit 0.")
    ap.add_argument("--quiet", action="store_true",
                    help="Print nothing; signal via exit code only.")
    args = ap.parse_args(argv)

    failed = False
    targets = [args.url] + list(args.inner)
    for u in targets:
        r = probe(u)
        ok = (
            200 <= r["status_code"] < 400
            and not r["is_stub"]
        )
        if not args.quiet:
            tag = "OK" if ok else "FAIL"
            stub = " (stub)" if r["is_stub"] else ""
            print(f"  [{tag}] {u}: status={r['status_code']}{stub}")
        if not ok:
            failed = True

    if failed:
        if not args.quiet:
            print("RESULT: BLOCKED -do not dispatch the wave.")
        return 1
    if not args.quiet:
        print("RESULT: LIVE -safe to dispatch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
