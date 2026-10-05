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
