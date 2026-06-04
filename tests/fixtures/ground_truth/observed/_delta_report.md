# Ground-truth observed-vs-expected delta report

- schema_version: `resemblio_ground_truth_delta_v2`
- generated_at: `2026-06-04T22:53:19.861390Z`
- fixture_count: 5
- harness: `tests/ground_truth_harness.py`
- source dispatch: R3-downstream cycle #1.5 (observed-payload capture)

## Summary

| brand | verdict | expected_color_slots | observed_color_slots |
|---|---|---|---|
| apple | FAIL | 2 | 9 |
| encexplorer | FAIL | 7 | 10 |
| figma | FAIL | 6 | 7 |
| stripe | FAIL | 3 | 10 |
| susann | FAIL | 5 | 0 |

## apple

- source_url: https://www.apple.com/
- observed_payload: `tests/fixtures/ground_truth/observed/apple.json`
- verdict: **FAIL** (2 failure(s))

### Expected colors
- `bg`: #ffffff
- `text`: #1d1d1f

### Observed colors
- `bg`: #000000
- `text`: #f5f5f7
- `accent`: #2997ff
- `border`: #424245
- `surface`: #1d1d1f
- `hairline`: #424245
- `surface_2`: #f5f5f7
- `text_muted`: #86868b
- `text_strong`: #ffffff

### Expected fonts
- `display`: SF Pro Display
- `body`: SF Pro Text

### Observed fonts
- `body`: SF Pro, -apple-system, BlinkMacSystemFont, sans-serif
- `mono`: SF Mono, ui-monospace, monospace
- `display`: SF Pro, -apple-system, BlinkMacSystemFont, sans-serif

### Assertion failures
- **font_mismatch**: font slot 'display': expected 'SF Pro Display' (mode=fuzzy), got 'SF Pro, -apple-system, BlinkMacSystemFont, sans-serif'
- **font_mismatch**: font slot 'body': expected 'SF Pro Text' (mode=fuzzy), got 'SF Pro, -apple-system, BlinkMacSystemFont, sans-serif'

## encexplorer

- source_url: https://encexplorer.com/
- observed_payload: `tests/fixtures/ground_truth/observed/encexplorer.json`
- verdict: **FAIL** (4 failure(s))

### Expected colors
- `bg`: #ffffff
- `text`: #313131
- `text_strong`: #000000
- `accent_primary`: #f8485e
- `accent_secondary`: #592a8a
- `accent_tertiary`: #1e73be
- `accent_quaternary`: #07a0c3

### Observed colors
- `bg`: #ffffff
- `text`: #313131
- `accent`: #007cba
- `border`: #dddddd
- `surface`: #f5f5f5
- `accent_2`: #006ba1
- `hairline`: #eeeeee
- `surface_2`: #eeeeee
- `text_muted`: #abb8c3
- `text_strong`: #000000

### Expected fonts
- (none asserted)

### Observed fonts
- `body`: 'Dosis', sans-serif
- `mono`: monospace
- `display`: 'Playfair Display', serif

### Assertion failures
- **color_missing**: slot 'accent_primary': expected #f8485e within 12.0 of any of ['#ffffff', '#313131', '#007cba', '#dddddd', '0px', '0.44rem', '0.67rem', '1rem', '1.5rem', '2.25rem', '3.38rem', '5.06rem', '#f5f5f5', '20px', '13px', '24px', '12px', '#006ba1', '#eeeeee', '28px', '36px', '42px', '48px', '12px', '8px', '4px', '6px 6px 0px rgb(0, 0, 0)', '12px 12px 50px rgba(0, 0, 0, 0.4)', '6px 6px 9px rgba(0, 0, 0, 0.2)', '#eeeeee', '16px', '#abb8c3', '9999px', '#000000', '1.35', '250ms', 'ease-in-out', '1.2', '1.5', '500ms', '1.625'], no match
- **color_missing**: slot 'accent_secondary': expected #592a8a within 12.0 of any of ['#ffffff', '#313131', '#007cba', '#dddddd', '0px', '0.44rem', '0.67rem', '1rem', '1.5rem', '2.25rem', '3.38rem', '5.06rem', '#f5f5f5', '20px', '13px', '24px', '12px', '#006ba1', '#eeeeee', '28px', '36px', '42px', '48px', '12px', '8px', '4px', '6px 6px 0px rgb(0, 0, 0)', '12px 12px 50px rgba(0, 0, 0, 0.4)', '6px 6px 9px rgba(0, 0, 0, 0.2)', '#eeeeee', '16px', '#abb8c3', '9999px', '#000000', '1.35', '250ms', 'ease-in-out', '1.2', '1.5', '500ms', '1.625'], no match
- **color_forbidden_present**: forbidden #007cba present as #007cba in extracted palette ['#ffffff', '#313131', '#007cba', '#dddddd', '0px', '0.44rem', '0.67rem', '1rem', '1.5rem', '2.25rem', '3.38rem', '5.06rem', '#f5f5f5', '20px', '13px', '24px', '12px', '#006ba1', '#eeeeee', '28px', '36px', '42px', '48px', '12px', '8px', '4px', '6px 6px 0px rgb(0, 0, 0)', '12px 12px 50px rgba(0, 0, 0, 0.4)', '6px 6px 9px rgba(0, 0, 0, 0.2)', '#eeeeee', '16px', '#abb8c3', '9999px', '#000000', '1.35', '250ms', 'ease-in-out', '1.2', '1.5', '500ms', '1.625']
- **palette_warning_mismatch**: expected palette_completeness_warning to be truthy, got None

## figma

- source_url: https://www.figma.com/
- observed_payload: `tests/fixtures/ground_truth/observed/figma.json`
- verdict: **FAIL** (2 failure(s))

### Expected colors
- `bg`: #ffffff
- `text`: #1e1e1e
- `accent_primary`: #1ABCFE
- `accent_secondary`: #A259FF
- `accent_tertiary`: #0ACF83
- `accent_quaternary`: #F24E1E

### Observed colors
- `bg`: #FFFFFF
- `text`: #000000
- `error`: #972121
- `accent`: #24cb71
- `surface`: #FFFFFF
- `accent_2`: #000000
- `text_strong`: #000000

### Expected fonts
- (none asserted)

### Observed fonts
- `body`: 'figmaSans', 'figmaSans Fallback', SF Pro Display, system-ui, helvetica, sans-serif
- `mono`: 'figmaMono', 'figmaMono Fallback', SF Mono, menlo, monospace
- `display`: 'figmaSans', 'figmaSans Fallback', SF Pro Display, system-ui, helvetica, sans-serif

### Assertion failures
- **color_missing**: slot 'accent_primary': expected #1ABCFE within 8.0 of any of ['#FFFFFF', '#000000', '#972121', '#24cb71', 'color-mix(in oklch, #000000, transparent 84%)', '0', '0.25rem', '0.5rem', '0.75rem', '1rem', '1.5rem', '2rem', '2.5rem', '#FFFFFF', '1.125rem', '0.875rem', '1.5rem', '0.75rem', '#000000', 'color-mix(in oklch, #000000, transparent 84%)', '3.5rem', '4rem', '5rem', '7.5rem', '2rem', '0.6875rem', '2.75rem', '3.5rem', '4rem', '4.5rem', '1rem', '0.5rem', '0.25rem', '0.125rem', '0 1.5rem 4.375rem 0 color-mix(in oklch, #000000, transparent 90%)', '0 0.25rem 2rem 0 color-mix(in oklch, #000000, transparent 90%)', 'rgba(0,0,0,0.04)', '1rem', 'color-mix(in oklch, #000000, transparent 46%)', '9999px', '0', '#000000', '1.2', '1.45', '1.1', '0.0625rem', '1.3', '-0.125rem', '1.4', '-0.0625rem'], no match
- **color_missing**: slot 'accent_secondary': expected #A259FF within 8.0 of any of ['#FFFFFF', '#000000', '#972121', '#24cb71', 'color-mix(in oklch, #000000, transparent 84%)', '0', '0.25rem', '0.5rem', '0.75rem', '1rem', '1.5rem', '2rem', '2.5rem', '#FFFFFF', '1.125rem', '0.875rem', '1.5rem', '0.75rem', '#000000', 'color-mix(in oklch, #000000, transparent 84%)', '3.5rem', '4rem', '5rem', '7.5rem', '2rem', '0.6875rem', '2.75rem', '3.5rem', '4rem', '4.5rem', '1rem', '0.5rem', '0.25rem', '0.125rem', '0 1.5rem 4.375rem 0 color-mix(in oklch, #000000, transparent 90%)', '0 0.25rem 2rem 0 color-mix(in oklch, #000000, transparent 90%)', 'rgba(0,0,0,0.04)', '1rem', 'color-mix(in oklch, #000000, transparent 46%)', '9999px', '0', '#000000', '1.2', '1.45', '1.1', '0.0625rem', '1.3', '-0.125rem', '1.4', '-0.0625rem'], no match

## stripe

- source_url: https://stripe.com/
- observed_payload: `tests/fixtures/ground_truth/observed/stripe.json`
- verdict: **FAIL** (1 failure(s))

### Expected colors
- `bg`: #ffffff
- `text`: #0A2540
- `accent`: #635bff

### Observed colors
- `bg`: #ffffff
- `text`: #0a2540
- `accent`: #635bff
- `border`: #e6ebf1
- `surface`: #f6f9fc
- `accent_2`: #0073e6
- `hairline`: #cfd7df
- `surface_2`: #e6ebf1
- `text_muted`: #425466
- `text_strong`: #031323

### Expected fonts
- `body`: sohne-var

### Observed fonts
- `body`: Sohne, sans-serif
- `mono`: Source Code Pro, monospace
- `display`: Sohne, sans-serif

### Assertion failures
- **font_mismatch**: font slot 'body': expected 'sohne-var' (mode=fuzzy), got 'Sohne, sans-serif'

## susann

- source_url: https://review.optsus.com/susann-camus/
- observed_payload: `tests/fixtures/ground_truth/observed/susann.json`
- verdict: **FAIL** (5 failure(s))

### Expected colors
- `bg`: #0B0B0F
- `surface`: #14141A
- `text`: #F5F2EA
- `accent`: #FBE71F
- `border`: #3A372E

### Observed colors
- (none extracted)

### Expected fonts
- `body`: Inter
- `display`: Anton

### Observed fonts
- (none extracted)

### Assertion failures
- **color_missing**: slot 'bg': expected #0B0B0F within 8.0 of any of [], no match
- **color_missing**: slot 'text': expected #F5F2EA within 8.0 of any of [], no match
- **color_missing**: slot 'accent': expected #FBE71F within 8.0 of any of [], no match
- **font_mismatch**: font slot 'body': expected 'Inter', not extracted
- **font_mismatch**: font slot 'display': expected 'Anton', not extracted
