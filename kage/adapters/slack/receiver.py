"""Slack side of the receiver's decision path: a button click on a card (`block_actions`) -> the runtime command that
records it and the private (ephemeral) answer to the clicker; a reply in a card thread (an Events API `message`) -> the
refs the runtime routes.

Payloads are checked here (workspace, action ids, ids and timestamps) before anything runs; the runtime CLI does the
work and owns authority. A click is never an agent action: the CLI runs without an agent identity. The answers come
from the owner's catalog (`receiver.*`). Diagnostics hold ids and enumerated codes only, never text or response URLs.
"""
from __future__ import annotations

import fnmatch
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable
import urllib.parse
import urllib.request

from kage import locale

DECISION_ACTION_PREFIX = "sf_decision:"
CONFIRM_ACTION_PREFIX = "sf_confirm:"
ASK_ACTION_PREFIX = "sf_ask:"
ASK_CONFIRM_ACTION_PREFIX = "sf_askconfirm:"
# P1a dispatcher cards: run card / failure card (value = dispatch id) and breaker card (value = breaker id).
DISPATCH_ACTION_PREFIX = "sf_dispatch:"
BREAKER_ACTION_PREFIX = "sf_breaker:"
ACTION_PREFIXES = (DECISION_ACTION_PREFIX, CONFIRM_ACTION_PREFIX, ASK_ACTION_PREFIX, ASK_CONFIRM_ACTION_PREFIX,
                   DISPATCH_ACTION_PREFIX, BREAKER_ACTION_PREFIX)
DISPATCH_CONTROL = {"stop", "resume", "iterm", "view"}
DISPATCH_RETRY = {"retry": "auto", "retry_codex": "codex", "retry_claude": "claude"}
BREAKER_CHOICES = {"continue", "stop"}
DECISION_TIMEOUT_SECONDS = 20.0
DISPATCH_EPHEMERAL_LIMIT = 3000
STALE_REASONS = {"proposal_superseded", "proposal_already_confirmed", "proposal_already_rejected", "already_decided"}
AGENT_ENV_KEYS = ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
SAFE_CODE_RE = re.compile(r"[a-z0-9_:]{1,64}")
SAFE_ERROR_RE = re.compile(r"[A-Za-z0-9_:]{1,64}")  # 0.78.4: an error type, e.g. TimeoutExpired
SAFE_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
# P39: the owner's typed replies in card threads arrive as Events API `message` events (bot event `message.groups`,
# the private decision / OOO channels) and are forwarded to the runtime as refs only (channel / ts / thread_ts / user),
# never the text. The runtime owns the gate (`card-reply-config`: `cards.realtime_replies`, channels, users) and the routing.
CARD_REPLY_CHANNEL_RE = re.compile(r"[CG][A-Z0-9]{1,63}")
CARD_REPLY_USER_RE = re.compile(r"U[A-Z0-9]{1,63}")
SLACK_TS_RE = re.compile(r"\d{1,20}\.\d{1,20}")
CARD_REPLY_TTL_SECONDS = 300.0          # runtime config cache; the runtime's ttl_seconds is clamped to 30 .. 3600
CARD_REPLY_TTL_BOUNDS = (30.0, 3600.0)
CARD_REPLY_ERROR_TTL_SECONDS = 60.0     # a failed `card-reply-config` call: treated as disabled this long


def _safe(value: Any, pattern: re.Pattern[str] = SAFE_ID_RE) -> str:
    text = str(value or "")
    return text if pattern.fullmatch(text) else ""


def is_decision_action(payload: Any) -> bool:
    if not isinstance(payload, dict) or payload.get("type") != "block_actions":
        return False
    actions = payload.get("actions") or []
    first = actions[0] if isinstance(actions, list) and actions and isinstance(actions[0], dict) else {}
    return str(first.get("action_id") or "").startswith(ACTION_PREFIXES)


def decision_context(payload: dict[str, Any], *, expected_team_id: str) -> dict[str, str]:
    if not is_decision_action(payload):
        raise ValueError("unsupported_action")
    action = payload["actions"][0]
    user = payload.get("user") or {}
    team_id = str((payload.get("team") or {}).get("id") or user.get("team_id") or "")
    action_id = str(action.get("action_id") or "")
    kind, prefix = next((k, p) for k, p in (("askconfirm", ASK_CONFIRM_ACTION_PREFIX), ("ask", ASK_ACTION_PREFIX),
                                             ("confirm", CONFIRM_ACTION_PREFIX), ("decide", DECISION_ACTION_PREFIX),
                                             ("dispatch", DISPATCH_ACTION_PREFIX), ("breaker", BREAKER_ACTION_PREFIX))
                        if action_id.startswith(p))
    # Decision and dispatcher cards stay strict. Ask cards live in shared conversations: an
    # external (Slack Connect) clicker is answered privately by the CLI instead of being dropped.
    actor_team = str(user.get("team_id") or team_id or "")
    if kind in ("decide", "confirm", "dispatch", "breaker") and team_id != expected_team_id:
        raise ValueError("unexpected_team")
    choice = action_id[len(prefix):]
    if (kind == "dispatch" and choice not in DISPATCH_CONTROL and choice not in DISPATCH_RETRY) or \
            (kind == "breaker" and choice not in BREAKER_CHOICES):
        raise ValueError("unsupported_action")
    external = team_id != expected_team_id or actor_team != expected_team_id
    container = payload.get("container") or {}
    context = {
        # decide: value = escalation event ID; confirm/askconfirm: proposal ID; ask: card ID.
        "kind": kind,
        "team_id": team_id,
        # Passed to the CLI only for an external clicker; the CLI then changes nothing.
        "actor_team": (actor_team if actor_team != expected_team_id else team_id) if external else "",
        "actor": str(user.get("id") or ""),
        "choice": action_id[len(prefix):],
        "value": str(action.get("value") or ""),
        "card_channel": str(container.get("channel_id") or ""),
        "card_ts": str(container.get("message_ts") or ""),
        "response_url": str(payload.get("response_url") or ""),
    }
    if kind == "ask":
        card_id, _, option_id = context["choice"].partition(":")
        if not option_id or (context["value"] and context["value"] != card_id):
            raise ValueError("missing_decision_context")
        context.update(value=card_id, choice=option_id)
    if not all(context[key] for key in ("actor", "choice", "value", "card_channel", "card_ts")):
        raise ValueError("missing_decision_context")
    return context


def decide_command(context: dict[str, str], *, python: str, script: Path, db: str | None = None) -> list[str]:
    # -I: no PYTHON* env, user site-packages, or cwd/script-dir imports.
    command = [python, "-I", str(script)]
    if db:
        command += ["--db", str(db)]
    if context.get("kind") in ("dispatch", "breaker"):
        if context["kind"] == "breaker":
            command += ["breaker-decide", "--breaker-id", context["value"], "--choice", context["choice"]]
        elif context["choice"] in DISPATCH_RETRY:
            command += ["dispatch-retry", "--dispatch-id", context["value"], "--mode", DISPATCH_RETRY[context["choice"]]]
        else:
            command += ["dispatch-control", "--dispatch-id", context["value"], "--action", context["choice"]]
        command += ["--actor", context["actor"], "--channel", context["card_channel"], "--message-ts", context["card_ts"]]
    elif context.get("kind") in ("ask", "askconfirm"):
        command += (["ask-answer", "--card-id", context["value"], "--option", context["choice"]]
                    if context["kind"] == "ask" else
                    ["ask-confirm", "--proposal-id", context["value"], "--answer", context["choice"]])
        command += ["--actor", context["actor"], "--channel", context["card_channel"],
                    "--message-ts", context["card_ts"]]
        if context.get("actor_team"):
            command += ["--actor-team", context["actor_team"]]
    elif context.get("kind") == "confirm":
        command += [
            "confirm-decision",
            "--proposal-id", context["value"],
            "--answer", context["choice"],
            "--actor", context["actor"],
            "--card-channel", context["card_channel"],
            "--message-ts", context["card_ts"],
        ]
    else:
        command += [
            "decide",
            "--event-id", context["value"],
            "--choice", context["choice"],
            "--actor", context["actor"],
            "--card-channel", context["card_channel"],
            "--card-ts", context["card_ts"],
        ]
    if context.get("response_url"):
        command += ["--response-url", context["response_url"]]
    return command


def _mrkdwn_escape(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_card_reply_event(payload: Any) -> bool:
    """An Events API `message` event (the only events_api payload the receiver acts on)."""
    return isinstance(payload, dict) and payload.get("type") == "event_callback" and \
        isinstance(payload.get("event"), dict) and payload["event"].get("type") == "message"


def card_reply_refs(payload: dict[str, Any], *, expected_team_id: str) -> tuple[dict[str, str] | None, str]:
    """The cheap receiver-side filter for a card-thread reply: (refs, "") for a plain human thread reply in this
    workspace, else (None, drop code). Refs are ids only; the message text is never read."""
    if not is_card_reply_event(payload):
        return None, "not_message_event"
    event = payload["event"]
    if str(payload.get("team_id") or "") != expected_team_id:
        return None, "unexpected_team"
    for key in ("subtype", "bot_id", "app_id"):  # edits, joins, bot / app (incl. user-token automation) posts
        if event.get(key) not in (None, ""):
            return None, "has_" + key
    refs = {key: event.get(key) for key in ("channel", "ts", "thread_ts", "user")}
    if not isinstance(refs["channel"], str) or not CARD_REPLY_CHANNEL_RE.fullmatch(refs["channel"]):
        return None, "bad_channel"
    if not isinstance(refs["ts"], str) or not SLACK_TS_RE.fullmatch(refs["ts"]):
        return None, "bad_ts"
    if refs["thread_ts"] in (None, ""):
        return None, "not_thread_reply"
    if not isinstance(refs["thread_ts"], str) or not SLACK_TS_RE.fullmatch(refs["thread_ts"]):
        return None, "bad_thread_ts"
    if refs["thread_ts"] == refs["ts"]:
        return None, "not_thread_reply"  # the thread root itself
    if not isinstance(refs["user"], str) or not CARD_REPLY_USER_RE.fullmatch(refs["user"]):
        return None, "bad_user"
    return refs, ""


def card_reply_app_refs(payload: dict[str, Any], *, expected_team_id: str, own_bot_user: str | None,
                        own_app_id: str | None = None, delegate_names: Any = ()) -> tuple[dict[str, str] | None, str]:
    """0.78.8 (F20): a card-thread reply a person posted through an app with their own user token (bot_id / app_id set,
    a U… / W… `user`): (refs, "") when the app is not ours, not the bot's own post, and its name is allowed by
    `intake.delegate_bot_names` (as F14 / F15 for intake); else (None, drop code). The runtime re-reads the message and
    checks that the user is the owner (or owner-like) and a person (users.info) before it counts."""
    if not is_card_reply_event(payload):
        return None, "not_message_event"
    event = payload["event"]
    if str(payload.get("team_id") or "") != expected_team_id:
        return None, "unexpected_team"
    if event.get("subtype") not in (None, ""):
        return None, "has_subtype"
    profile = event.get("bot_profile") if isinstance(event.get("bot_profile"), dict) else {}
    if own_bot_user and event.get("user") == own_bot_user:
        return None, "own_bot"
    if own_app_id and own_app_id in (event.get("app_id"), profile.get("app_id")):
        return None, "our_app"
    name = str(profile.get("name") or event.get("username") or "").lower()
    names = [str(n).lower() for n in delegate_names or () if isinstance(n, str)]
    if not name or not any(fnmatch.fnmatchcase(name, n) for n in names):
        return None, "app_not_delegated"
    plain = {**payload, "event": {k: v for k, v in event.items() if k not in ("bot_id", "app_id", "bot_profile")}}
    return card_reply_refs(plain, expected_team_id=expected_team_id)


def control_refs(payload: dict[str, Any], *, expected_team_id: str) -> tuple[dict[str, str] | None, str]:
    """Phase 2 C2: the receiver-side filter for a NEW top-level message (the owner's control message in the card
    channel): (refs, "") for a plain human top-level message in this workspace, else (None, drop code). Ids only; the
    runtime reads the message back and decides whether it is the owner's own (`control-push`)."""
    if not is_card_reply_event(payload):
        return None, "not_message_event"
    event = payload["event"]
    if str(payload.get("team_id") or "") != expected_team_id:
        return None, "unexpected_team"
    for key in ("subtype", "bot_id", "app_id"):
        if event.get(key) not in (None, ""):
            return None, "has_" + key
    refs = {key: event.get(key) for key in ("channel", "ts", "user")}
    if not isinstance(refs["channel"], str) or not CARD_REPLY_CHANNEL_RE.fullmatch(refs["channel"]):
        return None, "bad_channel"
    if not isinstance(refs["ts"], str) or not SLACK_TS_RE.fullmatch(refs["ts"]):
        return None, "bad_ts"
    if event.get("thread_ts") not in (None, "", refs["ts"]):
        return None, "thread_reply"
    if not isinstance(refs["user"], str) or not CARD_REPLY_USER_RE.fullmatch(refs["user"]):
        return None, "bad_user"
    return refs, ""


INTAKE_CHANNEL_RE = re.compile(r"[CGD][A-Z0-9]{1,63}")


def is_intake_event(payload: Any) -> bool:
    """0.77.0 bot intake: an `app_mention` (anywhere the bot is), or a `message` in a DM with the bot."""
    if not (isinstance(payload, dict) and payload.get("type") == "event_callback" and isinstance(payload.get("event"), dict)):
        return False
    event = payload["event"]
    if event.get("type") == "app_mention":
        return True
    # a DM message to the bot; a reply in a DM thread stays a card reply (cards may live in the owner's DM)
    return event.get("type") == "message" and event.get("channel_type") == "im" and \
        event.get("thread_ts") in (None, "", event.get("ts"))


def is_thread_followup_event(payload: Any) -> bool:
    """0.79.2 (F28): a `message` reply in a channel / private-channel thread (no @mention event of its own). The runtime
    takes it only when that thread is a conversation with the bot (`intake-push --followup`); a card-reply channel's
    threads stay with the card-reply path."""
    if not (isinstance(payload, dict) and payload.get("type") == "event_callback" and isinstance(payload.get("event"), dict)):
        return False
    event = payload["event"]
    return event.get("type") == "message" and event.get("channel_type") in ("channel", "group") and \
        event.get("thread_ts") not in (None, "", event.get("ts"))


def intake_followup_refs(payload: dict[str, Any], *, expected_team_id: str, own_bot_user: str | None = None,
                         own_app_id: str | None = None) -> tuple[dict[str, str] | None, str]:
    """0.79.2 (F28): `intake_refs` for a thread follow-up (`is_thread_followup_event`): the same checks, the thread kept."""
    if not is_thread_followup_event(payload):
        return None, "not_thread_followup"
    return _intake_refs(payload, expected_team_id=expected_team_id, own_bot_user=own_bot_user, own_app_id=own_app_id)


def intake_refs(payload: dict[str, Any], *, expected_team_id: str, own_bot_user: str | None = None,
                own_app_id: str | None = None) -> tuple[dict[str, str] | None, str]:
    """0.77.0 bot intake: the receiver-side filter for a message to the bot (an @mention, or a DM): (refs, "") for a
    plain human message in this workspace, else (None, drop code). Ids only; the runtime reads the message back
    (`intake-push`) and its intake decides what it is. 0.78.3 (F14): a post through an app (bot_id / app_id) with a
    person's `user` (their own user token) passes too unless it is ours; the runtime keeps it only when users.info says
    a person and the app is allowed (`intake.delegate_bot_names`)."""
    if not is_intake_event(payload):
        return None, "not_intake_event"
    return _intake_refs(payload, expected_team_id=expected_team_id, own_bot_user=own_bot_user, own_app_id=own_app_id)


def _intake_refs(payload: dict[str, Any], *, expected_team_id: str, own_bot_user: str | None,
                 own_app_id: str | None) -> tuple[dict[str, str] | None, str]:
    event = payload["event"]
    if str(payload.get("team_id") or "") != expected_team_id:
        return None, "unexpected_team"
    if event.get("subtype") not in (None, ""):
        return None, "has_subtype"
    if own_bot_user and event.get("user") == own_bot_user:
        return None, "our_bot"
    profile = event.get("bot_profile") if isinstance(event.get("bot_profile"), dict) else {}
    if own_app_id and own_app_id in (event.get("app_id"), profile.get("app_id")):
        return None, "our_app"
    refs = {key: event.get(key) for key in ("channel", "ts", "user")}
    if not isinstance(refs["channel"], str) or not INTAKE_CHANNEL_RE.fullmatch(refs["channel"]):
        return None, "bad_channel"
    if not isinstance(refs["ts"], str) or not SLACK_TS_RE.fullmatch(refs["ts"]):
        return None, "bad_ts"
    if not isinstance(refs["user"], str) or not CARD_REPLY_USER_RE.fullmatch(refs["user"]):
        return None, "bad_user"
    thread_ts = event.get("thread_ts")
    if thread_ts not in (None, "", refs["ts"]):
        if not isinstance(thread_ts, str) or not SLACK_TS_RE.fullmatch(thread_ts):
            return None, "bad_thread_ts"
        refs["thread_ts"] = thread_ts
    return refs, ""


def receipt_refs(payload: Any, *, expected_team_id: str, own_bot_user: str | None, own_app_id: str | None = None,
                 delegate_names: Any = ()) -> tuple[str, str, str | None] | None:
    """0.78.3 (F12): (channel, ts, person) of a message to the bot that gets 👀 at once, in the receiver itself; never
    the bot's own. A plain human @mention / DM (no bot_id / app_id / bot_profile): person None, decided from the payload
    alone. 0.78.4 (F15): a post through an app whose name intake allows (`intake.delegate_bot_names`, as P42.1): person =
    its `user`, reacted to only when users.info says a person (the receipt thread, cached)."""
    refs, _ = intake_refs(payload, expected_team_id=expected_team_id, own_bot_user=own_bot_user,
                          own_app_id=own_app_id) if is_intake_event(payload) else (None, "")
    if refs is None or not own_bot_user:
        return None
    event = payload["event"]
    if not any(event.get(key) not in (None, "") for key in ("bot_id", "app_id", "bot_profile")):
        return refs["channel"], refs["ts"], None
    profile = event.get("bot_profile") if isinstance(event.get("bot_profile"), dict) else {}
    name = str(profile.get("name") or event.get("username") or "").lower()
    names = [str(n).lower() for n in delegate_names or () if isinstance(n, str)]
    if not name or not any(fnmatch.fnmatchcase(name, n) for n in names):
        return None
    return refs["channel"], refs["ts"], refs["user"]


def intake_push_command(refs: dict[str, str], *, python: str, script: Path, db: str | None = None) -> list[str]:
    # Refs only: the runtime reads the message itself. Never the text.
    command = followup_base_command(python=python, script=script, db=db) + [
        "intake-push", "--channel", refs["channel"], "--ts", refs["ts"], "--user", refs["user"]]
    command += ["--thread-ts", refs["thread_ts"]] if refs.get("thread_ts") else []
    return command + ["--followup"] if refs.get("followup") else command


def control_push_command(refs: dict[str, str], *, python: str, script: Path, db: str | None = None) -> list[str]:
    # Refs only: the runtime reads the message itself. Never the text.
    return followup_base_command(python=python, script=script, db=db) + [
        "control-push", "--channel", refs["channel"], "--ts", refs["ts"], "--user", refs["user"]]


def followup_base_command(*, python: str, script: Path, db: str | None = None) -> list[str]:
    command = [python, "-I", str(script)]
    return command + ["--db", str(db)] if db else command


def card_reply_config_command(*, python: str, script: Path, db: str | None = None) -> list[str]:
    return followup_base_command(python=python, script=script, db=db) + ["card-reply-config"]


def card_reply_push_command(refs: dict[str, str], *, python: str, script: Path, db: str | None = None) -> list[str]:
    # Refs only: the runtime reads the message itself if it needs it. Never the text.
    return followup_base_command(python=python, script=script, db=db) + [
        "card-reply-push", "--channel", refs["channel"], "--ts", refs["ts"], "--thread-ts", refs["thread_ts"],
        "--user", refs["user"]]


def card_reply_ttl(value: Any) -> float:
    """The runtime's `ttl_seconds`, clamped to 30 .. 3600; 300 when it is not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return CARD_REPLY_TTL_SECONDS
    low, high = CARD_REPLY_TTL_BOUNDS
    return min(max(float(value), low), high)


def parse_card_reply_config(result: Any) -> dict[str, Any] | None:
    """A validated `card-reply-config` answer, or None (a failed / malformed answer: disabled for 60 s)."""
    if not isinstance(result, dict) or result.get("ok") is not True or not isinstance(result.get("enabled"), bool):
        return None
    channels, users = result.get("channels", []), result.get("users", [])
    if not isinstance(channels, list) or not isinstance(users, list):
        return None
    return {
        "enabled": result["enabled"],
        "channels": frozenset(c for c in channels if isinstance(c, str) and CARD_REPLY_CHANNEL_RE.fullmatch(c)),
        "users": frozenset(u for u in users if isinstance(u, str) and CARD_REPLY_USER_RE.fullmatch(u)),
        "ttl_seconds": card_reply_ttl(result.get("ttl_seconds")),
        "error": False,
        "control": result.get("control") is True,  # Phase 2 C2: forward the owner's new top-level messages too
        "intake_push": result.get("intake_push") is True,  # 0.77.0: forward @mentions of the bot and its DMs
        "intake_ack": result.get("intake_ack") is True,  # 0.78.3: 👀 in the receiver itself (intake.ack_on_receipt)
        "delegate_bot_names": tuple(str(n) for n in (result.get("delegate_bot_names") or [])[:20]
                                    if isinstance(n, str) and len(n) <= 80),  # 0.78.4: app posts by a person
    }


def card_reply_drop_diagnostic(payload: Any, reason: str) -> dict[str, str]:
    event = payload.get("event") if isinstance(payload, dict) else None
    event = event if isinstance(event, dict) else {}
    return {
        "event": "card_reply_dropped",
        "reason": _safe(reason, SAFE_CODE_RE),
        "channel_id": _safe(event.get("channel")),
        "message_ts": _safe(event.get("ts")),
    }

def run_followup_json(
    command: list[str],
    *,
    runner: Callable[..., Any] | None = None,
    script: Path,
    timeout: float = DECISION_TIMEOUT_SECONDS,
    prefix: str = "decide",
) -> dict[str, Any]:
    """Run one receiver-only runtime CLI command (cwd = the runtime's skill, agent identity removed, output captured)
    and return its JSON object; failures are `{"ok": false, "reason": "<prefix>_timeout|_unavailable|_failed|
    _invalid_output"}`."""
    runner = runner or subprocess.run
    # A click or a Slack event is never an agent action: do not leak an agent identity to the child.
    env = {key: value for key, value in os.environ.items() if key not in AGENT_ENV_KEYS}
    try:
        completed = runner(
            command,
            cwd=str(Path(script).resolve().parents[1]),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": prefix + "_timeout"}
    except OSError:
        return {"ok": False, "reason": prefix + "_unavailable"}
    if completed.returncode != 0:  # 0.78.4 (F16): which failure, as a code (the CLI's error type), never its text
        return {"ok": False, "reason": prefix + "_failed", "returncode": completed.returncode,
                "error": failure_code(getattr(completed, "stderr", ""))}
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        return {"ok": False, "reason": prefix + "_invalid_output"}
    return result if isinstance(result, dict) else {"ok": False, "reason": prefix + "_invalid_output"}


def failure_code(stderr: Any) -> str:
    """A failed CLI run's cause as a code: the `error` of its JSON line (`{"error", "detail"}`), else the exception type
    of a traceback's last line, else "unknown". Never the detail text."""
    lines = [line.strip() for line in str(stderr or "").splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            data = json.loads(line)
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("error"), str):
            return _safe(data["error"], SAFE_ERROR_RE) or "unknown"
        match = re.match(r"([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Exit|Interrupt|Expired))\b", line)
        if match:
            return _safe(match.group(1).rsplit(".", 1)[-1], SAFE_ERROR_RE) or "unknown"
    return "unknown"


def run_decide(
    context: dict[str, str],
    *,
    runner: Callable[..., Any] | None = None,
    python: str | None = None,
    script: Path,
    db: str | None = None,
    timeout: float = DECISION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    script = Path(script)
    return run_followup_json(decide_command(context, python=python or sys.executable, script=script, db=db),
                             runner=runner, script=script, timeout=timeout, prefix="decide")


def post_ephemeral(
    response_url: str,
    text: str,
    *,
    opener: Callable[..., Any] | None = None,
    guard_env: str | None = None,
) -> bool:
    """POST the private answer to Slack's response_url (hooks.slack.com only). With `guard_env` set to 1 in the
    environment (test runs), a real post is refused."""
    parsed = urllib.parse.urlsplit(response_url or "")
    if parsed.scheme != "https" or parsed.hostname != "hooks.slack.com":
        return False
    if opener is None and guard_env and os.environ.get(guard_env) == "1":  # tests never reach Slack
        raise RuntimeError(f"real_slack_disabled: {guard_env}=1 refuses a response_url post")
    body = json.dumps(
        {"response_type": "ephemeral", "replace_original": False, "text": text},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        response_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with (opener or urllib.request.urlopen)(request, timeout=10) as response:
        response.read()
    return True


class ClickAnswers:
    """The clicker's private answers in the owner's language, and the click path that sends them."""

    def __init__(self, language: str, *, owner_name: str, guard_env: str | None = None):
        self.language, self.guard_env = language, guard_env
        self.not_authorized = self.msg("receiver.not_authorized", owner=owner_name)
        self.not_proposal_author = self.msg("receiver.not_proposal_author", owner=owner_name)
        self.stale = self.msg("receiver.stale")
        self.ask_fallback = self.msg("receiver.ask_fallback")

    def msg(self, key: str, **values: Any) -> str:
        return locale.message(self.language, key, **values)

    def decision_ephemeral_text(self, context: dict[str, str], result: dict[str, Any]) -> str | None:
        if context.get("kind") in ("dispatch", "breaker"):
            # The dispatcher CLI owns that wording. A run card's [view] returns a multi-line run summary (masked steps),
            # so run-card answers may be longer than other card replies.
            text = result.get("ephemeral")
            limit = DISPATCH_EPHEMERAL_LIMIT if context.get("kind") == "dispatch" else 300
            if isinstance(text, str) and 0 < len(text) <= limit:
                return text
            if result.get("ok"):
                return None
            reason = str(result.get("reason") or "")
            if reason == "not_authorized":
                return self.not_authorized
            code = reason if SAFE_CODE_RE.fullmatch(reason) else "unknown"
            return self.msg("receiver.button_failed", code=code)
        if context.get("kind") in ("ask", "askconfirm"):
            # Ask cards speak in the owner's voice; the CLI owns their wording.
            text = result.get("ephemeral")
            if isinstance(text, str) and 0 < len(text) <= 300:
                return text
            return None if result.get("ok") else self.ask_fallback
        if result.get("ok"):
            if context.get("kind") == "confirm":
                return None
            if result.get("duplicate") and result.get("choice") != context["choice"]:
                actor = _safe(result.get("actor"))
                by = self.msg("receiver.decided_by", mention=f"<@{actor}>") if actor else ""
                label = _mrkdwn_escape(result.get("label") or result.get("choice"))
                return self.msg("receiver.already_decided", label=label, by=by)
            return None
        reason = str(result.get("reason") or "")
        if reason == "not_authorized":
            return self.not_authorized
        if reason == "not_proposal_author":
            return self.not_proposal_author
        if reason in STALE_REASONS:
            return self.stale
        code = reason if SAFE_CODE_RE.fullmatch(reason) else "unknown"
        return self.msg("receiver.choice_failed", code=code)

    def run_decide(self, context: dict[str, str], **kw: Any) -> dict[str, Any]:
        return run_decide(context, **kw)

    def post_ephemeral(self, response_url: str, text: str, *, opener: Callable[..., Any] | None = None) -> bool:
        return post_ephemeral(response_url, text, opener=opener, guard_env=self.guard_env)

    def handle_decision_action(
        self,
        payload: dict[str, Any],
        *,
        expected_team_id: str,
        runner: Callable[..., Any] | None = None,
        poster: Callable[..., Any] | None = None,
        python: str | None = None,
        script: Path,
        db: str | None = None,
        timeout: float = DECISION_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        context = decision_context(payload, expected_team_id=expected_team_id)
        result = self.run_decide(context, runner=runner, python=python, script=script, db=db, timeout=timeout)
        text = self.decision_ephemeral_text(context, result)
        ephemeral = "not_needed"
        if text:
            try:
                sent = bool(context["response_url"]) and (poster or self.post_ephemeral)(context["response_url"], text)
                ephemeral = "sent" if sent else "skipped"
            except Exception as exc:
                ephemeral = "failed:" + type(exc).__name__
        # Diagnostics carry identifiers and enumerated codes only: no response_url or text.
        return {
            "event": "decision_handled",
            "kind": context["kind"],
            "ok": bool(result.get("ok")),
            "reason": _safe(result.get("reason"), SAFE_CODE_RE),
            "value": _safe(context["value"]),
            "external_actor": bool(context.get("actor_team")),
            "choice": _safe(context["choice"]),
            "recorded_choice": _safe(result.get("choice")),
            "actor": _safe(context["actor"]),
            "channel_id": _safe(context["card_channel"]),
            "message_ts": _safe(context["card_ts"]),
            "duplicate": bool(result.get("duplicate")),
            "stage": _safe(result.get("stage"), SAFE_CODE_RE),
            "ephemeral": ephemeral,
        }
