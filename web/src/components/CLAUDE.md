# web/src/components/

Components shared by more than one page. Import as `@/components/<name>`.

- Entry points: `page.tsx` (`Page({title, strip?, className?, children})`: the top strip with the `h1` and optional strip content, then the body; `className` replaces the default body padding `p-8 max-md:px-4`), `state-text.tsx` (`StateText({tone, children})`: a one-word status in Label type; tone `normal` Steel, `idle` Slate, `caution` Ember, `alarm` Red; replaces Badge).
- `ui/`: shadcn/ui components, generated then edited to meet DESIGN.md (rules in `web/CLAUDE.md`).
- Not here: page components (`../routes/`), a component only one page uses (keep it in that page's file).
- Tests: `components.test.tsx` and `ui/*.test.ts` (Vitest, `renderToStaticMarkup` or class strings, no DOM).
