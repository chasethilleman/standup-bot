from standups.models import Standup

BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
DIM = "\033[2m"
RESET = "\033[0m"


def format_standup(standup: Standup) -> str:
    lines = []
    lines.append("")
    lines.append(f"{BOLD}{CYAN}{'═' * 50}{RESET}")
    lines.append(f"{BOLD}{CYAN}  Daily Standup — {standup.date}{RESET}")
    lines.append(f"{BOLD}{CYAN}{'═' * 50}{RESET}")
    lines.append("")

    if standup.yesterday:
        lines.append(f"{BOLD}{GREEN}## Yesterday{RESET}")
        lines.append(standup.yesterday)
        lines.append("")

    if standup.today:
        lines.append(f"{BOLD}{YELLOW}## Today{RESET}")
        lines.append(standup.today)
        lines.append("")

    if standup.blockers:
        lines.append(f"{BOLD}{RED}## Blockers{RESET}")
        lines.append(standup.blockers)
        lines.append("")

    tokens = standup.prompt_tokens + standup.completion_tokens
    if tokens:
        lines.append(f"{DIM}Tokens used: {tokens} (in: {standup.prompt_tokens}, out: {standup.completion_tokens}){RESET}")

    lines.append(f"{BOLD}{CYAN}{'═' * 50}{RESET}")
    lines.append("")

    return "\n".join(lines)


def format_raw_activities(activities) -> str:
    if not activities:
        return "\nNo activities found for this period.\n"

    lines = []
    lines.append("")
    lines.append(f"{BOLD}{CYAN}{'═' * 50}{RESET}")
    lines.append(f"{BOLD}{CYAN}  Raw Activities{RESET}")
    lines.append(f"{BOLD}{CYAN}{'═' * 50}{RESET}")

    current_source = None
    for activity in activities:
        if activity.source != current_source:
            current_source = activity.source
            lines.append("")
            lines.append(f"{BOLD}{GREEN}[{current_source.upper()}]{RESET}")

        timestamp = activity.occurred_at.strftime("%Y-%m-%d %H:%M")
        lines.append(f"  {DIM}{timestamp}{RESET} {YELLOW}{activity.activity_type}{RESET}: {activity.title}")
        if activity.url:
            lines.append(f"           {DIM}{activity.url}{RESET}")

    lines.append("")
    lines.append(f"{DIM}Total: {len(activities)} activities{RESET}")
    lines.append(f"{BOLD}{CYAN}{'═' * 50}{RESET}")
    lines.append("")

    return "\n".join(lines)
