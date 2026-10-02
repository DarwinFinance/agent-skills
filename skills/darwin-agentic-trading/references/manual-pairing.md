# Manual pairing (no python3)

Use this only when `scripts/darwin.py` cannot run. The canonical, always-current
version of these steps is https://darwin.finance/agents/setup.md (fetch it raw, e.g.
`curl -fsS https://darwin.finance/agents/setup.md`); if it disagrees with this file,
follow it.

⚠️ **Tell your user first:** without the helper, the API key passes through this
conversation's tool output once (in the step 3 response). Anything that records the
transcript records the key. Prefer the helper, or a key file imported with the
helper, whenever you can.

Every request needs a real `User-Agent` (Darwin's edge refuses some library defaults),
for example `-A "darwin-agent-skill/manual (<your app name>)"`.

1. Ask for a pairing code:

       curl -fsS -A "darwin-agent-skill/manual (Claude Code)" \
         -H 'Content-Type: application/json' \
         -d '{"client_name":"Claude Code"}' \
         https://darwin.finance/api/agent/v1/pair

   Add `"mode":"reconnect"` to give a new key to one of your user's EXISTING agents
   instead of creating a new one. If the answer does not echo `"mode":"reconnect"`,
   stop: the server would create a new agent.

2. Show your user `verification_uri_complete` and `user_code`. Keep `device_code`
   to yourself: do not show it or put it in a URL.

3. Every `interval` seconds (5), poll:

       curl -sS -A "darwin-agent-skill/manual (Claude Code)" \
         -H 'Content-Type: application/json' \
         -d '{"device_code":"<device_code>"}' \
         https://darwin.finance/api/agent/v1/pair/token

   `authorization_pending`: keep polling. `slow_down`: add 5 seconds.
   `expired_token`: start again if your user still wants to. `access_denied`: stop.
   A 200 carries `access_token`, shown **once**. Keep it in memory; never print,
   log or repeat it.

4. `GET https://darwin.finance/api/agent/v1/hello` with
   `Authorization: Bearer <access_token>`; show `welcome` to your user verbatim.

5. Read https://darwin.finance/agents/docs/llms.txt before trading, then
   `GET /api/agent/v1/grant` for your limits.

The agent can never send funds out of its Darwin account; moving perps margin back to
the agent's own wallet is allowed. Never ask your user for a password, passkey, seed
phrase or key in chat.
