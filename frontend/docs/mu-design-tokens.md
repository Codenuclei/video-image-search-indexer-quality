# Masters' Union — Design System Tokens

Extracted from the embedded design-system CSS inside  
`Downloads/Masters Union Marketing Site.html` (marker: **Masters' Union — Design System**).  
Cross-checked against the live site [mastersunion.org](https://mastersunion.org) (2026-07-22).

> **Note:** `#FFE24B` and `#111111` appear only in the HTML bundler splash/loader shell, **not** in the design-system token sheet. Canonical tokens use `#F7D344` (yellow) and `#090909` (near-black).

---

## Brand accent colors (exact hex)

| Token | Hex | Role |
|-------|-----|------|
| `--mu-sky` | `#39B6D8` | Primary brand accent (focus ring, eyebrow bar, scribble variant) |
| `--mu-orange` | `#E38330` | Secondary brand accent / warning |
| `--mu-yellow` | `#F7D344` | Signature highlight (selection, accent button, scribble) |
| `--mu-sky-wash` | `#EAF7FB` | Soft sky surface (badges) |
| `--mu-orange-wash` | `#FBEEE3` | Soft orange surface |
| `--mu-yellow-wash` | `#FEF8E0` | Soft yellow surface |

### Badge / status companion hex (not always aliased)

| Use | Hex |
|-----|-----|
| Badge sky text | `#0E6E8A` |
| Badge orange text | `#9A4E12` |
| Success | `#1F8A5B` |
| Success wash | `#E4F3EC` |
| Error | `#C8442B` |

### Live site confirmation

Public homepage CSS/SVG uses the same core accents: `#090909`, `#39B6D8`, `#F7D344` (and near-variant `#F7D544` / `#39B5D7` in places), `#E38330`.

---

## Neutrals

| Token | Hex |
|-------|-----|
| `--mu-white` | `#FFFFFF` |
| `--mu-n50` | `#FAFAFA` |
| `--mu-n100` | `#F5F5F5` |
| `--mu-n200` | `#E5E5E5` |
| `--mu-n300` | `#D4D4D4` |
| `--mu-n400` | `#A3A3A3` |
| `--mu-n500` | `#737373` |
| `--mu-n600` | `#525252` |
| `--mu-n700` | `#404040` |
| `--mu-n800` | `#262626` |
| `--mu-n900` | `#171717` |
| `--mu-n950` / `--mu-black` | `#090909` |

---

## Semantic color aliases

```css
--text-primary:    var(--mu-n950);
--text-secondary:  var(--mu-n600);
--text-tertiary:   var(--mu-n400);
--text-inverse:    var(--mu-white);
--text-on-accent:  var(--mu-n950);   /* text on yellow accent */

--surface-page:    var(--mu-white);
--surface-subtle:  var(--mu-n50);
--surface-card:    var(--mu-white);
--surface-sunken:  var(--mu-n100);
--surface-inverse: var(--mu-n950);

--border-subtle:   var(--mu-n200);
--border-default:  var(--mu-n300);
--border-strong:   var(--mu-n950);

--action-primary:        var(--mu-n950);
--action-primary-hover:  var(--mu-n800);
--action-primary-press:  var(--mu-black);
--accent-focus-ring:     var(--mu-sky);

--status-success: #1F8A5B;
--status-warning: var(--mu-orange);
--status-error:   #C8442B;
--status-info:    var(--mu-sky);
```

---

## Typography / font stack

### Families

```css
--font-sans: 'Galano Grotesque', 'Montserrat', system-ui, -apple-system, 'Segoe UI', sans-serif;
--font-display: var(--font-sans);
--font-mono: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
```

**Practical note:** Galano Grotesque is the brand primary in the design system. On [mastersunion.org](https://mastersunion.org) the live CSS loads **Montserrat** (plus Fraunces / Poppins in spots) — treat **Montserrat** as the public web fallback when Galano is unavailable/unlicensed.

### Weights

| Token | Value |
|-------|-------|
| `--fw-regular` | `400` |
| `--fw-medium` | `500` |
| `--fw-semibold` | `600` |
| `--fw-bold` | `700` |
| `--fw-black` | `800` |

### Type scale

| Role | Size | Line | Weight | Tracking |
|------|------|------|--------|----------|
| Display | `88px` (`--type-display-size`) | `0.96` | bold | `-0.03em` |
| H1 | `46px` | `1.08` | bold | `-0.02em` |
| H2 | `38px` | `1.12` | bold | `-0.015em` |
| H3 | `28px` | `1.2` | semibold | `-0.01em` |
| H4 | `20px` | `1.3` | medium | `-0.005em` |
| P1 | `18px` | `1.55` | medium | — |
| P2 | `14px` | `1.55` | medium | — |
| Caption | `12px` | `1.4` | — | — |
| Overline | `12px` | — | — | `0.14em` (uppercase) |

Body defaults: `font-family: var(--font-sans)`; `-webkit-font-smoothing: antialiased`.  
`::selection { background: var(--mu-yellow); }`

---

## Spacing scale

| Token | Value |
|-------|-------|
| `--space-0` | `0` |
| `--space-1` | `4px` |
| `--space-2` | `8px` |
| `--space-3` | `12px` |
| `--space-4` | `16px` |
| `--space-5` | `20px` |
| `--space-6` | `24px` |
| `--space-8` | `32px` |
| `--space-10` | `40px` |
| `--space-12` | `48px` |
| `--space-16` | `64px` |
| `--space-20` | `80px` |
| `--space-24` | `96px` |
| `--space-32` | `128px` |

---

## Radii

| Token | Value | Typical use |
|-------|-------|-------------|
| `--radius-none` | `0` | — |
| `--radius-sm` | `4px` | badges |
| `--radius-md` | `8px` | buttons, inputs |
| `--radius-lg` | `12px` | cards |
| `--radius-xl` | `16px` | larger panels |
| `--radius-pill` | `999px` | tags, avatars |

**Brand rule (from CSS comment):** corners stay subtle / editorial — **not** pill-y on cards.

---

## Borders & shadows

```css
--border-width: 1px;
--border-width-strong: 1.5px;

--shadow-xs: 0 1px 2px rgba(9, 9, 9, 0.06);
--shadow-sm: 0 1px 3px rgba(9, 9, 9, 0.08), 0 1px 2px rgba(9, 9, 9, 0.04);
--shadow-md: 0 4px 12px rgba(9, 9, 9, 0.08);
--shadow-lg: 0 12px 32px rgba(9, 9, 9, 0.10);
--shadow-focus: 0 0 0 3px rgba(57, 182, 216, 0.45); /* sky */
```

Buttons/inputs often use **`1.5px`** borders.

---

## Layout

```css
--container-max: 1200px;
--container-gutter: 24px;
--grid-columns: 12;

--logo-min-width: 90px;
--logo-max-width: 130px;
--logo-mark-min-h: 25px;
--logo-mark-max-h: 30px;
--logo-clearspace: 0.5;
```

---

## Motion

```css
--ease-standard: cubic-bezier(0.4, 0, 0.2, 1);
--ease-out: cubic-bezier(0.16, 1, 0.3, 1);
--ease-in: cubic-bezier(0.4, 0, 1, 1);
--dur-fast: 120ms;
--dur-base: 200ms;
--dur-slow: 360ms;
```

---

## Button patterns (`.mu-btn`)

**Base:** inline-flex, centered, `gap: space-2`, semibold, `line-height: 1`, `border: 1.5px solid transparent`, `border-radius: radius-md`, fast transitions.  
**Active:** `translateY(1px) scale(0.99)`.  
**Focus-visible:** `box-shadow: var(--shadow-focus)` (sky ring).  
**Disabled:** `opacity: 0.45; pointer-events: none`.

### Sizes

| Modifier | Height | Padding-x | Font |
|----------|--------|-----------|------|
| `--sm` | `36px` | `space-4` | `13px` |
| `--md` | `44px` | `space-5` | `15px` |
| `--lg` | `52px` | `space-6` | `16px` |

### Variants

| Modifier | Look |
|----------|------|
| `--primary` | Fill `--action-primary` (`#090909`), inverse text; hover → `--mu-n800` |
| `--secondary` | White fill, strong black border; hover → `--mu-n100` |
| `--ghost` | Transparent; hover → `--mu-n100` |
| `--accent` | Fill `--mu-yellow` (`#F7D344`), dark text; hover `brightness(0.96)` |

---

## Card patterns (`.mu-card`)

- White surface, `1px` `--border-subtle`, `--radius-lg` (`12px`), `--shadow-sm`
- `--flat`: no shadow
- `--interactive`: hover lifts `translateY(-2px)` + `--shadow-md`
- Media: `aspect-ratio: 16 / 10`, cover image
- Body: `padding: space-6`, gap `space-3`
- Title: `18px` / semibold / `-0.01em`
- Text: `14px` / medium / secondary color

---

## Other distinctive components

### Eyebrow (`.mu-eyebrow`)
Uppercase overline, semibold, secondary text; **18×2px sky bar** before the label (`::before`). `--bare` removes the bar.

### Scribble highlight (`.mu-scribble`)
Hand-drawn brand underline/highlight behind text: yellow (default), `--sky`, or `--orange`; slight `-0.6deg` rotation; irregular radius `0.2em 0.6em 0.3em 0.5em`. `--underline` is a thinner underline variant.

### Inputs (`.mu-input`)
Height `44px`, `1.5px` default border, `--radius-md`; focus → black border + sky focus shadow; invalid → error red.

### Badges (`.mu-badge`)
Uppercase `11px` semibold, `--radius-sm`. Variants: neutral / dark / sky / orange / success (washes + darkened text colors above).

### Tags (`.mu-tag`)
Pill chips for filters; pressed state = filled `#090909` + inverse text.

### Stats (`.mu-stat`)
Oversized display numerals (`clamp(40px, 6vw, 72px)`), tight tracking `-0.03em`, optional inverse mode.

### Avatar (`.mu-avatar`)
Pill; sizes 32 / 44 / 64.

---

## Quick copy-paste `:root` core

```css
:root {
  --mu-sky: #39B6D8;
  --mu-orange: #E38330;
  --mu-yellow: #F7D344;
  --mu-sky-wash: #EAF7FB;
  --mu-orange-wash: #FBEEE3;
  --mu-yellow-wash: #FEF8E0;

  --mu-white: #FFFFFF;
  --mu-n50: #FAFAFA;
  --mu-n100: #F5F5F5;
  --mu-n200: #E5E5E5;
  --mu-n300: #D4D4D4;
  --mu-n400: #A3A3A3;
  --mu-n500: #737373;
  --mu-n600: #525252;
  --mu-n700: #404040;
  --mu-n800: #262626;
  --mu-n900: #171717;
  --mu-n950: #090909;
  --mu-black: #090909;

  --font-sans: 'Galano Grotesque', 'Montserrat', system-ui, -apple-system, 'Segoe UI', sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;

  --radius-sm: 4px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --radius-xl: 16px;
  --radius-pill: 999px;
}
```

---

## Source notes

1. **Canonical source:** embedded CSS in the marketing-site HTML bundle under the “Masters' Union — Design System” section (`:root` custom properties + `.mu-*` component styles).
2. **Splash-only colors:** `#111111` body bg and `#FFE24B` loader art in the outer shell — do not treat as DS tokens.
3. **Live site:** confirms `#090909`, `#39B6D8`, `#F7D344`, `#E38330`; web fonts skew to Montserrat (Galano may be licensed/offline in the DS kit only).
4. **ui-demo:** not modified by this extraction.
