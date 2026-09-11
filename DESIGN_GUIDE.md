# Shifix — Premium Design Guide (dark glass, Montserrat, SVG-only)

> **Companion to `design-reference.html`** — that HTML *is* the live pattern library. This MD is the “what / why / how” to use when you remodel anything (cards, modals, pills, tables) without reinventing styles.

---

## 0) TL;DR for the frustrated revamp (the password card you pasted)

**Before (ugly):**

```html
<div class="lpv-latest-card ">
  <div class="field-name">password<span>[input]</span><span>field_change</span></div>
  <div class="field-meta">Yahoo pass.html • 2026-09-03 11:36:04</div>
  <div class="field-value">spiff <button>copy</button></div>
</div>
```

Flat, no icon, badge crammed in title, `field_change` typo-ish label, thin spacing, value has no inset, no accent, copy 20px, no hover lift. It reads as a dump, not a card.

**After (premium pattern from `design-reference.html`):**

```html
<div class="lpv-card lpv-card--change">
  <div class="lpv-card__accent"></div>
  <div class="lpv-card__head">
    <div class="lpv-card__icon"><svg>…lock…</svg></div>
    <div class="lpv-card__title">
      <span class="lpv-card__field">password</span>
      <span class="lpv-card__type">field <em>input</em></span>
    </div>
    <span class="lpv-badge lpv-badge--change">change</span>
    <span class="lpv-card__time">11:36:04</span>
  </div>
  <div class="lpv-card__value">
    <span class="lpv-card__val-text">spiff</span>
    <button class="lpv-card__copy">copy</button>
  </div>
  <div class="lpv-card__foot">
    <span class="lpv-card__page">Yahoo pass.html</span> • 2026-09-03
  </div>
</div>
```

Left 3px accent (`#4aa3ff` change / `#4ade80` final), 32px icon chip, stacked title, badge right, value inset (`rgba(0,0,0,0.22)` / final green wash), page pill, 44px copy, top 1px shine, hover `translateY(-1px)` + `shadow 0 14 36 -18`. Same shell for `final / click / submit` — just swap `lpv-card--variant` + `lpv-badge--variant`.

Copy this block verbatim. That’s the pattern.

---

## 1) Principles (research-backed)

### Glassmorphism — when & how to use it
- **Sweet spot:** `backdrop-filter: blur(10–22px)` + `rgba(255,255,255,0.05–0.10)` (dark glass) + `1px solid rgba(255,255,255,0.08–0.18)` + `border-radius 12–20` + `0 8 32 rgba(0,0,0,0.2)` — core recipe [dev.to • Glassmorphism Complete Guide 2026](https://dev.to/imran_khan_a3cc224344dbcf/glassmorphism-ui-template-complete-guide-free-downloads-2026-5c80)[uxpilot.ai • 12 Glassmorphism Features](https://uxpilot.ai/blogs/glassmorphism-ui).
- **Background matters more than the panel.** Put soft orbs/mesh behind glass (violet/navy), not high-frequency noise, so blur has something to refract [medium.com • Dark Glassmorphism 2026](https://medium.com/@developer_89726/dark-glassmorphism-the-aesthetic-that-will-define-ui-in-2026-93aa4153088f).
- **Readability > effect.** Add semi-opaque tint (10–30%) behind text, opposite tone text (dark glass → light text), optional 1px text-shadow, never pure white border in dark (use `#8a8a92` grey) [uxpilot.ai](https://uxpilot.ai/blogs/glassmorphism-ui).
- **Use selectively:** 20–30% of surfaces max, 5–8 blurred elements/page, `will-change: transform; transform: translateZ(0)` for animating orbs, bake static blur into image for perf [dev.to](https://dev.to/imran_khan_a3cc224344dbcf/glassmorphism-ui-template-complete-guide-free-downloads-2026-5c80)[freefrontend.com](https://freefrontend.com/css-glassmorphism/).

### Card UI — what makes a card scannable
- **One card = one concept**, whole card affords click (Fitts), subtle shadow = signifier [uxplanet.org • Best Practices for Cards](https://uxplanet.org/best-practices-for-cards-fa45e3ad94dd).
- **Padding 16–24, title 16–20 semibold, body ≥14, metadata 10–12 muted**, gap 8–12 title↔body, 16–32 between cards, max 2–3 sizes per card [thehangline.com • 12 Best Practices](https://www.thehangline.com/card-ui-design-best-practices-how-to-create-cards-that-improve-ux/)[bricxlabs.com • 10 Card Examples](https://bricxlabs.com/blogs/card-ui-design-examples).
- **Hierarchy in <2s:** bold weight + darker color + top-left placement + limited copy (1–2 lines, truncate >90) [digitalthriveai.com](https://digitalthriveai.com/en-us/resources/web-design/ui-card-design/)[alfdesigngroup.com](https://www.alfdesigngroup.com/post/best-practices-to-design-ui-cards-for-your-website).
- **Grid:** desktop 3–4 cols (decision) / 4–6 (browse), tablet 2–3, mobile 1 (sometimes 2 squares). Gutter ≥12 else cards merge. Infinite scroll for discovery, pagination for bookmarkable results [alfdesigngroup.com](https://www.alfdesigngroup.com/post/best-practices-to-design-ui-cards-for-your-website).
- **States:** hover lift `translateY(-2→4px)` + shadow `12–24 blur`, focus ring, skeleton/shimmer loading [layoutscene.com • Card Patterns 2026](https://www.layoutscene.com/card-ui-design-patterns-guide-2026/)[magicui.design • Cards Guide](https://magicui.design/blog/cards-ui-design).

### Typography — Montserrat only
- **Montserrat geometric = premium for dark dashboards.** Pairing advice if you ever need a second family: `Montserrat + Merriweather / Open Sans / Source Sans Pro` [madegooddesigns.com](https://madegooddesigns.com/montserrat-font-pairing/)[typographysmith.com](https://typographysmith.com/best-font-pairings/montserrat). Here we keep it solo for consistency.
- **Load 2–3 weights max** (`400,600,700,800` → each +20–30KB), `600–700` headings, `400–500` body, line-height 1.4–1.6 body / 1.1–1.3 titles, ratio 2:1 [madegooddesigns.com](https://madegooddesigns.com/montserrat-font-pairing/).

---

## 2) Tokens (copy from `design-reference.html :root`)

```css
:root{
  --bg-primary:#070709; --bg-secondary:#0a0a0c; --bg-card:#111114; --bg-hover:#19191d; --bg-input:#16161a;
  --text-primary:#ffffff; --text-secondary:#c9c9cf; --text-muted:#8a8a92;
  --border:rgba(255,255,255,0.08); --border-strong:rgba(255,255,255,0.12);
  --accent-violet:#6c5ce7; --accent-blue:#4aa3ff; --accent-green:#4ade80; --accent-amber:#fbbf24; --accent-purple:#b48eff;
  --radius-sm:8px; --radius-md:12px; --radius-lg:16px; --radius-xl:20px; --radius-pill:999px;
  --shadow-card:0 10px 28px -18px rgba(0,0,0,0.9);
  --shadow-modal:0 28px 80px rgba(0,0,0,0.65);
  --blur-glass:blur(22px) saturate(1.15);
  --space-1:4px; --space-2:8px; --space-3:12px; --space-4:16px; --space-5:20px; --space-6:24px; --space-8:32px;
}
```

**Usage:** never hardcode a hex in a component — use `var(--bg-card)` etc. That’s how a revamp stays consistent.

---

## 3) Components

### 3.1 Card — `lpv-card` (the one you asked to revamp)
**Anatomy:** `__accent (3px left bar) → __head (icon 32 + title stack + badge + time) → __value (inset + val-text + copy 44px) → __foot (page pill + dot + date)` — see live in `design-reference.html`.

**Variants:** `lpv-card--change` (blue `#4aa3ff`), `lpv-card--final` (green `#4ade80` + green wash value), `lpv-card--click` (`#b48eff`), `lpv-card--submit` (`#fbbf24`). Only these 4 classes change.

**Spacing:** head `14 14 12 17`, value `14 14 14 17`, foot `10 14 12 17`; gap 8–10 inside head, 10 inside value, 8 in foot; grid gap 14; card pad never <14.

**Type:** field `13/700` line 1.1, type `10/600` muted + `<em>` pill `9/700` uppercase, time `10` muted, value `14/700 -0.01em`, foot `10–11`.

**States:** `hover: translateY(-1px) + border 0.14 + shadow`, `active: translateY(0)`, `copy.copied: green border + bg 0.10`, focus-visible 2px. Tap target ≥44px.

### 3.2 Pills / Badges
` .pill` (muted), `.pill.violet/green/blue`, `.lpv-badge--change/final/click` (9/800, 4/8 pad, 999 pill), `.lpv-filter-pill` (10/700 uppercase, SVG 12, active = white bg). Use for page counts, event types.

### 3.3 Buttons
` .btn` (10/14 pad, 10 radius, 12/700), `.btn.primary` (white bg), `.btn.ghost`, `.btn.sm` (6/10). Hover lift 1px, active 0. Don’t put >1 primary per card.

### 3.4 Inputs
`.search` (11/12/11/36 pad, 12 radius, bg-input, border, focus `0 0 0 2px rgba(255,255,255,0.07)`). Always pair with leading SVG 14 muted.

### 3.5 Modal — KLG-identical (not glass)
`#lpvProfileModal` is **identical to KLG** — the only delta is `z-index:10050` so LPV stacks above profiles. Inherits ` .klg-modal-overlay` (`fixed inset:0` `rgba(0,0,0,0.85)` `backdrop-filter: blur(8px)` `opacity/visibility` transition), ` .klg-modal` (`max-width 800` incl. LPV override, `radius 16` `bg var(--bg-card)` `border var(--border)` `shadow 0 25 80` `max-height 85vh`), header `20 24 gap0 transparent`, body `16 24` scroll, footer `16 24`. Close `36×36 8 radius var(--bg-hover)` → hover `var(--bg-input)`/`danger`. Premium cards inside keep their own glass/accent — the shell itself is flat KLG. Previous premium `radial 1200×600 violet 0.14 + rgba(0,0,0,0.78) blur 22 saturate 1.15` + `radius 20 880/88vh 38×38 rotate 90` has been removed for parity. Body gets `.lpv-modal-open {overflow:hidden}`.

**Merged title:** `Final values — latest per field (most recent = final value)` — “latest per field” IS the final values. JS `_latestPerField()` → render as `lpv-card--final` only (green `4ade80` wash), filtered by `(filter === 'all' || filter === 'final')`. Do not render separate final row + latest grid — there is one canonical set.

### 3.6 Sticky bar
Search + page select + pills in `position:sticky top:0`. KLG-identical bar has **no blur/gradient** — it is `var(--bg-card)` `border-bottom var(--border)` (overrides any inline `linear-gradient… blur(12px)` via `!important`). Keeps context while scrolling without extra glass layers.

---

## 4) Revamp Checklist (use every time)

1. **Audit:** screenshot before, list every hardcoded color/font/emoji, measure pad/gap/radius.
2. **Tokens first:** replace all hards with `var(--*)` from §2. Add missing token to `:root` if needed (don’t inline).
3. **Shell:** pick the component shell from `design-reference.html` — don’t invent a new card. Copy the whole block, only swap content + variant class.
4. **Hierarchy:** one concept per card, headline 13–16/700 top-left, meta 10–12 muted below, value inset large. Truncate value >90 + show full on hover/`title`.
5. **Iconography:** every badge/pill gets an inline `viewBox 0 0 24 24` stroke SVG from the reference (lock for password, user for username, mouse for click, send for submit). `aria-hidden="true"` on icon, no emoji.
6. **States & a11y:** hover, active, focus-visible, empty, loading skeleton, error, 44px taps, 4.5:1 contrast (test with axe), screen-reader: value is real text, copy has `aria-label="Copy value"`.
7. **Blur budget:** count `backdrop-filter` uses — keep ≤8, promote animating orbs only.
8. **Responsive:** check 1280 (2 cols), 760 (1 col), 480 (full width + stacked foot). Never shrink desktop card.
9. **Motion:** `cubic-bezier(0.22,1,0.36,1) 0.32–0.38s` for entrance; respect `prefers-reduced-motion`.
10. **Verify:** hard refresh, compare before/after side-by-side in reference HTML, run `node --check` on Admin.html JS, push branch.

---

## 5) Implementation in this repo

**Pattern file:** `design-reference.html` (open in browser) — copy any block’s source.

**Apply to LPV cards** in `Admin.html`:

- Replace the old `lpv-latest-card` CSS (around `~3450`) with the `lpv-card` system from the reference (already staged as `lpv-card` in reference; next commit will swap Admin.html’s generator).
- In JS `renderLpvProfileLogs()` / `_latestPerField` path, change the template that builds `latestCards` from:
  ```js
  '<div class="lpv-latest-card ...">...'
  ```
  to the `lpv-card` markup shown in §0 (keep the same data: `fieldLabel`, `fieldType`, `val`, `page`, `time`, `badgeVariant`). The reference file’s `<pre>` is the exact string to paste.
- Keep `data-latest-copy` → `lpv-card__copy` click handler (already wired to `.copied`).

**Page selector:** stays as `<select id="lpvProfileModalPageSelect">` inside the sticky bar — the reference shows the same tokens (search + select share `.search` style).

---

## 6) Accessibility & Performance notes

- **Contrast:** dark glass text must be `var(--text-primary)` on `rgba(0,0,0,0.22)` inset, not on raw blur. Test with WCAG AA.
- **Theme:** if you ever add light, re-tune blur per theme (light needs less blur, darker border) [uxpilot.ai](https://uxpilot.ai/blogs/glassmorphism-ui).
- **Perf:** `backdrop-filter` is GPU-heavy — avoid animating it, limit to modal + sticky bar + ≤4 cards blurring at once.

---

## 7) References (search 2026-09-04)

- Glassmorphism core recipe & dark variant [dev.to](https://dev.to/imran_khan_a3cc224344dbcf/glassmorphism-ui-template-complete-guide-free-downloads-2026-5c80)
- 12 features, transparency/blur sweet spot, contrast & theme testing [uxpilot.ai](https://uxpilot.ai/blogs/glassmorphism-ui)[invernessdesignstudio.com](https://invernessdesignstudio.com/glassmorphism-what-it-is-and-how-to-use-it-in-2026)
- Dark Glassmorphism pillars, multi-layer alpha gradients, border light-catcher, performance [medium.com](https://medium.com/@developer_89726/dark-glassmorphism-the-aesthetic-that-will-define-ui-in-2026-93aa4153088f)
- Card spacing 16–24 / title 16–20 / body ≥14 / hover lift & focus [thehangline.com](https://www.thehangline.com/card-ui-design-best-practices-how-to-create-cards-that-improve-ux/)[bricxlabs.com](https://bricxlabs.com/blogs/card-ui-design-examples)[uitop.design](https://uitop.design/blog/design/card-ui-design/)
- Grid 3–4/2–3/1 cols + gutter ≥12 [alfdesigngroup.com](https://www.alfdesigngroup.com/post/best-practices-to-design-ui-cards-for-your-website)
- Card patterns, affordance, skeletons [layoutscene.com](https://www.layoutscene.com/card-ui-design-patterns-guide-2026)[magicui.design](https://magicui.design/blog/cards-ui-design)[digitalthriveai.com](https://digitalthriveai.com/en-us/resources/web-design/ui-card-design/)
- Montserrat weights & pairing [madegooddesigns.com](https://madegooddesigns.com/montserrat-font-pairing/)[typographysmith.com](https://typographysmith.com/best-font-pairings/montserrat)
- Code snippets for glass cards [freefrontend.com](https://freefrontend.com/css-glassmorphism/)

---

*Keep `design-reference.html` at repo root — it’s the contract. Any new screen you build, start by copying from there.*
