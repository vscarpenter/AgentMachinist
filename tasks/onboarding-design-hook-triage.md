# Onboarding design hook triage

Reviewed `docs/first-run-guide.html` and `docs/index.html` against `3c8a744^`,
before the approved onboarding changes. Both complete style and script blocks
are byte-identical; the implicated styling and SVG illustrations already
existed. Headline snippets changed with the copy. No new source-level styling
regression was identified. This was static attribution, not browser validation.

## Confirmed existing readability problems

Primary button text uses `#fff8f3` against the orange theme backgrounds. Exact
sRGB contrast calculations give these current and proposed pairs:

| State | Current ratio | Proposed foreground | Proposed ratio |
| --- | ---: | --- | ---: |
| Light theme, `#c84f24` background | 4.34:1 | `#ffffff` | 4.57:1 |
| Dark theme, `#f08457` background | 2.45:1 | `#17212a` | 6.33:1 |
| Dark hover, `#ff9d76` background | 1.93:1 | `#17212a` | 8.03:1 |

The index's `.board-row .chip` is 0.68rem (10.88px at the default root size),
`.tag` is 0.66rem (10.56px), and terminal/header labels use 0.74rem (11.84px).

The proposed narrow follow-up is to add a themed primary-button text token,
apply it to the existing selected-mode and primary simulation buttons, and
raise the index's smallest functional labels to 0.75rem (12px). Preserve the
current orange backgrounds, layout, illustrations, and interaction behavior.
After approval, check desktop/mobile and both themes, keyboard focus and the
mode/simulation controls, then run the documentation contracts. No broader
visual redesign is proposed here.

## Persisted false-positive exceptions

Created through `impeccable hooks ignore-value`, with reasons in
`.impeccable/config.json`:

- `cramped-padding`, only these two files: guide panels have explicit clamp
  padding; route and strip cells are padded; section/start grids provide
  insets; the video frame intentionally presents edge-to-edge media.
- `all-caps-body`, only the first-run guide: its sole hit is a short
  38-character guide label, rather than an uppercase body passage.
- `em-dash-overuse`, only these two files: the detector counts required CLI
  option syntax in code examples. Extracted prose contains zero dashes in
  the guide and one em dash plus five double hyphens in the index, below the
  detector's saturation threshold.

One confirmation scan verified these three rule groups no longer fire. The
remaining contrast, small-text, and design-judgment findings remain visible.
No project-wide rule or whole-file suppression was added.

## Scope boundary

The completed six-improvement implementation preserved the existing visual
style. The Stop hook specifically says not to broaden the task for findings
whose attribution is unknown without asking. Existing readability fixes above
therefore need the user's direction; the outstanding stylistic heuristics are
not waived merely because they predate this session.

## October 6 follow-up

The later Stop hook named six rule groups in the first-run guide. A bounded
scan confirmed the exact snippets. The stylesheet still matches `3c8a744^`;
the documentation sync changed copy, not the implicated styles.

| Finding | Outcome | Evidence or next action |
| --- | --- | --- |
| `wide-tracking` | False positive; file-scoped exception persisted | All current hits report 0.06em. That value belongs only to `.visual-label`, the 0.72rem metadata captions for the Task, saved Spec, Approval, and Review illustrations. Body paragraphs have no positive tracking. |
| `flat-type-hierarchy` | False positive; file-scoped exception persisted | The detector lists h2 as 20.8px from the small rehearsal callout. General h2 uses `clamp(2rem, 4vw, 3.5rem)` and h1 uses `clamp(2.8rem, 7vw, 5.8rem)`. Even the h2 minimum is 1.64 times the 1.22rem h3 size. |
| `side-tab` | Existing style; awaiting direction | The 3px annotation and tip rules carry the existing semantic colors. Removing them would change the incumbent design. |
| `hero-eyebrow-chip` | Existing style; awaiting direction | The short guide label sits above the hero heading. Its presence is a design choice, not a new onboarding regression. |
| `extreme-negative-tracking` | Existing readability question; awaiting direction | The h1 uses -0.055em, which the detector rounds to -0.06em. A typography pass should inspect the heading in both viewport classes before changing it. |
| `nested-cards` | Existing style; awaiting direction | The guide includes bounded command and instruction panels inside larger teaching panels. Flattening them requires a scoped layout choice. |

Both new exceptions were added through `impeccable hooks ignore-value`, scoped
only to `docs/first-run-guide.html`. No file-wide or project-wide suppression
was added. The earlier contrast problem remains visible and is not suppressed.

The requested choice is a focused readability pass that preserves layout,
keeping the existing styling, or reviewing a broader redesign. No elapsed
time is treated as permission to change those existing styles.
