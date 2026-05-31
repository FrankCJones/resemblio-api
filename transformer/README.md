# transformer (vendored into resemblio-api)

This is a **vendored copy** of the workspace-level `code/transformer/` package, pulled into the `resemblio-api` repo on 2026-05-31 so CI (which checks out only this repo) can resolve `from transformer import ...` without a sibling checkout.

**Source of truth:** `projects/Resemblio/code/transformer/`. Edits land there first; this copy is updated by `cp -r` (3 small files) and a single commit on `resemblio-api`.

**Single consumer:** `scripts/seed_from_drl.py` and its tests. No other repo (`code/mcp/` is TypeScript, `code/web/` is Next.js TS) imports this module, so the vendor pattern stays cheap.

**Sync convention** documented in `projects/Resemblio/Resemblio_INFRA.md` under "Vendored transformer package".

---

## What this module does

One-shot module that reads from the Design Reference Library (`projects/Design Reference Library/`) and produces brand-stripped versions for Resemblio's internal corpus.

**One-way only.** Never writes back to the DRL.

**Brand-strip operations:**

1. Replace source name with neutral slug (anthropic to warm-paper-editorial)
2. Strip wordmarks, monograms, logos from the bundle (replace with generic placeholders)
3. Replace brand-specific text in HTML with lorem ipsum
4. Rename CSS classes from brand-specific to generic patterns
5. Move source attribution into private metadata (not in deliverable)
6. Re-render the four-file bundle (`asset.html`, `tokens.css`, `meta.json`, `README.md`) per Resemblio's schema

**Run cadence:** once at v1 setup to populate Resemblio's internal corpus with the 19 DRL systems. Subsequent extractions originate inside Resemblio itself.
