# button_capture fixtures

Real-markup evidence for the openai/aeon button-capture decision.

## openai_homepage.html

**Source:** https://openai.com/ (live homepage)
**Captured:** 2026-06-02 (curl, Chrome UA; stored at `_handoff/inbox/claude/_snap/oai.html`)
**Size:** 418 799 bytes (full SSR HTML)
**Why pinned:** Proves the structural reason openai needed a selector override.
The default census selector `button, .cta, [role=button]` returns the first
`<button>` in document order, which is a transparent icon-only nav toggle
(padding 0, radius 0, transparent background). The real primary CTA is
`<a href="https://chatgpt.com/">` styled with Tailwind utilities; there is no
semantic class to match. The override in `BRAND_SELECTOR_OVERRIDES["openai"]`
(`a[href^='https://chatgpt.com'], header a[href*='chatgpt.com']`) was derived
from this markup. `test_button_selector_fixtures.py` asserts the contract
against this file so a future openai site redesign that breaks the selector
surfaces as a test failure rather than a silent capture regression.

## aeon_challenge.html

**Source:** https://aeon.co/ (live homepage)
**Captured:** 2026-06-02 (curl, Chrome UA; stored at `_handoff/inbox/claude/_snap/aeon.html`)
**Size:** 33 795 bytes (Vercel security-checkpoint challenge shell)
**Why pinned:** Proves aeon is structurally uncapturable by any selector or
wait-strategy fix. Every non-cookied request to aeon.co (including headless
Playwright) receives this 33 KB challenge shell
(`vercel.link/security-checkpoint`, element id `fix-text`) rather than the
real site DOM. There is no real DOM to select against. Sister site psyche.co
returns the identical shell. The evidence backing `DOCUMENTED_SKIP_BRANDS`
("aeon") and `BRAND_SELECTOR_OVERRIDES["aeon"]["cta"] = None` lives here.
See `projects/Resemblio/02-prd/2026-06-06-aeon-permanent-skip.md` for the
architectural decision record.

**Diagnosis references:**
- `projects/Resemblio/_handoff/inbox/claude/2026-06-02-openai-aeon-capture-diagnosis.md`
- `projects/Resemblio/_handoff/inbox/claude/2026-06-02-openai-aeon-selector-revision.md`
