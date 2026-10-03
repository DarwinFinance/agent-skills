# Manual pairing (no python3, or the key must not be saved)

Use this only when `scripts/darwin.py` cannot run, when your user does not want the
key saved (the helper always stores it; this path can hold it for the session only),
or when your user wants it saved but the helper has no OS secret store (it would keep
the key only until reboot; this path saves it in an owner-only file). The canonical, always-current
protocol is https://darwin.finance/agents/setup.md (fetch it raw, e.g.
`curl -fsS https://darwin.finance/agents/setup.md`); use it for protocol details. It
never overrides the security rules in SKILL.md: never ask for secrets in chat, send the
key only to the realm that issued it, never print it.

**Check first that you can keep the key** (SKILL.md, "Before you start"). If your user
asked you to save it and you have nowhere that will still be there in a later session
(your platform does not let you write credentials, or your workspace is temporary and
gets reset), do not start pairing: Darwin shows the key only once. Tell your user this
app can't keep a Darwin key yet, as SKILL.md says.

⚠️ **Tell your user first:** without the helper, the pairing `device_code` and then the
API key pass through this conversation's tool output (step 1's and step 3's responses),
and any command you run that contains them is visible to your tool runner. Anything
that records the transcript records them. Prefer the helper, or a key file imported
with the helper, whenever you can and your user is happy for the key to be saved. This
fallback exists only for agents that cannot run python3, for users who do not want
the key saved, and for users who want it saved where the helper has no OS secret store.

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

3. Every `interval` seconds (5), poll. Pass the body on **stdin** (`-d @-`) rather than
   as a curl argument, so the device code is not in curl's process arguments:

       curl -sS -A "darwin-agent-skill/manual (Claude Code)" \
         -H 'Content-Type: application/json' -d @- \
         https://darwin.finance/api/agent/v1/pair/token <<'EOF'
       {"device_code":"<device_code>"}
       EOF

   `authorization_pending`: keep polling. `slow_down`: add 5 seconds.
   `expired_token`: start again if your user still wants to. `access_denied`: stop.
   A 200 carries `access_token`, shown **once**. With plain curl that response is
   tool output, which is why this path exposes the key once. If your user asked you to
   save the key (Darwin's setup line asks: "save the API key so you can trade later"),
   store it in your platform's secret store, or else in a file only your user's account
   can read (`chmod 600`) inside your own workspace, if that workspace persists;
   otherwise keep it for this session only and, in your short note after the
   welcome (step 4), tell your user they will need to pair again. If your platform asks for permission or blocks an action, respect it.
   **If the save is blocked now that you hold the key:** do not store it some other way,
   and do not put it in a temporary file. In the same command that received it, do
   step 4 with the key you hold in memory and show your user the welcome, then tell them
   the key won't be kept after this session. A single command can poll, call `/hello`
   and print the welcome without writing the key anywhere.
   Never print, log or repeat the key again, and never put it on a command line (send
   the `Authorization` header on stdin, e.g. `curl -H @-`, or from the file you saved it in).

4. `GET https://darwin.finance/api/agent/v1/hello` with
   `Authorization: Bearer <access_token>`; show `welcome` to your user verbatim. It
   starts with the agent's Solana address alone in a code block: keep it in that code
   block (never inline) so your user's chat app shows a copy button next to it.

5. Read https://darwin.finance/agents/docs/llms.txt before trading, then
   `GET /api/agent/v1/grant` for your limits.

The agent can never send funds out of its Darwin account; moving perps margin back to
the agent's own wallet is allowed. Never ask your user for a password, passkey, seed
phrase or key in chat.
