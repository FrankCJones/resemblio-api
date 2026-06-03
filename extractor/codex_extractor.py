"""Resemblio URL extractor glue.

The extractor validates reachability with DRL helpers, fetches homepage HTML,
asks one Sonnet call for a flat TokenSet, validates it, and optionally writes
the attempt to Postgres when RESEMBLIO_DB_URL is configured.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Literal, Mapping, TypedDict, cast

from extractor.computed_styles import (
    ComputedStyleReport,
    capture_computed_styles,
    empty_report as empty_computed_report,
    render_for_prompt as render_computed_styles_for_prompt,
)
from extractor.css_root_parser import (
    RootCustomProperties,
    parse_root_custom_properties,
    render_for_prompt as render_root_props_for_prompt,
)
from extractor.drl_adapter import (
    REQUIRED_TOKEN_KEYS,
    SCHEMA_VERSION,
    ResemblioExtractor,
    TokenSet,
    fetch_html,
    recon,
    recon_ping,
    to_dtcg_json,
    to_postgres_row,
    validate_token_set,
)
from extractor.font_link_parser import (
    LoadedFonts,
    parse_loaded_fonts,
    render_for_prompt as render_loaded_fonts_for_prompt,
)

MODEL_ID = "claude-sonnet-4-6"
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_HTML_CHARS = 48_000
MAX_API_TOKENS = 4096
REQUEST_TIMEOUT_SECONDS = 90
BACKOFF_SECONDS = (1.0, 2.0, 4.0)
TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
LITELLM_URL_VARS = ("LITELLM_API_BASE", "LITELLM_BASE_URL", "MODEL_GATEWAY_URL")
LITELLM_KEY_VARS = ("LITELLM_API_KEY", "MODEL_GATEWAY_API_KEY", "ANTHROPIC_API_KEY")
OPTIONAL_TOKEN_KEYS = tuple(k for k in TokenSet.__annotations__ if k not in REQUIRED_TOKEN_KEYS)
ALLOWED_TOKEN_KEYS = frozenset(TokenSet.__annotations__)

PROMPT_TEMPLATE = """You extract design tokens from fetched homepage HTML and inline CSS.

Return a single JSON object inside a ```json fenced code block. Do not include prose.
The JSON object's keys must be only valid TokenSet keys.

Required keys, all mandatory:
{required_keys_json}

Optional keys, include only when you can fill them confidently:
{optional_keys_json}

Rules:
- Every value must be a non-empty string.
- Prefer the GROUND-TRUTH SIGNALS sections (declared CSS custom properties, computed styles, detected web fonts) over the raw HTML when they conflict. Priority order: declared `:root` custom properties (brand intent) > computed styles (rendered artifact) > raw HTML inline CSS.
- When a declared `--*` custom property exists with a literal hex/rgb value that matches a brand role (e.g. `--ink`, `--bg`, `--background` -> bg slot; `--bone`, `--fg`, `--text`, `--foreground` -> text slot; `--sun`, `--accent`, `--brand`, `--primary` -> accent slot), prefer the custom-property value over any computed-style sample.
- For font_body and font_display, only use family names that appear in the detected-web-fonts block, in a declared `--*-font*` / `--type-*` / `--font-*` custom property, or in literal `font-family:` declarations in the source. Do not invent fallbacks (no Georgia, Times New Roman, Arial unless they are literally present).
- For color slots, prefer the rgb()/hex values from computed styles. The raw CSS may contain var() indirection that is resolved only at render time.
- Use plausible defaults only when a required slot is unspecified by ALL signals.
- Use CSS-ready strings: hex/rgb/hsl colors, CSS font-family stacks, px/rem/em sizes, numeric line heights, box-shadow strings, ms durations, and cubic-bezier values.
- Do not emit null, arrays, nested objects, comments, markdown outside the fenced JSON block, or unknown keys.

Source URL: {url}

{signals_block}Homepage HTML and inline CSS:
```html
{html}
```"""


class GatewayConfig(TypedDict):
    """Endpoint configuration for direct Anthropic or LiteLLM gateway calls."""

    mode: Literal["anthropic", "litellm"]
    endpoint: str
    api_key: str


class HttpResult(TypedDict):
    """Minimal HTTP response shape used by the API client."""

    status: int
    body: str


class TransientApiError(RuntimeError):
    """Retryable API boundary failure."""


class CodexExtractor(ResemblioExtractor):
    """Concrete extractor. Satisfies ResemblioExtractor structurally."""

    def __init__(self, model: str = MODEL_ID) -> None:
        """Create an extractor with the configured model id."""
        self.model = model

    def extract(self, url: str) -> tuple[TokenSet | None, str | None]:
        """Extract validated tokens and persist success or failure if configured."""
        tokens, error = self._extract_without_persist(url)
        if error is not None:
            insert_error = self._persist(url, None, "failed", error)
            return None, f"{error}; postgres insert failed: {insert_error}" if insert_error else error
        assert tokens is not None
        insert_error = self._persist(url, tokens, "ok", None)
        return (None, f"postgres insert failed: {insert_error}") if insert_error else (tokens, None)

    def _extract_without_persist(self, url: str) -> tuple[TokenSet | None, str | None]:
        normalized_url, reason = validate_http_url(url)
        if reason:
            return None, f"invalid url: {reason}"

        probe = recon_ping.probe(cast(str, normalized_url))
        status = int(probe.get("status_code", 0))
        if status < 200 or status >= 300 or probe.get("is_stub"):
            return None, f"unreachable: {json.dumps(probe, sort_keys=True)}"

        try:
            recon.recon(slug_from_url(cast(str, normalized_url)), cast(str, normalized_url))
        except Exception as exc:
            return None, f"recon failed: {exc}"

        fetch_status, body, used_ua = fetch_html.fetch(cast(str, normalized_url))
        if fetch_status < 200 or fetch_status >= 300 or not body:
            return None, f"fetch failed: status={fetch_status} ua={used_ua}"

        decoded_html = body.decode("utf-8", errors="replace")
        loaded_fonts = parse_loaded_fonts(decoded_html)
        root_props = parse_root_custom_properties(decoded_html)
        if os.environ.get("RESEMBLIO_DISABLE_BROWSER_PASS") == "1":
            computed_styles: ComputedStyleReport = empty_computed_report("skipped", "disabled by env")
        else:
            computed_styles = capture_computed_styles(html=decoded_html)

        try:
            reply = self._dispatch_anthropic(
                build_prompt(
                    cast(str, normalized_url),
                    html_context(body),
                    loaded_fonts=loaded_fonts,
                    computed_styles=computed_styles,
                    root_props=root_props,
                )
            )
            tokens = coerce_token_set(extract_json_object(reply))
            validate_token_set(tokens)
        except ValueError as exc:
            return None, f"model JSON parse failed: {exc}"
        except Exception as exc:
            message = str(exc)
            prefix = "validation failed" if exc.__class__.__name__ == "ExtractionValidationError" else "anthropic failed"
            return None, f"{prefix}: {message}"
        return tokens, None

    def _dispatch_anthropic(self, prompt: str) -> str:
        config = gateway_config()
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                result = call_litellm(config, self.model, prompt) if config["mode"] == "litellm" else call_anthropic(config, self.model, prompt)
                return response_text(result["body"], config["mode"])
            except TransientApiError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(BACKOFF_SECONDS[attempt])
        raise RuntimeError(f"anthropic request failed after retries: {last_error}")

    def _persist(self, url: str, token_set: TokenSet | None, status: str, error_log: str | None) -> str | None:
        db_url = os.environ.get("RESEMBLIO_DB_URL")
        if not db_url:
            return None
        row = to_postgres_row(url, token_set, status, error_log)
        try:
            import psycopg  # type: ignore[import-not-found]
            from psycopg.types.json import Json  # type: ignore[import-not-found]

            with psycopg.connect(db_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO extractions
                            (url, url_normalized, status, dtcg_json, error_log, schema_version)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            row["url"],
                            row["url_normalized"],
                            row["status"],
                            Json(row["dtcg_json"]) if row["dtcg_json"] is not None else None,
                            row["error_log"],
                            row["schema_version"],
                        ),
                    )
                conn.commit()
        except Exception as exc:
            return str(exc)
        return None


def validate_http_url(url: str) -> tuple[str | None, str | None]:
    """Accept only parseable http/https URLs with a host and no whitespace."""
    candidate = url.strip()
    if not candidate:
        return None, "blank"
    if any(ch.isspace() for ch in candidate):
        return None, "contains whitespace"
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        return None, "scheme must be http or https"
    if not parsed.netloc:
        return None, "missing host"
    return candidate, None


def slug_from_url(url: str) -> str:
    """Create a filesystem-safe recon slug from the URL host."""
    host = urllib.parse.urlparse(url).netloc.lower()
    return re.sub(r"[^a-z0-9]+", "-", host).strip("-") or "source"


def html_context(body: bytes) -> str:
    """Decode fetched HTML and cap it to the prompt budget."""
    text = body.decode("utf-8", errors="replace")
    if len(text) <= MAX_HTML_CHARS:
        return text
    head = MAX_HTML_CHARS * 2 // 3
    return text[:head] + "\n<!-- resemblio-truncated -->\n" + text[-(MAX_HTML_CHARS - head):]


def build_prompt(
    url: str,
    html: str,
    loaded_fonts: LoadedFonts | None = None,
    computed_styles: ComputedStyleReport | None = None,
    root_props: RootCustomProperties | None = None,
) -> str:
    """Build the single extraction prompt sent to Sonnet.

    `loaded_fonts`, `computed_styles`, and `root_props` are the structured
    pre-LLM signals produced by `parse_loaded_fonts` (Phase A),
    `capture_computed_styles` (Phase B), and `parse_root_custom_properties`
    (R3.2). When any is None or empty the prompt simply omits that block,
    leaving the LLM to reason from the raw HTML the way it always has.

    Block order matters: `root_props` goes FIRST because brand-declared
    `:root` custom properties outrank computed-style samples (intent
    beats artifact). Computed styles come next, then loaded web fonts.
    """
    signals_chunks: list[str] = []
    if root_props is not None:
        rendered = render_root_props_for_prompt(root_props)
        if rendered:
            signals_chunks.append(rendered)
    if computed_styles is not None:
        rendered = render_computed_styles_for_prompt(computed_styles)
        if rendered:
            signals_chunks.append(rendered)
    if loaded_fonts is not None:
        rendered = render_loaded_fonts_for_prompt(loaded_fonts)
        if rendered:
            signals_chunks.append(rendered)
    signals_block = "\n\n".join(signals_chunks)
    if signals_block:
        signals_block = signals_block + "\n\n"
    return PROMPT_TEMPLATE.format(
        url=url,
        html=html,
        required_keys_json=json.dumps(list(REQUIRED_TOKEN_KEYS), indent=2),
        optional_keys_json=json.dumps(list(OPTIONAL_TOKEN_KEYS), indent=2),
        signals_block=signals_block,
    )


def gateway_config() -> GatewayConfig:
    """Use LiteLLM when configured, otherwise use direct Anthropic."""
    gateway_url = next((os.environ.get(name) for name in LITELLM_URL_VARS if os.environ.get(name)), None)
    if gateway_url:
        api_key = next((os.environ.get(name) for name in LITELLM_KEY_VARS if os.environ.get(name)), "")
        if not api_key:
            raise RuntimeError("LiteLLM gateway configured without an API key")
        return GatewayConfig(mode="litellm", endpoint=chat_completions_url(gateway_url), api_key=api_key)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return GatewayConfig(mode="anthropic", endpoint=ANTHROPIC_ENDPOINT, api_key=api_key)


def chat_completions_url(base_url: str) -> str:
    """Return a LiteLLM chat completions endpoint from a base URL."""
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/chat/completions"):
        return trimmed
    return f"{trimmed}/chat/completions" if trimmed.endswith("/v1") else f"{trimmed}/v1/chat/completions"


def call_anthropic(config: GatewayConfig, model: str, prompt: str) -> HttpResult:
    """Dispatch one direct Anthropic Messages API request."""
    payload = {"model": model, "max_tokens": MAX_API_TOKENS, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
    headers = {"content-type": "application/json", "x-api-key": config["api_key"], "anthropic-version": ANTHROPIC_VERSION}
    return post_json(config["endpoint"], payload, headers)


def call_litellm(config: GatewayConfig, model: str, prompt: str) -> HttpResult:
    """Dispatch one LiteLLM/OpenAI-compatible chat completions request."""
    payload = {"model": model, "max_tokens": MAX_API_TOKENS, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
    return post_json(config["endpoint"], payload, {"authorization": f"Bearer {config['api_key']}"})


def post_json(url: str, payload: Mapping[str, Any], headers: Mapping[str, str]) -> HttpResult:
    """POST JSON and classify transient HTTP failures for retry."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return HttpResult(status=resp.status, body=resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in TRANSIENT_STATUS_CODES:
            raise TransientApiError(f"status {exc.code}: {body[:300]}") from exc
        raise RuntimeError(f"status {exc.code}: {body[:500]}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise TransientApiError(str(exc)) from exc


def response_text(raw_body: str, mode: Literal["anthropic", "litellm"]) -> str:
    """Extract assistant text from Anthropic or LiteLLM response JSON."""
    data = json.loads(raw_body)
    if mode == "litellm":
        return str(data["choices"][0]["message"]["content"])
    return "\n".join(str(block.get("text", "")) for block in data.get("content", []) if isinstance(block, dict) and block.get("type") == "text")


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from fenced model output or leading prose."""
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.I | re.S):
        try:
            value = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return cast(dict[str, Any], value)
    value = json.loads(first_json_object(text))
    if not isinstance(value, dict):
        raise ValueError("model JSON was not an object")
    return cast(dict[str, Any], value)


def first_json_object(text: str) -> str:
    """Return the first balanced JSON object substring from text."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found")
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise ValueError("unterminated JSON object")


def coerce_token_set(data: Mapping[str, Any]) -> TokenSet:
    """Keep valid TokenSet keys and coerce scalar values to strings."""
    out: dict[str, str] = {}
    for key, value in data.items():
        if key in ALLOWED_TOKEN_KEYS and value is not None and not isinstance(value, (dict, list)):
            text = str(value).strip()
            if text:
                out[key] = text
    return cast(TokenSet, out)


def dtcg_payload_with_schema(token_set: TokenSet) -> dict[str, Any]:
    """Return the CLI/file DTCG payload with schema_version included."""
    return {"schema_version": SCHEMA_VERSION, **to_dtcg_json(token_set)}
