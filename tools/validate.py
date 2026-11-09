#!/usr/bin/env python3
"""Validate the content files before they reach phones.

Usage: tools/validate.py [DIR] [--feed] [--max-episode N] [--previous-catalog PATH] [--strict-text]
  DIR                 the folder holding the files (default: the repo root above tools/)
  --feed              fetch the RedCircle feed and check every referenced episode exists
  --max-episode N     offline alternative to --feed
  --previous-catalog  the catalog.json phones have today; the new one must keep
                      at least 90% of its episode entries, and (with --strict-text)
                      every tag must already be one of its keywords
  --strict-text       the numeric text rules of docs/CONTENT.md are errors, not
                      warnings (D-085; becomes the default after step 1 of the
                      text pass over the 63 episodes)

Checks catalog.json, glossary.json and transcript-corrections.json, and
quotes.json when it exists, with the same limits the app enforces
(GuidedListening/Catalog/ContentLimits.swift, D-051), and the text rules of
docs/CONTENT.md and the episode text guide (D-085) as warnings. For the word
fixes it also prints every whole-word hit of each rule over the delivered
transcripts (DIR/transcripts-delivered, or transcription/delivered in the app
repo) so each can be read before the rule merges, warns when every word of a
`from` is in the system dictionary, and refuses a rule that changes the word
count (D-086, REPORT-2 and REPORT-5). Exit 1 on any error; warnings do not
fail. Run by the repo's pull-request workflow.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

FEED = "https://feeds.redcircle.com/c1a3de7b-1b92-4c7f-8e36-8484721d29f4"
COLORS = {"brass", "mauve", "teal", "coral", "sage", "faint"}

# The same numbers as ContentLimits.swift; change both together.
MAX_FILE_BYTES = 2_000_000
MAX_WORD_FIX_FILE_BYTES = 200_000
MAX_TEXT_LENGTH = 1_000
MAX_CHAPTERS = 100
MAX_TAKEAWAYS = 8
MAX_QUOTE_REFS = 60      # ContentLimits.maxQuoteRefsPerEpisode
MAX_QUOTE_CONCEPTS = 3   # ContentLimits.maxConceptsPerQuote
MAX_GLOSSARY_TERMS = 500
MAX_WORD_FIX_RULES = 500
MIN_WORD_FIX_LETTERS = 4
MIN_EPISODE_SHARE = 0.9
MAX_TRIM_SECONDS = 300
MAX_MIDROLL_SECONDS = 120  # ContentLimits.maxMidrollSeconds
EVERYDAY_WORDS = {
    "that", "with", "this", "from", "have", "they", "were", "been", "what", "when", "there", "their",
    "which", "would", "about", "said", "like", "just", "people", "think", "know", "because", "also",
    "into", "your", "more", "some", "time", "very", "then", "than", "them", "these", "those", "only",
    "over", "such", "well", "where", "will", "other", "could", "should", "really", "thing", "things",
    "something", "someone", "life", "world", "mean", "means", "right", "here", "want", "going", "come",
    "came", "make", "made", "take", "took", "give", "gave", "look", "see", "saw", "say", "says",
}
# Content rules from docs/CONTENT.md, reported as warnings.
MAX_DEFINITION = 280
MAX_LINE = 155
MAX_TAGLINE = 65

# Episode text rules (docs/CONTENT.md, docs/briefs/episode-text-guide-2026-11-06.md, D-085).
# Errors only with --strict-text; warnings otherwise.
TAG_COUNT = (4, 5)                 # PLACE-4
MAX_WHY = 70                       # DOOR-5: under 70 characters
AUTHORED_DOORS = (2, 3)            # DOOR-1, for episodes from FIRST_GUIDED_EPISODE on
FIRST_GUIDED_EPISODE = 64          # the first episode drafted under the guide
TAKEAWAY_COUNT = (3, 5)            # SHORT-1
MAX_TAKEAWAY = 170                 # SHORT-1: under 170 characters
MAX_CHAPTER_TITLE = 40             # CHAP-2: under 40 characters
MIN_CHAPTER_GAP = 10               # CHAP-7: YouTube needs 10 s
# Always warnings.
DOORS_BOTH_WAYS = (4, 6)           # DOOR-1
MAX_TAKEAWAY_WORDS = 25            # SHORT-8
CHAPTER_COUNT = (8, 12)            # CHAP-1
FIRST_CHAPTER_TOLERANCE = 3        # CHAP-1: seconds from the pre-roll end
PREFERRED_CHAPTER_GAP = 120        # CHAP-7
# VOICE-12: American forms of the house spellings; the British form is the rule.
AMERICAN_SPELLINGS = re.compile(
    r"\b(colou?r(?<!colour)s?|colored|colorful|behaviou?r(?<!behaviour)s?|behavioral|centers?|centered|"
    r"favorites?|neighbors?|neighborhoods?|judgments?|toward|realiz(e|es|ed|ing)|recogniz(e|es|ed|ing)|"
    r"organiz(e|es|ed|ing)|analyz(e|es|ed|ing)|apologiz(e|es|ed)|criticiz(e|es|ed)|emphasiz(e|es|ed)|"
    r"symboliz(e|es|ed)|idealiz(e|es|ed)|rationaliz(e|es|ed)|fantasiz(e|es|ed)|characteriz(e|es|ed)|"
    r"civiliz(e|ed|ation|ations)|humor|honor|labor|rumors?|flavors?|savior|defense|offense|gray|"
    r"skeptics?|skeptical|skepticism|theaters?|traveled|traveling|canceled|fulfill|fulfills|jewelry|"
    r"pajamas|aging)\b",
    re.I,
)
# Words the catalog texts never use (VOICE-8, VOICE-9); a plain-sense use is flagged for Marcus.
NEVER_LIST = re.compile(
    r"\b(unlock(s|ed|ing)?|unleash(es|ed|ing)?|elevate[sd]?|curated|immersive|seamless(ly)?|empower(s|ed|ing)?|"
    r"mindful(ly|ness)?|sacred|timeless|discover your true self|deep dives?|content|consum(e|es|ed|ing)|enjoy|"
    r"excited|amazing|journey of self-discovery|streaks?|you haven't listened|don't miss)\b",
    re.I,
)
# DOOR-4: a reason true from one page only.
DOOR_DIRECTION_WORDS = re.compile(r"\b(this one|here|the evening before|follow-up)\b", re.I)

errors, warnings = [], []
strict_text = False


def text_rule(msg):
    """A numeric text rule of D-085: an error with --strict-text, a warning until then."""
    if strict_text:
        errors.append(msg)
    else:
        warnings.append("(strict-text) " + msg)


def error(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def load(path, limit):
    """The parsed file, or None after an error."""
    if not os.path.exists(path):
        return None
    size = os.path.getsize(path)
    if size > limit:
        error(f"{os.path.basename(path)}: {size} bytes, over the {limit} byte limit")
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        error(f"{os.path.basename(path)}: not valid JSON: {e}")
        return None


def check_text_lengths(obj, where):
    """Every string anywhere in the file must fit the app's text limit."""
    if isinstance(obj, str):
        if len(obj) > MAX_TEXT_LENGTH:
            error(f"{where}: text of {len(obj)} characters, over {MAX_TEXT_LENGTH}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            check_text_lengths(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            check_text_lengths(v, f"{where}[{i}]")


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def known_episodes(use_feed, max_ep):
    if use_feed:
        tmp = os.path.join(tempfile.gettempdir(), "feed-validate.xml")
        subprocess.run(["curl", "-sS", "-L", FEED, "-o", tmp], check=True)
        with open(tmp, encoding="utf-8", errors="replace") as f:
            xml = f.read()
        known = set()
        for title in re.findall(r"<item>.*?<title>(.*?)</title>", xml, flags=re.S):
            m = re.match(r"\s*(?:<!\[CDATA\[)?\s*(\d{1,3})", title)
            if m:
                known.add(int(m.group(1)))
        return known
    if max_ep:
        return set(range(1, max_ep + 1))
    return None


def check_trim(tr, where):
    if not isinstance(tr, dict):
        error(f"{where}: trim must be an object")
        return
    for k in ("insert", "intro", "outro"):
        v = tr.get(k)
        if v is None:
            continue
        if not is_number(v):
            error(f"{where}.{k} must be a number")
        elif not 0 <= v <= MAX_TRIM_SECONDS:
            error(f"{where}.{k} = {v}: must be between 0 and {MAX_TRIM_SECONDS} seconds")
    if is_number(tr.get("insert")) and is_number(tr.get("intro")) and tr["intro"] < tr["insert"]:
        error(f"{where}.intro must be >= {where}.insert")


def jung_term_patterns(glossary):
    """One compiled pattern per glossary Jung term, matching the term and its aliases
    of three letters or more as whole words, the way the app dots a transcript."""
    patterns = []
    terms = glossary.get("terms") if isinstance(glossary, dict) else None
    for t in terms or []:
        if not isinstance(t, dict) or t.get("kind", "jung") != "jung":
            continue
        words = [t.get("term") or ""] + list(t.get("aliases") or [])
        words = [w for w in words if isinstance(w, str) and len(w.strip()) >= 3]
        if not words:
            continue
        alt = "|".join(re.escape(w.strip()) for w in words)
        patterns.append((t.get("id", "?"), re.compile(rf"\b(?:{alt})\b", re.I)))
    return patterns


def first_two_words(text):
    return " ".join(re.findall(r"[\w']+", text.lower())[:2])


def check_voice(text, where):
    """The spelling list, the never-list and the punctuation rules on one episode text."""
    for m in AMERICAN_SPELLINGS.finditer(text):
        warn(f"{where}: American spelling {m.group(0)!r} (VOICE-12)")
    for m in NEVER_LIST.finditer(text):
        warn(f"{where}: never-list word {m.group(0)!r} (VOICE-8); fine if used in its plain sense, ask Marcus")
    if "—" in text:
        text_rule(f"{where}: em dash (VOICE-7)")
    if "!" in text:
        text_rule(f"{where}: exclamation mark (VOICE-7)")


def check_catalog(cat, known, previous, jung_terms=()):
    check_text_lengths(cat, "catalog")
    ob = cat.get("onboarding")
    if not isinstance(ob, dict) or not ob.get("prompt") or not isinstance(ob.get("maxTopics"), int):
        error("onboarding needs prompt (string) and maxTopics (int)")

    def ep_ok(n, where):
        if known is not None and n not in known:
            error(f"{where}: episode {n:03d} is not in the feed")

    topics = cat.get("topics")
    if not isinstance(topics, list) or not topics:
        error("topics must be a non-empty list; a catalog without topics is not adopted by phones")
        topics = []
    ids = [t.get("id") for t in topics]
    if len(ids) != len(set(ids)):
        error("topic ids are not unique")
    for t in topics:
        tid = t.get("id", "?")
        for k in ("id", "name", "tagline", "journey"):
            if k not in t:
                error(f"topic {tid}: missing {k}")
        for entry in t.get("journey", []):
            m = re.match(r"^(\d{3}) - ", entry)
            if not m:
                error(f"topic {tid}: journey entry not 'NNN - Title': {entry!r}")
            else:
                ep_ok(int(m.group(1)), f"topic {tid}")
        if len(t.get("tagline") or "") > MAX_TAGLINE:
            warn(f"topic {tid}.tagline: over {MAX_TAGLINE} characters")
        for field in ("name", "tagline", "about"):
            if "—" in (t.get(field) or ""):
                warn(f"topic {tid}.{field}: em dash")

    terr = {x.get("id") for x in cat.get("territories") or []}
    for x in cat.get("territories") or []:
        if x.get("color") not in COLORS:
            error(f"territory {x.get('id')}: color {x.get('color')!r} not in {sorted(COLORS)}")
        if "—" in (x.get("about") or ""):
            warn(f"territory {x.get('id')}.about: em dash")

    eps = cat.get("episodes") or {}
    if not isinstance(eps, dict):
        error("episodes must be an object keyed by NNN")
        eps = {}
    previous_tags = None
    if strict_text and isinstance(previous, dict):
        previous_tags = {tag for m in (previous.get("episodes") or {}).values()
                         if isinstance(m, dict) for tag in (m.get("tags") or [])}
    show_trim = cat.get("trim") if isinstance(cat.get("trim"), dict) else {}
    show_preroll = show_trim.get("intro") if is_number(show_trim.get("intro")) else None
    page_doors = {}  # NNN -> the door reasons shown on that page, both directions
    if previous is not None:
        have = len(previous.get("episodes") or {})
        if have and len(eps) < have * MIN_EPISODE_SHARE:
            error(f"episodes: {len(eps)} entries, down from {have}; phones keep their copy below {int(MIN_EPISODE_SHARE * 100)}%")
    for key, meta in eps.items():
        where = f"episodes.{key}"
        if not re.fullmatch(r"\d{3}", key):
            error(f"episodes key {key!r} is not NNN")
            continue
        ep_ok(int(key), where)
        if not isinstance(meta, dict):
            error(f"{where}: must be an object")
            continue
        if meta.get("territory") and meta["territory"] not in terr:
            error(f"{where}: unknown territory {meta['territory']!r}")
        tags = meta.get("tags") or []
        for tag in tags:
            if not re.fullmatch(r"[a-z0-9_]+", tag):
                warn(f"{where}: tag {tag!r} is not lowercase_with_underscores")
            if previous_tags is not None and tag not in previous_tags:
                text_rule(f"{where}: tag {tag!r} is not a keyword of the catalog phones have; a new keyword is a decision (PLACE-3)")
        if tags and not TAG_COUNT[0] <= len(tags) <= TAG_COUNT[1]:
            text_rule(f"{where}: {len(tags)} tags, not {TAG_COUNT[0]} or {TAG_COUNT[1]} (PLACE-4)")
        links = meta.get("links") or []
        for link in links:
            to = link.get("to", "")
            if not re.fullmatch(r"\d{3}", str(to)):
                error(f"{where}: link.to {to!r} is not NNN")
            elif to not in eps and known is not None and int(to) not in known:
                error(f"{where}: link to unknown episode {to}")
            why = link.get("why")
            if not why:
                error(f"{where}: link to {to} has no why")
                continue
            if len(why) >= MAX_WHY:
                text_rule(f"{where}: door to {to}: why of {len(why)} characters, not under {MAX_WHY} (DOOR-5)")
            if DOOR_DIRECTION_WORDS.search(why):
                warn(f"{where}: door to {to}: {why!r} has a direction word; the reason shows on both pages (DOOR-4)")
            check_voice(why, f"{where}: door to {to}")
            page_doors.setdefault(key, []).append(why)
            page_doors.setdefault(str(to), []).append(why)
        if int(key) >= FIRST_GUIDED_EPISODE and not AUTHORED_DOORS[0] <= len(links) <= AUTHORED_DOORS[1]:
            text_rule(f"{where}: {len(links)} authored doors, not {AUTHORED_DOORS[0]} or {AUTHORED_DOORS[1]} (DOOR-1)")
        if meta.get("trim") is not None:
            check_trim(meta["trim"], f"{where}.trim")
        if meta.get("loudness") is not None and not is_number(meta["loudness"]):
            error(f"{where}.loudness must be a number")
        takeaways = meta.get("takeaways") or []
        if len(takeaways) > MAX_TAKEAWAYS:
            error(f"{where}: {len(takeaways)} takeaways, over {MAX_TAKEAWAYS}")
        if takeaways and not TAKEAWAY_COUNT[0] <= len(takeaways) <= TAKEAWAY_COUNT[1]:
            text_rule(f"{where}: {len(takeaways)} takeaways, not {TAKEAWAY_COUNT[0]} to {TAKEAWAY_COUNT[1]} (SHORT-1)")
        for i, line in enumerate(takeaways):
            if not isinstance(line, str):
                continue
            tw = f"{where}.takeaways[{i}]"
            if len(line) >= MAX_TAKEAWAY:
                text_rule(f"{tw}: {len(line)} characters, not under {MAX_TAKEAWAY} (SHORT-1)")
            words = len(line.split())
            if words > MAX_TAKEAWAY_WORDS:
                warn(f"{tw}: {words} words, over {MAX_TAKEAWAY_WORDS} (SHORT-8)")
            spans = [(tid, m.span()) for tid, pat in jung_terms for m in pat.finditer(line)]
            # "collective unconscious" is one term, not two: drop a match inside a longer one.
            named = sorted({tid for tid, (a, b) in spans
                           if not any(o != tid and oa <= a and b <= ob and (oa, ob) != (a, b)
                                      for o, (oa, ob) in spans)})
            if len(named) > 1:
                warn(f"{tw}: {len(named)} Jung terms ({', '.join(named)}); at most one per line (SHORT-7)")
            check_voice(line, tw)
        midroll = meta.get("midroll")
        if midroll is not None:
            if not isinstance(midroll, dict) or not is_number(midroll.get("at")) or not is_number(midroll.get("length")):
                error(f"{where}.midroll needs 'at' and 'length' in seconds")
            elif midroll["at"] < 0 or not 0 < midroll["length"] <= MAX_MIDROLL_SECONDS:
                error(f"{where}.midroll: 'at' 0 or more, 'length' above 0 and at most {MAX_MIDROLL_SECONDS} s")

        chapters = meta.get("chapters") or []
        if len(chapters) > MAX_CHAPTERS:
            error(f"{where}: {len(chapters)} chapters, over {MAX_CHAPTERS}")
        if chapters and not CHAPTER_COUNT[0] <= len(chapters) <= CHAPTER_COUNT[1]:
            warn(f"{where}: {len(chapters)} chapters, outside {CHAPTER_COUNT[0]} to {CHAPTER_COUNT[1]} (CHAP-1)")
        preroll = ((meta.get("trim") or {}).get("intro") if isinstance(meta.get("trim"), dict) else None)
        if not is_number(preroll):
            preroll = show_preroll
        last = -1
        short_gaps = []
        openings = {}
        for i, ch in enumerate(chapters):
            at = ch.get("at") if isinstance(ch, dict) else None
            if not is_number(at) or at < 0:
                error(f"{where}.chapters[{i}]: 'at' must be a number of seconds, 0 or more")
                continue
            title = (ch.get("title") or "").strip()
            if not title:
                error(f"{where}.chapters[{i}]: no title")
            else:
                cw = f"{where}.chapters[{i}]"
                if len(title) >= MAX_CHAPTER_TITLE:
                    text_rule(f"{cw}: title of {len(title)} characters, not under {MAX_CHAPTER_TITLE} (CHAP-2)")
                if "?" in title:
                    warn(f"{cw}: question mark in the title (CHAP-3)")
                check_voice(title, cw)
                openings.setdefault(first_two_words(title), []).append(i)
            if i == 0 and is_number(preroll) and abs(at - preroll) > FIRST_CHAPTER_TOLERANCE:
                warn(f"{where}.chapters[0]: at {at} s, more than {FIRST_CHAPTER_TOLERANCE} s from the pre-roll end at {preroll} s (CHAP-1)")
            if at < last:
                warn(f"{where}.chapters[{i}]: out of time order")
            elif i > 0:
                gap = at - last
                if gap < MIN_CHAPTER_GAP:
                    text_rule(f"{where}.chapters[{i}]: {gap:.0f} s after the one before, under {MIN_CHAPTER_GAP} (CHAP-7)")
                elif gap < PREFERRED_CHAPTER_GAP:
                    short_gaps.append(f"{i}: {gap:.0f} s")
            last = at
        if short_gaps:
            warn(f"{where}: chapter gaps under {PREFERRED_CHAPTER_GAP} s before chapters {', '.join(short_gaps)} (CHAP-7)")
        for opening, idx in openings.items():
            if len(idx) > 1 and opening:
                warn(f"{where}: chapters {idx} open with the same two words {opening!r} (CHAP-6)")
        for i, m in enumerate(meta.get("mentions") or []):
            if not is_number(m.get("at")) or m["at"] < 0:
                error(f"{where}.mentions[{i}]: 'at' must be a number of seconds, 0 or more")
            if not re.fullmatch(r"\d{3}", str(m.get("to", ""))):
                error(f"{where}.mentions[{i}]: 'to' is not NNN")
        quote_refs = meta.get("quoteRefs") or []
        if len(quote_refs) > MAX_QUOTE_REFS:
            error(f"{where}: {len(quote_refs)} quoteRefs, over {MAX_QUOTE_REFS}")
        for i, q in enumerate(quote_refs):
            at, end = q.get("at"), q.get("end")
            if not is_number(at) or at < 0 or not is_number(end) or end <= at:
                error(f"{where}.quoteRefs[{i}]: 'at' and 'end' must be seconds, 0 or more, end after at")
            if q.get("kind") not in ("reading", "paraphrase"):
                error(f"{where}.quoteRefs[{i}]: kind must be reading or paraphrase")
            for field in ("source", "text"):
                if not isinstance(q.get(field), str) or not q[field].strip():
                    error(f"{where}.quoteRefs[{i}]: no {field}")
            if len(q.get("concepts") or []) > MAX_QUOTE_CONCEPTS:
                warn(f"{where}.quoteRefs[{i}]: more than {MAX_QUOTE_CONCEPTS} concepts; the app shows the first")

    for key in sorted(k for k in eps if re.fullmatch(r"\d{3}", str(k))):
        doors = page_doors.get(key, [])
        if doors and not DOORS_BOTH_WAYS[0] <= len(doors) <= DOORS_BOTH_WAYS[1]:
            warn(f"episodes.{key}: {len(doors)} doors counting both directions, outside {DOORS_BOTH_WAYS[0]} to {DOORS_BOTH_WAYS[1]}; hubs need Marcus's word (DOOR-1)")
        seen = {}
        for why in doors:
            seen.setdefault(first_two_words(why), []).append(why)
        for opening, same in seen.items():
            if len(same) > 1 and opening:
                warn(f"episodes.{key}: {len(same)} doors on the page open with {opening!r} (DOOR-6)")

    if cat.get("trim") is not None:
        check_trim(cat["trim"], "trim")
    if cat.get("loudnessTarget") is not None and not is_number(cat["loudnessTarget"]):
        error("loudnessTarget must be a number")

    for listname in ("featured", "popular"):
        for n in cat.get(listname) or []:
            if not re.fullmatch(r"\d{3}", str(n)):
                error(f"{listname}: {n!r} is not NNN")
            else:
                ep_ok(int(n), listname)
    return len(topics), len(eps)


def check_glossary(glossary, catalog_tags):
    check_text_lengths(glossary, "glossary")
    terms = glossary.get("terms") if isinstance(glossary, dict) else None
    if not isinstance(terms, list) or not terms:
        error("glossary.terms must be a non-empty list; an empty glossary is not adopted by phones")
        return 0
    if len(terms) > MAX_GLOSSARY_TERMS:
        error(f"glossary: {len(terms)} terms, over {MAX_GLOSSARY_TERMS}")
    ids = [t.get("id") for t in terms]
    if len(ids) != len(set(ids)):
        error("glossary: ids are not unique")
    known_ids = set(ids)
    tags_seen = {}
    # A spelling two entries share opens only one of them (logic audit G-4,
    # LA-W-4): a warning until the glossary's duplicates are fixed in a catalog
    # pull request, then an error.
    owners = {}
    for t in terms:
        for form in [t.get("term")] + list(t.get("aliases") or []):
            if isinstance(form, str) and form.strip():
                owners.setdefault(form.strip().lower(), []).append(t.get("id", "?"))
    for form, who in sorted(owners.items()):
        if len(set(who)) > 1:
            warn(f"glossary: the spelling {form!r} is on {', '.join(sorted(set(who)))}; it can open only one (G-4)")
    for t in terms:
        tid = t.get("id", "?")
        where = f"glossary {tid}"
        if not isinstance(tid, str) or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", tid):
            error(f"{where}: id must be kebab-case")
        if not (t.get("term") or "").strip():
            error(f"{where}: no term")
        kind = t.get("kind", "jung")
        if kind not in ("jung", "keyword"):
            error(f"{where}: kind {kind!r} must be jung or keyword")
        if "mentions" in t and not isinstance(t["mentions"], bool):
            error(f"{where}: mentions must be true or false")
        if "verified" in t and not isinstance(t["verified"], bool):
            error(f"{where}: verified must be true or false")
        for r in t.get("related") or []:
            if r not in known_ids:
                error(f"{where}: related id {r!r} does not exist")
        tag = t.get("tag")
        if tag:
            if tag in tags_seen:
                error(f"{where}: tag {tag!r} is also on {tags_seen[tag]}")
            tags_seen[tag] = tid
            if catalog_tags is not None and tag not in catalog_tags:
                warn(f"{where}: tag {tag!r} is on no episode in the catalog")
        if kind == "jung":
            for alias in t.get("aliases") or []:
                if len(alias) < 3:
                    warn(f"{where}: alias {alias!r} is under three letters; the app does not match it")
            if not (t.get("definition") or "").strip():
                warn(f"{where}: Jung's term without a definition")
        if len(t.get("definition") or "") > MAX_DEFINITION:
            warn(f"{where}: definition over {MAX_DEFINITION} characters")
        if len(t.get("line") or "") > MAX_LINE:
            warn(f"{where}: line over {MAX_LINE} characters")
        for field in ("definition", "line"):
            if "—" in (t.get(field) or ""):
                warn(f"{where}.{field}: em dash")
    return len(terms)


def system_dictionary():
    """The words of /usr/share/dict/words, lower case; empty where the file is missing (CI runners)."""
    try:
        with open("/usr/share/dict/words", encoding="utf-8", errors="ignore") as f:
            return {w.strip().lower() for w in f}
    except OSError:
        return set()


def delivered_transcripts(repo):
    """(episode, [(cue start, cue text)]) for every delivered transcript, read as a listener gets it."""
    app_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for folder in (os.path.join(repo, "transcripts-delivered"), os.path.join(repo, "transcription", "delivered"),
                   os.path.join(app_repo, "transcription", "delivered")):
        if os.path.isdir(folder):
            break
    else:
        return []
    out = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".vtt"):
            continue
        cues, start, buf = [], None, []
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if "-->" in s:
                    if start is not None:
                        cues.append((start, " ".join(buf)))
                    start, buf = s.split("-->")[0].strip()[:10], []
                elif s and s != "WEBVTT" and start is not None:
                    buf.append(s)
        if start is not None:
            cues.append((start, " ".join(buf)))
        out.append((name[:-4], cues))
    return out


def check_correction_hits(rules, repo):
    """REPORT-2: print every whole-word hit of each word-fix rule so it can be read before the merge."""
    transcripts = delivered_transcripts(repo)
    if not transcripts:
        print("word-fix hits: no delivered transcripts found beside the files; hits not listed")
        return
    for rule in rules:
        src = (rule.get("from") or "").strip() if isinstance(rule, dict) else ""
        if not src:
            continue
        pattern = re.compile(r"(?<!\w)" + re.escape(src) + r"(?!\w)", re.I)
        hits = []
        for ep, cues in transcripts:
            for start, text in cues:
                for m in pattern.finditer(text):
                    before = text[: m.start()].split()[-6:]
                    after = text[m.end():].split()[:6]
                    hits.append(f"{ep} {start}: …{' '.join(before)} [{m.group(0)}] {' '.join(after)}…")
        print(f"word fix {src!r} -> {rule.get('to')!r}: {len(hits)} hit{'s' if len(hits) != 1 else ''} in {len(transcripts)} transcripts")
        for h in hits:
            print("   ", h)


def check_corrections(fixes, repo=None):
    check_text_lengths(fixes, "transcript-corrections")
    rules = fixes.get("replacements") if isinstance(fixes, dict) else None
    if not isinstance(rules, list):
        error("transcript-corrections: replacements must be a list")
        return 0
    if len(rules) > MAX_WORD_FIX_RULES:
        error(f"transcript-corrections: {len(rules)} rules, over {MAX_WORD_FIX_RULES}")
    seen = set()
    dictionary = system_dictionary()
    for i, rule in enumerate(rules):
        where = f"transcript-corrections[{i}]"
        src = (rule.get("from") or "").strip() if isinstance(rule, dict) else ""
        dst = rule.get("to") if isinstance(rule, dict) else None
        if not src or not isinstance(dst, str):
            error(f"{where}: needs 'from' and 'to' strings")
            continue
        letters = sum(1 for c in src if c.isalpha())
        if letters < MIN_WORD_FIX_LETTERS:
            error(f"{where}: {src!r} has under {MIN_WORD_FIX_LETTERS} letters; the app ignores it")
        if src.lower() in EVERYDAY_WORDS:
            error(f"{where}: {src!r} is an everyday word; the app ignores it")
        if src.lower() == dst.lower():
            error(f"{where}: {src!r} to {dst!r} changes nothing")
        if len(src.split()) != len(dst.split()):
            error(f"{where}: {src!r} to {dst!r} changes the word count; the cue would lose its word timings (REPORT-5)")
        if dictionary and all(w.lower() in dictionary or (w.lower().endswith("'s") and w.lower()[:-2] in dictionary)
                              for w in re.findall(r"[A-Za-z']+", src)):
            warn(f"{where}: every word of {src!r} is in the system dictionary; make sure it is never right English (REPORT-2)")
        if src.lower() in seen:
            error(f"{where}: {src!r} appears twice")
        seen.add(src.lower())
    if repo:
        check_correction_hits(rules, repo)
    return len(rules)


def check_quotes(quotes):
    check_text_lengths(quotes, "quotes")
    entries = quotes.get("quotes") if isinstance(quotes, dict) else None
    if not isinstance(entries, list) or not entries:
        error("quotes.quotes must be a non-empty list")
        return 0
    ids = [q.get("id") for q in entries if isinstance(q, dict)]
    if len(ids) != len(set(ids)):
        error("quotes: ids are not unique")
    for q in entries:
        if not isinstance(q, dict) or q.get("id") is None:
            error("quotes: every quote needs an id")
            continue
        if not (q.get("text") or "").strip():
            error(f"quote {q['id']}: no text")
        for field in ("book", "reference"):
            if field in q and q[field] is not None and not isinstance(q[field], str):
                error(f"quote {q['id']}: {field} must be a string")
    return len(entries)


def main():
    global strict_text
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 2
    strict_text = "--strict-text" in args
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    positional = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] not in ("--max-episode", "--previous-catalog"))]
    if positional:
        repo = positional[0]
    use_feed = "--feed" in args
    max_ep = int(args[args.index("--max-episode") + 1]) if "--max-episode" in args else None
    previous_path = args[args.index("--previous-catalog") + 1] if "--previous-catalog" in args else None

    known = known_episodes(use_feed, max_ep)
    previous = None
    if previous_path and os.path.exists(previous_path) and os.path.getsize(previous_path) > 0:
        try:
            with open(previous_path, encoding="utf-8") as f:
                previous = json.load(f)
        except Exception:  # noqa: BLE001
            warn(f"previous catalog at {previous_path} did not parse; episode share not checked")

    summary = []
    catalog_tags = None
    glossary = load(os.path.join(repo, "glossary.json"), MAX_FILE_BYTES)
    jung_terms = jung_term_patterns(glossary) if glossary is not None else []
    cat = load(os.path.join(repo, "catalog.json"), MAX_FILE_BYTES)
    if cat is None:
        if not os.path.exists(os.path.join(repo, "catalog.json")):
            error("catalog.json is missing")
    else:
        topics, eps = check_catalog(cat, known, previous, jung_terms)
        catalog_tags = {tag for meta in (cat.get("episodes") or {}).values() for tag in (meta.get("tags") or [])}
        summary.append(f"catalog: {topics} topics, {eps} episode entries")

    if glossary is not None:
        summary.append(f"glossary: {check_glossary(glossary, catalog_tags)} terms")

    fixes = load(os.path.join(repo, "transcript-corrections.json"), MAX_WORD_FIX_FILE_BYTES)
    if fixes is not None:
        summary.append(f"word fixes: {check_corrections(fixes, repo)} rules")

    quotes = load(os.path.join(repo, "quotes.json"), MAX_FILE_BYTES)
    if quotes is not None:
        summary.append(f"quotes: {check_quotes(quotes)}")

    for w in warnings:
        print("warning:", w)
    for e in errors:
        print("ERROR:", e)
    mode = "strict-text on" if strict_text else "strict-text off"
    print("; ".join(summary) + f"; {len(errors)} errors, {len(warnings)} warnings ({mode})")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
