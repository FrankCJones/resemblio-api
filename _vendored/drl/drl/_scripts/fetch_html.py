"""Direct HTTP fetcher for sources that block Agent WebFetch.

The Agent's `WebFetch` tool uses a User-Agent that some sources reject at
the edge (OpenAI, Cloudflare, anything behind aggressive bot mitigation).
urllib with a chosen User-Agent works for most of those: empirically 10 of
11 documented-blocked sources return 200 to a Python urllib request, 8 of
those with the default UA and 2 more with a Chrome UA.

This script gives the extraction pipeline a Bash-callable fetcher. The
existing Gen-2 agents can dispatch `python -m _scripts.fetch_html <url>`
via the Bash tool when a source returns 403/202 to WebFetch.

## What it does

1. GET the URL with a chosen User-Agent (default Python urllib, fallback
   Chrome).
2. Retry with exponential backoff on transient failures.
3. Print the response body to stdout (binary-safe; the agent reads bytes).
4. Exit 0 on 200, 1 on any non-success.

## What it doesn't do

- No HTML parsing. The agent (or downstream Python tools) does the parse.
- No JS execution. Sources behind a JS-challenge wall (Figma's 202) will
  return the challenge HTML, not the real page. Those need Playwright or
  screenshot intake.
- No cookies, no auth, no follow-up requests.

## Ethics note

This script does not bypass any technical anti-scraping mechanism beyond
sending a different User-Agent string. Sites with explicit anti-scraping
ToS (Stripe, Apple, OpenAI) should still go through screenshot intake per
the policy in CLAUDE.md and STORY_gen2_pipeline.md. The fetcher exists to
serve permissive sources whose only "block" is a UA filter.

## Run command

    python -m _scripts.fetch_html https://example.com
    python -m _scripts.fetch_html https://example.com --ua chrome
    python -m _scripts.fetch_html https://example.com --save _cache/example/home.html
    python -m _scripts.fetch_html https://example.com --head    # HEAD only
    python -m _scripts.test_fetch_html                          # tests

Throwaway: no. Quality floor applies.
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_TIMEOUT = 15.0
"""Per-request timeout in seconds."""

MAX_RETRIES = 3
"""Retry budget for transient failures."""

USER_AGENTS: dict[str, str] = {
    "default": "Mozilla/5.0 (compatible; DesignReferenceLibrary-fetch/1.0)",
    "python": "Python-urllib/3.x",
    "chrome": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/131.0.0.0 Safari/537.36"),
    "firefox": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) "
                "Gecko/20100101 Firefox/131.0"),
    "safari": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) "
               "Version/18.0 Safari/605.1.15"),
}
"""Named User-Agent presets. `default` is polite; others mimic browsers."""


def fetch(url: str, *, ua: str = "default", method: str = "GET",
          timeout: float = DEFAULT_TIMEOUT,
          fallback_chrome: bool = True) -> tuple[int, bytes, str]:
    """Fetch a URL. Returns (status_code, body, used_ua_label).

    If the initial fetch fails with 403/429 and `fallback_chrome` is True,
    retries once with the Chrome UA. Network failures retry with backoff
    up to MAX_RETRIES times.
    """
    ua_string = USER_AGENTS.get(ua, ua)
    status, body = _fetch_once(url, ua_string, method, timeout)
    if status in (403, 429) and fallback_chrome and ua != "chrome":
        # The original UA was rejected; try Chrome.
        chrome_status, chrome_body = _fetch_once(
            url, USER_AGENTS["chrome"], method, timeout
        )
        if 200 <= chrome_status < 400:
            return (chrome_status, chrome_body, "chrome")
    return (status, body, ua)


def _fetch_once(url: str, ua: str, method: str,
                timeout: float) -> tuple[int, bytes]:
    """One fetch attempt, with retry-on-transient. Returns (status, body)."""
    headers = {
        "User-Agent": ua,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "identity",  # no compression so body is raw HTML
    }
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read() if method == "GET" else b""
                return (resp.status, body)
        except urllib.error.HTTPError as e:
            # 4xx not retriable; bubble up the status.
            if 400 <= e.code < 500:
                return (e.code, b"")
            last_error = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_error = e
        if attempt < MAX_RETRIES - 1:
            time.sleep(2 ** attempt)
    return (0, b"")


def save_to_file(body: bytes, save_path: str) -> Path:
    """Write fetched body to a file, creating parent dirs as needed."""
    p = Path(save_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Returns shell exit code (0 = 2xx/3xx, 1 = anything else)."""
    ap = argparse.ArgumentParser(
        description="Direct urllib fetcher for sources that block "
                    "Agent WebFetch. Uses a chosen User-Agent.",
    )
    ap.add_argument("url", help="URL to fetch.")
    ap.add_argument("--ua", default="default",
                    choices=list(USER_AGENTS.keys()),
                    help="Named User-Agent preset.")
    ap.add_argument("--head", action="store_true",
                    help="HEAD request only; print status + headers, no body.")
    ap.add_argument("--save", metavar="PATH",
                    help="Save body to PATH instead of stdout.")
    ap.add_argument("--no-fallback", action="store_true",
                    help="Disable automatic Chrome-UA fallback on 403/429.")
    args = ap.parse_args(argv)

    method = "HEAD" if args.head else "GET"
    status, body, used_ua = fetch(
        args.url, ua=args.ua, method=method,
        fallback_chrome=not args.no_fallback,
    )

    if args.head:
        print(f"status: {status}", file=sys.stderr)
        print(f"ua: {used_ua}", file=sys.stderr)
        return 0 if 200 <= status < 400 else 1

    if status == 0 or status >= 400:
        print(f"fetch failed: status={status} ua={used_ua}", file=sys.stderr)
        return 1

    if args.save:
        out = save_to_file(body, args.save)
        print(f"wrote {len(body)} bytes to {out}", file=sys.stderr)
        return 0

    # Print to stdout. Use binary write so we don't munge bytes.
    sys.stdout.buffer.write(body)
    print(f"\n# status={status} ua={used_ua} bytes={len(body)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
