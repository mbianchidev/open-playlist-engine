# Accessibility

Open Playlist Engine targets **WCAG 2.2 Level AA** for its web interface and
documentation. This is a contribution target, not a certification or a claim
that every workflow has passed a comprehensive accessibility audit.

The [product principles](docs/PRODUCT.md) describe the design intent. Apply these
practices to the owner workspaces, public sharing pages, and documentation.

## Contribution guidelines

- Prefer native headings, buttons, links, labels and form controls. Use ARIA only
  where native semantics are insufficient, and keep accessible names meaningful.
- Preserve the workspace tabs' `tablist`/`tab`/`tabpanel` relationships, selected
  state and single Tab stop. Left/Right arrows move between tabs; Home/End move to
  the first/last tab. Tab must leave the tab list.
- Keep visible keyboard focus, logical reading/focus order, and a way to dismiss
  dialogs and return to the triggering control. Do not introduce keyboard traps.
- Associate validation messages with fields. Expose loading, progress, errors,
  review requirements and completed operations to assistive technology without
  relying only on visual changes.
- Meet AA contrast requirements: at least 4.5:1 for normal text, 3:1 for large
  text, and applicable 3:1 non-text contrast. Provider colors must not be the only
  indication of identity, state or availability.
- Respect `prefers-reduced-motion`. Avoid flashing and unnecessary motion.
- Support zoom and reflow. At a 320 CSS-pixel viewport, navigation labels must
  remain readable without page-wide horizontal scrolling. Wide data tables may
  use a clearly bounded, keyboard-usable scroll area.
- Aim for 44-by-44 CSS-pixel touch targets, while meeting WCAG 2.2 AA target-size
  and spacing requirements. Do not shrink labels or remove actions to make a
  mobile layout fit.
- Give meaningful screenshots descriptive alt text and a nearby text walkthrough.
  Decorative images should not repeat nearby text. Videos need captions and an
  equivalent text explanation; do not add inaccessible media to the README.

## Verification

Use synthetic accounts, tracks and provider responses. Follow
[local development](docs/DEVELOPMENT.md) and the [CI checks](docs/CI.md).
Type-checking and builds do not establish accessibility conformance.

For each affected UI flow:

1. Use only Tab, Shift+Tab, arrows, Enter/Space, Home/End and Escape as appropriate.
   Confirm visible focus, activation, dialog exit, and a usable route back.
2. Test at 320px, 390px, 720px, just above the 720px breakpoint, and a desktop
   width. Also test 200%/400% browser zoom and portrait/landscape layouts.
3. With a screen reader, check landmarks, names, selected tabs, form errors,
   progress/review announcements, and reading order. VoiceOver with Safari and
   NVDA with Firefox or Chromium are useful manual combinations.
4. Check text/control contrast, non-color state cues, reduced motion, and media
   alternatives. Include touch or a real device when the change affects it.
5. Record browsers, sizes and assistive technologies actually checked in the PR.
   State what remains untested rather than implying coverage.

Browser automation can check tab visibility, bounding boxes, keyboard focus and
responsive behavior. The repository does not currently configure a dedicated
automated accessibility audit suite; manual assistive-technology checks remain
necessary.

## Limitations and reporting

Live provider authorization may include external windows, device-code pages or
Apple MusicKit controls that this project does not own. Test those separately;
synthetic provider fixtures do not validate their accessibility.

Report accessibility barriers through the
[bug report template](https://github.com/mbianchidev/open-playlist-engine/issues/new?template=bug_report.md).
Include the workspace/action, expected and actual behavior, browser/OS,
viewport/zoom, and assistive technology if relevant. Redact personal data and
credentials. Security-sensitive reports belong in [SECURITY.md](SECURITY.md).
