#!/usr/bin/env python3
"""Fail if the version differs between SKILL.md, darwin.py and any manifest,
or if SKILL.md's frontmatter breaks the agentskills.io limits."""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(ROOT, "skills", "darwin-agentic-trading")


def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


def main():
    errors = []
    versions = {}
    py = read("skills/darwin-agentic-trading/scripts/darwin.py")
    versions["darwin.py"] = re.search(r'^__version__ = "([^"]+)"', py, re.M).group(1)
    skill = read("skills/darwin-agentic-trading/SKILL.md")
    fm = re.match(r"^---\n(.*?)\n---\n", skill, re.S)
    if not fm:
        errors.append("SKILL.md has no frontmatter")
    else:
        front = fm.group(1)
        versions["SKILL.md"] = re.search(r'^\s+version:\s*"([^"]+)"', front, re.M).group(1)
        name = re.search(r"^name:\s*(\S+)", front, re.M).group(1)
        if name != os.path.basename(SKILL_DIR) or not re.match(r"^[a-z0-9-]{1,64}$", name):
            errors.append("SKILL.md name must equal the folder name and match [a-z0-9-]{1,64}")
        desc = re.search(r"^description:\s*>-\n((?:  .*\n?)+)", front + "\n", re.M)
        text = " ".join(l.strip() for l in desc.group(1).splitlines()) if desc else ""
        if not text or len(text) > 1024:
            errors.append("description missing or over 1024 chars (%d)" % len(text))
        for phrase in ("set up a Darwin agent", "agentic trading", "Darwin Finance", "darwin.finance"):
            if phrase.lower() not in text[:200].lower():
                errors.append("first 200 chars of description must mention %r" % phrase)
    for p in (".claude-plugin/plugin.json", "gemini-extension.json", ".cursor-plugin/plugin.json", ".codex-plugin/plugin.json"):
        versions[p] = json.loads(read(p))["version"]
    for i, pl in enumerate(json.loads(read(".claude-plugin/marketplace.json"))["plugins"]):
        versions["marketplace.json plugins[%d]" % i] = pl["version"]
    if len(set(versions.values())) != 1:
        errors.append("version mismatch: %s" % versions)
    for e in errors:
        print("ERROR:", e)
    if not errors:
        print("ok: version %s everywhere" % versions["darwin.py"])
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
