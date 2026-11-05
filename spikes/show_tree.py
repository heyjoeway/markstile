#!/usr/bin/env python3
"""Print a bookmarks tree dump compactly: one line per node with all non-children fields."""
import json, sys
d = json.load(open(sys.argv[1]))
tree = d.get("tree") or d
def walk(n, depth=0):
    print("  " * depth + json.dumps({k: v for k, v in n.items() if k != "children"}))
    for c in n.get("children", []):
        walk(c, depth + 1)
for n in tree:
    walk(n)
