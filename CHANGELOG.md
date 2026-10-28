# Changelog

Earlier versions are described in their pull requests and commit messages.

## 1.4.2 (2026-10-28)

- Darwin retired its MCP page (darwin.finance/mcp now opens the Agents and API Keys tab). The skill
  no longer names it, and points where Darwin itself now does:
  - to connect a chat app, or to create a Read-Only API Key: **Connect an AI** on Darwin
    (https://darwin.finance/agents?create=1);
  - to review or revoke an owner key or a read-only key: Darwin's **Agents and API Keys** tab
    (https://darwin.finance/agents?tab=agents). An agent key is still revoked on the agent's
    **Manage** tab.
- When a paired owner key could not be stored, or Darwin's owner-key answer was malformed, the
  error names the Agents and API Keys tab of the key's realm and carries it as `keys_url`.
- The helper's own messages follow, with the key's realm: `read_only_unsupported` and a refused
  read-only `call` point at Connect an AI; a read-only pairing's `read_only_note` names the Agents and
  API Keys tab; `forget` says where to revoke each kind of key and links the tab of every realm it
  removed a key from.
- README: chat apps connect with Connect an AI.

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
- A kept key file is made owner-only (`chmod 600`) after import; where it cannot (or on Windows, where the ACL is not checked), the helper says to keep the file in a folder only the agent can read.
- `import --help` explains `--keep-file`.
