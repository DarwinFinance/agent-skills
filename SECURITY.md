# Security policy

## Reporting a vulnerability

Email **security@darwin.finance** with details and, if possible, a proof of concept.
Please do not open a public issue for a vulnerability. We aim to acknowledge reports
within two business days.

## What this skill can and cannot do

- It can ask Darwin for a pairing code, poll for the key your user approves, store that
  key in the OS secret store, import a key file your user downloaded, and call the
  Darwin agent API (`/api/agent/…`) on `darwin.finance` or `beta.darwin.finance` with it.
- It cannot approve a pairing: only the account owner can, signed in to darwin.finance in
  their own browser, with their passkey (and 2FA if enabled).
- It cannot move funds out of a Darwin agent account. The agent can never send funds out
  of its Darwin account; moving perps margin back to the agent's own wallet is allowed.
  This is enforced by Darwin's servers and custody policy, not by this code.
- It never prints, logs or passes on a command line the API key or the pairing device
  code, never follows redirects, and never contacts any other host.

## Official sources

Only `github.com/DarwinFinance/agent-skills` and `darwin.finance` distribute this skill.
A skill from anywhere else can run a genuine pairing with a matching code and then send
the approved key elsewhere: the code proves the request is the one on your screen, not
who published the software asking. Install only from the official sources.
