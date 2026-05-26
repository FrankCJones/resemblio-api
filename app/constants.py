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
R2_BUCKET_NAME = "resemblio-extractions"
DEFAULT_API_SCOPE = "extract"
SCHEMA_V1 = 1
# Hard upper bound on the request body for `/v1/webhooks/stripe`. Real Stripe
# webhook payloads are a few KB; 256 KiB is roughly two orders of magnitude
# above the largest observed legitimate event and small enough to bound the
# work a single rejected request can cost the API. Audit finding M-API-1
# (`projects/OptSus Team/security-audits/2026-05-26-initial.md`).
STRIPE_WEBHOOK_MAX_BODY_BYTES = 256 * 1024
