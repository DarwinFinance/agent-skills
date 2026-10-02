#!/usr/bin/env python3
"""Build dist/darwin-agentic-trading.zip deterministically and print its sha256.

The skill FOLDER is the top level of the archive (darwin-agentic-trading/SKILL.md),
which is what Claude desktop / Claude.ai skill upload expects. Entries are sorted,
timestamps fixed, permissions normalised (0755 for scripts/*.py, 0644 otherwise),
so the same tree always yields the same bytes and the same digest.

Usage: python3 scripts/build-zip.py [--out dist/darwin-agentic-trading.zip]
"""
import argparse
import hashlib
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = "darwin-agentic-trading"
SRC = os.path.join(ROOT, "skills", SKILL)
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
SKIP = {"__pycache__", ".DS_Store"}


def files():
    out = []
    for dirpath, dirnames, filenames in os.walk(SRC):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP)
        for name in sorted(filenames):
            if name in SKIP or name.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                sys.exit("refusing symlink in skill folder: %s" % full)
            rel = os.path.relpath(full, SRC).replace(os.sep, "/")
            out.append((rel, full))
    return sorted(out)


def build(out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel, full in files():
            info = zipfile.ZipInfo("%s/%s" % (SKILL, rel), date_time=FIXED_TIME)
            mode = 0o755 if rel.startswith("scripts/") and rel.endswith(".py") else 0o644
            info.external_attr = (0o100000 | mode) << 16
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(full, "rb") as f:
                data = f.read().replace(b"\r\n", b"\n")
            z.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    with open(out_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist", SKILL + ".zip"))
    a = ap.parse_args()
    digest = build(a.out)
    print("%s  %s  (%d bytes)" % (digest, os.path.relpath(a.out, ROOT), os.path.getsize(a.out)))
