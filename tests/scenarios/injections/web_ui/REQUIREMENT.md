MINIMAL WEB INTERFACE (added by the human mid-run; binding).

The product must serve a server-rendered HTML page, standard library only,
as ONE feature leaf (src/web_ui.py) reading through the existing modules:

- GET /ui  — an HTML page listing all notes (newest first), and containing an
             add form: a text input named "text" that POSTs back to /ui and
             then shows the new note in the list.

The handler returns (200, "<html>...") — the WSGI app passes HTML strings
through. No JavaScript frameworks, no new storage, no API changes; reuse the
notes stored via src/db.py and the existing /notes logic.

The acceptance test for this requirement is already in tests/smoke/
(platform-protected); the root integrate cannot pass until the page exists.
