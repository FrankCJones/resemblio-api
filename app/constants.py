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
