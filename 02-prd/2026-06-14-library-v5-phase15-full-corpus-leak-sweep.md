# PRD: Library v5 Phase 15 - Full-corpus trademark leak sweep

```
schema:              phase15_corpus_leak_sweep_v1
date:                2026-06-14 UTC
author:              Sonnet 4.6 (Builder)
gate:                Gate 15 (Opus)
predecessor:         Phase 14 COMPLETE (Gate 14 APPROVED 2026-06-14)
suite_baseline:      304 passed, 1 skipped (tests/render/ only; CI baseline on 1474cef1)
```

---

## Problem statement

Phase 14 proved the live v6 fidelity gate fired correctly and came back clean. The Gate 14
audit found the gap the GO verdict hides: that run covered 6 brands. Prod serves 41. Roughly 35
public-browsable brands have never had their live rendered HTML checked for a wordmark/logo leak.
They rely only on 10 universal forbidden substrings. If a brand's logo survives those universal
rules (e.g. an `<img src="someotherbrand-cdn/logo.png">` not named by any universal or per-brand
rule), it leaks to the public unchecked.

Phase 15 extends the trademark wordmark-leak guarantee from 6 to all 41 prod brands using
HTML-only fetches (no Playwright, no new pixel captures).

---

## Pre-phase probe outputs (all 4 required by handoff)

### Probe 1: prod brand count + slugs

Command:
```
python -c "
import urllib.request, json
req = urllib.request.Request('https://api.resemblio.com/v1/library/brands?page_size=100', headers={'User-Agent':'phase15-probe'})
with urllib.request.urlopen(req, timeout=20) as r:
    d = json.load(r)
data = d['data']
print('schema_version:', d['schema_version'])
print('total:', data['total'])
slugs = sorted(b['brand_slug'] for b in data['featured'])
print('slug count:', len(slugs))
print(slugs)
"
```

Output:
```
schema_version: 2
total: 41
slug count: 41
['a24', 'aeon', 'aesop', 'airtable', 'anthropic', 'apple', 'are-na', 'cloudflare',
 'craig-mod', 'cursor', 'daring-fireball', 'figma', 'framer', 'frank-chimero',
 'github', 'glossier', 'gwern', 'hugging-face', 'linear', 'locomotive', 'loom',
 'maggie-appleton', 'mailchimp', 'notion', 'olipop', 'openai', 'patagonia',
 'pentagram', 'pitch', 'quanta', 'read-cv', 'replit', 'resend', 'robin-sloan',
 'shared', 'stripe', 'substack', 'the-markup', 'the-pudding', 'vercel', 'webflow']
```

CRITICAL note: the response uses `brand_slug` (not `slug`) as the key in each featured entry.
The handoff said `slug`; the actual API uses `brand_slug`. All code uses `brand_slug`.

---

### Probe 2: trademark targets structure

Command:
```
python -c "
import yaml, pathlib
d = yaml.safe_load(pathlib.Path('app/trademark_strip_targets.yml').read_text())
print('schema:', d['schema_version'])
print('universal count:', len(d['universal_forbidden_substrings']))
print('per-brand entries:', [b['slug'] for b in d['brands']])
print('sample brand entry:', d['brands'][0])
"
```

Output:
```
schema: trademark_strip_targets_v1
universal count: 10
per-brand entries: ['aeon', 'apple', 'openai', 'stripe', 'vercel', 'linear']
sample brand entry: {'slug': 'aeon', 'pretty': 'Aeon', 'forbidden_image_substrings': ['aeon.co/logo', 'aeon-logo', 'aeon-wordmark']}
```

Coverage gap: 41 prod brands minus 6 per-brand entries = **35 brands with only universal rules**.

Per-brand slugs covered: aeon, apple, openai, stripe, vercel, linear.

Uncovered (universal-only) slugs:
a24, aesop, airtable, anthropic, are-na, cloudflare, craig-mod, cursor, daring-fireball,
figma, framer, frank-chimero, github, glossier, gwern, hugging-face, locomotive, loom,
maggie-appleton, mailchimp, notion, olipop, patagonia, pentagram, pitch, quanta, read-cv,
replit, resend, robin-sloan, shared, substack, the-markup, the-pudding, webflow

(Note: figma and quanta have vendored fidelity specs but no per-brand trademark_strip_targets
entry; they get only the 10 universal forbidden substrings in the corpus sweep.)

---

### Probe 3: live brand page URL + fetch test

Command:
```
python -c "
import urllib.request
for slug in ('apple', 'a24'):
    url = f'https://resemblio.com/library/{slug}'
    req = urllib.request.Request(url, headers={'User-Agent':'phase15-probe'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode('utf-8', 'replace')
        print(slug, r.status, 'html_len', len(html))
    except Exception as e:
        print(slug, 'ERROR', e)
"
```

Output:
```
apple 200 html_len 53376
a24 200 html_len 53479
```

Decision: sweep hits `/library/{brand}` hub page, one fetch per brand (41 total).
Category-depth sweep (per-category pages) is a Phase 16 extension. The hub page is where brand
wordmarks would most plausibly leak (logos, brand headers in the hub nav/header area).

---

### Probe 4: evaluator contract shape (anti-vacuity pin)

Command:
```
python -c "
from tests.render.assertion_eval import evaluate_all_assertions_against_live_html, NO_LEAK_ID_MARKER, AssertionSweepResult
print('NO_LEAK_ID_MARKER:', NO_LEAK_ID_MARKER)
a = {
  'id': 'demo-no-wordmark-logo-leak',
  'evaluate': \"(() => { const html = document.documentElement.outerHTML.toLowerCase(); const forbidden = ['apple-logo']; return forbidden.every(s => !html.includes(s)); })()\",
  'expected': True
}
clean = evaluate_all_assertions_against_live_html([a], '<div>brand stripped</div>')
leak  = evaluate_all_assertions_against_live_html([a], '<img src=\"apple-logo.svg\">')
print('clean.wordmark_leak:', clean.wordmark_leak, '| leak.wordmark_leak:', leak.wordmark_leak)
print('clean.passed:', clean.passed, '| clean.failed:', clean.failed)
print('leak.passed:', leak.passed, '| leak.failed:', leak.failed)
"
```

Output:
```
NO_LEAK_ID_MARKER: no-wordmark-logo-leak
clean.wordmark_leak: False | leak.wordmark_leak: True
clean.passed: ['demo-no-wordmark-logo-leak'] | clean.failed: []
leak.passed: [] | leak.failed: ['demo-no-wordmark-logo-leak']
```

EXACT assertion shape (from vendored `apple_alphabet.json` and confirmed by probe):
```python
{
    "id": "<brand>-corpus-no-wordmark-logo-leak",   # must contain NO_LEAK_ID_MARKER
    "evaluate": (
        "(() => { const html = document.documentElement.outerHTML.toLowerCase(); "
        "const forbidden = ['tok1', 'tok2']; "
        "return forbidden.every(s => !html.includes(s)); })()"
    ),
    "expected": True
}
```

The `forbidden_tokens_from_evaluator` regex `r"const\s+forbidden\s*=\s*\[(.*?)\]"` matches
the `const forbidden = [...]` inside the IIFE. The `evaluate_assertion_against_live_html`
dispatcher fires on `"forbidden.every" in evaluator`. Both confirmed by probe.

---

## New files

- `code/api/tests/render/corpus_leak_sweep.py` - the sweep module
- `code/api/tests/render/test_corpus_leak_sweep.py` - TDD tests

No existing files modified. `assertion_eval.py` and `trademark_strip_targets.yml` are untouched.

---

## TDD results (RED/GREEN per sub-phase)

### 15.1 - forbidden_for_brand + build_no_leak_assertion

RED commit: `test(leak): Phase 15.1 RED - forbidden_for_brand + build_no_leak_assertion`
GREEN commit: `feat(leak): Phase 15.1 GREEN - forbidden_for_brand + build_no_leak_assertion`

### 15.2 - assess_brand_html (core + anti-vacuity)

RED commit: `test(leak): Phase 15.2 RED - assess_brand_html clean vs leaking`
GREEN commit: `feat(leak): Phase 15.2 GREEN - assess_brand_html`

### 15.3 - audit_coverage

RED commit: `test(leak): Phase 15.3 RED - audit_coverage`
GREEN commit: `feat(leak): Phase 15.3 GREEN - audit_coverage`

### 15.4 - run_corpus_leak_sweep + CorpusLeakReport

RED commit: `test(leak): Phase 15.4 RED - run_corpus_leak_sweep aggregate + go`
GREEN commit: `feat(leak): Phase 15.4 GREEN - run_corpus_leak_sweep + CorpusLeakReport`

### 15.5 - render_corpus_leak_markdown

RED commit: `test(leak): Phase 15.5 RED - render_corpus_leak_markdown`
GREEN commit: `feat(leak): Phase 15.5 GREEN - render_corpus_leak_markdown`

---

## Live 41-brand sweep result

Run timestamp: `20260614T195533Z`
Output: `_verification/library-inspirado-correction-20260604/corpus_leak_runs/20260614T195533Z/`

```
schema_version:      corpus_leak_sweep_v1
generated_at_utc:    2026-06-14T19:55:52.946737+00:00
total_brands:        41
brands_swept:        41
leak_count:          0
error_count:         0
go:                  True
```

**Verdict: GO. 0 wordmark leaks across all 41 prod brands. 0 fetch errors.**

All 41 brands returned HTTP 200. All 41 pass the trademark wordmark-leak check.

Coverage note: 35 of 41 brands pass on universal-only rules (no per-brand entry in
`trademark_strip_targets.yml`). They are clean today. The decision of whether to add
per-brand rules for these 35 is a Frank/Opus judgment call; Phase 15 surfaces the list,
does not make the call.

No actual leak found; `trademark_strip_targets.yml` is unchanged (handoff rule: a silent
patch would be the prohibited move, and there is nothing to patch).

---

## Final pytest count

```
343 passed, 1 skipped (tests/render/ only)
Baseline was 304 passed, 1 skipped.
New tests: 39 (all in test_corpus_leak_sweep.py)
```

---

## What this unblocks / what remains

- Phase 16: avatar/PII sweep across all 41 brands (needs Playwright per-brand; deferred by design)
- Phase 7: Frank's CTA flip gate (still Frank's irreversible decision)
- Tolerance ratification: still Frank's YELLOW gate
- Phase 15 report covers only the hub page per brand; per-category pages are a Phase 16 extension
