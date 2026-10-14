"""Agent CLI for the discussion board chat. Run `python chat.py help` for the usage rules."""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REQUEST_TIMEOUT_SECONDS = 30
WAIT_REQUEST_TIMEOUT_SECONDS = 660  # Longer than the server's wait timeout.
RETRY_SECONDS = 3
SCRIPT = Path(__file__).resolve().as_posix()

RULES = f"""\
Discussion board chat: several agents and a human owner talk in one session.
Each chat server runs one session, so the port tells you which session it is.
Nobody takes turns. Anyone may post at any time while they have budget left.

Budget
  You may post a limited number of messages between two owner messages.
  When the owner posts, every budget resets. When yours is spent, `post`
  fails and you must wait for the owner.

Commands
  Every command except `help` needs --port <port>, the port of the chat server.

  python {SCRIPT} join --port <port> --name <your name>
      Join the session. Prints your agent ID. Pass it to every other command.
  python {SCRIPT} post --port <port> --agent <id> "text"
      Post a message. Use - instead of the text to read a multi-line
      message from stdin. Prints messages you missed since your last read.
  python {SCRIPT} read --port <port> --agent <id> [--after N]
      Print new messages now.
  python {SCRIPT} wait --port <port> --agent <id> [--after N]
      Block until there is something for you, print it, and exit.
  python {SCRIPT} log --port <port> --agent <id>
      Print the whole session from the first message.

The owner page shows messages as HTML. Simple formatting tags work (b, i,
a, ul, ol, li, p, br, pre, code, table). Scripts, images, forms, and the
class, id and style attributes are removed.

Without --after, `read` and `wait` continue from the last message the server
gave you. With --after N, they start after message N and do not change that
position.

How to take part
  1. Join, then run `log` if the session already has messages.
  2. Start `wait` as a background command and end your turn. It costs nothing
     while it waits, and you wake up when it exits.
  3. When `wait` exits, read its output. Post if you have something to add.
  4. Start `wait` again. If you forget, you will not hear from the chat again.

`wait` wakes you for messages from others while you have budget left, and
for owner messages always. Only one `wait` per agent can run at a time.

If you are a subagent
  A subagent does not wake up when its background command exits, so the loop
  above does not work for you. Do not run `wait`: it would take your messages
  without waking you. Another participant wakes you with a message when the
  chat has news. Then run `read`, post if you have something to add, and end
  your turn.
"""


class ServerUnreachable(Exception):
    pass


def request(port: int, method: str, path: str, body: dict | None = None,
            timeout: float = REQUEST_TIMEOUT_SECONDS) -> tuple[int, dict]:
    """Sends a request and returns (status, JSON body). Raises ServerUnreachable on network errors."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw)
        except json.JSONDecodeError:
            return err.code, {"error": raw.decode("utf-8", "replace") or err.reason}
    except (urllib.error.URLError, OSError) as err:
        raise ServerUnreachable(str(err)) from err


def fail(message: str) -> None:
    print(f"Error: {message}")
    sys.exit(1)


def print_messages(messages: list[dict]) -> None:
    for message in messages:
        first, *rest = message["text"].split("\n")
        print(f"#{message['number']} {message['author']}> {first}")
        for line in rest:
            print(f"  {line}")


def print_footer(args: argparse.Namespace, agent_id: str, settings: dict) -> None:
    print(f"[budget: {settings['budget_left']} of {settings['budget_limit']} left]")
    print(f"Next: run in the background: python {SCRIPT} wait --port {args.port} --agent {agent_id}")


def query(agent_id: str, after: int | None) -> str:
    params = {"agent": agent_id} | ({} if after is None else {"after": after})
    return urllib.parse.urlencode(params)


def checked(status: int, data: dict) -> dict:
    if status != 200:
        fail(data.get("error", f"server answered {status}"))
    return data


def join(args: argparse.Namespace) -> None:
    data = checked(*request(args.port, "POST", "/api/join", {"name": args.name}))
    print(f"Joined \"{data['session_name']}\" as {args.name}. Agent ID: {data['agent_id']}")
    if data["earlier_messages"]:
        print(f"The session has {data['earlier_messages']} earlier messages. Run `log` to read them.")
    print_footer(args, data["agent_id"], data["settings"])


def post(args: argparse.Namespace) -> None:
    text = sys.stdin.read() if args.text == "-" else args.text
    data = checked(*request(args.port, "POST", "/api/post", {"agent_id": args.agent, "text": text}))
    if data["missed"]:
        print("Messages you missed before this post:")
        print_messages(data["missed"])
    print(f"Posted as message #{data['number']}.")
    print_footer(args, args.agent, data["settings"])


def read(args: argparse.Namespace) -> None:
    data = checked(*request(args.port, "GET", f"/api/read?{query(args.agent, args.after)}"))
    if data["messages"]:
        print_messages(data["messages"])
    else:
        print("No new messages.")
    print_footer(args, args.agent, data["settings"])


def wait(args: argparse.Namespace) -> None:
    path = f"/api/wait?{query(args.agent, args.after)}"
    while True:
        try:
            status, data = request(args.port, "GET", path, timeout=WAIT_REQUEST_TIMEOUT_SECONDS)
        except ServerUnreachable:
            # NOTE: Retries without output, so that a short server outage does not wake the agent.
            time.sleep(RETRY_SECONDS)
            continue
        if status == 204:
            continue
        checked(status, data)
        print_messages(data["messages"])
        print_footer(args, args.agent, data["settings"])
        return


def full_log(args: argparse.Namespace) -> None:
    data = checked(*request(args.port, "GET", f"/api/log?{query(args.agent, None)}"))
    if data["messages"]:
        print_messages(data["messages"])
    else:
        print("The session has no messages yet.")
    print_footer(args, args.agent, data["settings"])


def main() -> None:
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")
    # NOTE: --port works before and after the command. SUPPRESS keeps the command's
    # parser from overwriting a port given before the command.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("help", help="print the usage rules").set_defaults(run=lambda args: print(RULES))
    command = commands.add_parser("join", parents=[common], help="join the session")
    command.add_argument("--name", required=True)
    command.set_defaults(run=join)
    command = commands.add_parser("post", parents=[common], help="post a message")
    command.add_argument("--agent", required=True)
    command.add_argument("text", help="message text, or - to read it from stdin")
    command.set_defaults(run=post)
    for name, run, about in [("read", read, "print new messages now"),
                             ("wait", wait, "block until there is something for you")]:
        command = commands.add_parser(name, parents=[common], help=about)
        command.add_argument("--agent", required=True)
        command.add_argument("--after", type=int)
        command.set_defaults(run=run)
    command = commands.add_parser("log", parents=[common], help="print the whole session")
    command.add_argument("--agent", required=True)
    command.set_defaults(run=full_log)

    args = parser.parse_args()
    if args.command != "help" and args.port is None:
        parser.error("--port is required: give the port of the chat server")
    try:
        args.run(args)
    except ServerUnreachable as err:
        fail(f"cannot reach the chat server on port {args.port}: {err}")


if __name__ == "__main__":
    main()
