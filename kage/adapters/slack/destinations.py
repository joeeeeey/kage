"""Slack-verified private destination for every owner-facing post (the guard right before chat.postMessage).

The membership check of kage.core.authority cannot catch a misconfiguration (callers read the channel from the same
config), so Slack itself (conversations.info with the assistant's token) must report the destination private or an IM
with the owner; public, a group DM, a channel shared with another organization, an IM with someone else, unknown, or a
failed check refuses the post (fail closed). Verdicts are cached per channel in the runtime's `runtime_meta`, shared by
every process. A transient failure of a re-check keeps a private verdict for a while instead of refusing everything.
A local failure (no token, real Slack disabled, the assistant's identity not confirmed) says nothing about the channel:
that check is refused but never cached, so the verdict other processes share stays as it was (B1).

It reaches Slack and the database only through the runtime handle `rt` (slack.api_call, api_identity, safe_error,
_meta_note) and the caller's `db`. The runtime passes its own facts: the decisions-config loader, the tables that store
destinations of owner-facing posts (for the install-time report) and the report's advice text. As with the transport,
`require` calls back through the adapter's module-level names (`hooks`: check, record_refusal, private) so a caller or
test that replaces one of them is what `require` uses.
"""
from __future__ import annotations
import http.client
import json
import re
import ssl
import time
import urllib.error

DESTINATION_META = 'card_destination:'            # runtime_meta key prefix: {verdict, error, checked_at} per channel
DESTINATION_BLOCKED = 'card_destination_blocked'  # runtime_meta: latest refused post "<time> <kind> <channel> <why>"
DESTINATION_REFUSALS = 'card_destination_refusals'  # runtime_meta: {"total": N, "recent": [epoch, …]} for health
DESTINATION_LOCAL_ERROR = 'card_destination_local_error'  # runtime_meta: latest local failure "<time> <channel> <code>"
DESTINATION_TTL = 3600       # a private / public verdict is trusted this long (re-check: one Slack call per channel)
DESTINATION_ERROR_TTL = 300  # a failed check stays refused this long, then retried; also the transient re-check delay
DESTINATION_STALE_OK = 86400  # a private verdict this old survives a transient re-check failure
DESTINATION_LEGACY_DAYS = 30  # report: stored rows older than this are listed as legacy, not counted
PRIVATE_VERDICTS = ('im', 'private')
TRANSIENT_SLACK_ERRORS = ('ratelimited', 'rate_limited', 'internal_error', 'fatal_error', 'service_unavailable',
                          'request_timeout')
REFUSALS_KEPT = 500  # refusal times kept for the rolling 24 h count


class DestinationRefused(ValueError):
    """An owner-facing post refused by the destination guard; callers record the class name as the error code."""


def transient_failure(exc):
    """True when a failed conversations.info says nothing about the channel (timeout, network, HTTP 429 / 5xx, a
    non-JSON body, Slack `ratelimited` / `internal_error` …). Anything else is a definite answer: cached and refused."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException,
                        json.JSONDecodeError, ssl.SSLError)):
        return True  # (an SSLError while reading the response comes unwrapped)
    match = re.fullmatch(r'Slack API error: ([a-z_]+)', str(exc))
    return bool(match) and match[1] in TRANSIENT_SLACK_ERRORS


def local_failure(exc):
    """True when a check failed on this side before Slack said anything about the channel: real Slack disabled here
    (no request was made). A failure while confirming the assistant's identity counts too (see Destinations.check)."""
    return str(exc).startswith('real_slack_disabled')


class Destinations:
    def __init__(self, authority, *, owner_user, owner_key, load_config, stored_destinations, report_next,
                 refresh_command, hooks=None):
        self.authority, self.owner_user, self.load_config = authority, owner_user, load_config
        self.refresh_command = refresh_command
        hooks = hooks or {}
        self.hook_check = hooks.get('check', self.check)
        self.hook_record = hooks.get('record_refusal', self.record_refusal)
        self.hook_private = hooks.get('private', authority.private_card_destination)
        self.im_not_owner = f'im_not_{owner_key}'
        self.refused_verdicts = ('public', 'mpim', 'ext_shared', self.im_not_owner)
        self.stored_destinations, self.report_next = tuple(stored_destinations), report_next
        self.refusal_prefix = f'{owner_key}_card_destination_not_private'

    def verdict(self, info):
        """A conversations.info `channel` object -> 'im' | 'private' | a refused verdict. `is_mpim` (group DM);
        `is_ext_shared` / `is_pending_ext_shared`, or `is_shared` without `is_org_shared` (shared with another
        organization); an IM's `user` is the other member, which must be the owner."""
        if info.get('is_mpim'):
            return 'mpim'
        if info.get('is_ext_shared') or info.get('is_pending_ext_shared') or (
                info.get('is_shared') and not info.get('is_org_shared')):
            return 'ext_shared'
        if info.get('is_im'):
            return 'im' if info.get('user') in (None, self.owner_user) else self.im_not_owner
        return 'private' if info.get('is_private') else 'public'

    def entry(self, db, channel):
        """The cached entry for `channel` at any age, or None (missing or unreadable)."""
        try:
            row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (DESTINATION_META + channel,)).fetchone()
            hit = json.loads(row[0]) if row else None
            float(hit['checked_at'])
            return hit if hit['verdict'] in (*PRIVATE_VERDICTS, *self.refused_verdicts, 'error') else None
        except Exception:  # noqa: BLE001 - an unreadable cache entry just means asking Slack again
            return None

    def cached(self, db, channel, now):
        """A still-fresh cached verdict, or None (missing, expired, from the future, or unreadable)."""
        hit = self.entry(db, channel)
        if hit is None:
            return None
        age = now - float(hit['checked_at'])
        if age < 0:
            return None
        if hit['verdict'] == 'error':
            return hit if age < DESTINATION_ERROR_TTL else None
        if age < DESTINATION_TTL:
            return hit
        # A private verdict kept through a transient re-check failure is served until that failure's retry time.
        stale_ok = hit['verdict'] in PRIVATE_VERDICTS and age < DESTINATION_STALE_OK
        return hit if stale_ok and now < float(hit.get('retry_at') or 0) else None

    def check(self, rt, db, channel, now=None, refresh=False):
        """Slack's privacy verdict for `channel`, cached: {'channel', 'verdict', 'error', 'checked_at', 'cached',
        'transient'}. Never raises. The owner's id (the unconfigured DM fallback) addresses the IM with the owner by
        construction: no Slack call."""
        now = time.time() if now is None else now
        channel = str(channel)
        if channel == self.authority.fallback:
            return {'channel': channel, 'verdict': 'im', 'error': None, 'checked_at': now, 'cached': False,
                    'transient': None}
        hit = None if refresh else self.cached(db, channel, now)
        if hit:
            return {'channel': channel, 'verdict': hit['verdict'], 'error': hit.get('error'),
                    'checked_at': hit['checked_at'], 'cached': True, 'transient': hit.get('transient')}
        entry, identity = None, False
        try:
            token = rt.api_identity(bot=True)
            identity = True
            info = (rt.slack.api_call('conversations.info', token, params={'channel': channel}) or {}).get('channel')
            verdict, error = (self.verdict(info), None) if isinstance(info, dict) else ('error', 'no_channel_info')
        except Exception as exc:  # noqa: BLE001 - any failure is a refusal, never a pass ...
            verdict, error = 'error', rt.safe_error(exc)
            if not transient_failure(exc) and (not identity or local_failure(exc)):
                # ... and a local one (B1: missing token, real Slack disabled, identity not confirmed) is not an answer
                # about the channel: refused here, recorded, and the cached verdict is left as it was.
                error = 'real_slack_disabled' if local_failure(exc) else error
                rt._meta_note(db, DESTINATION_LOCAL_ERROR, f'{channel} {error}')
                prev = self.entry(db, channel)
                return {'channel': channel, 'verdict': 'error', 'error': error, 'checked_at': now, 'cached': False,
                        'transient': None, 'local': True, 'kept': prev['verdict'] if prev else None}
            prev = self.entry(db, channel) if transient_failure(exc) else None
            if prev and prev['verdict'] in PRIVATE_VERDICTS and 0 <= now - float(prev['checked_at']) < DESTINATION_STALE_OK:
                # ... except a transient one while Slack said private / IM recently: keep that verdict (and its time),
                # re-ask after DESTINATION_ERROR_TTL.
                entry = {'verdict': prev['verdict'], 'error': None, 'checked_at': prev['checked_at'],
                         'retry_at': now + DESTINATION_ERROR_TTL, 'transient': error}
        entry = entry or {'verdict': verdict, 'error': error, 'checked_at': now}
        if not db.in_transaction:  # never touch a caller's open transaction
            try:
                db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', (DESTINATION_META + channel,
                                                                                json.dumps(entry)))
                db.commit()
            except Exception:  # noqa: BLE001 - an uncached verdict only costs another Slack call
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
        return {'channel': channel, 'verdict': entry['verdict'], 'error': entry['error'],
                'checked_at': entry['checked_at'], 'cached': False, 'transient': entry.get('transient')}

    def record_refusal(self, rt, db, kind, channel, reason):
        """A refused owner-facing post: the latest one and a rolling count for health. Codes and channel ids only;
        never inside a caller's transaction and never raises."""
        rt._meta_note(db, DESTINATION_BLOCKED, f'{kind} {channel} {reason}')
        if db.in_transaction:
            return
        now = time.time()
        try:
            row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (DESTINATION_REFUSALS,)).fetchone()
            stats = json.loads(row[0]) if row else {}
            stats = stats if isinstance(stats, dict) else {}
            recent = [t for t in stats.get('recent') or [] if isinstance(t, (int, float)) and 0 <= now - t < 86400]
            db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', (DESTINATION_REFUSALS, json.dumps(
                {'total': int(stats.get('total') or 0) + 1, 'recent': recent[-(REFUSALS_KEPT - 1):] + [int(now)]})))
            db.commit()
        except Exception:  # noqa: BLE001 - the count is diagnostics only
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass

    def require(self, rt, db, channel, kind, config=None, configured=True):
        """The guard every owner-facing post passes right before chat.postMessage: `channel` must be a configured
        destination (`configured=False` for a channel stored from an earlier guarded post) AND Slack must report it
        private / an IM with the owner. Otherwise the refusal is recorded and DestinationRefused raised. Returns the
        verdict, which a caller that checks before its first write passes on to its post."""
        config = self.load_config() if config is None else config  # a malformed config raises: fail closed
        if configured and not self.hook_private(config, channel):
            reason = 'not_a_configured_destination'
        else:
            check = self.hook_check(rt, db, channel)
            if check['verdict'] in PRIVATE_VERDICTS:
                return check['verdict']
            reason = check['error'] or check['verdict']
        self.hook_record(rt, db, kind, channel, reason)
        raise DestinationRefused(f'{self.refusal_prefix}:{channel}:{reason}')

    def report(self, rt, db, refresh=False, now=None):
        """Install-time check: the configured destinations plus every distinct stored channel of an owner-facing post,
        each with its Slack verdict through the same cached check. Posts nothing; writes only the cache. Only the
        configured channels and channels of rows from the last DESTINATION_LEGACY_DAYS decide `ok`."""
        now = time.time() if now is None else now
        since = now - DESTINATION_LEGACY_DAYS * 86400
        sources, recent = {}, set()
        config_error = None
        try:
            config = self.load_config()
            for label, ch in (('config.decision_channel', config['decision_channel']),
                              ('config.ooo.channel', config['ooo']['channel'])):
                if ch:
                    sources.setdefault(ch, {})[label] = 1
                    recent.add(ch)
        except ValueError as exc:
            config_error = str(exc)[:200]
        for table, column, at, where in self.stored_destinations:
            if not {column, at} <= {r[1] for r in db.execute(f'PRAGMA table_info({table})')}:
                continue  # an older / partial DB: that table or column does not exist yet
            for ch, n, fresh in db.execute(  # a row without a time counts as recent (conservative)
                    f'SELECT {column}, count(*), sum({at} IS NULL OR {at}>=?) FROM {table} WHERE {column} IS NOT NULL '
                    f"AND {column}!='' {'AND ' + where if where else ''} GROUP BY {column}", (since,)):
                sources.setdefault(ch, {})[f'{table}.{column}'] = n
                if fresh:
                    recent.add(ch)
        checked = [{**self.check(rt, db, ch, now=now, refresh=refresh), 'sources': src}
                   for ch, src in sorted(sources.items())]
        channels = [c for c in checked if c['channel'] in recent]
        legacy = [c for c in checked if c['channel'] not in recent]
        refused = [c['channel'] for c in channels if c['verdict'] not in PRIVATE_VERDICTS]
        blocked = db.execute('SELECT value FROM runtime_meta WHERE key=?', (DESTINATION_BLOCKED,)).fetchone()
        return {'ok': not refused and not config_error, 'checked': len(checked), 'refused': refused,
                'config_error': config_error, 'channels': channels, 'legacy': legacy,
                'legacy_refused': [c['channel'] for c in legacy if c['verdict'] not in PRIVATE_VERDICTS],
                'legacy_days': DESTINATION_LEGACY_DAYS, 'last_blocked': blocked[0] if blocked else None,
                'ttl_seconds': DESTINATION_TTL,
                # B1: a check that could not ask Slack here (cached verdicts untouched); fix this side, not the channel
                'local_error': next((c['error'] for c in checked if c.get('local')), None),
                'next': None if not refused and not config_error else self.report_next}

    def overview(self, db, now=None):
        """Health read model: refused owner-facing posts (24 h / total, the latest one), the configured destinations'
        cached verdicts, and refused breaker / failure cards awaiting a re-post. Codes and channel ids only. No Slack."""
        now = time.time() if now is None else now

        def meta(key):
            row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (key,)).fetchone()
            return row[0] if row else None
        try:
            stats = json.loads(meta(DESTINATION_REFUSALS) or '{}')
        except ValueError:
            stats = {}
        stats = stats if isinstance(stats, dict) else {}
        recent = [t for t in stats.get('recent') or [] if isinstance(t, (int, float)) and 0 <= now - t < 86400]
        last, raw = None, meta(DESTINATION_BLOCKED)
        if raw:
            parts = raw.split(' ', 3) + [None] * 3
            try:
                at = float(parts[0])
            except ValueError:
                at = None
            last = {'at': at, 'kind': parts[1], 'channel': parts[2], 'reason': parts[3]}
        destinations, config_error = [], None
        try:
            config = self.load_config()
            for label, ch in (('decision_channel', config['decision_channel']), ('ooo.channel', config['ooo']['channel'])):
                if not ch:
                    continue
                hit = ({'verdict': 'im', 'checked_at': None} if ch == self.authority.fallback
                       else self.entry(db, ch) or {})
                destinations.append({'channel': ch, 'source': label, 'verdict': hit.get('verdict'),
                                     'error': hit.get('error'), 'checked_at': hit.get('checked_at'),
                                     'transient': hit.get('transient')})
        except ValueError as exc:
            config_error = str(exc)[:200]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        refused_stage = 'card_failed:' + DestinationRefused.__name__
        awaiting = {}
        if 'agent_breakers' in tables:
            awaiting['breaker_cards'] = db.execute("SELECT count(*) FROM agent_breakers WHERE state='paused' AND card_ts "
                                                   'IS NULL AND card_stage=?', (refused_stage,)).fetchone()[0]
        if 'dispatches' in tables:
            awaiting['failure_cards'] = db.execute("SELECT count(*) FROM dispatches WHERE state='failed' AND "
                                                   'fail_card_ts IS NULL AND card_stage=?',
                                                   ('failure_' + refused_stage,)).fetchone()[0]
        refusing = config_error or any(d['verdict'] and d['verdict'] not in PRIVATE_VERDICTS for d in destinations)
        return {'status': 'refusing' if refusing else 'refused_recently' if recent or any(awaiting.values()) else 'ok',
                'refused_24h': len(recent), 'refused_total': int(stats.get('total') or 0), 'last': last,
                'destinations': destinations, 'config_error': config_error, 'awaiting_repost': awaiting,
                'command': self.refresh_command}
