"""Claude Code chat history as standup context.

Reads the session transcripts Claude Code writes to ~/.claude/projects (CLI and
desktop app alike) plus the desktop app's per-session metadata, and saves one
activity per session per local day. Only messages the user typed count; tool
output, subagent transcripts and automated turns (CI monitors, task
notifications, session events) are ignored.
"""

import json
import logging
import os
import re
from collections import Counter
from datetime import date, datetime
from glob import glob

from integrations.models import Activity

from .base import BaseIntegrationService

logger = logging.getLogger(__name__)

DEFAULT_PROJECTS_DIR = "~/.claude/projects"
DEFAULT_SESSIONS_DIR = "~/Library/Application Support/Claude/claude-code-sessions"

MAX_PROMPTS = 4
MAX_PROMPT_CHARS = 220
MAX_RECAPS = 3
MAX_RECAP_CHARS = 320
MAX_TITLE_CHARS = 120

WRAP_UP_TOOL = "mcp__ccd_turn__wrap_up"
# Closing tags can repeat the opening tag's attributes: </pasted_content id="35b5">.
TAG_BLOCK_RE = re.compile(r"<(pasted_content|system-reminder|command-[a-z]+)[^>]*>.*?</\1[^>]*>", re.DOTALL)
WHITESPACE_RE = re.compile(r"\s+")
# Prefixed tickets (ATH-2626) in any case; without configured prefixes only
# uppercase matches count, so "utf-8" and "sha-256" don't become tickets.
ANY_TICKET_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{1,9})-(\d+)\b")
UPPER_TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d+)\b")


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def clean_text(text: str, limit: int) -> str:
    text = TAG_BLOCK_RE.sub(
        lambda m: " [pasted text] " if m.group(1) == "pasted_content" else " ", text
    )
    text = WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def is_human_prompt(entry: dict) -> bool:
    """True for a message the user typed (not tool results or automated turns)."""
    if entry.get("type") != "user" or entry.get("isSidechain") or entry.get("isMeta"):
        return False
    origin = entry.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") == "human"
    # CLI transcripts carry no origin: drop tool results and tagged system turns.
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list) and not any(
        isinstance(b, dict) and b.get("type") == "text" for b in content
    ):
        return False
    text = message_text(content).strip()
    return bool(text) and not text.startswith("<") and not text.startswith("[Request interrupted")


def repo_name(cwd: str) -> str:
    """Folder name when the session ran inside a git checkout, else ""."""
    if not cwd or not os.path.exists(os.path.join(cwd, ".git")):
        return ""
    return os.path.basename(cwd.rstrip("/"))


def extract_tickets(text: str, prefixes: set[str]) -> list[str]:
    pattern = ANY_TICKET_RE if prefixes else UPPER_TICKET_RE
    tickets = []
    for prefix, number in pattern.findall(text or ""):
        prefix = prefix.upper()
        if prefixes and prefix not in prefixes:
            continue
        tickets.append(f"{prefix}-{number}")
    return tickets


class SessionDay:
    def __init__(self):
        self.prompts: list[tuple[datetime, str]] = []
        self.recaps: list[tuple[datetime, str]] = []
        self.last_reply: tuple[datetime, str] | None = None


class ClaudeCodeService(BaseIntegrationService):
    integration_type = "claude_code"

    def projects_dir(self) -> str:
        return os.path.expanduser(self.config.get("projects_dir") or DEFAULT_PROJECTS_DIR)

    def sessions_dir(self) -> str:
        return os.path.expanduser(self.config.get("sessions_dir") or DEFAULT_SESSIONS_DIR)

    def ticket_prefixes(self) -> set[str]:
        return {p.upper() for p in self.config.get("ticket_prefixes", []) if p}

    def is_configured(self) -> bool:
        return os.path.isdir(self.projects_dir())

    def load_desktop_sessions(self) -> dict[str, dict]:
        """Desktop app metadata keyed by CLI session id: title + latest status."""
        sessions = {}
        for path in glob(os.path.join(self.sessions_dir(), "*", "*", "*.json")):
            try:
                with open(path) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            cli_id = data.get("cliSessionId")
            if not cli_id:
                continue
            summary = data.get("postTurnSummary") or {}
            sessions[cli_id] = {
                "title": data.get("title") or "",
                "status_detail": summary.get("status_detail") or "",
                "needs_action": summary.get("needs_action") or "",
            }
        return sessions

    def sync(self, since: datetime, until: datetime) -> list[Activity]:
        self.load_config()
        if not self.is_configured():
            logger.warning("Claude Code projects dir not found, skipping sync")
            return []

        desktop = self.load_desktop_sessions()
        since_ts = since.timestamp()
        activities = []

        # Top-level transcripts only: subagent runs live in <session>/subagents/.
        for path in glob(os.path.join(self.projects_dir(), "*", "*.jsonl")):
            try:
                if os.path.getmtime(path) < since_ts:
                    continue
                activities.extend(self.sync_transcript(path, desktop, since, until))
            except Exception:
                logger.exception("Failed to read Claude Code transcript %s", path)

        if activities:
            self.mark_synced()
        return activities

    def sync_transcript(self, path: str, desktop: dict, since: datetime, until: datetime) -> list[Activity]:
        from standups.services.generator import activity_local_date

        session_id = os.path.splitext(os.path.basename(path))[0]
        custom_title = ""
        cwd = ""
        days: dict[date, SessionDay] = {}

        with open(path, errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue

                if entry.get("type") == "custom-title":
                    custom_title = entry.get("customTitle") or custom_title
                    continue

                ts = parse_timestamp(entry.get("timestamp", ""))
                if ts is None or ts < since or ts > until or entry.get("isSidechain"):
                    continue
                cwd = entry.get("cwd") or cwd

                if is_human_prompt(entry):
                    text = clean_text(message_text(entry["message"]["content"]), MAX_PROMPT_CHARS)
                    if text:
                        days.setdefault(activity_local_date(ts), SessionDay()).prompts.append((ts, text))
                elif entry.get("type") == "assistant":
                    self.collect_assistant(entry, ts, days, activity_local_date)

        meta = desktop.get(session_id, {})
        title = custom_title or meta.get("title", "")
        last_day = max((d for d, day in days.items() if day.prompts), default=None)

        activities = []
        for day_date, day in days.items():
            if not day.prompts:
                continue  # nothing the user asked for that day
            activity = self.save_day(
                session_id=session_id,
                day_date=day_date,
                day=day,
                title=title,
                cwd=cwd,
                status_detail=meta.get("status_detail", "") if day_date == last_day else "",
            )
            if activity:
                activities.append(activity)
        return activities

    def collect_assistant(self, entry: dict, ts: datetime, days: dict, local_date) -> None:
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == WRAP_UP_TOOL:
                recap = clean_text((block.get("input") or {}).get("recap", ""), MAX_RECAP_CHARS)
                if recap:
                    days.setdefault(local_date(ts), SessionDay()).recaps.append((ts, recap))
            elif block.get("type") == "text" and block.get("text", "").strip():
                days.setdefault(local_date(ts), SessionDay()).last_reply = (
                    ts,
                    clean_text(block["text"], MAX_RECAP_CHARS),
                )

    def save_day(self, *, session_id, day_date, day, title, cwd, status_detail) -> Activity | None:
        prompts = [text for _, text in day.prompts]
        recaps = [text for _, text in day.recaps]
        if not recaps and day.last_reply:
            recaps = [day.last_reply[1]]  # CLI sessions have no wrap-up recap

        title = clean_text(title or prompts[0], MAX_TITLE_CHARS)
        prefixes = self.ticket_prefixes()
        # The session's ticket is the one it was started for (title, else the
        # day's first prompt). Tickets merely mentioned in replies (e.g. whose
        # change broke CI) must not make it into Today on their own.
        primary = extract_tickets(title, prefixes) or extract_tickets(prompts[0], prefixes)
        ticket_id = primary[0] if primary else ""
        mentioned = Counter(extract_tickets(" ".join(prompts + recaps), prefixes))

        # First prompt sets the task; the last ones show where it ended up.
        shown_prompts = prompts if len(prompts) <= MAX_PROMPTS else prompts[:2] + prompts[-(MAX_PROMPTS - 2):]
        lines = [f"Asked: {p}" for p in shown_prompts]
        lines += [f"Outcome: {r}" for r in recaps[-MAX_RECAPS:]]
        if status_detail:
            lines.append(f"Latest status: {clean_text(status_detail, MAX_RECAP_CHARS)}")

        return self.save_activity(
            source="claude_code",
            activity_type="chat_session",
            external_id=f"{session_id}:{day_date.isoformat()}",
            title=title,
            description="\n".join(lines),
            url="",
            repository=repo_name(cwd),
            ticket_id=ticket_id,
            occurred_at=day.prompts[-1][0],
            metadata={
                "session_id": session_id,
                "prompt_count": len(prompts),
                "tickets_mentioned": sorted(mentioned),
            },
        )
