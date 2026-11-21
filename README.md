# Jung Helpdesk — catalog

The live content map for the Jung Helpdesk iOS app: topics, journey order,
check-in wording, territories, tags, episode doors, and the Most Popular list.

**Editing `catalog.json` here updates the app** — every phone fetches this
file on launch (with the app's bundled copy as offline fallback). Episodes are
referenced by their `NNN` number prefix.

The app's source lives in the private `guided-listening` repository.

**Checks.** A pull request that touches `catalog.json`, `glossary.json`,
`transcript-corrections.json`, `quotes.json` or the delivered transcripts runs
`tools/validate.py . --feed` (the workflow `validate.yml`): the app's own
limits and rules, and every referenced episode against the feed. The file is a
copy of `tools/validate.py` in the app repository; change it there and copy it
here, never only here.

## Rights

© 2026 C.G. Jung Helpdesk. All rights reserved.

The catalog, journeys, glossary definitions, transcripts and word fixes in
this repository are the podcast's own work. They are public only so that the
Jung Helpdesk app and podcast apps can load them. No licence is granted to
copy, republish or reuse them in another app, site or dataset.

Quotations from C.G. Jung's works are short, each is given with its
reference, and they remain the copyright of their respective rights holders.
A rights holder who wants a passage changed or removed: please open an issue
in this repository.
