"""Parse web-font family declarations from a page's <head>.

The Resemblio extractor previously sent raw HTML to the LLM and asked it
to reason about which fonts were loaded. On pages where the family name
appears ONLY in a `<link rel="stylesheet">` URL or behind a CSS custom
property (the Susann pathology), the LLM either hallucinated a familiar
fallback (Georgia) or returned a generic system-stack default.

This module is the deterministic pre-LLM pass that closes the
"missed <head> font-link declarations" diagnostic class from the R3.1
Phase A probe. It supports the short list of major web-font CDNs that
cover the vast majority of modern sites:

- fonts.googleapis.com (Google Fonts)
- fonts.bunny.net (Bunny Fonts, Google-compatible)
- use.typekit.net (Adobe Typekit)
- api.fontshare.com (Fontshare)

Plus inline `@font-face { font-family: ... }` declarations in any
`<style>` block.

The module is pure-data: HTML string in, structured `LoadedFonts` out.
No network. No DOM. Trivially unit-testable.

Throwaway: NO. Quality floor applies. Tests in tests/test_font_link_parser.py.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import TypedDict

# Hosts whose stylesheet URLs encode font families in a `family=` query param.
# Google Fonts and Bunny Fonts share the same `family=Name:wght@400` syntax.
GOOGLE_LIKE_HOSTS: frozenset[str] = frozenset({
    "fonts.googleapis.com",
    "fonts.bunny.net",
})
"""CDN hosts using Google-Fonts-style `family=` URL parameters."""

# Typekit URLs (use.typekit.net/<kit>.css) do not expose family names in
# the URL; we record the kit-id as a marker and rely on the LLM to map
# it. Fontshare uses `api.fontshare.com/v2/css?f[]=Name`.
TYPEKIT_HOST = "use.typekit.net"
FONTSHARE_HOST = "api.fontshare.com"

# Pull <link rel="stylesheet" href="..."> from <head>. We do not require a
# specific attribute order; `rel` may appear before or after `href`.
_LINK_RE = re.compile(
    r"<link\b(?P<attrs>[^>]*?)/?>",
    re.IGNORECASE,
)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_REL_RE = re.compile(r"""rel\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

# Pull every <style>...</style> block so we can scan for @font-face.
_STYLE_BLOCK_RE = re.compile(
    r"<style\b[^>]*>(?P<body>.*?)</style>",
    re.IGNORECASE | re.DOTALL,
)
_FONT_FACE_RE = re.compile(
    r"@font-face\s*\{[^}]*?font-family\s*:\s*(?P<family>[^;}]+)",
    re.IGNORECASE | re.DOTALL,
)

# Strip the first <head>...</head> block; if no <head> tag we fall back to
# the whole document (single-page HTML sometimes omits <head>).
_HEAD_RE = re.compile(r"<head\b[^>]*>(?P<body>.*?)</head>", re.IGNORECASE | re.DOTALL)


class LoadedFont(TypedDict):
    """A single web-font family detected on the page.

    Fields:
    - family: the family name as it appeared in the source ("Anton",
      "Playfair Display", "Inter"). Unquoted; no weight suffix.
    - source: how it was discovered. One of "google", "bunny", "typekit",
      "fontshare", "font-face".
    - raw: the raw URL or selector substring that yielded this family,
      kept so downstream tooling can audit the decision.
    """

    family: str
    source: str
    raw: str


class LoadedFonts(TypedDict):
    """Aggregate output of `parse_loaded_fonts`.

    Fields:
    - families: deduplicated list of family names in discovery order.
      Use this when you want a flat "fonts loaded on this page" list.
    - entries: every individual detection, with source + raw URL. Use
      this when you need provenance per detection.
    - schema_version: bumped if the shape changes.
    """

    families: list[str]
    entries: list[LoadedFont]
    schema_version: int


SCHEMA_VERSION = 1


def parse_loaded_fonts(html: str) -> LoadedFonts:
    """Scan `<head>` for web-font declarations; return families + entries.

    The function is total: any unparseable input returns an empty result
    with `schema_version` set. Never raises.

    Behaviour:
    - Restrict scanning to the first `<head>` block when present; fall
      back to the whole document otherwise.
    - For each `<link>` whose `rel` contains "stylesheet" (case-insensitive),
      classify by host: Google-like, Typekit, Fontshare. Extract families.
    - Scan `<style>` blocks for `@font-face { font-family: ... }`.
    - Deduplicate families while preserving first-seen order.

    Edge cases handled:
    - Missing `<head>` tag: scans the whole document.
    - `<link>` without `href`: skipped.
    - `<link>` whose `rel` is exactly "preconnect" or "preload": ignored
      (we want stylesheet links only).
    - URL-encoded family names: decoded before recording.
    - "+": treated as a space (Google Fonts uses "+" for spaces).
    - Multiple families per URL (`family=Foo&family=Bar` and
      `family=Foo|Bar`): all extracted.
    """
    if not isinstance(html, str) or not html:
        return LoadedFonts(families=[], entries=[], schema_version=SCHEMA_VERSION)

    head_match = _HEAD_RE.search(html)
    scope = head_match.group("body") if head_match else html

    entries: list[LoadedFont] = []
    for link_match in _LINK_RE.finditer(scope):
        attrs = link_match.group("attrs")
        rel_match = _REL_RE.search(attrs)
        if rel_match is None:
            continue
        if "stylesheet" not in rel_match.group(1).lower():
            continue
        href_match = _HREF_RE.search(attrs)
        if href_match is None:
            continue
        href = href_match.group(1).strip()
        entries.extend(_classify_link(href))

    for style_match in _STYLE_BLOCK_RE.finditer(scope):
        body = style_match.group("body") or ""
        for face_match in _FONT_FACE_RE.finditer(body):
            family_raw = face_match.group("family").strip()
            family = _clean_family_token(family_raw)
            if family:
                entries.append(LoadedFont(family=family, source="font-face", raw=family_raw))

    families: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        key = entry["family"].lower()
        if key in seen:
            continue
        seen.add(key)
        families.append(entry["family"])

    return LoadedFonts(families=families, entries=entries, schema_version=SCHEMA_VERSION)


def _classify_link(href: str) -> list[LoadedFont]:
    """Return zero or more LoadedFont entries for one stylesheet href."""
    try:
        parsed = urllib.parse.urlparse(href if "://" in href else f"https:{href}" if href.startswith("//") else href)
    except ValueError:
        return []
    host = (parsed.netloc or "").lower()
    if host in GOOGLE_LIKE_HOSTS:
        source = "google" if host == "fonts.googleapis.com" else "bunny"
        return [LoadedFont(family=fam, source=source, raw=href) for fam in _families_from_google_query(parsed.query)]
    if host == TYPEKIT_HOST:
        # Typekit URLs do not expose family names; record the kit id as a
        # marker so the LLM has the SIGNAL that Typekit is in use without
        # us inventing family names we cannot prove.
        kit_id = parsed.path.rsplit("/", 1)[-1].split(".")[0]
        if kit_id:
            return [LoadedFont(family=f"typekit:{kit_id}", source="typekit", raw=href)]
        return []
    if host == FONTSHARE_HOST:
        return [LoadedFont(family=fam, source="fontshare", raw=href) for fam in _families_from_fontshare_query(parsed.query)]
    return []


def _families_from_google_query(query: str) -> list[str]:
    """Extract every family name from a Google/Bunny `?family=...` query.

    Google Fonts supports two URL shapes:
    - `?family=Foo+Bar&family=Baz` (CSS2 API; one family per `family=`)
    - `?family=Foo|Bar|Baz` (CSS1 API; pipe-separated)

    Weight specs follow a colon (`Foo:wght@400;500`) and are stripped.
    """
    families: list[str] = []
    for key, value in urllib.parse.parse_qsl(query, keep_blank_values=False):
        if key != "family":
            continue
        # CSS1 used pipe-separated families in a single param.
        for segment in value.split("|"):
            family_part = segment.split(":", 1)[0]
            cleaned = _clean_family_token(family_part)
            if cleaned:
                families.append(cleaned)
    return families


def _families_from_fontshare_query(query: str) -> list[str]:
    """Extract family names from `api.fontshare.com/v2/css?f[]=Name&f[]=Other`."""
    families: list[str] = []
    for key, value in urllib.parse.parse_qsl(query, keep_blank_values=False):
        if key not in {"f", "f[]"}:
            continue
        family_part = value.split("@", 1)[0]
        cleaned = _clean_family_token(family_part)
        if cleaned:
            families.append(cleaned)
    return families


def _clean_family_token(raw: str) -> str:
    """Normalise a raw family token: strip quotes, decode "+", trim whitespace.

    Google Fonts encodes spaces as "+"; CSS `font-family` declarations use
    quoted multi-word names. We normalise to the bare, human-readable form
    ("Playfair Display", "Open Sans") so downstream comparisons work.
    """
    text = raw.strip()
    if not text:
        return ""
    text = urllib.parse.unquote(text)
    text = text.replace("+", " ")
    text = text.strip().strip("'").strip('"').strip()
    # Collapse internal whitespace runs.
    text = re.sub(r"\s+", " ", text)
    return text


def render_for_prompt(loaded: LoadedFonts) -> str:
    """Render a LoadedFonts result as a short Markdown block for the LLM prompt.

    Empty input returns an empty string so the caller can omit the section
    entirely rather than emit a misleading "no fonts detected" claim.
    """
    if not loaded["families"]:
        return ""
    lines = ["Detected web fonts (loaded via <head> link or @font-face):"]
    for entry in loaded["entries"]:
        lines.append(f"- {entry['family']} (source: {entry['source']})")
    return "\n".join(lines)
