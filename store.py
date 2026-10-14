"""Chat state for one session: members, budgets, messages, wake rules and the log file."""

import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

OWNER = "owner"
NAME_PATTERN = re.compile(r"^[\w.-]{1,40}$")
SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{8}$")
SESSION_NAME_MAX_LENGTH = 80
HEADER_PREFIX = "# "
DEFAULT_BUDGET = 3
WAIT_TIMEOUT_SECONDS = 600
BURST_HOLD_SECONDS = 2
CLOSED_CHECK_SECONDS = 5

log = logging.getLogger(__name__)


class ChatError(Exception):
    """Request error with the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Message:
    number: int
    author: str
    text: str

    def to_dict(self) -> dict:
        return {"number": self.number, "author": self.author, "text": self.text}


@dataclass
class Member:
    name: str
    agent_id: str
    cursor: int
    budget_limit: int | None = None
    spent: int = 0
    # Returns True when the client of the open wait has disconnected.
    open_wait: Callable[[], bool] | None = None


def format_log_entry(author: str, text: str) -> str:
    """Formats a message as `author> text`, with continuation lines indented by two spaces."""
    first, *rest = text.split("\n")
    return "".join([f"{author}> {first}\n", *(f"  {line}\n" for line in rest)])


def parse_log(path: Path) -> tuple[dict[str, str], list[Message]]:
    """Reads header values and messages from a log file. A later header overrides an earlier one."""
    headers: dict[str, str] = {}
    messages: list[Message] = []
    lines = path.read_text(encoding="utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for line_number, line in enumerate(lines, start=1):
        if (line.startswith("  ") or line == "") and messages:
            messages[-1].text += "\n" + line[2:]
            continue
        if line.startswith(HEADER_PREFIX):
            key, separator, value = line.removeprefix(HEADER_PREFIX).partition(": ")
            if separator:
                headers[key] = value
                continue
        author, separator, text = line.partition(">")
        if not separator or not NAME_PATTERN.match(author):
            log.warning("%s:%d: skipped a line that is not a header or a message", path, line_number)
            continue
        messages.append(Message(len(messages) + 1, author, text.removeprefix(" ")))
    return headers, messages


class Store:
    """Thread-safe state of one session. Every change wakes all threads that wait on `lock`."""

    def __init__(self, session_id: str, log_path: Path, name: str | None, default_budget: int,
                 port: int | None, messages: list[Message]):
        self.lock = threading.Condition()
        self.version = 0
        self.session_id = session_id
        self.log_path = log_path
        self.name = name
        self.default_budget = default_budget
        self.port = port
        self.messages = messages
        self.members: dict[str, Member] = {}
        self.agents: dict[str, Member] = {}

    @classmethod
    def create(cls, log_dir: Path, name: str | None, default_budget: int, port: int) -> "Store":
        """Starts a new session with a new log file."""
        if name is not None:
            name = _validate_session_name(name)
        log_dir.mkdir(parents=True, exist_ok=True)
        session_id = secrets.token_hex(4)
        store = cls(session_id, log_dir / f"{session_id}.log", None, default_budget, port, [])
        store.log_path.touch()
        if name is not None:
            store._set_name(name)
        store._write(f"{HEADER_PREFIX}default_budget: {default_budget}\n{HEADER_PREFIX}port: {port}\n")
        return store

    @classmethod
    def open(cls, log_dir: Path, session_id: str) -> "Store":
        """Continues a session from its log file."""
        log_path = log_dir / f"{session_id}.log"
        if not SESSION_ID_PATTERN.match(session_id) or not log_path.exists():
            raise FileNotFoundError(f"no log file for session {session_id!r} at {log_path}")
        headers, messages = parse_log(log_path)
        logged_budget = headers.get("default_budget", "")
        logged_port = headers.get("port", "")
        return cls(session_id, log_path, headers.get("name"),
                   int(logged_budget) if logged_budget.isdigit() else DEFAULT_BUDGET,
                   int(logged_port) if logged_port.isdigit() else None, messages)

    def _changed(self) -> None:
        self.version += 1
        self.lock.notify_all()

    def _write(self, text: str) -> None:
        with self.log_path.open("a", encoding="utf-8", newline="\n") as log_file:
            log_file.write(text)

    def _set_name(self, name: object) -> None:
        self.name = name = _validate_session_name(name)
        self._write(f"{HEADER_PREFIX}name: {name}\n")

    def _set_default_budget(self, budget: object) -> None:
        self.default_budget = _validate_budget(budget)
        self._write(f"{HEADER_PREFIX}default_budget: {self.default_budget}\n")

    def _agent(self, agent_id: str) -> Member:
        member = self.agents.get(agent_id)
        if member is None:
            raise ChatError(404, "unknown agent ID; the server may have restarted, join again")
        return member

    def _append(self, author: str, text: str) -> Message:
        text = text.replace("\r\n", "\n").strip()
        if not text:
            raise ChatError(400, "message is empty")
        message = Message(len(self.messages) + 1, author, text)
        self.messages.append(message)
        self._write(format_log_entry(author, text))
        return message

    def _limit_of(self, member: Member) -> int:
        return self.default_budget if member.budget_limit is None else member.budget_limit

    def _budget_left(self, member: Member) -> int:
        return max(0, self._limit_of(member) - member.spent)

    def _messages_after(self, number: int) -> list[Message]:
        # Message numbers start at 1 and have no gaps.
        return self.messages[max(0, number):]

    def _agent_settings(self, member: Member) -> dict:
        return {"budget_limit": self._limit_of(member), "budget_left": self._budget_left(member)}

    def _should_wake(self, member: Member, start: int) -> bool:
        has_budget = self._budget_left(member) > 0
        return any(
            message.author == OWNER or (message.author != member.name and has_budget)
            for message in self._messages_after(start)
        )

    def _deliver(self, member: Member, after: int | None) -> dict:
        """Returns messages after `after`, or after the cursor and moves it when `after` is None."""
        messages = self._messages_after(member.cursor if after is None else after)
        if after is None and messages:
            member.cursor = messages[-1].number
        return {
            "messages": [message.to_dict() for message in messages],
            "settings": self._agent_settings(member),
        }

    def join(self, name: str) -> dict:
        with self.lock:
            if not NAME_PATTERN.match(name) or name == OWNER:
                raise ChatError(400, f"invalid name {name!r}: use 1-40 letters, digits, '_', '.', '-'; '{OWNER}' is reserved")
            if name in self.members:
                raise ChatError(409, f"name {name!r} is taken in this session")
            member = Member(name, secrets.token_hex(8), cursor=len(self.messages))
            self.members[name] = member
            self.agents[member.agent_id] = member
            self._changed()
            log.info("%s joined", name)
            return {
                "agent_id": member.agent_id,
                "session_name": self.name or self.session_id,
                "earlier_messages": len(self.messages),
                "settings": self._agent_settings(member),
            }

    def post(self, agent_id: str, text: str) -> dict:
        with self.lock:
            member = self._agent(agent_id)
            if self._budget_left(member) == 0:
                raise ChatError(409, "budget spent; wait until the owner posts")
            message = self._append(member.name, text)
            missed = [
                missed_message.to_dict()
                for missed_message in self.messages[member.cursor:message.number - 1]
                if missed_message.author != member.name
            ]
            member.cursor = message.number
            member.spent += 1
            self._changed()
            return {"number": message.number, "missed": missed, "settings": self._agent_settings(member)}

    def owner_post(self, text: str) -> dict:
        with self.lock:
            message = self._append(OWNER, text)
            for member in self.members.values():
                member.spent = 0
            self._changed()
            return {"number": message.number}

    def read(self, agent_id: str, after: int | None) -> dict:
        with self.lock:
            return self._deliver(self._agent(agent_id), after)

    def full_log(self, agent_id: str) -> dict:
        with self.lock:
            member = self._agent(agent_id)
            return {
                "messages": [message.to_dict() for message in self.messages],
                "settings": self._agent_settings(member),
            }

    def wait(self, agent_id: str, after: int | None, is_closed: Callable[[], bool]) -> dict | None:
        """Blocks until the wake rules allow a wake-up. Returns None on timeout or disconnect."""
        deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
        with self.lock:
            member = self._agent(agent_id)
            if member.open_wait is not None and not member.open_wait():
                raise ChatError(409, "a wait is already running for this agent; end your turn and let it wake you")
            member.open_wait = is_closed
            self._changed()
            try:
                while not self._should_wake(member, member.cursor if after is None else after):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self.lock.wait(min(remaining, CLOSED_CHECK_SECONDS))
                    if member.open_wait is not is_closed or is_closed():
                        return None
            finally:
                if member.open_wait is is_closed:
                    member.open_wait = None
                    self._changed()
        # NOTE: Holding the answer lets one wake-up cover a burst of posts.
        time.sleep(BURST_HOLD_SECONDS)
        if is_closed():
            return None
        with self.lock:
            return self._deliver(member, after)

    def wait_for_change(self, version: int, timeout: float) -> int:
        """Blocks until the state version differs from `version` or the timeout passes."""
        with self.lock:
            if self.version == version:
                self.lock.wait(timeout)
            return self.version

    def snapshot(self, after: int) -> dict:
        """Returns the owner's view of the session, with messages after `after`."""
        with self.lock:
            return {
                "id": self.session_id,
                "name": self.name,
                "default_budget": self.default_budget,
                "messages": [message.to_dict() for message in self._messages_after(after)],
                "members": [
                    {
                        "name": member.name,
                        "budget_limit": member.budget_limit,
                        "budget_left": self._budget_left(member),
                        "effective_limit": self._limit_of(member),
                        "listening": member.open_wait is not None,
                    }
                    for member in self.members.values()
                ],
            }

    def update_settings(self, name: str | None, default_budget: int | None, port: int | None = None) -> None:
        """Changes session settings and logs the changed ones. None leaves a value as it is."""
        with self.lock:
            if name is not None and name != self.name:
                self._set_name(name)
            if default_budget is not None and default_budget != self.default_budget:
                self._set_default_budget(default_budget)
            if port is not None and port != self.port:
                self.port = port
                self._write(f"{HEADER_PREFIX}port: {port}\n")
            self._changed()

    def set_member_budget(self, name: str, budget: int | None) -> None:
        with self.lock:
            member = self.members.get(name)
            if member is None:
                raise ChatError(404, f"unknown member {name!r}")
            member.budget_limit = None if budget is None else _validate_budget(budget)
            self._changed()


def _validate_budget(budget: object) -> int:
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
        raise ChatError(400, "budget must be a whole number, 0 or more")
    return budget


def _validate_session_name(name: object) -> str:
    name = str(name).strip()
    if not name or len(name) > SESSION_NAME_MAX_LENGTH or "\n" in name:
        raise ChatError(400, f"session name must be one line of 1-{SESSION_NAME_MAX_LENGTH} characters")
    return name
