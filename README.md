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
