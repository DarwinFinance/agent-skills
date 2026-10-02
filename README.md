<p align="center">
  <a href="https://darwin.finance">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/darwin-mark-white.svg">
      <img src="assets/darwin-mark-blue.svg" alt="Darwin" width="96" height="96">
    </picture>
  </a>
</p>

<h1 align="center">Darwin Finance agent skill</h1>

<p align="center">
  <strong><a href="https://darwin.finance">darwin.finance</a></strong> ·
  <strong><a href="https://darwin.finance/agents/setup">Set up a Darwin agent</a></strong> ·
  <a href="https://darwin.finance/agents/docs">Agent API docs</a>
</p>

> **This is the official Darwin Finance agent skill. `github.com/DarwinFinance/agent-skills`
> is the only official repository.** Any other repository, package or skill claiming to be
> Darwin's is not ours. The canonical list of official install commands is on
> [darwin.finance/agents/setup](https://darwin.finance/agents/setup).

This skill lets an AI agent (Claude Code, Claude, Codex, Gemini CLI, Cursor, Hermes and
others) **set up agentic trading on [Darwin](https://darwin.finance)** when you say
something like *"set up a Darwin agent"* or *"agentic trading on Darwin"*:

1. The agent asks Darwin for a pairing code and shows you a link and a code.
2. You open the link, sign in to Darwin and approve, in your own browser.
3. Darwin gives the agent an API key for **one Darwin agent account**, which it can trade
   inside the limits you set. The helper keeps the key in your operating system's secret
   store, so it never appears in the chat.

**No withdrawals:** the agent can never send funds out of its Darwin account; moving perps
margin back to the agent's own wallet is allowed. Funding and withdrawals stay with you, in
the Darwin app.

## Install

| Where | How |
|---|---|
| Most agents (Claude Code, Codex, Cursor, Gemini CLI, Copilot, OpenCode, Goose, …) | `npx skills add DarwinFinance/agent-skills` |
| Claude Code plugin marketplace | `/plugin marketplace add DarwinFinance/agent-skills` then `/plugin install darwin@darwin-finance` |
| Claude desktop / Claude.ai | Download [darwin.finance/agents/skill.zip](https://darwin.finance/agents/skill.zip), then **Settings → Capabilities → Skills → Upload skill** (needs code execution with network access to darwin.finance) |
| Gemini CLI | `gemini extensions install https://github.com/DarwinFinance/agent-skills` |
| Codex | `codex plugin marketplace add DarwinFinance/agent-skills`, or `npx skills add DarwinFinance/agent-skills -a codex` |
| Hermes Agent | `hermes skills install DarwinFinance/agent-skills/skills/darwin-agentic-trading` |
| Anything else | Copy `skills/darwin-agentic-trading/` into your agent's skills folder |

Then tell your agent: **"Set up a Darwin agent."**

Updates: re-run the install command (`npx skills add …`), `/plugin update darwin@darwin-finance`
(or enable auto-update for this marketplace), or `gemini extensions update`.

This skill is distributed only from this repository and from darwin.finance. It is **not**
listed in the Anthropic or OpenAI plugin directories.

No Python? Paste this line to your agent instead; it follows
[darwin.finance/agents/setup.md](https://darwin.finance/agents/setup.md) directly:

```
Set up Darwin Agentic Trading using https://darwin.finance/agents/setup.md and save the API key so you can trade later.
```

The key then passes through the chat once, so the helper is preferred.

## What's inside

```
skills/darwin-agentic-trading/
├── SKILL.md                     # what the agent reads
├── scripts/darwin.py            # stdlib-only Python helper (pair, reconnect, import, call)
└── references/manual-pairing.md # curl fallback
```

`darwin.py` commands (each prints one line of JSON):

| Command | Does |
|---|---|
| `pair start --client-name "Claude Code"` | Checks the secret store works, asks Darwin for a pairing code, returns the link and code at once |
| `pair wait` | Polls until you approve (≤ 8 min per call; re-run it; codes live up to 25 min), stores the key, prints Darwin's welcome |
| `pair reconnect --client-name "Claude Code"` | Same, but gives a **new key to one of your existing agents**, which you pick and confirm in the browser |
| `import <file>` | Imports a key file you downloaded from the agent's **Manage** tab, verifies it with Darwin, then deletes the file |
| `call GET /api/agent/v1/grant` | Calls the Darwin agent API with the stored key |
| `status [--check]` / `forget …` | Lists stored agents / deletes the local copy |

## Security model

- **You approve every key** in your own signed-in browser on darwin.finance, with your
  passkey (and 2FA if you use it). The agent never sees a password, passkey or seed phrase,
  and the skill tells it to refuse any request for one.
- **A new pairing creates a new, empty agent account.** Reconnecting an existing agent is a
  separate, explicit mode: the page names the agent and its balance, you confirm with your
  passkey, and Darwin emails you. Other keys keep working until you revoke them on the
  agent's Manage tab.
- **The key never enters the conversation.** The helper redeems it in-process and stores it
  in the macOS Keychain, Windows Credential Manager or the Linux Secret Service (via native
  APIs or stdin, never on a command line). With no secret store it keeps the key in memory
  only (a RAM-backed file, gone at reboot) and says so. It checks the store before pairing.
- **Pinned hosts.** The helper talks only to `https://darwin.finance` and
  `https://beta.darwin.finance`, only under `/api/agent/`, never follows redirects, and only
  ever sends a key to the realm that issued it.
- **Limits are enforced by Darwin, not by this skill.** Trading caps, allowed markets and the
  no-withdrawal rule are server-side; you can pause the agent or revoke a key at any time.
- **The client name is unverified.** Anyone can run a pairing and call themselves anything.
  Only approve codes shown by an agent you set up yourself, from this repository or
  darwin.finance.

Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Development

```sh
python3 -m unittest discover -s tests -v   # helper tests (stdlib only)
python3 scripts/check-versions.py          # one version everywhere, SKILL.md limits
python3 scripts/build-zip.py               # deterministic dist/darwin-agentic-trading.zip + sha256
```

The zip served at darwin.finance/agents/skill.zip is built from this folder.

## License

[MIT](LICENSE) © 2026 Darwin Finance
