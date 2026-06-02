# resemblio-figma

Convert a Resemblio DTCG manifest into a Figma Variables import payload. Colors land in a `Colors` collection (COLOR type, RGBA floats), spacing tokens in `Spacing` (FLOAT), font families in `Typography` (STRING), and numeric tokens in `Numbers` (FLOAT). Each collection ships with a single Light mode in v1.

Position in the Resemblio ecosystem:

```
URL -> Resemblio API -> DTCG manifest -> resemblio-shadcn  -> shadcn theme files
                                      -> resemblio-figma   -> figma-variables.json
                                      -> resemblio-tailwind        (future)
                                      -> resemblio-style-dictionary (future)
```

This is the Figma converter. Every brand team and design system maintainer already lives in Figma; dropping a Resemblio-derived Variables payload into a file lets the in-house design org adopt an extracted brand without rebuilding tokens by hand.

## Install

```bash
pip install resemblio-figma
```

Or from a workspace checkout:

```bash
cd projects/Resemblio/code/converters/figma
pip install -e .
```

## Usage

### Python API

```python
import requests
from resemblio_figma import dtcg_to_figma_variables

resp = requests.post(
    "https://api.resemblio.com/v1/extractions",
    headers={"Authorization": "Bearer rsk_..."},
    json={"url": "https://stripe.com", "private": False},
).json()

payload = dtcg_to_figma_variables(resp["dtcg"], source_url=resp["manifest"]["source_url"])

import json
with open("figma-variables.json", "w") as f:
    json.dump(payload.model_dump(mode="json"), f, indent=2)
```

### CLI

```bash
# From a saved manifest
python -m resemblio_figma manifest.json --out figma-variables.json

# From stdin via curl piped through jq
curl -s -H "Authorization: Bearer rsk_..." \
  -X POST https://api.resemblio.com/v1/extractions \
  -d '{"url":"https://stripe.com"}' \
  | jq .dtcg \
  | python -m resemblio_figma - --out figma-variables.json
```

## Sample input -> output

**Input (Resemblio DTCG manifest, abridged):**

```json
{
  "color": {
    "brand-primary": {"$value": "#cc3344", "$type": "color"}
  },
  "dimension": {
    "radius-md": {"$value": "8px", "$type": "dimension"}
  },
  "fontFamily": {
    "body": {"$value": "Inter", "$type": "fontFamily"}
  }
}
```

**Output (Figma Variables payload, abridged):**

```json
{
  "collections": [
    {
      "id": "collection-colors",
      "name": "Colors",
      "modes": [{"modeId": "mode-light", "name": "Light"}]
    },
    {
      "id": "collection-spacing",
      "name": "Spacing",
      "modes": [{"modeId": "mode-light", "name": "Light"}]
    },
    {
      "id": "collection-typography",
      "name": "Typography",
      "modes": [{"modeId": "mode-light", "name": "Light"}]
    }
  ],
  "variables": [
    {
      "id": "collection-colors::brand-primary",
      "name": "Color/Brand/Primary",
      "resolvedType": "COLOR",
      "collectionId": "collection-colors",
      "valuesByMode": {
        "mode-light": {"r": 0.8, "g": 0.2, "b": 0.266667, "a": 1.0}
      }
    },
    {
      "id": "collection-spacing::radius-md",
      "name": "Spacing/Radius/Md",
      "resolvedType": "FLOAT",
      "collectionId": "collection-spacing",
      "valuesByMode": {"mode-light": 8.0}
    },
    {
      "id": "collection-typography::body",
      "name": "Typography/Body",
      "resolvedType": "STRING",
      "collectionId": "collection-typography",
      "valuesByMode": {"mode-light": "Inter"}
    }
  ],
  "figma_schema_version": 1
}
```

## Mapping table

| DTCG group | Figma collection | Figma type | Notes |
|---|---|---|---|
| `color.*`       | `Colors`     | `COLOR`  | Hex -> RGBA floats 0.0-1.0 |
| `dimension.*`   | `Spacing`    | `FLOAT`  | `px` passthrough; `rem` multiplied by 16 |
| `fontFamily.*`  | `Typography` | `STRING` | Family name verbatim |
| `number.*`      | `Numbers`    | `FLOAT`  | Numeric passthrough |

Unrouted groups in the manifest are skipped silently. Unparseable leaf values (named colors, `rgb()`, malformed dimensions) are also skipped; the converter degrades gracefully rather than emitting broken Variables.

## Name hierarchy

DTCG leaf names like `brand-primary` or `color.brand.primary` map to Figma's slash-hierarchy convention (`Brand/Primary`, `Color/Brand/Primary`). Figma uses the slash separator to group Variables visually in the UI, so the imported set lands in the right nested groups without manual cleanup.

## Schema version

This package emits `figma_schema_version=1` (single Light mode). A future `schema_version=2` will add multi-mode emission (Light + Dark) once Resemblio's extractor produces explicit dark variants. Auto-inversion does not belong here - the converter should not invent design decisions the source did not make.

## License

MIT. See `LICENSE`.

## Caveats

- v1 emits a single Light mode per collection. Dark mode is the extractor's responsibility, not the converter's.
- Only hex color values are honored. `rgb()`, `hsl()`, and named colors are skipped. Resemblio's extractor emits hex by convention.
- Figma Variables are unitless `FLOAT`s; the converter normalizes `rem` to pixels (multiplied by 16) so spacing tokens stay in one numeric universe.
