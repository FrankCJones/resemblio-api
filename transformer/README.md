# transformer (vendored into resemblio-api)

This is a **vendored copy** of the workspace-level `code/transformer/` package, pulled into the `resemblio-api` repo on 2026-05-31 so CI (which checks out only this repo) can resolve `from transformer import ...` without a sibling checkout.

**Source of truth:** `projects/Resemblio/code/transformer/`. Edits land there first; this copy is updated by `cp -r` (3 small files) and a single commit on `resemblio-api`.

**Single consumer:** `scripts/seed_from_drl.py` and its tests. No other repo (`code/mcp/` is TypeScript, `code/web/` is Next.js TS) imports this module, so the vendor pattern stays cheap.

**Sync convention** documented in `projects/Resemblio/Resemblio_INFRA.md` under "Vendored transformer package".

---

## What this module does

One-shot module that reads from the Design Reference Library (`projects/Design Reference Library/`) and produces design-preserving, identity-scrubbed entries for Resemblio's internal corpus. The output keeps the design language intact: colors, type, spacing, scale, layout rhythm, component styling, motion, and interaction states. It removes only the protected identity layer: wordmarks, monograms, logos, trademark uses inside assets, copyrighted copy, proprietary imagery, unsafe people photos, and affiliation claims. Inspirado, no copiado.

**One-way only.** Never writes back to the DRL.

**Identity-scrub operations:**

1. Keep factual source attribution available for public page chrome and metadata, such as "inspired by Anthropic"
2. Strip wordmarks, monograms, and logos from the bundle (replace with generic placeholders)
3. Replace brand-specific copy in HTML with lorem ipsum
4. Rename CSS classes from trademark-bearing names to generic patterns when the class itself carries protected identity
5. Preserve private source details for audit and takedown handling while exposing only safe, non-affiliating public attribution
6. Re-render the four-file bundle (`asset.html`, `tokens.css`, `meta.json`, `README.md`) per Resemblio's schema

The colors, type, spacing, weights, layout patterns, and component behaviors themselves are preserved as the design-faithful starting point Resemblio's public framing promises.

**Run cadence:** once at v1 setup to populate Resemblio's internal corpus with the 19 DRL systems. Subsequent extractions originate inside Resemblio itself.

