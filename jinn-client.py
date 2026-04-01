#!/usr/bin/env python3
"""
jinn-client.py [--new] [--timeout N] <path> <message>

Sends a message to a Jarvis session scoped to <path>.
By default, reuses the last session for that path (.jinn-session file).
Use --new to force a fresh session.
Use --timeout N to set max wait in seconds (default: 600).
If <path>/CLAUDE.md exists, its contents are injected as context (new sessions only).
"""

import sys
import time
import httpx
from pathlib import Path
from typing import Optional

GATEWAY = "http://localhost:7777"
POLL_INTERVAL = 3   # seconds
DEFAULT_TIMEOUT = 600
SESSION_FILE = ".jinn-session"
IDLE_STATUSES = {"completed", "idle", "done", "error"}


def load_project_skills(path: Path) -> str:
    skills_dir = path / "skills"
    if not skills_dir.is_dir():
        return ""
    sections = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        skill_name = skill_md.parent.name
        content = skill_md.read_text().strip()
        sections.append(f"### /{skill_name}\n{content}")
    if not sections:
        return ""
    block = "\n\n".join(sections)
    return (
        f"\nProject-local skills (these OVERRIDE any global skill with the same name):\n"
        f"When the user references /{'{skill_name}'} or asks to use one of these skills, "
        f"follow the instructions below instead of any built-in version:\n\n"
        f"{block}\n"
    ).replace("{skill_name}", "skill-name")


def build_prompt(path: Path, message: str) -> str:
    claude_md = path / "CLAUDE.md"
    project_instructions = ""
    if claude_md.exists():
        project_instructions = f"\nProject instructions (CLAUDE.md):\n{claude_md.read_text().strip()}\n"

    project_skills = load_project_skills(path)

    return (
        f"<system>\n"
        f"Your working directory for this session is: {path}\n"
        f"All file operations should default to this directory unless an absolute path is given.\n"
        f"When asked where you are or what directory you are in, the answer is: {path}\n"
        f"{project_instructions}"
        f"{project_skills}"
        f"</system>\n\n"
        f"{message}"
    )


def load_session_id(path: Path) -> Optional[str]:
    session_file = path / SESSION_FILE
    if session_file.exists():
        sid = session_file.read_text().strip()
        return sid if sid else None
    return None


def save_session_id(path: Path, session_id: str):
    (path / SESSION_FILE).write_text(session_id)


def get_session(session_id: str) -> Optional[dict]:
    try:
        r = httpx.get(f"{GATEWAY}/api/sessions/{session_id}", timeout=10.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def create_session(prompt: str) -> str:
    r = httpx.post(f"{GATEWAY}/api/sessions", json={
        "prompt": prompt,
        "engine": "claude",
    }, timeout=30.0)
    r.raise_for_status()
    return r.json()["id"]


def send_message(session_id: str, message: str):
    r = httpx.post(f"{GATEWAY}/api/sessions/{session_id}/message", json={
        "message": message,
    }, timeout=30.0)
    r.raise_for_status()


def wait_for_reply(session_id: str, after_id: Optional[str], timeout: int) -> str:
    deadline = time.time() + timeout
    seen_after = after_id is None
    elapsed = 0

    while time.time() < deadline:
        session = get_session(session_id)
        if not session:
            raise RuntimeError(f"Session {session_id} not found")

        messages = session.get("messages", [])
        status = session.get("status", "")

        for msg in messages:
            if not seen_after:
                if msg["id"] == after_id:
                    seen_after = True
                continue
            if msg["role"] == "assistant":
                return msg["content"]

        # If session is idle/completed but we still haven't found the reply,
        # do one final pass before giving up
        if seen_after and status in IDLE_STATUSES:
            raise RuntimeError(f"Session finished ({status}) but no reply found after {after_id}")

        elapsed += POLL_INTERVAL
        if elapsed % 30 == 0:
            print(f"[jinn] Still waiting... ({elapsed}s)", file=sys.stderr)

        time.sleep(POLL_INTERVAL)

    raise TimeoutError(f"No reply after {timeout}s. Check logs: ~/.jinn/logs/gateway.log")


def parse_args(argv):
    args = argv[1:]
    force_new = "--new" in args
    timeout = DEFAULT_TIMEOUT

    filtered = []
    i = 0
    while i < len(args):
        if args[i] == "--new":
            i += 1
        elif args[i] == "--timeout" and i + 1 < len(args):
            timeout = int(args[i + 1])
            i += 2
        else:
            filtered.append(args[i])
            i += 1

    return force_new, timeout, filtered


def main():
    force_new, timeout, args = parse_args(sys.argv)

    if len(args) < 2:
        print(f"Usage: {sys.argv[0]} [--new] [--timeout N] <path> <message>", file=sys.stderr)
        sys.exit(1)

    path = Path(args[0]).resolve()
    message = args[1]

    if not path.exists():
        print(f"Error: path does not exist: {path}", file=sys.stderr)
        sys.exit(1)

    session_id = None

    if not force_new:
        session_id = load_session_id(path)
        if session_id and not get_session(session_id):
            print(f"[jinn] Saved session {session_id} not found, creating new one.", file=sys.stderr)
            session_id = None

    if session_id:
        print(f"[jinn] Reusing session {session_id}", file=sys.stderr)
        session = get_session(session_id)
        messages = session.get("messages", []) if session else []
        last_id = messages[-1]["id"] if messages else None
        send_message(session_id, message)
    else:
        print(f"[jinn] Creating new session for {path}", file=sys.stderr)
        prompt = build_prompt(path, message)
        session_id = create_session(prompt)
        save_session_id(path, session_id)
        session = get_session(session_id)
        messages = session.get("messages", []) if session else []
        last_id = messages[-1]["id"] if messages else None

    reply = wait_for_reply(session_id, last_id, timeout)
    print(reply)


if __name__ == "__main__":
    main()
