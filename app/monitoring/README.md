# app/monitoring - Resemblio production monitoring primitives

Closes Stage 1 of the CTO TDD recovery plan at
`projects/OptSus Team/cto-reviews/2026-06-03-resemblio-back-on-track-tdd-plan.md`.

The synthetic prod probe + state-machine alert dedup that closes the gap the
2026-06-02 Library three-hour outage exposed. The existing `/v1/healthz` cron
returned 200 the entire time the user-facing Library pages were broken;
status-only checks miss the "200 but body empty / token leaked" failure mode.

## File map

| File | Role |
|---|---|
| `__init__.py` | Package docstring; no logic. |
| `synthetic_probe.py` | The probe core: check definitions, retry+backoff HTTP, marker predicates, state machine, Resend alert sink, per-tick `run_tick` orchestration. |
| `link_crawl.py` | Link-crawl smoke gate: DOM-parses every registered surface, classifies each link as internal vs external, asserts every internal link returns 200 (or a documented 301). Standing PR gate locked 2026-06-03. |
| Tests | `tests/test_synthetic_probe.py`, `tests/test_link_crawl.py`, `tests/test_link_crawl_deploy_yml.py` - synthetic responses via injected fetchers; no live IO. |
| CLI wrappers | `scripts/synthetic_probe.py` (5-min systemd timer), `scripts/link_crawl_smoke.py` (CI post-deploy step). |
| systemd | `deploy/systemd/resemblio-synthetic-probe.{service,timer}` - 5-minute timer; hardened oneshot service. |

## Link-crawl smoke gate (standing PR gate)

Closes the failure shape where a deploy returns green CI but a link in the
rendered HTML 404s or 500s (Library v1.1 metadata-route outage 2026-06-02;
Susann WP staging nav 404 2026-06-02). Status-only smoke (`/v1/healthz`,
`/v1/readyz`) cannot catch link-shape regressions because the smoke routes
themselves were healthy.

Wired in `code/api/.github/workflows/deploy.yml` AFTER the `/v1/readyz`
probe and BEFORE the security-header smoke. Reads the surface registry from
`projects/Resemblio/surfaces.yml`. DOM parse via stdlib `html.parser` (no
regex over HTML, no new runtime dep). Report JSON
(`schema_version=link_crawl_report_v1`) uploaded as a workflow artifact.

Exit code 0 = clean; exit 1 = at least one link failed; exit 2 = operator
bug (surfaces.yml unreadable, dependency missing).

## Data flow

```
systemd timer (every 5 min)
  -> scripts.synthetic_probe (CLI)
       -> run_tick()
            -> run_probe()         (5 checks, retry+backoff)
            -> load_state()        (/var/lib/resemblio/synthetic-probe-state.json)
            -> decide_alert()      (state machine, dedup)
            -> alert_sink()        (Resend POST, if transition or re-nag)
            -> save_state()
            -> append_report_log() (/var/log/resemblio/synthetic-probe-<date>.log)
```

## Synthetic checks (Stage 1 contract)

| Check | URL | Status | Body marker |
|---|---|---|---|
| `web_root` | `https://resemblio.com/` | 200 | `<html ... lang=` present; no URN leak |
| `library_hub` | `https://resemblio.com/library/` | 200 | `href="/library/` present (>=1 brand card); no URN leak |
| `library_brand_buttons` | `https://resemblio.com/library/aeon/buttons/` | 200 | `.b-btn` OR `--ds-` (CSS composition fired) AND `<button` or `role="button"` (body fragment) AND no URN leak |
| `api_healthz` | `https://api.resemblio.com/v1/healthz` | 200 | status only |
| `api_readyz` | `https://api.resemblio.com/v1/readyz` | 200 | status only |

The Library brand defaults to Aeon (the reference brand with full computed-style
snapshot per `code/api/OPS.md` Section 8.11). Override via
`RESEMBLIO_PROBE_BRAND`.

## State machine and alert dedup

State file: `/var/lib/resemblio/synthetic-probe-state.json`,
schema `synthetic_probe_state_v1`.

| Transition | Alert? | Subject |
|---|---|---|
| green -> green | no | `steady_green` |
| green -> red | yes | `[Resemblio] DOWN on <host>: <first-failure detail (80 chars)>` |
| unknown -> red | yes | same as above (`new_failure`) |
| unknown -> green | no | `steady_green` |
| red -> red, same detail, within `DEDUP_WINDOW_SEC` (15 min) | no | `suppressed_dedup` |
| red -> red, same detail, past window | yes | `[Resemblio] STILL DOWN on <host>: <detail>` (`renag`) |
| red -> red, different detail | yes | `[Resemblio] FAILURE MODE CHANGED on <host>: <detail>` |
| red -> green | yes | `[Resemblio] RECOVERED on <host>` |

This contract sits between two failure modes the workspace has already hit:
the 2026-06-02 silent 3-hour outage (zero alerts because no probe existed)
and the naive every-tick sender (36 alerts for a 3-hour outage). The 15-minute
dedup window is calibrated so a real outage produces one ping plus ~10-12
re-nags over three hours.

## Contracts

- Per-tick `ProbeReport` carries `schema_version=synthetic_probe_report_v1`
- State JSON carries `schema_version=synthetic_probe_state_v1`
- CLI summary line carries `schema_version=synthetic_probe_cli_v1`

Bump the schema version in lockstep with any breaking field change; the
loader fails closed (returns a fresh state) on schema mismatch.

## Operator install (parent session)

See `deploy/systemd/resemblio-synthetic-probe.service` head comment plus
the operator-handoff in this Builder dispatch report. The probe expects
`RESEND_API_KEY` in `/etc/resemblio/probe.env` (chmod 0600 root:root).
