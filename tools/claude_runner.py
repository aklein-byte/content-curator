"""
Headless Claude CLI runner for cheap batch AI evaluation.

Wraps `claude -p` for high-volume tasks (post scoring, reply evaluation)
where API cost matters more than latency. Falls back gracefully so callers
can use Anthropic API as backup.

Usage:
    from tools.claude_runner import claude_eval, claude_json
    text = claude_eval("You score posts 1-10.", "Score: japanese garden photo, 500 likes")
    result = claude_json("Return JSON.", '{"score": 8, "reason": "..."}')
"""

import subprocess
import json
import logging
import shutil

from tools.common import parse_json_response

log = logging.getLogger("claude_runner")

# Find claude binary once at import
CLAUDE_BIN = shutil.which("claude") or "/usr/bin/claude"


def claude_eval(system: str, prompt: str, timeout: int = 30) -> str:
    """Run a prompt through headless Claude CLI. Returns raw text or empty string."""
    # CLI has no --system flag; combine into a single prompt
    combined = f"""<system>
{system}
</system>

{prompt}"""

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", "--max-turns", "1"],
            input=combined,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.stderr:
            log.debug(f"claude CLI stderr: {result.stderr[:200]}")
        return ""
    except subprocess.TimeoutExpired:
        log.warning(f"claude CLI timed out after {timeout}s")
        return ""
    except FileNotFoundError:
        log.warning("claude CLI not found")
        return ""
    except Exception as e:
        log.warning(f"claude CLI error: {e}")
        return ""


def claude_json(system: str, prompt: str, timeout: int = 30) -> dict | None:
    """Run a prompt through headless Claude CLI and parse JSON from response.

    Returns parsed dict or None on failure. Callers should fall back to
    Anthropic API when this returns None.
    """
    text = claude_eval(system, prompt, timeout=timeout)
    if not text:
        return None

    result = parse_json_response(text)
    if result is None:
        log.debug(f"Failed to parse JSON from claude CLI output: {text[:200]}")
    return result
