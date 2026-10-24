#!/usr/bin/env python3
"""Validate the content files before they reach phones.

Usage: tools/validate.py [DIR] [--feed] [--max-episode N] [--previous-catalog PATH]
  DIR                 the folder holding the files (default: the repo root above tools/)
  --feed              fetch the RedCircle feed and check every referenced episode exists
  --max-episode N     offline alternative to --feed
  --previous-catalog  the catalog.json phones have today; the new one must keep
                      at least 90% of its episode entries

Checks catalog.json, glossary.json and transcript-corrections.json, and
quotes.json when it exists, with the same limits the app enforces
(GuidedListening/Catalog/ContentLimits.swift, D-051). Exit 1 on any error;
warnings do not fail. Run by the repo's pull-request workflow.
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
MAX_GLOSSARY_TERMS = 500
MAX_WORD_FIX_RULES = 500
MIN_WORD_FIX_LETTERS = 4
MIN_EPISODE_SHARE = 0.9
MAX_TRIM_SECONDS = 300
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

errors, warnings = [], []


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


def check_catalog(cat, known, previous):
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
        for tag in meta.get("tags") or []:
            if not re.fullmatch(r"[a-z0-9_]+", tag):
                warn(f"{where}: tag {tag!r} is not lowercase_with_underscores")
        for link in meta.get("links") or []:
            to = link.get("to", "")
            if not re.fullmatch(r"\d{3}", str(to)):
                error(f"{where}: link.to {to!r} is not NNN")
            elif to not in eps and known is not None and int(to) not in known:
                error(f"{where}: link to unknown episode {to}")
            if not link.get("why"):
                error(f"{where}: link to {to} has no why")
            elif "—" in link["why"]:
                warn(f"{where}: link why has an em dash")
        if meta.get("trim") is not None:
            check_trim(meta["trim"], f"{where}.trim")
        if meta.get("loudness") is not None and not is_number(meta["loudness"]):
            error(f"{where}.loudness must be a number")
        takeaways = meta.get("takeaways") or []
        if len(takeaways) > MAX_TAKEAWAYS:
            error(f"{where}: {len(takeaways)} takeaways, over {MAX_TAKEAWAYS}")
        chapters = meta.get("chapters") or []
        if len(chapters) > MAX_CHAPTERS:
            error(f"{where}: {len(chapters)} chapters, over {MAX_CHAPTERS}")
        last = -1
        for i, ch in enumerate(chapters):
            at = ch.get("at") if isinstance(ch, dict) else None
            if not is_number(at) or at < 0:
                error(f"{where}.chapters[{i}]: 'at' must be a number of seconds, 0 or more")
                continue
            if not (ch.get("title") or "").strip():
                error(f"{where}.chapters[{i}]: no title")
            if at < last:
                warn(f"{where}.chapters[{i}]: out of time order")
            last = at
        for i, m in enumerate(meta.get("mentions") or []):
            if not is_number(m.get("at")) or m["at"] < 0:
                error(f"{where}.mentions[{i}]: 'at' must be a number of seconds, 0 or more")
            if not re.fullmatch(r"\d{3}", str(m.get("to", ""))):
                error(f"{where}.mentions[{i}]: 'to' is not NNN")

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


def check_corrections(fixes):
    check_text_lengths(fixes, "transcript-corrections")
    rules = fixes.get("replacements") if isinstance(fixes, dict) else None
    if not isinstance(rules, list):
        error("transcript-corrections: replacements must be a list")
        return 0
    if len(rules) > MAX_WORD_FIX_RULES:
        error(f"transcript-corrections: {len(rules)} rules, over {MAX_WORD_FIX_RULES}")
    seen = set()
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
        if src.lower() in seen:
            error(f"{where}: {src!r} appears twice")
        seen.add(src.lower())
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
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 2
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
    cat = load(os.path.join(repo, "catalog.json"), MAX_FILE_BYTES)
    if cat is None:
        if not os.path.exists(os.path.join(repo, "catalog.json")):
            error("catalog.json is missing")
    else:
        topics, eps = check_catalog(cat, known, previous)
        catalog_tags = {tag for meta in (cat.get("episodes") or {}).values() for tag in (meta.get("tags") or [])}
        summary.append(f"catalog: {topics} topics, {eps} episode entries")

    glossary = load(os.path.join(repo, "glossary.json"), MAX_FILE_BYTES)
    if glossary is not None:
        summary.append(f"glossary: {check_glossary(glossary, catalog_tags)} terms")

    fixes = load(os.path.join(repo, "transcript-corrections.json"), MAX_WORD_FIX_FILE_BYTES)
    if fixes is not None:
        summary.append(f"word fixes: {check_corrections(fixes)} rules")

    quotes = load(os.path.join(repo, "quotes.json"), MAX_FILE_BYTES)
    if quotes is not None:
        summary.append(f"quotes: {check_quotes(quotes)}")

    for w in warnings:
        print("warning:", w)
    for e in errors:
        print("ERROR:", e)
    print("; ".join(summary) + f"; {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
