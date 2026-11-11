# Morrow interface system

This system applies to the Stage 4 web app. Components use native HTML controls and the existing no-build CSS/JavaScript setup.

## Visual foundation

- **Brand:** Morrow, “your money for tomorrow”. Use the supplied logo asset as a single, aspect-ratio-preserving lockup.
- **Core palette:** deep navy (`--navy`) for trust and emphasis, refined blue (`--blue`) for primary actions, white surfaces over a cool light background.
- **Semantic palette:** mint for completed/success, amber for pending/attention, rose for errors and destructive actions. Do not use these colors as decoration without status meaning.
- **Typography:** system sans stack, compact display headings, readable body copy, and tabular numerals for balances and amounts.
- **Layout:** 4 px spacing base, 8/12/16/20/24/32 px spacing tokens, 10/14/20 px component radii, and restrained card/floating shadows.

## Tokens

The bundled CSS defines the color, spacing, type, radius, focus, and shadow tokens in `:root`. New components should reference these tokens instead of adding one-off values. Financial values should keep tabular numerals and remain visually distinct from labels and helper text.

## Components and behavior

- **Navigation:** persistent desktop navigation; horizontally scrollable mobile navigation with an inset keyboard focus ring. Current route uses `aria-current="page"`.
- **Buttons:** primary, secondary, quiet, and danger variants. All controls meet a 44 px minimum height; disabled actions are visually muted and use a non-interactive cursor.
- **Fields:** visible labels, full-width controls, clear focus ring, muted placeholder and helper text. Keep native input semantics and autocomplete hints.
- **Panels and lists:** white bordered cards, consistent radius, modest shadow. Activity, request, and authorization lists separate rows with rules rather than nested cards.
- **Status:** status pills use semantic color and visible text; meaning must not rely on color alone.
- **Feedback:** success, error, uncertainty, info, loading, empty, and disabled states are distinct. Dynamic status/loading updates use accessible live/status roles where applicable.
- **Accessibility:** skip link, visible keyboard focus, responsive layouts, reduced-motion support, semantic landmarks, and no icon-only action without an accessible name.

## Responsive rules

The layout has desktop and compact breakpoints at 760 px, 430 px, and 360 px. Grids collapse to one column on narrow screens; two-column forms stack; long navigation scrolls horizontally; values may wrap only where the amount remains readable. Preserve 320 px minimum viewport support and avoid horizontal page scrolling.
