#!/usr/bin/env python3
"""Check every intra-repo markdown link in docs/ + README.md resolves to a file:heading pair.
GitHub lowercases anchors, strips punctuation and swaps spaces for dashes."""
import os
import re
import sys
from urllib.parse import unquote

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
md_files = ["README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md"] + \
           sorted(os.path.join("docs", f) for f in os.listdir(os.path.join(REPO, "docs")) if f.endswith(".md"))


def slug(h):
    h = h.strip().lower()
    h = re.sub(r"[^\w\s-]", "", h)          # drop punctuation (keeping unicode word chars)
    return re.sub(r"\s", "-", h)


anchors = {}
for f in md_files:
    heads = []
    in_fence = False
    for line in open(os.path.join(REPO, f), encoding="utf-8"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            heads.append(slug(m.group(2)))
    # dedupe like GitHub (repeated headings get -1, -2...)
    seen = {}
    deduped = []
    for h in heads:
        if h in seen:
            seen[h] += 1
            deduped.append(f"{h}-{seen[h]}")
        else:
            seen[h] = 0
            deduped.append(h)
    anchors[f] = set(deduped)

bad = 0
for f in md_files:
    text = open(os.path.join(REPO, f), encoding="utf-8").read()
    for m in re.finditer(r"\]\(([^)#]+?)(#[^)]*)?\)(?!\()", text):
        target, frag = m.group(1), m.group(2)
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        rel = os.path.normpath(os.path.join(os.path.dirname(f), unquote(target)))
        if not os.path.isfile(os.path.join(REPO, rel)):
            print(f"BROKEN FILE: {f}: {target}")
            bad += 1
            continue
        if frag:
            want = slug(unquote(frag[1:]))
            if want not in anchors.get(rel, set()):
                print(f"BROKEN ANCHOR: {f}: {target}{frag}")
                bad += 1

# also: every heading DEPLOY.md's ToC promises must exist (sanity for the rewrite)
deploy_toc = re.findall(r"\]\(#([^)]+)\)", open(os.path.join(REPO, "docs/DEPLOY.md"), encoding="utf-8").read())
deploy_slugs = anchors["docs/DEPLOY.md"]
for t in deploy_toc:
    if slug(unquote(t)) not in deploy_slugs:
        print(f"DEPLOY ToC self-link missing: #{t}")
        bad += 1

print("LINKS OK" if not bad else f"{bad} broken")
sys.exit(1 if bad else 0)
