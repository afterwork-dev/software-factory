Does the change look like it belongs in this app?

Look at it running if you can (dev server on your port, the page-viewing
approach from the brief) at desktop and phone widths. Don't sign in or submit
forms. If you can't run it, review the markup and styles, and list what you
couldn't see under Notes in your review.

- **Design system**: it uses the project's design tokens, theme values, shared
  components and utility classes (the project notes say where they live) rather
  than one-off CSS or hard-coded colours, sizes and fonts.
- **Consistency**: colours, type scale, spacing, radii, shadows and icons match
  the neighbouring components.
- **Layout**: alignment, spacing rhythm and visual hierarchy hold up; nothing
  overflows, overlaps or wraps badly at phone width.
- **Readability**: text has enough contrast in every theme the app supports;
  interactive elements look interactive and show focus.

Use CHANGES for visible breakage or clear inconsistency with the existing
design. Taste goes under Notes.
