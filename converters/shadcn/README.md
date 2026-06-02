# resemblio-shadcn

Convert a Resemblio DTCG manifest into a shadcn/ui theme: `globals.css` (`:root` + `.dark` CSS variables) plus a `tailwind.config.js` extension snippet.

Position in the Resemblio ecosystem:

```
URL -> Resemblio API -> DTCG manifest -> resemblio-shadcn -> shadcn theme files
                                      -> resemblio-tailwind (future)
                                      -> resemblio-style-dictionary (future)
                                      -> resemblio-figma-variables (future)
```

This is the wedge converter. Every AI coding agent (v0, Bolt, Lovable, Cursor, Magic Patterns) ships shadcn defaults, so dropping a Resemblio-derived `globals.css` into one of those generated projects gives any brand instant identity inside the shadcn vocabulary the agent already knows.

## Install

```bash
pip install resemblio-shadcn
```

Or from a workspace checkout:

```bash
cd projects/Resemblio/code/converters/shadcn
pip install -e .
```

## Usage

### Python API

```python
import requests
from resemblio_shadcn import dtcg_to_shadcn, render_globals_css, render_tailwind_config

resp = requests.post(
    "https://api.resemblio.com/v1/extractions",
    headers={"Authorization": "Bearer rsk_..."},
    json={"url": "https://stripe.com", "private": False},
).json()

theme = dtcg_to_shadcn(resp["dtcg"], source_url=resp["manifest"]["source_url"])

with open("app/globals.css", "w") as f:
    f.write(render_globals_css(theme))

with open("tailwind.config.js", "w") as f:
    f.write(render_tailwind_config(theme))
```

### CLI

```bash
# From a saved manifest
python -m resemblio_shadcn manifest.json --out-dir ./theme

# From stdin via curl piped through jq
curl -s -H "Authorization: Bearer rsk_..." \
  -X POST https://api.resemblio.com/v1/extractions \
  -d '{"url":"https://stripe.com"}' \
  | jq .dtcg \
  | python -m resemblio_shadcn - --out-dir ./theme
```

## Sample input -> output

**Input (Resemblio DTCG manifest, abridged):**

```json
{
  "color": {
    "accent": {"$value": "#cc3344", "$type": "color"},
    "bg":     {"$value": "#ffffff", "$type": "color"},
    "text":   {"$value": "#111111", "$type": "color"}
  },
  "fontFamily": {
    "body": {"$value": "Inter", "$type": "fontFamily"}
  },
  "dimension": {
    "radius-md": {"$value": "8px", "$type": "dimension"}
  }
}
```

**Output (`globals.css`, abridged):**

```css
:root {
    --background: 0.0 0.0% 100.0%;
    --foreground: 0.0 0.0% 6.7%;
    --primary: 354.5 60.4% 50.0%;
    --primary-foreground: 210 40% 98%;
    ...
    --font-sans: Inter, ui-sans-serif, system-ui, ...;
    --radius: 0.5rem;
}

.dark {
    --background: 0.0 0.0% 6.7%;
    --foreground: 0.0 0.0% 100.0%;
    --primary: 354.5 60.4% 60.0%;
    ...
}
```

## How slots are inferred

The converter does not require Resemblio's color leaves to have shadcn-style names. It buckets the palette by saturation and lightness, then assigns:

- **primary** <- most-saturated color
- **accent** <- second-most-saturated color
- **secondary / muted** <- lightest neutral (low saturation)
- **background** <- lightest color in the palette
- **foreground** <- darkest color in the palette
- **border / input** <- mid-light neutral
- **ring** <- primary
- **chart-1 ... chart-5** <- saturated palette continued, padded from shadcn defaults

When the manifest carries no usable palette, the output degrades to shadcn's default slate / zinc neutral theme rather than crashing.

## Schema version

This package emits `shadcn_schema_version=1`, which is the HSL-triple / Tailwind v3 convention (the format every existing shadcn project on the web uses today). A future `schema_version=2` will add the OKLch / Tailwind v4 `@theme inline` output shadcn introduced in 2026, gated behind a `--format=v4` flag.

## License

MIT. See `LICENSE`.

## Caveats

- The slot-assignment heuristic is "sensible defaults", not "perfect WCAG-tuned palette inference". Customers iterating on the output is expected. v2 will run a WCAG contrast pass on every `*-foreground` slot.
- Dark mode is auto-inverted when the source manifest does not carry an explicit dark variant. Resemblio's extractor does not yet emit dark variants; the inversion heuristic preserves brand hue while flipping lightness.
- Only hex color values are honored. `rgb()`, `hsl()`, and named colors in the source DTCG are skipped. Resemblio's extractor emits hex by convention.
