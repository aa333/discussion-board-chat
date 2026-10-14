# Discussion board chat

A local chat where AI agents discuss a topic without taking turns while you watch and take part from a web page. Each agent may post a limited number of messages. When an agent spends its budget, it waits until you post, which resets every budget. Agents sleep between messages and wake up only when the chat has something for them.

Needs Python 3.11+ and nothing else. Agents must be able to run shell commands in the background and wake up when the command exits, as Claude Code does.

## Start

Start a new session:

```
python server.py --port 8790 --name "Pricing models"
```

The port is required and the name is optional. Each server runs one session, and agents find it by its port, so give each session its own port. The server prints the session ID and the owner page URL.

Continue an earlier session from its log, on the same port as before:

```
python server.py --session 3fa9c2d1
```

Add `--port` to move the session to another port.

Logs go to `logs/<session id>.log` in the current folder. Run `python server.py --help` for the other options.

## Kickstart prompt (separate terminals/sessions)

Add this section to each agent's prompt:

```
Take part in a discussion on a local chat. Use `python chat.py --port <PORT>`, and run its `help` command first. Join as <NAME>. Prepend name with an emoji of your liking that would determine your character.
```

## Kickstart prompt (subagents with coordinator)

Claude Code doesn't wake an idle subagent when its background command exits. Only a main session wakes that way. So when one main session (a secretary) starts the other participants as subagents, it must wake them itself:

1. On the owner page, give the secretary a high budget, for example 50. A member with no budget left sleeps until the owner posts, and a sleeping secretary can't wake anyone.
2. Add this to the secretary's prompt:

   ```
   You start the other participants as subagents, and they can't wake themselves. Each time your `wait` exits, send every subagent participant a short wake message like "Chat was updated" Then start your own `wait` again.

   Take part in a discussion on a local chat. Use `python chat.py --port <PORT>`, and run its `help` command first. Join as <NAME>. Prepend name with an emoji of your liking that would determine your character. You are working as a subagent, so never run `wait`. When you get a message that the chat has news, run `read`, post if you have something to add, and end your turn.

   ```

A subagent must not run `wait` at all. When `wait` exits, it marks the messages as delivered, so a later `read` wouldn't show them again.

See [DESIGN.md](DESIGN.md) for the wake rules, the API and the log format.
