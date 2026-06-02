"""Named constants for the shadcn converter.

Centralized so the slot list, defaults, and heuristics live in one place;
any other module that needs to know "what slots does shadcn define" reads
from here.
"""
from __future__ import annotations

from typing import Final

# Output schema version stamped into the rendered ``ShadcnTheme``.
SHADCN_SCHEMA_VERSION: Final[int] = 1

# Ordered list of semantic color slots shadcn/ui expects in :root and .dark.
# Order matters for stable diffable output. Reference:
# https://ui.shadcn.com/docs/theming (HSL-triple convention, Tailwind v3 era).
SHADCN_COLOR_SLOTS: Final[tuple[str, ...]] = (
    "background",
    "foreground",
    "card",
    "card-foreground",
    "popover",
    "popover-foreground",
    "primary",
    "primary-foreground",
    "secondary",
    "secondary-foreground",
    "muted",
    "muted-foreground",
    "accent",
    "accent-foreground",
    "destructive",
    "destructive-foreground",
    "border",
    "input",
    "ring",
    "chart-1",
    "chart-2",
    "chart-3",
    "chart-4",
    "chart-5",
)

# shadcn defaults in HSL-triple form for the slate / zinc neutral theme.
# Used when the Resemblio manifest carries no usable palette so the
# converter degrades to a sensible baseline instead of crashing.
SHADCN_DEFAULT_LIGHT: Final[dict[str, str]] = {
    "background": "0 0% 100%",
    "foreground": "222.2 84% 4.9%",
    "card": "0 0% 100%",
    "card-foreground": "222.2 84% 4.9%",
    "popover": "0 0% 100%",
    "popover-foreground": "222.2 84% 4.9%",
    "primary": "222.2 47.4% 11.2%",
    "primary-foreground": "210 40% 98%",
    "secondary": "210 40% 96.1%",
    "secondary-foreground": "222.2 47.4% 11.2%",
    "muted": "210 40% 96.1%",
    "muted-foreground": "215.4 16.3% 46.9%",
    "accent": "210 40% 96.1%",
    "accent-foreground": "222.2 47.4% 11.2%",
    "destructive": "0 84.2% 60.2%",
    "destructive-foreground": "210 40% 98%",
    "border": "214.3 31.8% 91.4%",
    "input": "214.3 31.8% 91.4%",
    "ring": "222.2 84% 4.9%",
    "chart-1": "12 76% 61%",
    "chart-2": "173 58% 39%",
    "chart-3": "197 37% 24%",
    "chart-4": "43 74% 66%",
    "chart-5": "27 87% 67%",
}

SHADCN_DEFAULT_DARK: Final[dict[str, str]] = {
    "background": "222.2 84% 4.9%",
    "foreground": "210 40% 98%",
    "card": "222.2 84% 4.9%",
    "card-foreground": "210 40% 98%",
    "popover": "222.2 84% 4.9%",
    "popover-foreground": "210 40% 98%",
    "primary": "210 40% 98%",
    "primary-foreground": "222.2 47.4% 11.2%",
    "secondary": "217.2 32.6% 17.5%",
    "secondary-foreground": "210 40% 98%",
    "muted": "217.2 32.6% 17.5%",
    "muted-foreground": "215 20.2% 65.1%",
    "accent": "217.2 32.6% 17.5%",
    "accent-foreground": "210 40% 98%",
    "destructive": "0 62.8% 30.6%",
    "destructive-foreground": "210 40% 98%",
    "border": "217.2 32.6% 17.5%",
    "input": "217.2 32.6% 17.5%",
    "ring": "212.7 26.8% 83.9%",
    "chart-1": "220 70% 50%",
    "chart-2": "160 60% 45%",
    "chart-3": "30 80% 55%",
    "chart-4": "280 65% 60%",
    "chart-5": "340 75% 55%",
}

# Default border radius shadcn uses (in rem). The Resemblio manifest's
# ``dimension`` group may carry a ``radius-md`` entry that overrides this.
SHADCN_DEFAULT_RADIUS_REM: Final[float] = 0.5

# Saturation threshold (HSL, 0-100) below which a color is treated as
# "neutral" and therefore eligible for the ``muted`` slot. Anything above
# is a candidate for ``primary`` / ``accent``.
NEUTRAL_SATURATION_CEILING: Final[float] = 12.0

# Lightness threshold (HSL, 0-100) above which a color is "light enough"
# to serve as a ``background`` candidate. Symmetric ``100 - x`` is the
# dark-mode background floor.
LIGHT_BACKGROUND_FLOOR: Final[float] = 92.0

# Foreground contrast pivot. A color whose lightness is <= this gets a
# light foreground (``210 40% 98%``); otherwise a dark foreground
# (``222.2 47.4% 11.2%``). Crude but deterministic; v2 should call
# wcag-contrast directly.
FOREGROUND_LIGHTNESS_PIVOT: Final[float] = 60.0

# Monospace family detection: if any font-family token contains one of
# these substrings (case-insensitive) the converter emits ``--font-mono``
# in addition to ``--font-sans``.
MONO_FAMILY_HINTS: Final[tuple[str, ...]] = (
    "mono",
    "courier",
    "consolas",
    "menlo",
    "monaco",
    "code",
    "ibm plex mono",
    "jetbrains",
    "fira code",
    "source code",
)
