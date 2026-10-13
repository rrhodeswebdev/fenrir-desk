---
name: Fenrir Desk
description: Local-only futures prop-trading desk; dark operator console for backtesting and running armed strategies.
colors:
  ground: "#0E1116"
  ground-deep: "#0A0C10"
  rule: "#262D37"
  rule-strong: "#3A4350"
  idle: "#1E242D"
  ink: "#E8EEF4"
  ink-2: "#C9D3DD"
  ink-3: "#8FA0B2"
  ember: "#E0A33A"
  red: "#E5484D"
typography:
  display:
    fontFamily: "Chakra Petch, sans-serif"
    fontSize: "clamp(56px, 6.4vw, 96px)"
    fontWeight: 500
    lineHeight: 0.92
    letterSpacing: "0.01em"
  headline:
    fontFamily: "Chakra Petch, sans-serif"
    fontSize: "32px"
    fontWeight: 600
    lineHeight: 1
    letterSpacing: "0.02em"
  title:
    fontFamily: "Chakra Petch, sans-serif"
    fontSize: "24px"
    fontWeight: 500
    lineHeight: 1
    letterSpacing: "0.04em"
  readout:
    fontFamily: "Chakra Petch, sans-serif"
    fontSize: "20px"
    fontWeight: 500
    lineHeight: 1
    letterSpacing: "0.04em"
  page-title:
    fontFamily: "Chakra Petch, sans-serif"
    fontSize: "15px"
    fontWeight: 600
    lineHeight: 1
    letterSpacing: "0.3em"
  account-name:
    fontFamily: "IBM Plex Mono, ui-monospace, monospace"
    fontSize: "15px"
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: "0.3em"
  body:
    fontFamily: "IBM Plex Mono, ui-monospace, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "tnum"
  value:
    fontFamily: "IBM Plex Mono, ui-monospace, monospace"
    fontSize: "13px"
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: "0.04em"
    fontFeature: "tnum"
  label:
    fontFamily: "IBM Plex Mono, ui-monospace, monospace"
    fontSize: "11px"
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: "0.24em"
rounded:
  none: "0px"
spacing:
  s1: "4px"
  s2: "8px"
  s3: "12px"
  s4: "16px"
  s5: "24px"
  s6: "32px"
  s7: "48px"
components:
  control:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 24px"
    height: "44px"
  control-disabled:
    textColor: "{colors.ink-3}"
  control-edge-kill:
    backgroundColor: "{colors.red}"
    width: "14px"
  control-edge-caution:
    backgroundColor: "{colors.ember}"
    width: "14px"
  nav-item:
    textColor: "{colors.ink-3}"
    typography: "{typography.label}"
    padding: "0 24px"
    height: "44px"
  nav-item-active:
    backgroundColor: "{colors.ground-deep}"
    textColor: "{colors.ink}"
    padding: "0 12px"
    height: "44px"
  code-cell:
    backgroundColor: "{colors.ink-2}"
    width: "22px"
    height: "10px"
  code-cell-large:
    backgroundColor: "{colors.ink-2}"
    width: "40px"
    height: "14px"
  code-cell-idle:
    backgroundColor: "{colors.idle}"
  code-cell-caution:
    backgroundColor: "{colors.ember}"
  code-cell-alarm:
    backgroundColor: "{colors.red}"
  meter-segment:
    backgroundColor: "{colors.idle}"
    height: "10px"
  meter-segment-on:
    backgroundColor: "{colors.ink-2}"
  sleeve:
    backgroundColor: "{colors.ground}"
    rounded: "{rounded.none}"
    padding: "24px"
  sleeve-hover:
    backgroundColor: "{colors.ground-deep}"
  kill-dialog:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "32px"
    width: "520px"
---

# Design System: Fenrir Desk

## Overview

**Creative North Star: "The Catalogued Sleeve"**

Every armed account is filed like a factory record sleeve: a matte Night ground, one Ice hairline plot owning the field, and a catalog number (LFE 0507) set at display scale as the headline. The desk is read from across the room on a second monitor, so the field is calm and monochrome by default. Anything abnormal carries colour; nothing else does.

Density is terminal-grade. Every label is tracked IBM Plex Mono capitals, every figure is tabular, and Chakra Petch is reserved for the identifiers and readouts a trader scans for. Structure comes from 1px hairlines and outlined rectangles, never from fills, cards, radius or shadow. The system rejects the KPI-card trading dashboard with green/red P&L tiles.

Dark only. There is no light theme, no theme switch, and no `.dark` class variant; the dark palette is the root palette.

**Key Characteristics:**
- Night ground, Ice and Steel hairlines, Slate labels; colour quarantined to state.
- Square everything: radius 0, outlined controls, 1px rules.
- Catalog-number headlines in Chakra Petch; tracked mono caps for every label.
- Segmented, cell-based readouts instead of smooth bars and badges.
- Stepped 90ms state switches; one authored ease-out moment.

## Colors

A cold blue-grey monochrome on near-black, with two signal colours that only ever mean something.

### Primary
- **Ice** (ink): primary text, catalog numbers, the live dial trace, fill ticks, the "now" dot, hovered control outlines.

### Secondary
- **Ember** (ember): caution and selected. Caution code cells, safety-ring at-risk, meter segments past the safety mark, the safety mark itself, the hover/focus edge block on plain controls, the active nav block, focus outline, text selection, HALT and ALERT journal kinds (REJECT stays plain Slate: a pre-trade rejection means the guard worked and needs no action), the synthetic-data notice.

### Tertiary
- **Signal Red** (red): halt, breach and destructive. Alarm code cells, floor-ring at-risk, meter segments at 100%, the kill/resolve edge block, the kill-dialog border, alarm sleeve outline, MISMATCH journal kind, the alarm name beside the top-strip code strip.

### Neutral
- **Night** (ground): the page ground and every surface, including dialogs and sleeves.
- **Night Deep** (ground-deep): the one tonal step: active nav cell and sleeve hover.
- **Steel** (ink-2): the normal state. Normal code cells, lit meter segments, secondary text, target ring, journal messages.
- **Slate** (ink-3): labels, control outlines, rings, hour numerals, muted figures.
- **Unlit** (idle): unlit meter segments, idle code cells, mini-dial grid.
- **Hairline** (rule): section dividers, journal row rules, sleeve separators.
- **Hairline Strong** (rule-strong): dial grid, disabled control outline, scrollbar thumb, link underline.

### Named Rules
**The Quarantine Rule.** Colour appears only for abnormal or selected state: code strips, segmented readouts, active-edge blocks, at-risk rings. Steel is normal, Ember is caution or selected, Signal Red is halt, breach or destructive. Never decorative; if removing the colour loses no information, the colour was wrong.

**The No Green Rule.** Normal is Steel, not green. Profit and loss are figures with a sign, never green or red tiles.

### shadcn/ui and Tailwind v4 mapping

Paste into `web/src/index.css` (after `@import "tailwindcss";`). Values come from the frontmatter; do not redefine them elsewhere.

```css
:root {
  color-scheme: dark;

  /* Fenrir tokens */
  --ground: #0E1116;
  --ground-deep: #0A0C10;
  --rule: #262D37;
  --rule-strong: #3A4350;
  --idle: #1E242D;
  --ink: #E8EEF4;
  --ink-2: #C9D3DD;
  --ink-3: #8FA0B2;
  --ember: #E0A33A;
  --red: #E5484D;
  --track-label: 0.24em;
  --track-value: 0.04em;
  --step: 90ms;

  /* shadcn theme variables (dark only, no .dark block) */
  --radius: 0rem;
  --background: var(--ground);
  --foreground: var(--ink);
  --card: var(--ground);              /* Card is replaced by the sleeve; no fill */
  --card-foreground: var(--ink);
  --popover: var(--ground);
  --popover-foreground: var(--ink);
  --primary: var(--ink);              /* neutral on purpose: a stray default never adds colour */
  --primary-foreground: var(--ground);
  --secondary: var(--idle);
  --secondary-foreground: var(--ink);
  --muted: var(--idle);
  --muted-foreground: var(--ink-3);
  --accent: var(--ground-deep);       /* hover/selected row ground */
  --accent-foreground: var(--ink);
  --destructive: var(--red);
  --destructive-foreground: var(--ground);
  --border: var(--rule);
  --input: var(--ink-3);              /* control outlines */
  --ring: var(--ember);               /* focus outline */
  --chart-1: var(--ink);              /* live trace */
  --chart-2: var(--ink-2);            /* normal / target */
  --chart-3: var(--ink-3);            /* rings, overnight */
  --chart-4: var(--ember);            /* caution only */
  --chart-5: var(--red);              /* breach only */
}

@theme inline {
  --color-background: var(--background);
  --color-foreground: var(--foreground);
  --color-card: var(--card);
  --color-card-foreground: var(--card-foreground);
  --color-popover: var(--popover);
  --color-popover-foreground: var(--popover-foreground);
  --color-primary: var(--primary);
  --color-primary-foreground: var(--primary-foreground);
  --color-secondary: var(--secondary);
  --color-secondary-foreground: var(--secondary-foreground);
  --color-muted: var(--muted);
  --color-muted-foreground: var(--muted-foreground);
  --color-accent: var(--accent);
  --color-accent-foreground: var(--accent-foreground);
  --color-destructive: var(--destructive);
  --color-destructive-foreground: var(--destructive-foreground);
  --color-border: var(--border);
  --color-input: var(--input);
  --color-ring: var(--ring);
  --color-chart-1: var(--chart-1);
  --color-chart-2: var(--chart-2);
  --color-chart-3: var(--chart-3);
  --color-chart-4: var(--chart-4);
  --color-chart-5: var(--chart-5);

  --color-ground: var(--ground);
  --color-ground-deep: var(--ground-deep);
  --color-rule: var(--rule);
  --color-rule-strong: var(--rule-strong);
  --color-idle: var(--idle);
  --color-ink: var(--ink);
  --color-ink-2: var(--ink-2);
  --color-ink-3: var(--ink-3);
  --color-ember: var(--ember);
  --color-red: var(--red);

  --font-display: "Chakra Petch", sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, monospace;
  --font-sans: var(--font-mono);      /* body is mono */

  --tracking-label: var(--track-label);
  --tracking-value: var(--track-value);

  --radius-sm: 0px;
  --radius-md: 0px;
  --radius-lg: 0px;
  --radius-xl: 0px;

  --ease-trace: cubic-bezier(0.16, 1, 0.3, 1);
}
```

Tailwind's default `--spacing: 0.25rem` already matches the 4px rhythm (s1..s7 = `1, 2, 3, 4, 6, 8, 12`). Stepped transitions use `duration-90` with an arbitrary `[transition-timing-function:steps(2)]` or a small `transition-step` utility.

## Typography

**Display Font:** Chakra Petch (500, 600) (with sans-serif)
**Body Font:** IBM Plex Mono (400, 500) (with ui-monospace, monospace)

**Character:** A squared technical display face for identifiers against a neutral monospace that does everything else. The pairing is the brand wordmark's pairing, carried into the UI.

### Hierarchy
- **Display** (Chakra Petch 500, fluid 56 to 96px, 0.92): the focused account's catalog number. One per page.
- **Headline** (Chakra Petch 600, 32px, 1): dialog titles.
- **Title** (Chakra Petch 500, 24px, 1): sleeve catalog numbers.
- **Readout** (Chakra Petch 500, 20px, 1): the session clock; the dial's centre P&L uses the same face at 600, 22px.
- **Page Title** (Chakra Petch 600, 15px, 0.3em, uppercase): the page name in the top strip.
- **Account Name** (Plex Mono 500, 15px, 0.3em, uppercase): the account type under a catalog number.
- **Body** (Plex Mono 400, 13px, 1.5, tabular): descriptions, max 44ch.
- **Value** (Plex Mono 500, 13px, 0.04em, tabular): every data figure.
- **Label** (Plex Mono 500, 11px, 0.24em, uppercase, Slate): every label, nav item and control caption. Journal rows run at 12px.

### Named Rules
**The Tabular Rule.** All figures are tabular (`font-variant-numeric: tabular-nums` on the body). Numbers never shift width as they tick.

**The Self-Hosted Faces Rule.** Fonts are bundled, never fetched. The React build installs `@fontsource/chakra-petch` (import `500.css`, `600.css`) and `@fontsource/ibm-plex-mono` (import `400.css`, `500.css`) in the entry module. The app is local-only and must work offline; the Google Fonts link in `design/live.html` is prototype-only and must not be copied.

**The Legible Floor Rule.** No text below 11px, including SVG labels inside the dial.

## Layout

App shell is a two-column grid: a 184px index rail and the main column, divided by a 1px Hairline. The main column stacks a top strip (min 72px, 32px side padding), the focus field, and a lower band.

The focus field is three columns (identity, dial, data: `minmax(280px,1fr) minmax(360px,1.25fr) minmax(260px,0.8fr)`, 32px gap and padding). The lower band holds the other armed accounts as sleeves (three columns, min 220px) and the journal tape (min 320px) to the right.

Spacing is a 4px rhythm: 4, 8, 12, 16, 24, 32, 48. Controls and nav rows are 44px tall.

Responsive: at 1180px and below the dial takes a full row above identity and data, and the journal drops below the sleeves at full width. At 760px and below the rail becomes a horizontal scrolling strip at the top (32px mark, 40px items), focus, sleeves and journal stack to one column with sleeves separated by bottom hairlines, and form rows put the label above the control. At any width the top strip wraps onto another line when its content does not fit, so nothing in it is ever off-screen. No page scrolls sideways down to 390px: long keys and hashes break, and a table too wide for the column scrolls inside its own box.

## Elevation & Depth

Flat. There are no drop shadows and no elevation. Depth is conveyed by one tonal step (Night to Night Deep) and by 1px hairlines and outlines. `box-shadow` is used only as an inset 1px outline (active nav cell, alarm and caution sleeves) or an inset solid edge block (Ember hover edge on plain controls, the safety mark on meters), never blurred and never offset outward. The dialog backdrop is a flat scrim (`rgb(5 7 10 / 0.82)`).

### Named Rules
**The Flat Field Rule.** Nothing floats. A surface is distinguished by a hairline or a tonal step, never by a shadow.

## Shapes

Square everything: radius 0 on every element, including dialogs, controls, cells and focus outlines. The recurring silhouette is the outlined rectangle with a solid edge block on its right side (controls, active nav). State cells are short horizontal bars (22x10px, 40x14px large) with 4px gaps; meter segments are 20 equal cells with 3px gaps. The dial is the only circular form.

## Components

### Control (shadcn Button)
Outlined, square, captioned in tracked mono caps.
- **Shape:** square (0), 1px Slate outline, min 44px tall, 24px caption padding.
- **Edge block:** an optional 14px block on the right, separated by a 1px rule. Red for kill and resolve (destructive), Ember for caution; a plain control shows a 10px Ember inset edge on hover and focus.
- **Hover / Focus:** outline steps to Ice in 90ms `steps(2)`; focus also draws a 1px Ember outline offset 3px.
- **Disabled:** Slate text, Hairline Strong outline, not-allowed cursor.
- **shadcn:** replace Button's variants with `outline` (plain), `destructive` (red edge), `caution` (ember edge). No filled variant.

### Index Rail (shadcn: plain nav, no Sidebar chrome)
- **Style:** mark at top, page names as 44px Label rows in Slate, Ice on hover.
- **Active:** a boxed cell (Night Deep ground, 1px Slate inset outline, 12px inset from the rail edges) with an 8x16px Ember block at the right. No page numbers.
- **Mobile:** horizontal scroll strip, auto-scrolled to the active item.

### Top Strip
Page title, New York session clock (Readout), connection links as Label over Value, the system code strip with the alarming account named beside it in Red, and the kill switch at the far right.

### Code Strip (shadcn Badge replacement)
A fixed-order row of state cells, split by scope. The system strip in the top strip has Feed · Broker · Drift (shared by every account); each account's strip has Risk · Book (Risk: Ember for a safety-margin halt or daily loss past the safety mark, Red for a breach; Book: Red for a position mismatch). Stale and drift halts light the system cell, never the account. Steel normal, Unlit idle, Ember caution, Red alarm. Always carries an `aria-label` spelling out each cell's state. Replaces status Badges; there are no pill badges.

### Catalog-Number Headline
The account identifier at Display scale, the account type in Account Name style beneath, then the code strip with the mode and phase as a Label.

### Session Dial (custom SVG)
A polar plot of the session: angle is time (18:00 to 17:00 ET, hour spokes, even hours labelled), radius is equity between floor and target.
- **Rings:** floor (dashed 2/4), safety (dashed 8/4, when the account has one), start, target (Steel). Rings are Slate; floor turns Red and safety turns Ember only when at risk.
- **Trace:** overnight segment dimmed (Slate at 0.35 opacity, dotted); the live segment is a 1.2px Ice line ending in a 4px Ice dot.
- **Fill ticks:** 1.5px Ice radial ticks at each fill. A dotted flat-by spoke marks the forced-flat time.
- **Hub:** Night disc with today's P&L at its centre.
- **Mini:** sleeves use the same dial without labels or ticks, trace at 2.5px.

### Segmented Meter (shadcn Progress replacement)
Label and Value on one row above 20 cells, 10px tall. Lit cells Steel; past the safety fraction they turn Ember; at 100% Red. The safety threshold is marked by an Ember tick above its cell. "No limit" renders unlit cells with a muted figure.

### Sleeve (replaces shadcn Card)
- **Corner Style:** square (0).
- **Background:** Night; Night Deep on hover. No card fill, no shadow.
- **Border:** 1px Hairline separators between sleeves; alarm sleeves get a 1px inset Red outline, caution sleeves Ember. Idle (dry run) sleeves dim their catalog number and mini dial (0.55 opacity, greyscale).
- **Internal Padding:** 24px.
- **Content:** catalog number (Title) and code strip, type and mode Label, alert line in the state colour, mini dial beside a Today / DD left / Position fact list, and a Resolve control when in alarm. Clicking a sleeve focuses that account.

### Journal Tape (shadcn Table, unstyled)
Rows of time and message on a 72px / 1fr grid, 12px, 1px Hairline above each row. Event kind prefixed in tracked caps: Slate by default, Ember for HALT and ALERT, Red for MISMATCH. Ends with an underlined "Full journal" link.

### Kill-Switch Confirmation (shadcn AlertDialog)
Square Night panel, 1px Red border, 32px padding, max 520px wide, over a flat scrim. Headline title, a plain-language consequence line, the counts as Values, then a red-edge confirm ("Flatten and disarm all") and a plain Cancel. One clear step, never a nested confirm.

### Motion
State switches (hover, focus, sleeve hover, outline changes) are stepped: 90ms `steps(2)`, no easing. The one authored moment: the focused dial's live trace draws itself over 900ms with `cubic-bezier(0.16, 1, 0.3, 1)` when focus changes. `prefers-reduced-motion: reduce` disables both.

## Do's and Don'ts

### Do:
- **Do** keep every surface Night (#0E1116) and separate regions with 1px Hairline rules.
- **Do** use Steel for normal, Ember for caution or selected, Signal Red for halt, breach or destructive, and nothing else for colour.
- **Do** set every label in IBM Plex Mono 500, 11px, uppercase, 0.24em tracking, in Slate.
- **Do** use Chakra Petch only for catalog numbers, readouts, page and dialog titles.
- **Do** self-host the fonts through `@fontsource`; the app must render identically offline.
- **Do** put the destructive edge block on the right of kill and resolve controls, and confirm the kill switch in an AlertDialog with a Red border.
- **Do** give every code strip and meter an `aria-label` that states each cell's state in words.

### Don't:
- **Don't** add a light theme, a theme toggle, or a `.dark` variant.
- **Don't** use colour decoratively, including green for profit or red for loss on figures.
- **Don't** round any corner; radius is 0.
- **Don't** use drop shadows, glows or elevation; inset hairlines and edge blocks only.
- **Don't** use KPI cards, filled buttons or pill badges; use sleeves, outlined controls and code strips.
- **Don't** load fonts from Google Fonts or any CDN in the app.
- **Don't** ease ordinary state changes; only the dial trace draw is eased.
