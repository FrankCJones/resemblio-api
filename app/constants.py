"""Shared constants for the Resemblio API."""

EXTRACTION_PUBLIC_CENTS = 500
EXTRACTION_PRIVATE_CENTS = 1000
DEFAULT_EXTRACTION_CENTS = EXTRACTION_PUBLIC_CENTS
ONBOARDING_GRANT_CENTS = 1000
TOPUP_MIN_CENTS = 2000
# Hard ceiling on a single Checkout top-up. Prevents typo-or-exploit single charges
# from creating runaway authorized amounts on the user's payment method. Re-tunable.
TOPUP_MAX_CENTS = 1_000_000
# Max IntegrityError retries on credit charge insert. The CHECK constraint on
# credit_ledger.balance_after_cents guards against concurrent double-spend; under
# contention a loser retries, recomputes balance, and either commits or 402s.
CHARGE_MAX_RETRIES = 3
SPEND_CAP_WINDOW_DAYS = 30
STRIPE_RETRY_DELAYS_SECONDS = (1.0, 4.0, 16.0)
RESEND_RETRY_DELAYS_SECONDS = (1.0, 4.0, 16.0)
ROTATION_GRACE_HOURS = 48
RATE_LIMIT_PER_MIN = 60
RATE_LIMIT_PER_DAY = 5000
RATE_LIMIT_MIN_WINDOW_SECONDS = 60
RATE_LIMIT_DAY_WINDOW_SECONDS = 86_400
API_KEY_RANDOM_BYTES = 32
API_KEY_TOKEN_LENGTH = 43
API_KEY_PREFIX_HEAD = 8
API_KEY_PREFIX_TAIL = 4
MIN_KEY_PEPPER_CHARS = 32
DOWNLOAD_URL_TTL_SECONDS = 900
# Tokens-JSON presigned URL TTL. Longer than the ZIP TTL (15 min) because
# customers integrating the API typically fetch the tokens payload to render
# a preview, then re-fetch over the next few hours while wiring it into
# their pipeline. 24h is the v1.1 brief target. R2/S3 presigned URLs cap at
# 7 days with V4 signing, so 24h is well within the safe range.
TOKENS_URL_TTL_SECONDS = 24 * 60 * 60
R2_BUCKET_NAME = "resemblio-extractions"
DEFAULT_API_SCOPE = "extract"
SCHEMA_V1 = 1
# v1.1 response-shape bump. The bump signals additive fields: top-level
# `manifest` envelope and the signed `tokens_url`. Old fields stay populated;
# clients pinned to v1 continue to work. Provenance: v1.1 mission brief Section 3
# and R2 dispatch in `projects/OptSus Team/drafts/2026-05-28-resemblio-next-steps.md`.
SCHEMA_V1_1 = 2
# Hard upper bound on the request body for `/v1/webhooks/stripe`. Real Stripe
# webhook payloads are a few KB; 256 KiB is roughly two orders of magnitude
# above the largest observed legitimate event and small enough to bound the
# work a single rejected request can cost the API. Audit finding M-API-1
# (`projects/OptSus Team/security-audits/2026-05-26-initial.md`).
STRIPE_WEBHOOK_MAX_BODY_BYTES = 256 * 1024

# S20 R4 auto-refund customer-comms constants. The support email is the
# canonical address customers reply to for manual review; matches the address
# in the v1 brand pages and the resemblio.com MX records. The subject and body
# template live next to it so a copy review (Frank) is a one-file diff.
AUTO_REFUND_SUPPORT_EMAIL = "hello@resemblio.com"
# Reply-to address attached to every transactional email so a confused
# customer's reply lands in a human inbox rather than the no-reply ``hello@``
# alias the messages are sent from. Cold-user E2E audit finding #3
# (`projects/Resemblio/marketing/2026-06-02-cold-user-e2e-audit.md`).
# Open item: switch to ``hello@resemblio.com`` once that mailbox is staffed.
TRANSACTIONAL_REPLY_TO = "frank@optsus.com"
AUTO_REFUND_EMAIL_SUBJECT = "Your Resemblio extraction was auto-refunded"
# Format args: amount (USD string, e.g. "$5.00"), source_url, support_email.
# Plain text body; no HTML rendering on Resend for this transactional message.
# Kept short on purpose; longer copy invites the customer to skim past the
# refund confirmation, which is the line that matters.
AUTO_REFUND_EMAIL_BODY_TEMPLATE = (
    "Your extraction of {source_url} returned generic placeholders our "
    "quality-scoring system flagged as low confidence. We've credited your "
    "account back {amount}. Try a different URL or contact us at "
    "{support_email} if you'd like manual review."
)
# Audit row schema version. Bumped (with a migration) when the audit-row
# shape changes in a way downstream consumers must notice.
AUTO_REFUND_AUDIT_SCHEMA_VERSION = "auto_refund_audit_v1"

# S3 internal-auth (magic link + BFF session) constants. The magic-link
# token is 32 random bytes URL-safe-base64 encoded; we never persist the
# plaintext, only its SHA-256 hash. Expiry is short (15 minutes) so a
# leaked-link window is bounded. The session-key rotation cadence is
# advisory only for now (the BFF key revokes-and-mints on every login
# regardless of age); 30 days is the upper bound a single BFF key can
# live before a forced rotation on next login.
MAGIC_LINK_TOKEN_BYTES = 32
MAGIC_LINK_EXPIRY_MINUTES = 15
# Minimum interval between magic-link emails to the same address, in seconds.
# Closes a mailbox-bombing / volume-enumeration edge: if the internal-auth
# shared secret leaks (or a buggy BFF loop-fires the endpoint), every retry
# would otherwise mint a fresh token AND fire a fresh Resend send to the
# target's inbox. Within this window the API still returns the standard
# ``{ok: true}`` anti-enumeration ack but skips the mint+send. Tuned so a
# human-pace retry (refresh, "didn't get it") still works while loop-fire
# is rate-bounded. Raise if abuse is observed.
MAGIC_LINK_REQUEST_COOLDOWN_SECONDS = 30
BFF_SESSION_MAX_AGE_DAYS = 30
# Magic-link email copy lives next to the constant so a copy review is a
# one-file diff. The link URL is supplied by the caller (the web BFF
# knows its own origin); the API only assembles the body text.
MAGIC_LINK_EMAIL_SUBJECT = "Your Resemblio sign-in link"
MAGIC_LINK_EMAIL_BODY_TEMPLATE = (
    "Click to finish signing in to Resemblio:\n\n"
    "{link}\n\n"
    "This link expires in {minutes} minutes and can only be used once. "
    "If you did not request this, you can ignore this email."
)
# How many of the leading characters of a BFF api-key plaintext are safe
# to log. The full plaintext is never logged; only this prefix appears in
# operational log lines so an operator can trace a session without the
# log line itself becoming a credential.
BFF_KEY_LOG_PREFIX_CHARS = 8
# ApiKey.kind vocabulary. Treat as a closed set; new values require a
# migration plus a code-side switch update.
API_KEY_KIND_USER = "user"
API_KEY_KIND_INTERNAL_BFF = "internal_bff"
API_KEY_KIND_SERVICE = "service"
# schema_version literal for downstream consumers of magic-link / session
# rows. Bumped together with the migrations if the row shape changes.
MAGIC_LINK_SCHEMA_VERSION = "magic_link_tokens_v1"
WEB_SESSION_SCHEMA_VERSION = "web_session_keys_v1"

# S3b Wave 2c top-up bundle tiers. The dashboard `/app/billing` page surfaces
# three buttons that map to these bundles. The *_PAID value is what Stripe
# charges the user's card; the *_CREDITED value is what lands in the credit
# ledger after the webhook fires. The bonus math (10% on $100+, 20% on $500+)
# matches the canonical pricing reference in `projects/Resemblio/CLAUDE.md`.
# Bonus is applied server-side; never trust a client-supplied credited amount.
#
# IMPORTANT: today's implementation passes `*_PAID` to Stripe as the charge
# amount AND to the credit ledger as the credit amount; the *_CREDITED values
# below are kept for forward-compatibility once the route extension that
# decouples charge-vs-credit ships. Until then the bonus is surfaced as UI
# copy on the buttons but not yet applied to the ledger.
TOPUP_BUNDLE_20_CENTS_PAID = 2000
TOPUP_BUNDLE_20_CENTS_CREDITED = 2000  # no bonus on the $20 tier
TOPUP_BUNDLE_100_CENTS_PAID = 10000
TOPUP_BUNDLE_100_CENTS_CREDITED = 11000  # +10%
TOPUP_BUNDLE_500_CENTS_PAID = 50000
TOPUP_BUNDLE_500_CENTS_CREDITED = 60000  # +20%

# Closed set of accepted bundle amounts for the dashboard billing surface.
# Requests carrying any other amount_cents value are rejected at the route
# boundary; this is belt-and-braces protection against a tampered client
# request that tries to mint Checkout sessions outside the documented tiers.
TOPUP_BUNDLE_ACCEPTED_PAID_CENTS = frozenset(
    {TOPUP_BUNDLE_20_CENTS_PAID, TOPUP_BUNDLE_100_CENTS_PAID, TOPUP_BUNDLE_500_CENTS_PAID}
)

# Idempotency-Key support on POST /v1/extractions. The header value is bound
# per-user for 24 hours: a replay within that window returns the original
# cached response with ``X-Idempotency-Replayed: true`` and does NOT re-charge
# credits. A replay carrying the SAME key but a DIFFERENT request body hash is
# rejected with HTTP 409 (Stripe's behavior; the client used one idempotency
# token for two semantically different requests, which is always a bug).
# TTL chosen to bound the cache table size (sweep query can prune any row
# older than this constant); 24h is long enough to absorb sane client retry
# loops including overnight cron jobs that re-fire on resume.
IDEMPOTENCY_KEY_TTL_SECONDS = 24 * 60 * 60
# Header name the API reads; matches Stripe / IETF draft conventions.
IDEMPOTENCY_HEADER_NAME = "Idempotency-Key"
# Header echoed on a cache-hit replay so the client can distinguish
# a fresh computation from a replayed cached response.
IDEMPOTENCY_REPLAYED_HEADER_NAME = "X-Idempotency-Replayed"
# Bounds on the supplied key value. Lower bound prevents trivial collisions
# (e.g. ``"1"``); upper bound caps the row width and the URL-encoded header
# cost. Character allowlist excludes whitespace and control characters so a
# malformed CRLF-injection cannot land in the persisted row.
IDEMPOTENCY_KEY_MIN_LENGTH = 8
IDEMPOTENCY_KEY_MAX_LENGTH = 256
import re as _re  # local alias to keep the module's top-level imports clean

IDEMPOTENCY_KEY_PATTERN = _re.compile(r"^[A-Za-z0-9._-]+$")


# Feature flag env var name. Both the API and the web BFF read this; the value
# is the literal string "true" (case-insensitive) to enable. Anything else,
# including unset, means disabled. Disabled = the API returns 503 and the web
# `/app/billing` route returns 404 (notFound). Frank flips this AFTER his own
# Stripe-LIVE smoke succeeds; see the 2026-06-02 Wave 2c handoff for the
# seven-step flip sequence.
BILLING_UI_FLAG_ENV_VAR = "RESEMBLIO_BILLING_UI_ENABLED"

# Converter target identifiers exposed via the public conversion endpoints
# (``POST /v1/convert/<target>/{extraction_id}``). Treated as a closed set;
# adding a new converter requires adding both the constant and the route.
CONVERT_TARGET_SHADCN = "shadcn"
CONVERT_TARGET_FIGMA = "figma"
# Response-shape schema version for the conversion endpoints. Bumped
# independently of the extraction response contract; v2 is the first publicly
# documented shape (matches the v1.1 envelope semantics: a `schema_version`
# field plus a `payload` block plus optional `rendered` artifacts).
CONVERT_RESPONSE_SCHEMA_VERSION = 2

# Asset-versions library schema (migrations 0015-0018). Each ``asset_versions``
# row is the deduplicated DTCG snapshot of one URL at one moment in time; a
# successful extraction points at exactly one row via
# ``extractions.asset_version_id``. Dedup key is ``(url, content_hash)`` where
# ``content_hash`` is the SHA-256 of the canonical-JSON serialization of the
# DTCG payload (see ``app/asset_versions.py``). Public corpus visibility is
# the ``is_public`` flag; defaults to False in v1.1 and is intended to flip on
# in v1.2 once moderation tooling exists.
ASSET_VERSION_MANIFEST_SCHEMA_DEFAULT = 2
ASSET_VERSIONS_SEED_SOURCE_LABEL = "drl_v1"

# R3 extraction-fidelity heuristic constants. Added 2026-06-02 per mission
# `projects/OptSus Team/missions/resemblio-r3-extraction-fidelity-v1.md`
# (Deliverable C). The two original penalties (system_font_stack +
# common_default_colors) live in app/quality_heuristics.py; these constants
# back the two additional R3 rules.
#
# PENALTY_ACCENT_TEXT_LAB_THRESHOLD: minimum CIE LAB Delta-E distance
# (CIE76 formula) between `accent` and `text` for a token set to count as
# having a distinctive accent. Distances under this floor read as "the
# accent and text are essentially the same color" — the LLM-defaulted-its-
# way-into-a-monochrome-palette pathology described in the Susann finding's
# Hypothesis-adjacent failure surface. Calibrated against fixture 010
# (default HTML baseline) which must trip the threshold.
PENALTY_ACCENT_TEXT_LAB_THRESHOLD: float = 5.0

# Penalty magnitude for the missing-accent-diversity rule. Sized so that
# this penalty alone does NOT drive a baseline-distinctive output below the
# refund threshold, but stacks usefully with the other penalties when the
# extractor truly defaulted.
PENALTY_ACCENT_DIVERSITY: float = 0.15

# Penalty magnitude for the display-equals-body rule. Same sizing rationale
# as PENALTY_ACCENT_DIVERSITY: standalone informative; stackable.
PENALTY_DISPLAY_EQUALS_BODY: float = 0.15

# R3.2 near-default-extraction rule (2026-06-02). Source mission:
# `projects/Resemblio/_handoff/inbox/claude/2026-06-02-susann-extraction-fidelity-investigation.md`.
#
# When BOTH the system-stack font score AND the default-color score for a
# TokenSet exceed `NEAR_DEFAULT_EXTRACTION_THRESHOLD`, the extractor almost
# certainly missed the source's brand identity (CSS-variable indirection
# unresolved, web fonts not parsed, computed-style pass unavailable). The
# `apply_heuristic_penalties` helper drives `penalized_score` to 0.0 and
# adds the `near_default_extraction` flag to `penalties_applied`.
#
# Threshold sized at 0.9 (not 1.0) so a TokenSet with ONE legitimately-
# distinctive slot in five still passes - the failure mode is "almost
# everything is a default", not "exactly everything".

# System-stack score: fraction of populated font slots whose primary family
# is in `_SYSTEM_FONT_FAMILIES` (Arial / system-ui / Georgia / etc.).
SYSTEM_STACK_SCORE_THRESHOLD: float = 0.9
"""Minimum system-stack fraction across populated font slots to count as 'all defaults'."""

# Default-color score: fraction of populated color slots within Manhattan-RGB
# distance `DEFAULT_COLOR_DISTANCE_MAX` of any common default
# (#000 / #fff / #888 + extras).
DEFAULT_COLOR_SCORE_THRESHOLD: float = 0.9
"""Minimum default-color fraction across populated color slots to count as 'all defaults'."""

# Manhattan distance in RGB (0-765) under which a color is considered
# "near a common gray-scale default". 30 keeps the rule tight: #f5f5f5
# vs #ffffff is Manhattan 30; #1a1a1a vs #000000 is Manhattan 78 - both
# match a default. Anything chromatic (a brand red, blue, yellow) falls
# well outside this radius.
DEFAULT_COLOR_DISTANCE_MAX: int = 80
"""Manhattan-RGB distance under which a color is 'near a common gray-scale default'."""

# Failure-mode string surfaced via the diagnostic + penalty flag when the
# near-default rule fires. Customers see this string via the route handler
# in the low-quality response path.
NEAR_DEFAULT_EXTRACTION_FLAG: str = "near_default_extraction"
"""Canonical penalty flag name for the R3.2 near-default-extraction rule."""

NEAR_DEFAULT_EXTRACTION_FAILURE_MODE: str = (
    "Source uses :root custom properties or web fonts that extractor missed. "
    "Re-extract after enabling R3.2 parsers."
)
"""Human-readable failure-mode message surfaced when near-default rule fires."""


# Library indexer (mission Phase 4). Constants here are read by both the
# worker (``app/library_indexer.py``) and the migration-aware ORM models
# (``LibraryIndexJob``). Centralized so changing a value never requires
# touching the worker and the model in lockstep.
LIBRARY_INDEX_BATCH_SIZE: int = 10
"""Maximum number of pending jobs the worker drains per CLI tick."""

LIBRARY_INDEX_MAX_ATTEMPTS: int = 3
"""Retry budget per job before the row is parked at ``status='failed'``."""

LIBRARY_INDEX_QUALITY_THRESHOLD: float = 0.7
"""Quality-gate floor (mission D2). Below this, pages are not generated."""

LIBRARY_PAGE_METADATA_SCHEMA_VERSION: int = 1
"""Schema-version tag stamped onto every ``library_pages.metadata_json``."""


# DRL bootstrap orchestration (mission Phase 8). The DRL ships TWO data
# surfaces: ``corpus.json`` at the DRL root (41 systems, 955 component-level
# assets - the seed_from_drl source) and ``_extractions/<brand>/`` directories
# (24 brands actually pre-composed into per-category renders). The
# orchestrator anchors brand discovery on ``_extractions/`` because that is
# the set the indexer can immediately compose into library pages; corpus
# systems without an ``_extractions/`` row require a separate compose pass.
DRL_EXTRACTIONS_DIRNAME = "_extractions"
"""Subdirectory of the DRL root that contains per-brand pre-composed renders."""

DRL_BOOTSTRAP_REPORT_SCHEMA_VERSION = 1
"""Schema version stamped onto the verify harness Markdown report."""

DRL_BOOTSTRAP_EXPECTED_PAGES_PER_BRAND = 10
"""Heuristic floor for per-brand library_pages once the Phase 4 indexer
drains the queue. Matches the typical ``compose_report.composed`` length
observed in ``_extractions/<brand>/compose_report.json`` (alphabet, library,
hero, navigation, footer, feature-grid, article-layout, marketing-page,
about-page, article-page). Verify harness warns rather than fails on
brands that fall under this floor; the indexer may legitimately skip
categories the brand's extraction lacks."""

DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS = 19
"""Mission target floor: '19+ pre-extracted brands'. Verify harness exits
non-zero when the asset_versions DRL-tagged count corresponds to fewer
than this many distinct brand slugs."""


# Stage O1 anonymous-extraction constants. Source: CTO respec
# `projects/OptSus Team/cto-reviews/2026-06-03-resemblio-url-first-onboarding-respec.md`
# Stage O1, plus Frank's decisions baked in for this Builder dispatch
# (per-IP cap = 1; signup grant raised to $10; four export formats only).

# Per-IP anonymous-extraction daily cap (default 1). Overridable via env
# ``ANON_EXTRACT_PER_IP_PER_DAY`` so we can loosen during cohort tests
# without a code change. Decisions 3 (default Y) of the respec.
ANON_EXTRACT_PER_IP_PER_DAY_DEFAULT: int = 1
ANON_EXTRACT_PER_IP_PER_DAY_ENV_VAR: str = "ANON_EXTRACT_PER_IP_PER_DAY"

# Claim-token random byte count. URL-safe base64 of 32 bytes is 43 chars,
# fits comfortably in the ``claim_token`` VARCHAR(64) column.
ANON_CLAIM_TOKEN_BYTES: int = 32

# Anonymous-extraction claim window. After this, the row is reaped by
# the daily cleanup script (``scripts/reap_anonymous_extractions.py``).
ANON_EXTRACTION_CLAIM_WINDOW_HOURS: int = 24

# Site-class taxonomy (Decision 3 / 2026-06-03). The classifier (O3) emits
# exactly one of these labels. ``SUPPORTED_CLASSES`` gates whether the
# anonymous endpoint enqueues a real extraction or returns the
# "notify-when-supported" out-of-scope payload.
ANON_CLASS_HTML_FIRST: str = "html_first"
ANON_CLASS_JS_RENDERED: str = "js_rendered"
ANON_CLASS_WIX: str = "wix_class"
ANON_CLASS_WAF_BLOCKED: str = "waf_blocked"
ANON_CLASS_UNKNOWN: str = "unknown"
ANON_SUPPORTED_CLASSES: frozenset[str] = frozenset(
    {ANON_CLASS_HTML_FIRST, ANON_CLASS_JS_RENDERED}
)

# Stage O1 response envelope. Bumped together with the migration if the
# response shape changes; downstream consumers switch on this field.
ANON_EXTRACTION_SCHEMA_VERSION: int = 1

# Feature flag env var name. The route returns 503 ``feature_disabled``
# when this is anything other than the literal string "true"
# (case-insensitive). Frank flips this AFTER O3 lands in shadow per
# the respec rollout plan; the flip itself is YELLOW.
ANON_EXTRACT_FLAG_ENV_VAR: str = "RESEMBLIO_ANON_EXTRACT_ENABLED"

# Stage O1 free-signup grant (Frank decision 4 / 2026-06-03). Raised
# from $5 (``ONBOARDING_GRANT_CENTS`` at module top) to $10 so a new
# user can run an extraction + top-up smoke + a re-extraction without
# hitting the wall. Overridable via env so a temporary promo or test
# environment can dial it without a redeploy.
ANON_SIGNUP_GRANT_CENTS_DEFAULT: int = 1000
ANON_SIGNUP_GRANT_CENTS_ENV_VAR: str = "RESEMBLIO_SIGNUP_GRANT_CENTS"

# Failure-mode strings the anonymous route surfaces in the response body.
# Kept as constants so a copy review (Frank) is a one-file diff and the
# Playwright test in O2/O4 can switch on a stable string.
ANON_OUT_OF_SCOPE_MESSAGE: str = (
    "We can't extract this site yet. Tell us your email and we'll let you know "
    "when {detected_class} sites are supported."
)
ANON_RATE_LIMITED_MESSAGE: str = (
    "One anonymous extraction per 24 hours. Create an account to keep going."
)
"""Mission target floor: '19+ pre-extracted brands'. Verify harness exits
non-zero when the asset_versions DRL-tagged count corresponds to fewer
than this many distinct brand slugs."""

