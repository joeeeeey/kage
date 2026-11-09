"""Slack Socket Mode: the receiver's connection loop (Kage O0, moved unchanged from the deployment's receiver script).

N concurrent connections (one stays live while another reconnects; Slack delivers each interactive payload to one open
connection and never again), each acked at once, kept alive by ping / pong, reconnected with capped backoff and jitter;
envelopes are processed once on one dispatch thread: card buttons (`decide`), card-thread replies (`card-reply-push`),
the owner's top-level messages in a decision channel (`control-push`), anything else interactive goes to the
deployment's `handle_interactive`.

`SocketModeRunner` reads every name the loop uses through `self.ns`: the deployment passes the module that binds them
(its tokens, clients, Slack-side receiver functions, tunables), so what it binds, or a test patches there, is what the
loop uses. The `websockets` package (>= 11) is imported only inside `run()`: Kage imports without it.
"""
from __future__ import annotations

from collections import OrderedDict
import json
from pathlib import Path
import queue
import random
import re
import sys
import threading
import time
from typing import Any, Callable


BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
# Socket Mode delivers each interactive payload to one open connection and never re-delivers it: a click that
# arrives while the only socket is reconnecting is lost. Two concurrent connections (Slack allows several per app)
# keep one live while the other reconnects; payloads are acked on the socket that got them and processed once.
SOCKET_CONNECTIONS = 2
MAX_SOCKET_CONNECTIONS = 4
STAGGER_SECONDS = 2.0          # the second socket starts a little later, so the two never reconnect in lockstep
JITTER_FRACTION = 0.2          # up to +20 % on every backoff wait
FATAL_RETRY_SECONDS = 300.0    # a non-transient error while connecting (e.g. a revoked app token): slow retry, no crash
ENVELOPE_SEEN_MAX = 2048       # envelope ids remembered for processing a payload once across sockets
STOP_CHECK_SECONDS = 1.0       # socket reads and the dispatch queue wake up this often to notice a stop
JOIN_SECONDS = 2.0
# Slack `ok:false` codes that are service-side and retryable. Auth/config errors
# (invalid_auth, token_revoked, team mismatch...) still stop the receiver.
TRANSIENT_SLACK_ERRORS = {"ratelimited", "internal_error", "fatal_error", "service_unavailable",
                          "request_timeout", "team_added_to_org"}
_SLACK_HTTP_RE = re.compile(r"Slack API HTTP (\d{3}) for ")
_SLACK_ERROR_RE = re.compile(r"Slack API error for [a-z.]+: ([a-z_]+)")
_SSL_REASON_RE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


def next_backoff(previous: float) -> float:
    """Bounded exponential backoff: 1, 2, 4, ... capped at 30 seconds."""
    return min(max(previous, BACKOFF_INITIAL_SECONDS / 2) * 2, BACKOFF_MAX_SECONDS)


def jittered(delay: float) -> float:
    """A backoff wait plus up to 20 % jitter (two sockets never retry at the same instant)."""
    return delay + random.uniform(0.0, delay * JITTER_FRACTION)


def is_transient_transport_error(exc: BaseException) -> bool:
    """Network/TLS/socket/5xx/429 failures are retried in-loop; auth/config errors are not.

    Covers ssl.SSLError such as UNEXPECTED_EOF_WHILE_READING (an OSError), URLError,
    timeouts, http.client protocol errors, websockets handshake/close errors, and the
    RuntimeError wrapper that SlackClient raises after its own three attempts.
    """
    import http.client

    # EOFError: the websockets handshake ends with "connection closed while reading HTTP status line" when the TLS
    # stream drops mid-handshake (it crashed the receiver before P3.1).
    if isinstance(exc, (OSError, TimeoutError, EOFError, http.client.HTTPException)):
        return True
    try:
        from websockets.exceptions import WebSocketException
    except ImportError:  # pragma: no cover - websockets is required by run()
        WebSocketException = ()  # type: ignore[assignment]
    if WebSocketException and isinstance(exc, WebSocketException):
        return True
    if isinstance(exc, RuntimeError):
        text = str(exc)
        if text.startswith("Slack API transport failed") or text == "Slack did not return a Socket Mode URL":
            return True
        status = _SLACK_HTTP_RE.match(text)
        if status:
            return status[1] == "429" or status[1].startswith("5")
        code = _SLACK_ERROR_RE.match(text)
        return bool(code and code[1] in TRANSIENT_SLACK_ERRORS)
    return False


def transport_error_code(exc: BaseException) -> str:
    """Enumerated, secret-free code for diagnostics (never the message: it may hold URLs)."""
    import ssl
    import urllib.error

    inner: BaseException | None = exc
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        inner = exc.reason
    if isinstance(inner, ssl.SSLError):
        reason = str(getattr(inner, "reason", "") or "")
        return "ssl:" + (reason if _SSL_REASON_RE.fullmatch(reason) else type(inner).__name__)
    if isinstance(exc, RuntimeError):
        text = str(exc)
        status = _SLACK_HTTP_RE.match(text)
        if status:
            return "http:" + status[1]
        code = _SLACK_ERROR_RE.match(text)
        if code:
            return "slack:" + code[1]
        if text.startswith("Slack API transport failed"):
            for marker in ("UNEXPECTED_EOF_WHILE_READING", "CERTIFICATE_VERIFY_FAILED", "timed out", "Connection reset"):
                if marker in text:
                    return "transport:" + marker.replace(" ", "_").lower()
            return "transport:failed"
    return type(inner or exc).__name__


def backoff_diagnostic(event: str, exc: BaseException, attempt: int, delay: float) -> dict[str, Any]:
    return {
        "event": event,
        "error_type": type(exc).__name__,
        "error_code": transport_error_code(exc),
        "attempt": attempt,
        "delay_seconds": delay,
    }


def envelope_diagnostic(envelope: dict[str, Any]) -> dict[str, str]:
    payload = envelope.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "event": "envelope_received",
        "envelope_type": str(envelope.get("type") or ""),
        "payload_type": str(payload.get("type") or ""),
        "callback_id": str(payload.get("callback_id") or ""),
    }


def reconnect_diagnostic(exc: BaseException) -> dict[str, str]:
    return {
        "event": "receiver_reconnecting",
        "error_type": type(exc).__name__,
    }


class SocketLivenessError(OSError):
    pass


class SocketModeDisabled(RuntimeError):
    """Slack's disconnect with reason link_disabled: Socket Mode is off for the app (retried slowly, never fast)."""


def assert_websocket_liveness(websocket: Any, pong_timeout_seconds: float) -> None:
    pong_waiter = websocket.ping()
    if not pong_waiter.wait(timeout=pong_timeout_seconds):
        raise SocketLivenessError("Slack Socket Mode liveness ping timed out")


class SocketModeRunner:
    """The Socket Mode loop over the deployment's namespace `ns`. The deployment's subclass sets `args`, `app_client`
    (the app-level token's client), `ns`, and implements `preflight()` and `handle_interactive(payload)`."""

    ns: Any = None

    def handle_interactive(self, payload: dict[str, Any]) -> bool:
        """An interactive payload that is not a card button: the deployment's own (default: ignored). True = `--once`
        is satisfied."""
        return False

    def preflight(self) -> None:
        raise NotImplementedError("the deployment checks its tokens' identity")

    def _log(self, record: dict[str, Any], stderr: bool = False) -> None:
        """One JSON line; serialized across the socket threads (secret-free diagnostics only)."""
        lock = getattr(self, "_log_lock", None)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if lock is None:
            print(line, file=sys.stderr if stderr else sys.stdout, flush=True)
            return
        with lock:
            print(line, file=sys.stderr if stderr else sys.stdout, flush=True)

    def process_decision(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """A card or confirmation button -> the runtime's `decide` / `confirm-decision`; never raises."""
        try:
            result = self.ns.handle_decision_action(
                payload,
                expected_team_id=self.args.expected_team_id,
                script=Path(getattr(self.args, "followup_script", self.ns.FOLLOWUP_SCRIPT)),
                db=getattr(self.args, "followup_db", None),
                timeout=float(getattr(self.args, "decision_timeout_seconds", self.ns.DECISION_TIMEOUT_SECONDS)),
            )
            self._log(result)
            return result
        except Exception as exc:
            self._log(self.ns.decision_error_diagnostic(payload, exc), stderr=True)
            return None

    def _card_reply_clock(self) -> float:
        return time.monotonic()

    def _followup_target(self) -> tuple[Path, str | None, float]:
        return (Path(getattr(self.args, "followup_script", self.ns.FOLLOWUP_SCRIPT)), getattr(self.args, "followup_db", None),
                float(getattr(self.args, "decision_timeout_seconds", self.ns.DECISION_TIMEOUT_SECONDS)))

    def _followup_json(self, command: list[str], prefix: str) -> dict[str, Any]:
        script, _, timeout = self._followup_target()
        # `followup_runner` (tests): a subprocess.run stand-in; production runs the real CLI.
        return self.ns.run_followup_json(command, runner=getattr(self, "followup_runner", None), script=script,
                                 timeout=timeout, prefix=prefix)

    def card_reply_settings(self) -> dict[str, Any]:
        """The runtime's card-reply gate (`card-reply-config`): fetched on the first message event, then cached for its
        `ttl_seconds`; a failed call (non-zero exit, timeout, bad JSON) counts as disabled for 60 s."""
        now = self._card_reply_clock()
        cached = getattr(self, "_card_reply_cache", None)
        if cached is not None and now < cached[0]:
            return cached[1]
        script, db, _ = self._followup_target()
        try:
            result = self._followup_json(self.ns.card_reply_config_command(python=sys.executable, script=script, db=db),
                                         "card_reply_config")
        except Exception:  # noqa: BLE001 - a broken runner is a config failure like any other
            result = {"ok": False, "reason": "card_reply_config_crashed"}
        config = self.ns.parse_card_reply_config(result)
        if config is None:
            config = {"enabled": False, "channels": frozenset(), "users": frozenset(),
                      "ttl_seconds": self.ns.CARD_REPLY_ERROR_TTL_SECONDS, "error": True}
            self._log({"event": "card_reply_config_error",
                       "reason": self.ns._safe(result.get("reason"), self.ns.SAFE_CODE_RE) or "invalid_config",
                       "retry_seconds": self.ns.CARD_REPLY_ERROR_TTL_SECONDS}, stderr=True)
        else:
            self._log({"event": "card_reply_config", "enabled": config["enabled"], "channels": len(config["channels"]),
                       "users": len(config["users"]), "ttl_seconds": config["ttl_seconds"]})
        self._card_reply_cache = (now + config["ttl_seconds"], config)
        return config

    def process_card_reply(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """The owner's card-thread reply -> the runtime's `card-reply-push`, refs only; never raises. Cheap filters first
        (team, no subtype / bot / app, a thread reply with well-formed ids), then the runtime gate (enabled, channel
        and user allowed); anything else is dropped with a code and no per-message process. Logs carry ids and
        codes only, never the message text."""
        try:
            refs, code = self.ns.card_reply_refs(payload, expected_team_id=self.args.expected_team_id)
            if refs is None:
                pushed = self.process_control_message(payload) if code == "not_thread_reply" else None
                if pushed is not None:
                    return pushed
                record = self.ns.card_reply_drop_diagnostic(payload, code)
                self._log(record)
                return record
            config = self.card_reply_settings()
            reason = ("config_error" if config["error"] else "disabled") if not config["enabled"] else \
                "channel_not_allowed" if refs["channel"] not in config["channels"] else \
                "user_not_allowed" if refs["user"] not in config["users"] else ""
            if reason:
                record = self.ns.card_reply_drop_diagnostic(payload, reason)
                self._log(record)
                return record
            script, db, _ = self._followup_target()
            result = self._followup_json(self.ns.card_reply_push_command(refs, python=sys.executable, script=script, db=db),
                                         "card_reply_push")
            record = {
                "event": "card_reply_pushed",
                "channel_id": self.ns._safe(refs["channel"]),
                "message_ts": self.ns._safe(refs["ts"]),
                "thread_ts": self.ns._safe(refs["thread_ts"]),
                "ok": bool(result.get("ok")),
                "outcome": self.ns._safe(result.get("outcome"), self.ns.SAFE_CODE_RE),
                "reason": self.ns._safe(result.get("reason"), self.ns.SAFE_CODE_RE),
            }
            self._log(record)
            return record
        except Exception as exc:  # noqa: BLE001 - one bad event never stops the dispatch thread
            event = payload.get("event") if isinstance(payload, dict) else None
            event = event if isinstance(event, dict) else {}
            self._log({"event": "card_reply_error", "error_type": type(exc).__name__,
                       "channel_id": self.ns._safe(event.get("channel")), "message_ts": self.ns._safe(event.get("ts"))}, stderr=True)
            return None

    def process_intake(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """0.77.0 bot intake: an @mention of the bot or a DM to it -> the runtime's `intake-push` (refs only) when the
        runtime takes them (`card-reply-config` "intake_push", on only for a bot-identity intake). None otherwise (not
        such an event, or the gate is off: the event goes on to the card-reply path as before). A deployment whose
        receiver namespace has no intake functions never reaches it. Logs carry ids and codes only, never the text."""
        is_intake = getattr(self.ns, "is_intake_event", None)
        if is_intake is None or not is_intake(payload):
            return None
        try:
            if not self.card_reply_settings().get("intake_push"):
                return None
            refs, code = self.ns.intake_refs(payload, expected_team_id=self.args.expected_team_id)
            if refs is None:
                event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
                record = {"event": "intake_dropped", "reason": code, "channel_id": self.ns._safe(event.get("channel")),
                          "message_ts": self.ns._safe(event.get("ts"))}
                self._log(record)
                return record
            script, db, _ = self._followup_target()
            result = self._followup_json(self.ns.intake_push_command(refs, python=sys.executable, script=script, db=db),
                                         "intake_push")
            record = {
                "event": "intake_pushed",
                "channel_id": self.ns._safe(refs["channel"]),
                "message_ts": self.ns._safe(refs["ts"]),
                "ok": bool(result.get("ok")),
                "outcome": self.ns._safe(result.get("outcome"), self.ns.SAFE_CODE_RE),
                "reason": self.ns._safe(result.get("reason"), self.ns.SAFE_CODE_RE),
            }
            self._log(record)
            return record
        except Exception as exc:  # noqa: BLE001 - one bad event never stops the dispatch thread
            self._log({"event": "intake_push_error", "error_type": type(exc).__name__}, stderr=True)
            return {"event": "intake_push_error"}

    def process_control_message(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """A new top-level message in a decision channel -> the runtime's `control-push` (refs only; the runtime
        reads it back and keeps it only if it is the owner's own hand-typed message) when the cached gate says the
        control entry takes them (`card-reply-config` "control"). None otherwise: the caller logs the drop record,
        with no call at all. Logs carry ids and codes only, never the text."""
        refs, _ = self.ns.control_refs(payload, expected_team_id=self.args.expected_team_id)
        if refs is None:
            return None
        # Only an already-cached gate (a card reply fetched it within its TTL): a top-level message never costs a
        # runtime call of its own, so with the entry off nothing changes here. When the cache is cold, the runtime's
        # fallback poll (control.channel_poll_seconds) picks the message up instead.
        cached = getattr(self, "_card_reply_cache", None)
        config = cached[1] if cached and cached[0] > self._card_reply_clock() else None
        if not config or not config.get("control") or refs["channel"] not in config["channels"] or \
                refs["user"] not in config["users"]:
            return None
        script, db, _ = self._followup_target()
        result = self._followup_json(self.ns.control_push_command(refs, python=sys.executable, script=script, db=db),
                                     "control_push")
        record = {
            "event": "control_pushed",
            "channel_id": self.ns._safe(refs["channel"]),
            "message_ts": self.ns._safe(refs["ts"]),
            "ok": bool(result.get("ok")),
            "outcome": self.ns._safe(result.get("outcome"), self.ns.SAFE_CODE_RE),
            "reason": self.ns._safe(result.get("reason"), self.ns.SAFE_CODE_RE),
        }
        self._log(record)
        return record

    def _idle_expired(self, started: float) -> bool:
        return self.args.idle_timeout_seconds > 0 and \
            self.args.idle_timeout_seconds - (time.monotonic() - started) <= 0

    def preflight_with_retry(self, started: float) -> bool:
        """Identity preflight that survives transient TLS/network errors; False = idle timeout."""
        delay, attempt = self.ns.BACKOFF_INITIAL_SECONDS, 0
        while True:
            try:
                self.preflight()
                return True
            except Exception as exc:
                if not self.ns.is_transient_transport_error(exc):
                    raise  # wrong workspace/user, revoked token...: fail closed
                attempt += 1
                print(json.dumps(self.ns.backoff_diagnostic("receiver_preflight_retry", exc, attempt, delay),
                                 ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
                if self._idle_expired(started):
                    return False
                time.sleep(delay)
                delay = self.ns.next_backoff(delay)

    def _pause(self, seconds: float) -> bool:
        """Backoff wait that ends early on stop; True = stop requested."""
        return self._stop.wait(seconds)

    def _socket_session(self, slot: int, connect: Callable[..., Any], liveness_interval: float, pong_timeout: float,
                        ready: Callable[[], None]) -> str:
        """One Socket Mode connection from a fresh `apps.connections.open` until Slack's disconnect ('disconnect') or
        a stop ('stop'). Every envelope is acked at once on this socket; interactive payloads and Events API
        envelopes go to the dispatch queue, so slow runtime calls never delay a read or an ack. Errors propagate to
        the connection loop."""
        opened, _ = self.app_client.call("apps.connections.open", json_body={})
        socket_url = str(opened.get("url") or "")
        if not socket_url:
            raise RuntimeError("Slack did not return a Socket Mode URL")
        with connect(socket_url, open_timeout=30, close_timeout=5) as websocket:
            ready()
            self._log({"event": "receiver_ready", "slot": slot, "liveness_interval_seconds": liveness_interval,
                       "pong_timeout_seconds": pong_timeout})
            last_socket_traffic = time.monotonic()
            while not self._stop.is_set():
                remaining = liveness_interval - (time.monotonic() - last_socket_traffic)
                if remaining <= 0:
                    self.ns.assert_websocket_liveness(websocket, pong_timeout)
                    last_socket_traffic = time.monotonic()
                    continue
                try:
                    raw = websocket.recv(timeout=min(remaining, self.ns.STOP_CHECK_SECONDS))
                except TimeoutError:
                    continue
                last_socket_traffic = time.monotonic()
                envelope = json.loads(raw)
                envelope_id = str(envelope.get("envelope_id") or "")
                if envelope_id:
                    websocket.send(json.dumps({"envelope_id": envelope_id}))
                kind = envelope.get("type")
                if kind == "hello":
                    self._log({"event": "receiver_hello", "slot": slot,
                               "num_connections": int(envelope.get("num_connections") or 0)})
                    continue
                self._log({**self.ns.envelope_diagnostic(envelope), "slot": slot})
                if kind == "disconnect":
                    if str(envelope.get("reason") or "") == "link_disabled":
                        raise self.ns.SocketModeDisabled("Socket Mode is disabled for this app")
                    return "disconnect"
                if kind in ("interactive", "events_api"):
                    self._inbox.put(envelope)
        return "stop"

    def _connection_loop(self, slot: int, connect: Callable[..., Any], liveness_interval: float,
                         pong_timeout: float) -> None:
        """Keep one Socket Mode connection up until stop. Nothing escapes: a dropped socket, an SSL error on
        `apps.connections.open` or in the handshake reconnects with capped exponential backoff (1 s ... 30 s, + jitter),
        forever; a non-transient error while connecting retries every 5 minutes instead of crash-looping."""
        delay, attempt = self.ns.BACKOFF_INITIAL_SECONDS, 0
        if slot and self._pause(self.ns.STAGGER_SECONDS * slot):
            return
        while not self._stop.is_set():
            connected = []

            def ready() -> None:
                nonlocal delay, attempt
                delay, attempt = self.ns.BACKOFF_INITIAL_SECONDS, 0
                connected.append(True)

            try:
                if self._socket_session(slot, connect, liveness_interval, pong_timeout, ready) == "disconnect":
                    continue  # Slack asked for a reconnect (refresh / warning): a fresh URL right away
                return
            except Exception as exc:  # noqa: BLE001 - one socket's failure never stops the receiver
                attempt += 1
                transient = (bool(connected) and not isinstance(exc, self.ns.SocketModeDisabled)) or \
                    self.ns.is_transient_transport_error(exc)
                wait = self.ns.jittered(delay if transient else max(delay, self.ns.FATAL_RETRY_SECONDS))
                self._log({**self.ns.reconnect_diagnostic(exc),
                           **self.ns.backoff_diagnostic("receiver_reconnecting", exc, attempt, round(wait, 3)),
                           "slot": slot, "transient": transient}, stderr=True)
                if self._pause(wait):
                    return
                delay = self.ns.next_backoff(delay)

    def _handle_envelope(self, envelope: dict[str, Any]) -> bool:
        """Dispatch thread (the main thread): process one interactive payload or Events API message event once,
        even if two sockets delivered it. True = `--once` is satisfied."""
        envelope_id = str(envelope.get("envelope_id") or "")
        if envelope_id:
            if envelope_id in self._seen:
                self._log({"event": "envelope_duplicate", "envelope_type": str(envelope.get("type") or "")})
                return False
            self._seen[envelope_id] = True
            while len(self._seen) > self.ns.ENVELOPE_SEEN_MAX:
                self._seen.popitem(last=False)
        payload = envelope.get("payload") or {}
        if envelope.get("type") == "events_api":
            if self.process_intake(payload) is not None:  # 0.77.0: a message to the bot, when the runtime takes them
                return False
            if self.ns.is_card_reply_event(payload):
                self.process_card_reply(payload)
            return False
        if self.ns.is_decision_action(payload):
            self.process_decision(payload)
            return False
        return self.handle_interactive(payload)

    def run(self) -> int:
        from websockets.sync.client import connect

        started = time.monotonic()
        if not self.preflight_with_retry(started):
            return 0
        liveness_interval = max(1.0, float(self.args.liveness_interval_seconds))
        pong_timeout = max(1.0, float(self.args.pong_timeout_seconds))
        # The parser defaults to two sockets; a programmatic caller without the option keeps one.
        count = max(1, min(self.ns.MAX_SOCKET_CONNECTIONS, int(getattr(self.args, "socket_connections", 1) or 1)))
        self._stop, self._inbox, self._seen = threading.Event(), queue.Queue(), OrderedDict()
        self._log_lock = threading.Lock()
        threads: dict[int, threading.Thread] = {}

        def start(slot: int) -> None:
            thread = threading.Thread(target=self._connection_loop, args=(slot, connect, liveness_interval, pong_timeout),
                                      name=f"socket-mode-{slot}", daemon=True)
            threads[slot] = thread
            thread.start()

        for slot in range(count):
            start(slot)
        try:
            while not self._stop.is_set() and not self._idle_expired(started):
                try:
                    envelope = self._inbox.get(timeout=self.ns.STOP_CHECK_SECONDS)
                except queue.Empty:
                    for slot, thread in list(threads.items()):
                        if not thread.is_alive() and not self._stop.is_set():  # never expected: the loop catches all
                            self._log({"event": "receiver_socket_restarted", "slot": slot}, stderr=True)
                            start(slot)
                    continue
                if self._handle_envelope(envelope):
                    return 0
            return 0
        finally:
            self._stop.set()
            for thread in threads.values():
                thread.join(timeout=self.ns.JOIN_SECONDS)
