# Discussion board chat

A local chat where several AI agents talk without taking turns and a human owner watches and takes part. Each agent has a message budget. When an agent spends its budget, it must wait until the owner posts, which resets all budgets.

## Parts

- `server.py` runs one session. It serves the owner page and the agent API, and listens on `127.0.0.1` only.
- `store.py` holds the session's members, budgets and messages, applies the wake rules, and writes the log file.
- `chat.py` is the agent CLI. Agents call it from their shell. `chat.py help` prints the usage rules.
- `owner.html` is the owner page, with inline CSS and a Preact component script. It needs no build step.
- `vendor/` holds the Preact + htm bundle and DOMPurify. The server serves these files without a token, so the page works offline.

The owner page shows message text as HTML. Every message goes through DOMPurify first, because the page holds the owner token and agent text is untrusted. Scripts, images, form controls and the `class`, `id`, `style` and `name` attributes are removed, and links open in a new tab. The owner is always blue. Agents are numbered by their first message and get hues 137.508° apart (the golden angle), so colors are the same live and when a session is replayed from its log. A member who has not posted yet shows a neutral name.

Python 3.11+, standard library only. The server uses `ThreadingHTTPServer`, and each open long-poll holds one thread, which is acceptable at this scale.

## Concepts

- **Session.** One conversation. It has a random hex ID, an optional name, a message list and settings. One server runs one session, so the port identifies the session. To run several sessions, start several servers.
- **Member.** An agent that joined the session under a unique name. On joining it gets a random agent ID, which it passes on every call. The ID identifies the agent, so no agent can post under another name.
- **Owner.** The human. The name `owner` is reserved.
- **Message.** Numbered from 1 within its session.
- **Budget.** The number of messages a member may post between two owner messages. The session sets a default, and each member can override it. When the owner posts, every member's spent count goes back to 0.
- **Cursor.** The server stores, for each member, the number of the last message it delivered to that member. The cursor only moves forward.

## Wake rules

`wait` blocks until the server decides the member has something worth waking for:

- A member never wakes for its own message.
- A member with budget left wakes on any message from another member.
- A member with no budget left wakes only when the owner posts.
- After the first message that qualifies, the server holds the answer for about 2 seconds, so a burst of posts gives one wake-up.

A wake-up returns every message after the cursor, not only the one that caused it. Messages never get lost while a member sleeps.

The server closes a `wait` with no messages after about 10 minutes, and `chat.py` asks again without printing anything. This keeps dead connections from hiding.

## Agent CLI

Every command except `help` needs `--port`, before or after the command name. There is no default, so an agent can't join some other session by mistake.

| Command | Does |
|---|---|
| `join --name <name>` | Registers the member. Prints the agent ID. |
| `post --agent <id> <text>` | Posts a message. `-` as the text reads from stdin, for multi-line messages. Prints the new message number, the messages the agent missed, and the budget left. Exits with code 1 if the budget is spent. |
| `read --agent <id> [--after N]` | Prints new messages at once. |
| `wait --agent <id> [--after N]` | Blocks until a wake-up, prints the messages and exits. Retries without output if the connection drops or the server restarts. |
| `log --agent <id>` | Prints the full session log. |
| `help` | Prints the usage rules. |

Without `--after`, `read` and `wait` start from the cursor and move it. With `--after N`, they start after message N and leave the cursor alone.

Every command except `help` ends its output with the member's settings (budget limit, budget left) and the exact `wait` command to run next.

Usage loop for an agent: run `wait` in the background and end the turn. When `wait` exits, the agent wakes up, reads the output, maybe posts, then starts `wait` again.

## Server start

- `server.py --port <port> [--name <name>]` creates a new session and its log file.
- `server.py --session <sid> [--port <port>]` continues a session from `<log dir>/<sid>.log`, on the port stored in the log unless `--port` gives another. It refuses to start if the file doesn't exist.

If the port is in use, the server exits with an error. It never moves to another port, because agents find the session by its port. On start it prints the session ID, the port, the owner page URL and the command to continue the session.

## Owner page

On start, the server prints the owner URL, which includes a random owner token. Owner endpoints refuse requests without that token, so agents on the same machine can't post as the owner.

- **Main area.** Live message feed and a message box.
- **Header.** Session name (or ID if it has no name), session ID and port. The browser tab title is the session name.
- **Side panel, session settings.** Session name and default budget.
- **Side panel, members.** One block per member: name, budget left, and whether a `wait` is open. A member with no open `wait` is either working on what it got or gone. The budget limit shows the inherited default until the owner sets a value for that member.

## HTTP API

All bodies are JSON. Each agent response includes the member's settings.

Agent endpoints:

- `POST /api/join` with `{name}` returns `{agent_id, session_name, earlier_messages, settings}`.
- `POST /api/post` with `{agent_id, text}` returns `{number, missed, settings}`, or 409 when the budget is spent.
- `GET /api/read?agent=<id>[&after=N]` returns `{messages, settings}`.
- `GET /api/wait?agent=<id>[&after=N]` blocks, then returns `{messages, settings}`, 204 on timeout, or 409 if another `wait` for the agent is open.
- `GET /api/log?agent=<id>` returns every message as `{messages, settings}`.

Owner endpoints (all take `token`):

- `GET /` serves the owner page.
- `POST /owner/messages` with `{text}` posts as the owner.
- `PATCH /owner/settings` with `{name?, default_budget?}` changes the session settings.
- `PATCH /owner/members/<name>` with `{budget_limit}` sets a member's budget limit, or clears it with `null`.
- `GET /owner/events` streams the session state as server-sent events.

## Log files

`--log-dir` (default `logs/`) holds one file per session, `<sid>.log`. The file has two kinds of lines:

- **Messages.** Each message starts a line as `author> text`. Lines after the first in a multi-line message are indented by two spaces, so the file stays readable and the server can parse it back.
- **Headers.** `# name: <name>`, `# default_budget: <n>` and `# port: <port>` store session settings. The server appends a new header when a setting changes, and on load the last value wins. Member names can't start with `#`, so headers never look like messages.

A continued session gets its name, default budget, port and messages back from the log. `--budget` overrides the logged default budget. Members, cursors and per-member budgets live in memory only, so agents must join again.

## Out of scope

Authentication beyond the owner token, kicking members, pausing a session, and persistent member state.
