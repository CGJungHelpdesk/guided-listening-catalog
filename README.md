# Jung Helpdesk — catalog

The live content map for the Jung Helpdesk iOS app: topics, journey order,
check-in wording, territories, tags, episode doors, and the Most Popular list.

**Editing `catalog.json` here updates the app** — every phone fetches this
file on launch (with the app's bundled copy as offline fallback). Episodes are
referenced by their `NNN` number prefix.

The app's source lives in the private `guided-listening` repository.

**Publishing** (since 2.0): changes go through a pull request, never a push
to `main`. The `validate` check runs `tools/validate.py` against the feed and
against the catalog phones have today; merge when it is green. The app holds
every file to the same limits, so a value the validator refuses would be
ignored on the phone anyway.
