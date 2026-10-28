# Changelog

Earlier versions are described in their pull requests and commit messages.

## 1.4.1 (2026-10-28)

- Machines with no OS secret store (for example a Linux sandbox such as Muse's, where the key
  is kept memory-only in `/dev/shm`): import a key file with `darwin.py import --keep-file <file>`
  (`--realm beta` too for a beta file), keep the file, and import it again whenever the helper
  says the key is gone. SKILL.md says this is what "import the key with the Darwin skill" means,
  matching Darwin's /agents/setup.md.
- `key_missing` now points at the kept file: "re-import it with `darwin.py import --keep-file <file>`"
  (with `--realm beta` for a beta key).
- `import --keep-file` on a memory-only store says to keep the file and import it again, instead of
  asking for it to be deleted; an import that deleted the file there says to use `--keep-file` next time.
- `import --help` explains `--keep-file`.
