---
version: 1
slug: "design-live-html"
primary_target: "design/live.html"
related_targets: []
---

# Live page (design prototype)

Scope: the Live page plus the app shell; it sets the visual system every other page inherits. Mode: Operate.
Audience and job: the trader at the desk during market hours, glancing from a second monitor: is every armed account safe, which one needs me, act now (resolve, disarm, kill).
Constraints: dark only; brand palette and faces from assets/brand/README.md; terse operator voice; synthetic data labelled as such; implemented later with React + shadcn/ui + Tailwind v4.

## Direction contract

THESIS: Every armed account is a catalogued sleeve: matte ground, one white hairline plot owns the field, a catalog number as the headline. Refuses the KPI-card trading dashboard with green/red P&L tiles.

OWN-WORLD: Night ground, Ice hairlines and plots, tracked IBM Plex Mono caps for every label, Chakra Petch for the catalog numbers. Colour is quarantined to code strips, segmented signal readouts and the active-edge block on controls: Steel = normal, Ember = caution or selected, Signal Red = halt or breach. Outlined rectangular controls, no radius, no shadows.

STORY: The trader sees one calm field; anything abnormal carries colour and nothing else does. They read the focused account's session dial (rings = floor, safety margin, start, target), check the other sleeves' code strips, and resolve, disarm or kill without hunting.

FIRST VIEWPORT: Left index rail (mark, page names, LIVE in a boxed active cell; no page numbers, because page order carries no information and the craft floor bans decorative section numbers). Top strip: session clock (New York), links (LSE, NT, heartbeat) as a code strip with the alarming account named beside it, kill switch at top right with a red edge block. Upper field: the most severe account is focused by default; its catalog number at display scale left, polar session dial centre, transmission-data column right with segmented readouts for drawdown, daily loss, contracts, target. Lower band: the other armed accounts as sleeves; journal tape bottom right.

FORM: Factory catalog sleeve (challenger brand-identity-canon-saville-catalog-sleeve, bolder register, re-roll 2), seed key 0a094294.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
