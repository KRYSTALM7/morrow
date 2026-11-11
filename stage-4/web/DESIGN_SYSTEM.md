# Morrow interface system

This system applies to the Stage 4 web app. Components use native HTML controls and the existing no-build CSS/JavaScript setup.

## Visual foundation

- **Brand:** Morrow, “your money for tomorrow”. Use the supplied M wave and compass mark with a live lowercase `morrow` wordmark and the exact tagline `your money for tomorrow`. The transparent `morrow-mark.png` is the compact header and favicon mark; the wordmark and tagline remain crisp live text.
- **Core palette:** deep navy (`--navy`) for trust and emphasis, refined blue (`--blue`) for primary actions, white surfaces over a cool light background.
- **Semantic palette:** mint for completed/success, amber for pending/attention, rose for errors and destructive actions. Do not use these colors as decoration without status meaning.
- **Typography:** system sans stack, compact display headings, readable body copy, and tabular numerals for balances and amounts.
- **Layout:** 4 px spacing base, 8/12/16/20/24/32 px spacing tokens, 10/14/20 px component radii, and restrained card/floating shadows.

## Tokens

The bundled CSS defines the color, spacing, type, radius, focus, and shadow tokens in `:root`. New components should reference these tokens instead of adding one-off values. Financial values should keep tabular numerals and remain visually distinct from labels and helper text.

## Components and behavior

- **Navigation:** desktop has a keyboard-accessible header toggle that collapses the persistent sidebar into a labeled icon rail. Mobile uses the same visible hamburger to open a separate left drawer with overlay, close control, Escape handling, focus containment, scroll lock, outside dismissal, and route-based `aria-current="page"`.
- **Buttons:** primary, secondary, quiet, and danger variants. All controls meet a 44 px minimum height; disabled actions are visually muted and use a non-interactive cursor.
- **Fields:** visible labels, full-width controls, clear focus ring, muted placeholder and helper text. Keep native input semantics and autocomplete hints.
- **Panels and lists:** white bordered cards, consistent radius, modest shadow. Activity, request, and authorization lists separate rows with rules rather than nested cards.
- **Authenticated dashboard:** one dominant, interactive wallet summary. Its keyboard-accessible expansion reveals existing wallet totals, actual month-to-date ledger movement, open wallet holds, commitments, shared-expense receivables, recent wallet transactions, and forecast details. Receivables remain explicitly separate from wallet cash. Safe-to-Spend uses the existing server response and retains its unavailable state and assumptions. Compact dashboard metrics and previews remain secondary; transaction forms live on the Assistant & payments page.
- **Icons:** a small, consistent 24 px outline SVG family uses a 1.8 px stroke and inherits its color. Decorative icons are hidden from assistive technology; icon-only controls require an accessible name.
- **Status:** status pills use semantic color and visible text; meaning must not rely on color alone.
- **Feedback:** success, error, uncertainty, info, loading, empty, and disabled states are distinct. Dynamic status/loading updates use accessible live/status roles where applicable.
- **Accessibility:** skip link, visible keyboard focus, responsive layouts, reduced-motion support, semantic landmarks, and no icon-only action without an accessible name.

## Responsive rules

The layout uses 1120 px to adapt the dashboard grid, 860 px for the landing story, 760 px for the navigation drawer and mobile content collapse, 430 px for compact phones, and 360 px for the narrowest header adjustments. The expanded wallet becomes a single-column section stack on mobile and respects reduced-motion preferences. Transaction forms use a 620 px maximum width and appear on the dedicated Assistant & payments route. Dashboard previews collapse into a short vertical sequence on mobile. Values may wrap where needed while remaining readable. Preserve 320 px minimum viewport support and avoid horizontal page scrolling.
