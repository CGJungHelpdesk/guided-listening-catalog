#!/usr/bin/env python3
"""Validate catalog.json before it reaches phones.

Usage: tools/validate_catalog.py path/to/catalog.json [--feed] [--max-episode N]
  --feed          fetch the RedCircle feed and check every referenced episode exists
  --max-episode   offline alternative to --feed
Exit 1 on any error; warnings do not fail.
"""
import json, re, sys, subprocess, tempfile, os

FEED = "https://feeds.redcircle.com/c1a3de7b-1b92-4c7f-8e36-8484721d29f4"
COLORS = {"brass", "mauve", "teal", "coral", "sage", "faint"}

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__); return 2
    path = args[0]; use_feed = "--feed" in args
    max_ep = int(args[args.index("--max-episode")+1]) if "--max-episode" in args else None
    errors, warnings = [], []
    try:
        cat = json.load(open(path))
    except Exception as e:
        print(f"ERROR: not valid JSON: {e}"); return 1

    known = None
    if use_feed:
        import xml.etree.ElementTree as ET
        tmp = os.path.join(tempfile.gettempdir(), "feed-validate.xml")
        subprocess.run(["curl", "-sS", "-L", FEED, "-o", tmp], check=True)
        known = set()
        for it in ET.parse(tmp).getroot().findall("./channel/item"):
            m = re.match(r"\s*(\d{1,3})", it.findtext("title") or "")
            if m: known.add(int(m.group(1)))
    elif max_ep:
        known = set(range(1, max_ep + 1))

    def ep_ok(n, where):
        if known is not None and n not in known:
            errors.append(f"{where}: episode {n:03d} is not in the feed")

    ob = cat.get("onboarding")
    if not isinstance(ob, dict) or not ob.get("prompt") or not isinstance(ob.get("maxTopics"), int):
        errors.append("onboarding needs prompt (string) and maxTopics (int)")

    topics = cat.get("topics")
    if not isinstance(topics, list) or not topics:
        errors.append("topics must be a non-empty list"); topics = []
    ids = [t.get("id") for t in topics]
    if len(ids) != len(set(ids)): errors.append("topic ids are not unique")
    for t in topics:
        tid = t.get("id", "?")
        for k in ("id", "name", "tagline", "journey"):
            if k not in t: errors.append(f"topic {tid}: missing {k}")
        for entry in t.get("journey", []):
            m = re.match(r"^(\d{3}) - ", entry)
            if not m: errors.append(f"topic {tid}: journey entry not 'NNN - Title': {entry!r}")
            else: ep_ok(int(m.group(1)), f"topic {tid}")
        for field in ("name", "tagline", "about"):
            if "—" in (t.get(field) or ""): warnings.append(f"topic {tid}.{field}: em dash")

    terr = {x.get("id") for x in cat.get("territories") or []}
    for x in cat.get("territories") or []:
        if x.get("color") not in COLORS: errors.append(f"territory {x.get('id')}: color {x.get('color')!r} not in {sorted(COLORS)}")
        if "—" in (x.get("about") or ""): warnings.append(f"territory {x.get('id')}.about: em dash")

    eps = cat.get("episodes") or {}
    for key, meta in eps.items():
        if not re.fullmatch(r"\d{3}", key): errors.append(f"episodes key {key!r} is not NNN"); continue
        ep_ok(int(key), f"episodes.{key}")
        if meta.get("territory") and meta["territory"] not in terr:
            errors.append(f"episodes.{key}: unknown territory {meta['territory']!r}")
        for link in meta.get("links") or []:
            to = link.get("to", "")
            if not re.fullmatch(r"\d{3}", to): errors.append(f"episodes.{key}: link.to {to!r} is not NNN")
            elif to not in eps and known is not None and int(to) not in known: errors.append(f"episodes.{key}: link to unknown episode {to}")
            if not link.get("why"): errors.append(f"episodes.{key}: link to {to} has no why")
            elif "—" in link["why"]: warnings.append(f"episodes.{key}: link why has an em dash")
        tr = meta.get("trim")
        if tr is not None and not all(isinstance(tr.get(k, 0.0), (int, float)) for k in ("insert", "intro", "outro")):
            errors.append(f"episodes.{key}: trim values must be numbers")

    tr = cat.get("trim")
    if tr is not None:
        for k in ("insert", "intro", "outro"):
            v = tr.get(k)
            if v is not None and not isinstance(v, (int, float)): errors.append(f"trim.{k} must be a number")
        if isinstance(tr.get("insert"), (int, float)) and isinstance(tr.get("intro"), (int, float)) and tr["intro"] < tr["insert"]:
            errors.append("trim.intro must be >= trim.insert")

    for listname in ("featured", "popular"):
        for n in cat.get(listname) or []:
            if not re.fullmatch(r"\d{3}", str(n)): errors.append(f"{listname}: {n!r} is not NNN")
            else: ep_ok(int(n), listname)

    for w in warnings: print("warning:", w)
    for e in errors: print("ERROR:", e)
    print(f"{path}: {len(topics)} topics, {len(eps)} episode entries, {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0

if __name__ == "__main__":
    sys.exit(main())
