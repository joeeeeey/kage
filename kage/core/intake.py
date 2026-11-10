"""Intake: the pure helpers, the reader-language gate and the owner's notes guard (K11a); the engine inside the
supervisor (K11b); and the inbox / oncall commands the agents use (K11c): triage records, context and
reconcile, the send-once ledger and replies as the owner, launch / forward / hand-off, the owner's brief /
draft / incident cards, request-binding migration, status, probe and smoke.
Creating a task workspace is the deployment's (a hook). `rt` is the loaded runtime module.
"""
from __future__ import annotations
import datetime as dt
import errno
import fnmatch
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import types
import unicodedata
import urllib.error
import urllib.parse
import uuid

from kage import locale
from kage.adapters import trackers as _trackers
from kage.core import control as _control

ACK_EMOJI = 'eyes'  # 0.78.2: the bot's immediate receipt on an accepted message (kage.core.loop SEEN_EMOJI)
_OWNER_DEFAULT = object()  # a parameter whose default is the owner's value, filled in at call time


def _owner_kwargs(fn):
    """A method whose keyword names carry the owner's key (`needs_<key>`) takes them under the generic name
    (`needs_owner`)."""
    @functools.wraps(fn)
    def call(self, *args, **kw):
        pattern = re.compile(rf'(?<![a-z0-9]){re.escape(self.key)}(?![a-z0-9])')
        return fn(self, *args, **{pattern.sub('owner', k): v for k, v in kw.items()})
    return call


SLACKBOT = 'USLACKBOT'


TS_RE = re.compile(r'\d+\.\d+')


USER_RE = re.compile(r'[UW][A-Z0-9]{2,}')


CHANNEL_ID_RE = re.compile(r'[CDG][A-Z0-9]{2,}')


BURST_ID_RE = re.compile(r'in[0-9a-f]{14}')


EVENT_ID_RE = re.compile(r'[0-9a-f]{24}')


RECORD_ID_RE = re.compile(r'tr[0-9a-f]{14}')


ALLOWED_SUBTYPES = (None, 'file_share', 'thread_broadcast', 'bot_message', 'me_message')


STATE_KEY = 'intake_state'


BOOTSTRAP_KEY = 'intake_bootstrap'


SEARCH_COUNT = 50


SEARCH_OVERLAP_SECONDS = 900


HISTORY_LIMIT = 100


CONV_MAX_PAGES = 2


FETCH_QUEUE_PER_CYCLE = 12


FETCH_MAX_ATTEMPTS = 3


DM_REWIND_SECONDS = 3600


DM_BINDING_SECONDS = 2 * 86400  # pre-P39 sliding whole-DM bindings (kept for the migration / legacy rows only)


RELATED_TASKS_MAX = 8           # P39: tasks listed in the hint of a new top-level DM routed to <owner key>-inbox


OPEN_ASK_DAYS, OPEN_ASKS_MAX, OPEN_ASK_READS = 14, 6, 4  # P46.3: agent posts @-mentioning a person count as asks 14 days


INBOX_DM_REASONS = ('dm_new_topic', 'dm_conflict', 'dm_unresolved_ref')


STALL_SECONDS = 300


ROUTE_BATCH = 20


WAITING_RETRY_SECONDS = 30


BINDING_SWEEP_SECONDS = 60


REFS_CAP = 50


PR_INDEX_TTL = 600


ERROR_BACKOFF_MAX = 900


SEEN_RETENTION_SECONDS = 30 * 86400


AUTH_ERRORS = ('invalid_auth', 'not_authed', 'token_revoked', 'token_expired', 'account_inactive', 'missing_scope',
               'not_allowed_token_type', 'unexpected_slack_identity', 'expected_human_user_token')


CHANNEL_ERRORS = ('channel_not_found', 'not_in_channel', 'is_archived', 'thread_not_found', 'message_not_found')


INTENTS = ('request', 'question', 'directive', 'follow_up', 'fyi', 'chit_chat', 'alert', 'other')


CLARITIES = ('clear', 'partial', 'vague')


REQUEST_STYLES = ('standard', 'strategic_directive')


HANDLING_FIELDS = ('request_style', 'auto_execute')


NO_AUTO_REFUSED = ('launch',)                                 # auto_execute=false: no launch without the owner


DEFAULT_ACK = '收到，在弄～'


SEND_KINDS = ('intake_reply', 'intake_ack')  # P38 (F29): what intake posts AS THE OWNER to a burst's requester


AGAIN_ERROR = 'already_replied_to_this_burst_use_--again_to_send_another'


SMOKE_USER = 'USMOKEINTAKE'


SMOKE_DM = 'D0SMOKEINTAKE'


SMOKE_ONCALL = 'C0SMOKEONCALL'


SMOKE_TASK_PREFIX = 'smoke-dispatch-intake-'


def parse_permalink_thread(permalink):
    query = urllib.parse.urlsplit(str(permalink or '')).query
    value = (urllib.parse.parse_qs(query).get('thread_ts') or [None])[0]
    return value if value and TS_RE.fullmatch(value) else None


def match_conv_type(match):
    channel = match.get('channel') if isinstance(match.get('channel'), dict) else {}
    cid = str(channel.get('id') or '')
    if cid.startswith('D') or channel.get('is_im') or match.get('type') == 'im':
        return 'im'
    if channel.get('is_mpim') or match.get('type') == 'mpim' or str(channel.get('name') or '').startswith('mpdm-'):
        return 'mpim'
    return 'channel'


def _strip_markup(text):
    text = re.sub(r'<(?:https?|mailto):[^|>]*\|([^>]*)>', r'\1', str(text or ''))
    text = re.sub(r'<(https?:[^>]*)>', r'\1', text)
    text = re.sub(r'<[@!#][^>]*>', ' ', text)
    return text


def first_line(text, limit=120):
    line = next((x.strip() for x in _strip_markup(text).splitlines() if x.strip()), '')
    return (line[:limit] + '…') if len(line) > limit else line or None


def message_text(m):
    """Every text-bearing part of a message (text, attachments, block sections). Transient only."""
    parts = [str(m.get('text') or '')]
    for att in m.get('attachments') or []:
        if isinstance(att, dict):
            parts += [str(att.get(k) or '') for k in ('pretext', 'title', 'title_link', 'text', 'fallback')]
            for field in att.get('fields') or []:
                if isinstance(field, dict):
                    parts += [str(field.get('title') or ''), str(field.get('value') or '')]
    for block in m.get('blocks') or []:
        if isinstance(block, dict):
            text = block.get('text')
            if isinstance(text, dict):
                parts.append(str(text.get('text') or ''))
            for field in block.get('fields') or []:
                if isinstance(field, dict):
                    parts.append(str(field.get('text') or ''))
    return '\n'.join(p for p in parts if p)


def digest_message(m):
    return hashlib.sha256(json.dumps({'text': m.get('text'), 'files': [f.get('id') for f in m.get('files') or []
                                                                       if isinstance(f, dict)]},
                                     sort_keys=True).encode()).hexdigest()


PR_RE = re.compile(r'https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)', re.I)


def pr_refs(text):
    return sorted({f'{o}/{r}#{n}'.lower() for o, r, n in PR_RE.findall(str(text or ''))})


STATUS_TOKENS = r'(?:re-?triggered|triggered|recovered|resolved|warn(?:ing)?|alert(?:ing)?|no data|firing(?::\d+)?|ok|' \
                r'critical|error|down|up|p[0-5]|sev-?[0-5])'


STATUS_PREFIX_RE = re.compile(r'^\s*(?:(?:\[' + STATUS_TOKENS + r'\]|\(' + STATUS_TOKENS + r'\)|' + STATUS_TOKENS +
                              r'\s*[:：\-–])\s*)+', re.I)


STATUS_WORD_RE = re.compile(r'\b(re-?triggered|triggered|recovered|resolved|firing|warn(?:ing)?|no data|down|up)\b',
                            re.I)


STATUS_ANYWHERE_RE = re.compile(r'[\[(]' + STATUS_TOKENS + r'[\])]|\b(?:re-?triggered|triggered|recovered|resolved|'
                                r'firing)\b\s*[:：]?', re.I)


SERVICE_RES = (re.compile(r'\bservice\s*[:=]\s*`?([A-Za-z0-9][A-Za-z0-9_./-]{1,63})', re.I),
               re.compile(r'\b(?:app|job|deployment|container|svc|kube_deployment)\s*[:=]\s*`?([A-Za-z0-9][A-Za-z0-9_./-]{1,63})',
                          re.I))


VOLATILE_RE = re.compile(r'0x[0-9a-f]+|\b[0-9a-f]{8,}\b|\d+(?:[.,:]\d+)*%?', re.I)


EMOJI_RE = re.compile(r':[a-z0-9_+-]+:')


def alert_fingerprint(m, text=None):
    """(fingerprint, service, status) of a bot alert: service + normalized title, volatile numbers removed.

    The title is the first non-empty attachment title / fallback / text line with status prefixes such as
    [Triggered], [Recovered], FIRING:1, P2 stripped, so a recovery merges with its trigger. Only the hash and
    the service identifier are stored.
    """
    text = message_text(m) if text is None else text
    attachments = [a for a in m.get('attachments') or [] if isinstance(a, dict)]
    # Most specific first: an attachment title (Datadog, Grafana ...), the message text, fallbacks, then bodies.
    candidates = [a.get('title') for a in attachments] + [m.get('text')]
    candidates += [x for a in attachments for x in (a.get('fallback'), a.get('pretext'))]
    candidates += [b['text'].get('text') for b in m.get('blocks') or []
                   if isinstance(b, dict) and isinstance(b.get('text'), dict)]
    candidates += [a.get('text') for a in attachments]
    title = ''
    for candidate in candidates:
        for line in _strip_markup(candidate).splitlines():
            line = EMOJI_RE.sub(' ', line).strip(' *_`>-•')
            if line:
                title = line
                break
        if title:
            break
    status_match = STATUS_WORD_RE.search(title) or STATUS_WORD_RE.search(_strip_markup(text)[:200])
    status = status_match[1].lower() if status_match else None
    stripped = STATUS_ANYWHERE_RE.sub(' ', STATUS_PREFIX_RE.sub('', title))
    service = None
    for regex in SERVICE_RES:
        found = regex.search(_strip_markup(text))
        if found:
            service = found[1].strip('`.,;')
            break
    if not service:
        profile = m.get('bot_profile') if isinstance(m.get('bot_profile'), dict) else {}
        service = str(profile.get('name') or m.get('username') or m.get('bot_id') or 'unknown-bot')
    service = re.sub(r'[^a-z0-9._/-]+', '-', service.lower()).strip('-')[:64] or 'unknown'
    norm = ' '.join(VOLATILE_RE.sub('#', stripped.lower()).split())[:200]
    fp = hashlib.sha256(f'{service}|{norm}'.encode()).hexdigest()[:16]
    return fp, service, status


def utc_stamp(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def digest_text(text):
    return hashlib.sha256(' '.join(str(text or '').split()).encode()).hexdigest()


_HANDLING_CACHE = {}


def _handling_of(person):
    handling = person.get('handling') if isinstance(person.get('handling'), dict) else {}
    style = handling.get('request_style', person.get('request_style'))
    auto = handling.get('auto_execute', person.get('auto_execute'))
    communication = person.get('communication') if isinstance(person.get('communication'), dict) else {}
    tone = communication.get('tone_hint')
    langs = communication.get('languages')  # P46.2: only when set (empty for most people), see reply_language
    langs = [str(x) for x in langs if str(x).strip()][:8] if isinstance(langs, list) else []
    return {'person': str(person.get('id') or ''),
            'request_style': style if style in REQUEST_STYLES else 'standard',
            'auto_execute': auto if isinstance(auto, bool) else None,
            'tone_hint': tone[:300] if isinstance(tone, str) else None, **({'languages': langs} if langs else {})}


def load_handling(path):
    """Slack user id -> {person, request_style, auto_execute, tone_hint}. Missing/malformed file = no facts."""
    path = Path(path).expanduser()
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _HANDLING_CACHE.get('key') != key:
        out = {}
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raw = {}
        people = raw.get('people') if isinstance(raw, dict) else None
        for person in people if isinstance(people, list) else []:
            if not isinstance(person, dict):
                continue
            for ident in person.get('identities') or []:
                if isinstance(ident, dict) and ident.get('type') == 'slack' and USER_RE.fullmatch(str(ident.get('value'))):
                    out[str(ident['value'])] = _handling_of(person)
        _HANDLING_CACHE.clear()
        _HANDLING_CACHE.update(key=key, value=out)
    return _HANDLING_CACHE['value']


_NAMES_CACHE = {}


def display_names(path):
    """Slack user id -> people-registry display name (`name.display`). Missing/malformed file = no names."""
    path = Path(path).expanduser()
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _NAMES_CACHE.get('key') != key:
        out = {}
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raw = {}
        for person in (raw.get('people') if isinstance(raw, dict) else None) or []:
            name = person.get('name') if isinstance(person, dict) else None
            display = name.get('display') if isinstance(name, dict) else None
            if not isinstance(display, str) or not display.strip():
                continue
            for ident in person.get('identities') or []:
                if isinstance(ident, dict) and ident.get('type') == 'slack' and USER_RE.fullmatch(str(ident.get('value'))):
                    out[str(ident['value'])] = display.strip()[:60]
        _NAMES_CACHE.clear()
        _NAMES_CACHE.update(key=key, value=out)
    return _NAMES_CACHE['value']


def person_name(rt, db, uid):
    """A name for the owner-facing card text (P3.1: names, not ids): the people registry, then the dashboard's cached
    Slack profile name. None when unknown (the card then says 有人 / 对方)."""
    if not uid or not USER_RE.fullmatch(str(uid)):
        return None
    name = display_names(rt.envelopes.people_path()).get(uid)
    if name:
        return name
    try:
        row = db.execute('SELECT name FROM dashboard_profiles WHERE user_id=?', (uid,)).fetchone()
    except Exception:  # noqa: BLE001 - no profile cache table
        row = None
    return row['name'] if row and row['name'] and row['name'] != uid else None


ZH_TZ = ('Asia/Shanghai', 'Asia/Chongqing', 'Asia/Harbin', 'Asia/Urumqi', 'Asia/Taipei', 'Asia/Hong_Kong', 'Asia/Macau')


ZH_SHARE, MIN_LETTERS = 0.15, 8  # Chinese: Han >= 15% of the letters (a Han character carries about a word: 4-5 letters)


UNMEASURED_RE = re.compile(r'```.*?```|`[^`\n]*`|<[^>\n]*>|https?://\S+|:[a-z0-9_+\'-]+:', re.S)


STOP = {lang: set(words.split()) for lang, words in {  # stop words: a Latin language counts only with a clear margin
    'en': 'the a an and or but is are was be to of in on for with this that it its i you u we they can could would will '
          'should do does did not no yes please pls thanks thank thx have has my me your what how why when where who '
          'just so if at from get there here about any need help hi hey',
    'fr': 'le la les l un une des du de d et est je j tu il elle on nous vous pas ne n que qu qui pour avec sur dans ce '
          'c cette mais ou où merci bonjour salut peux peut oui non au aux mon ma ton son ça ai suis très bien à moi',
    'es': 'el la los las un una y o es son está estoy de del que por para con sin no sí si pero como qué yo tú usted me '
          'te se lo le mi tu su al muy más gracias hola puedes puede hay esto ya también',
    'de': 'der die das den dem des ein eine einen und ist sind nicht ich du er sie es wir ihr mit für auf zu im in an von '
          'bei bitte danke hallo ja nein aber oder auch noch mir mich dich dir wie was wo hast habe hat kannst kann schon',
    'pt': 'o a os as um uma e é de do da dos das que não em no na para com por você eu ele ela isso isto obrigado '
          'obrigada olá oi pode sim mas como meu minha seu sua está estou se te tem também já muito'}.items()}


NAMES = {'zh': 'Chinese', 'en': 'English', 'fr': 'French', 'es': 'Spanish', 'de': 'German', 'pt': 'Portuguese'}


LATIN = tuple(STOP)


ACKS = {'zh': DEFAULT_ACK, 'en': 'On it.', 'fr': "Je m'en occupe.", 'es': 'Me pongo con ello.',
        'de': 'Ich kümmere mich darum.', 'pt': 'Estou cuidando disso.'}


_SLACK_PEOPLE = {}  # users.info (tz, name, why unknown): once per person per process, never stored


def text_language(rt, text):
    """(language, Han share): 'zh' when Han >= 15% of the letters, else a clear Latin language (stop words), else 'zh' if
    any Han, else 'other'; None = too little text (a link, a command, "ok"). Code, links, mentions, emoji not measured."""
    clean = UNMEASURED_RE.sub(' ', str(text or ''))
    letters = [c for c in clean if c.isalpha()]
    han = sum(1 for c in letters if rt.CJK_RE.match(c))
    if han < 2 and len(letters) < MIN_LETTERS:
        return None, 0.0
    if han >= ZH_SHARE * len(letters):
        return 'zh', han / len(letters)
    words = re.findall(r'[^\W\d_]+', clean.lower())
    (top, best), (second, _) = sorted(((sum(w in s for w in words), k) for k, s in STOP.items()), reverse=True)[:2]
    return (best if top >= 2 and top >= 2 * second else 'zh' if han else 'other'), han / len(letters)


def own_words(rt, channel, root, user, oldest=None):
    """The person's own messages in this thread (or DM stretch), newest first: read live when iterated, never stored."""
    params = {'channel': channel, 'limit': 100, 'inclusive': 'true', **({'ts': root} if root else {}),
              **({'oldest': oldest} if oldest else {})}
    try:
        found = rt.slack.api_call('conversations.replies' if root else 'conversations.history', rt.api_identity(),
                                  params=params)
    except Exception:  # noqa: BLE001 - unreadable = no evidence here; the fallback decides
        found = {}
    mine = [m for m in found.get('messages') or [] if m.get('user') == user and m.get('ts')]
    yield from (message_text(m) for m in sorted(mine, key=lambda m: -float(m['ts'])))


def _burst_words(rt, b):
    return own_words(rt, b['channel'], b['thread_root'] or (b['first_ts'] if b['conv_type'] == 'channel' else None),
                     b['sender'], b['first_ts'])


def reply_language(rt, db, user_id, words=()):
    """(langs, why, name, share). Their own latest message here (`words`, newest first; an unclear Latin one with up to 2
    earlier ones) -> {'zh'} / {'en'} / {'fr'} ... / {'other'} and its Han share; only when they wrote nothing here, the
    registry `communication.languages`, then the Slack time zone (what they read; share None). Unknown: langs None."""
    name = person_name(rt, db, user_id)
    mine = [t for t in words if text_language(rt, t)[0]][:3]
    if mine:
        lang, share = text_language(rt, mine[0])
        if lang == 'other':  # Latin, language unclear: their earlier Han-free messages here may tell
            lang = text_language(rt, '\n'.join(t for t in mine if not text_language(rt, t)[1]))[0]
        return {lang}, f'their latest message here is {NAMES.get(lang, "not Chinese (language unclear)")}', name, share
    langs = load_handling(rt.envelopes.people_path()).get(user_id, {}).get('languages')
    if langs:
        return {'zh' if re.match(r'(?i)zh|chinese|mandarin|cantonese|中文', x) else x.lower()[:2] for x in langs}, \
            'they have not written here; people registry', name, None
    if user_id not in _SLACK_PEOPLE:
        try:
            user = rt.slack.api_call('users.info', rt.api_identity(), params={'user': user_id}).get('user') or {}
        except Exception as exc:  # noqa: BLE001 - unknown: logged once, never blocks (fail open)
            user = {'error': 'users.info ' + rt.safe_error(exc)}
        profile = user.get('profile') or {}
        _SLACK_PEOPLE[user_id] = (user.get('tz'), profile.get('real_name') or profile.get('display_name'),
                                  user.get('error') or 'no Slack time zone')
        if not user.get('tz'):
            print(json.dumps({'warning': 'reply_language_unknown', 'user': user_id,
                              'why': _SLACK_PEOPLE[user_id][2]}), file=sys.stderr)
    tz, slack_name, unknown = _SLACK_PEOPLE[user_id]
    return ({'zh', 'en'} if tz in ZH_TZ else {'en'}) if tz else None, \
        f'they have not written here; {f"Slack tz {tz}" if tz else unknown}', name or slack_name, None


def _in(langs):
    names = [NAMES[x] for x in NAMES if x in (langs or ())]
    return ' or '.join(names) if langs and len(names) == len(langs) else 'the language of their message'


def reply_line(rt, db, user_id, words=()):
    """What the writer reads BEFORE writing: 'reply to Mira Lind in English (their latest message here is English)'."""
    langs, why, name, _ = reply_language(rt, db, user_id, words)
    return f'reply to {name or user_id} in {_in(langs)} ({why})'


def default_ack(langs, share):
    """The ack in the language of their message; without a message here: Chinese unless they read no Chinese."""
    return ACKS.get(min(langs) if share is not None else 'en' if langs and 'zh' not in langs else 'zh', ACKS['en'])


NOTES_MAX = 600   # the line shown in intake-context / context


NOTE_QUOTE = 24   # a verbatim fragment this heavy is refused: a Han character weighs 2, any other letter or digit 1


_NOTES_CACHE = {}


def _plain(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', str(text or '')).casefold() if c.isalnum())


def guards(style, auto_execute):
    strategic = style == 'strategic_directive'
    return {'strategic': strategic, 'may_auto_execute': not strategic and auto_execute is not False,
            'may_reply': not strategic}


def people_set_field(path, person, field, value):
    """Set handling.<field> of one person in a COPY of the people registry (atomic, mode kept). Never prints
    or returns <owner key>_view; the coordinator applies the copy to the real file."""
    path = Path(path).expanduser()
    if field not in HANDLING_FIELDS:
        raise ValueError('field_must_be_request_style_or_auto_execute')
    if field == 'request_style':
        if value not in REQUEST_STYLES:
            raise ValueError('request_style_must_be_' + '_or_'.join(REQUEST_STYLES))
    else:
        text = str(value).strip().lower()
        if text not in ('true', 'false'):
            raise ValueError('auto_execute_must_be_true_or_false')
        value = text == 'true'
    if path.is_symlink() or not path.is_file():
        raise ValueError('people_file_must_be_a_regular_file')
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        raise ValueError('people_file_is_not_valid_json') from None
    people = raw.get('people') if isinstance(raw, dict) else None
    if not isinstance(people, list):
        raise ValueError('people_file_has_no_people_list')
    hits = [p for p in people if isinstance(p, dict) and (p.get('id') == person or any(
        isinstance(i, dict) and i.get('type') == 'slack' and i.get('value') == person for i in p.get('identities') or []))]
    if not hits:
        raise ValueError('person_not_found')
    if len(hits) > 1:
        raise ValueError('person_is_ambiguous_use_the_person_id')
    target = hits[0]
    if 'handling' in target and not isinstance(target['handling'], dict):
        raise ValueError('person_handling_is_not_an_object')
    handling = target.setdefault('handling', {})
    old = handling.get(field)
    handling[field] = value
    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(prefix='.people.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(raw, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.chmod(tmp, mode or 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    slack_id = next((i.get('value') for i in target.get('identities') or []
                     if isinstance(i, dict) and i.get('type') == 'slack'), None)
    return {'ok': True, 'file': str(path), 'person': target.get('id'), 'slack': slack_id, 'field': field,
            'old': old, 'new': value}


class BudgetExhausted(Exception):
    pass


class EndpointBackoff(Exception):
    pass


class Budget:
    def __init__(self, limit):
        self.limit, self.used, self.calls = int(limit), 0, {}

    def take(self, endpoint):
        if self.used >= self.limit:
            raise BudgetExhausted(endpoint)
        self.used += 1
        self.calls[endpoint] = self.calls.get(endpoint, 0) + 1


def _slack_code(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f'HTTP_{exc.code}'
    match = re.fullmatch(r'Slack API error: ([a-z_]+)', str(exc))
    if match:
        return match[1]
    if isinstance(exc, RuntimeError) and str(exc) in AUTH_ERRORS:
        return str(exc)
    return type(exc).__name__


def dm_routing(cfg):
    """P39: intake (not a watch) routes every NEW top-level DM / group-DM message (`intake.dm_request_routing`)."""
    return bool(cfg.get('enabled', True) and cfg.get('dm_request_routing', True))


class Intake:
    def __init__(self, db, db_path, rt, clock=time.time, *, core):
        self.core = core
        self.db, self.db_path, self.rt, self.clock = db, str(db_path), rt, clock
        self.token = None
        self.budget = None
        self._pr_index = (0.0, {})
        self._next_waiting = 0.0
        self._next_bindings = 0.0
        self._next_prune = 0.0
        self._watch_cache = None

    # ---- config / state ----
    def config(self):
        return self.rt.worker_config.load()[0]

    def load_state(self):
        row = self.db.execute('SELECT value FROM runtime_meta WHERE key=?', (STATE_KEY,)).fetchone()
        try:
            state = json.loads(row[0]) if row else {}
        except ValueError:
            state = {}
        return state if isinstance(state, dict) else {}

    def save_state(self, state):
        with self.rt.transaction(self.db):
            self.db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)',
                            (STATE_KEY, json.dumps(state, ensure_ascii=False, sort_keys=True)))

    def intake_tasks(self, cfg):
        return {cfg['inbox_task'], cfg['oncall_task']}

    def bootstrapped(self, cfg):
        return self.rt.registry.task_dir(cfg['inbox_task']).is_dir()

    # ---- the supervisor step ----
    def step(self, now=None):
        now = self.clock() if now is None else now
        try:
            config = self.config()
        except ValueError as exc:
            self.rt._meta_error(self.db, 'intake_config_error', exc)
            return 'config_invalid'
        cfg = config['intake']
        if not cfg['enabled']:
            return 'disabled'
        if not self.bootstrapped(cfg):
            return 'not_bootstrapped'
        state = self.load_state()
        dirty = False
        if state.get('baseline') is None:
            # First enabled cycle: never process history from before intake existed.
            state.update(baseline=now, covered_until=now, next_cycle_at=now, errors=0, search={})
            dirty = True
        if now >= float(state.get('next_cycle_at') or 0):
            self.fetch_cycle(now, cfg, state)
            dirty = True
        if dirty:
            self.save_state(state)
        self.seal_due(now, cfg, state)
        self.route_due(now, config)
        if now >= self._next_bindings:
            self._next_bindings = now + BINDING_SWEEP_SECONDS
            self.materialize_bindings(now)
            self.release_closed(now)
        if now >= self._next_prune:
            self._next_prune = now + 3600
            self.prune(now)
        return state.get('status') or 'ok'

    def prune(self, now):
        """Dedupe rows are only needed while search overlaps / cursors could re-read a message: keep 30 days."""
        with self.rt.transaction(self.db):
            self.db.execute('DELETE FROM intake_seen WHERE seen_at<?', (now - SEEN_RETENTION_SECONDS,))
            self.db.execute('DELETE FROM intake_fetch_queue WHERE queued_at<?', (now - 86400,))
            self.db.execute('DELETE FROM intake_reads WHERE read_at<?', (now - SEEN_RETENTION_SECONDS,))

    # ---- Slack calls (budget + backoff) ----
    def backoff_active(self, endpoint, now, state):
        until = max(float((state.get('backoff') or {}).get(endpoint) or 0), float(self.rt.RATE_LIMITS.get(endpoint) or 0))
        return now < until

    def call(self, endpoint, params, now, state):
        if self.backoff_active(endpoint, now, state):
            raise EndpointBackoff(endpoint)
        self.budget.take(endpoint)
        try:
            return self.rt.slack.api_call(endpoint, self.token, params=params)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    delay = max(1.0, float((exc.headers or {}).get('Retry-After') or 60))
                except (TypeError, ValueError, AttributeError):
                    delay = 60.0
                self._rate_limited(endpoint, now, delay, state)
                raise EndpointBackoff(endpoint) from None
            raise
        except RuntimeError as exc:
            if 'ratelimited' in str(exc):
                self._rate_limited(endpoint, now, 60.0, state)
                raise EndpointBackoff(endpoint) from None
            raise

    def _rate_limited(self, endpoint, now, delay, state):
        state.setdefault('backoff', {})[endpoint] = now + delay
        # The watch poller shares the owner's token and Slack's per-method limits: back it off too.
        self.rt.RATE_LIMITS[endpoint] = max(float(self.rt.RATE_LIMITS.get(endpoint) or 0), now + delay)
        state['rate_limited'] = state.get('rate_limited', 0) + 1

    # ---- fetch cycle ----
    def fetch_cycle(self, now, cfg, state):
        self.budget = Budget(cfg['max_api_calls_per_cycle'])
        stats = {'at': now, 'api_calls': 0, 'calls': {}, 'discovered': 0, 'processed': 0, 'accepted': 0,
                 'filtered': {}, 'complete': False, 'error': None}
        channels, unresolved = self.core.resolve_channels(cfg['channels'])
        oncall, _ = self.core.resolve_channels(cfg['oncall_channels'])
        state['unresolved_channels'] = unresolved
        ctx = {'subscribed': set(channels), 'oncall': set(oncall), 'now': now, 'cfg': cfg,
               'baseline': float(state['baseline']), 'stats': stats, 'state': state}
        self._watch_cache = None
        complete = True
        try:
            bot = cfg.get('identity') == 'bot'  # 0.77.0: a bot-only deployment reads with the bot token
            self.token = self.rt.api_identity(bot=bot)
            try:
                if not bot:  # search.messages needs a user token; the receiver pushes mentions instead
                    complete &= self.search_phase(ctx)
                complete &= self.queue_phase(ctx)
                complete &= self.conversations_phase(ctx, channels)
            except BudgetExhausted:
                complete = False  # the rest waits for the next cycle (never sooner than poll_seconds)
        except Exception as exc:  # noqa: BLE001 - recorded and backed off; the supervisor keeps running
            code = _slack_code(exc)
            stats['error'] = code
            state['errors'] = int(state.get('errors') or 0) + 1
            state['last_error'] = code
            state['last_error_at'] = now
            if code in AUTH_ERRORS:
                delay, state['status'] = ERROR_BACKOFF_MAX, 'auth_error'
            else:
                delay, state['status'] = min(ERROR_BACKOFF_MAX, cfg['poll_seconds'] * 2 ** min(state['errors'] - 1, 5)), 'error'
            state['next_cycle_at'] = now + delay
            complete = False
        else:
            state['errors'] = 0
            state['status'] = 'backoff' if any(now < float(v or 0) for v in (state.get('backoff') or {}).values()) else 'ok'
            state['next_cycle_at'] = now + cfg['poll_seconds']
        if complete:
            state['covered_until'] = now
        stats.update(api_calls=self.budget.used, calls=dict(self.budget.calls), complete=bool(complete),
                     duration=round(self.clock() - now, 3) if self.clock() >= now else 0)
        state['last_cycle'] = stats
        state['cycles'] = int(state.get('cycles') or 0) + 1
        hour = [x for x in state.get('calls_last_hour') or [] if isinstance(x, list) and len(x) == 2 and x[0] > now - 3600]
        hour.append([now, self.budget.used])
        state['calls_last_hour'] = hour[-120:]
        return stats

    # search.messages
    def search_phase(self, ctx):
        cfg, state, now = ctx['cfg'], ctx['state'], ctx['now']
        queries = [(q, 'dm_search') for q in cfg['dm_queries']]
        if cfg['mention_search']:
            queries.append((self.core.mention_query, 'mention_search'))
        complete, seen_now = True, set()
        for query, source in queries:
            if self.backoff_active('search.messages', now, state):
                return False
            qstate = state.setdefault('search', {}).setdefault(query, {})
            hwm = float(qstate.get('hwm') or ctx['baseline'])
            stop_at = hwm - SEARCH_OVERLAP_SECONDS
            newest, done = hwm, False
            for page in range(1, cfg['search_max_pages'] + 1):
                try:
                    response = self.call('search.messages', {'query': query, 'count': SEARCH_COUNT, 'sort': 'timestamp',
                                                             'sort_dir': 'desc', 'page': page}, now, state)
                except EndpointBackoff:
                    return False
                block = response.get('messages') if isinstance(response.get('messages'), dict) else {}
                matches = [m for m in block.get('matches') or [] if isinstance(m, dict)]
                stamps = []
                for match in matches:
                    ts = str(match.get('ts') or '')
                    if TS_RE.fullmatch(ts):
                        stamps.append(float(ts))
                        self.on_match(ctx, match, source, seen_now)
                if stamps:
                    newest = max(newest, max(stamps))
                paging = block.get('paging') if isinstance(block.get('paging'), dict) else {}
                pagination = block.get('pagination') if isinstance(block.get('pagination'), dict) else {}
                pages = int(paging.get('pages') or pagination.get('page_count') or page)
                if not stamps or min(stamps) <= stop_at or page >= pages:
                    done = True
                    break
            if done:
                qstate['hwm'] = f'{newest:.6f}'
            else:
                complete = False
        return complete

    def on_match(self, ctx, match, source, seen_now):
        channel_obj = match.get('channel') if isinstance(match.get('channel'), dict) else {}
        channel, ts = str(channel_obj.get('id') or ''), str(match.get('ts') or '')
        if not CHANNEL_ID_RE.fullmatch(channel) or not TS_RE.fullmatch(ts) or float(ts) < ctx['baseline']:
            return
        if (channel, ts) in seen_now:
            return
        seen_now.add((channel, ts))
        ctx['stats']['discovered'] += 1
        if self.db.execute('SELECT 1 FROM intake_seen WHERE channel=? AND ts=?', (channel, ts)).fetchone():
            return
        conv_type = match_conv_type(match)
        root = parse_permalink_thread(match.get('permalink'))
        reply = bool(root and root != ts)
        with self.rt.transaction(self.db):
            if conv_type in ('im', 'mpim') and not reply:
                conv = self.db.execute('SELECT * FROM intake_convs WHERE channel=?', (channel,)).fetchone()
                if conv and conv['cursor'] and float(ts) <= float(conv['cursor']):
                    return
                if conv is None:
                    self.db.execute('INSERT INTO intake_convs (channel,conv_type,wanted_since) VALUES (?,?,?)',
                                    (channel, conv_type, float(ts)))
                else:
                    self.db.execute('UPDATE intake_convs SET wanted_since=min(coalesce(wanted_since,?),?) WHERE channel=?',
                                    (float(ts), float(ts), channel))
            elif conv_type == 'channel' and not reply and channel in ctx['subscribed']:
                return  # the channel history read sees it
            else:
                self.db.execute('INSERT OR IGNORE INTO intake_fetch_queue (channel,ts,thread_root,conv_type,source,queued_at) '
                                'VALUES (?,?,?,?,?,?)', (channel, ts, root if reply else None, conv_type, source, ctx['now']))

    # exact fetches (thread replies, channel mentions)
    def queue_phase(self, ctx):
        now, state = ctx['now'], ctx['state']
        rows = self.db.execute('SELECT * FROM intake_fetch_queue ORDER BY queued_at, ts LIMIT ?',
                               (FETCH_QUEUE_PER_CYCLE,)).fetchall()
        complete = True
        for row in rows:
            endpoint = 'conversations.replies' if row['thread_root'] else 'conversations.history'
            params = {'channel': row['channel'], 'oldest': row['ts'], 'latest': row['ts'], 'inclusive': 'true',
                      'limit': 2 if row['thread_root'] else 1, 'include_all_metadata': 'true'}
            if row['thread_root']:
                params['ts'] = row['thread_root']
            try:
                response = self.call(endpoint, params, now, state)
            except EndpointBackoff:
                complete = False
                continue
            except RuntimeError as exc:
                if _slack_code(exc) not in CHANNEL_ERRORS:
                    raise
                response = {'messages': []}
                attempts = FETCH_MAX_ATTEMPTS
            else:
                attempts = row['attempts'] + 1
            message = next((m for m in response.get('messages') or [] if isinstance(m, dict) and m.get('ts') == row['ts']),
                           None)
            if message is not None:
                self.ingest([message], row['channel'], row['conv_type'] or 'channel', row['source'], ctx)
            with self.rt.transaction(self.db):
                if message is not None or attempts >= FETCH_MAX_ATTEMPTS:
                    self.db.execute('DELETE FROM intake_fetch_queue WHERE channel=? AND ts=?', (row['channel'], row['ts']))
                else:
                    self.db.execute('UPDATE intake_fetch_queue SET attempts=? WHERE channel=? AND ts=?',
                                    (attempts, row['channel'], row['ts']))
        left = self.db.execute('SELECT count(*) FROM intake_fetch_queue').fetchone()[0]
        return complete and not left

    # conversations.history: wanted DMs / group DMs and subscribed channels
    def conversations_phase(self, ctx, channels):
        now = ctx['now']
        with self.rt.transaction(self.db):
            for channel in channels:
                self.db.execute("INSERT OR IGNORE INTO intake_convs (channel,conv_type) VALUES (?,'channel_sub')", (channel,))
        wanted = self.db.execute("SELECT * FROM intake_convs WHERE conv_type IN ('im','mpim') AND wanted_since IS NOT NULL "
                                 'AND coalesce(next_at,0)<=? ORDER BY wanted_since', (now,)).fetchall()
        subs = [self.db.execute('SELECT * FROM intake_convs WHERE channel=?', (c,)).fetchone() for c in channels]
        complete = True
        for conv in list(wanted) + [c for c in subs if c is not None]:
            if conv['conv_type'] == 'channel_sub' and float(conv['next_at'] or 0) > now:
                complete = False
                continue
            if self.backoff_active('conversations.history', now, ctx['state']):
                return False
            complete &= self.read_history(conv, ctx)
        return complete

    def read_history(self, conv, ctx):
        now, state, channel = ctx['now'], ctx['state'], conv['channel']
        if conv['cursor']:
            oldest = conv['cursor']
        elif conv['conv_type'] == 'channel_sub':
            oldest = f"{ctx['baseline']:.6f}"
        else:
            oldest = f"{max(ctx['baseline'], float(conv['wanted_since'] or now) - DM_REWIND_SECONDS):.6f}"
        page_cursor, high, done = conv['page_cursor'], conv['page_high'] or oldest, False
        source = 'channel_history' if conv['conv_type'] == 'channel_sub' else 'dm_history'
        conv_type = 'channel' if conv['conv_type'] == 'channel_sub' else conv['conv_type']
        error = None
        for _ in range(CONV_MAX_PAGES):
            params = {'channel': channel, 'oldest': oldest, 'limit': HISTORY_LIMIT, 'include_all_metadata': 'true'}
            if page_cursor:
                params['cursor'] = page_cursor
            try:
                response = self.call('conversations.history', params, now, state)
            except EndpointBackoff:
                break
            except RuntimeError as exc:
                if _slack_code(exc) not in CHANNEL_ERRORS:
                    raise
                error = _slack_code(exc)
                break
            batch = [m for m in response.get('messages') or [] if isinstance(m, dict) and TS_RE.fullmatch(str(m.get('ts')))]
            if batch:
                high = max([high] + [m['ts'] for m in batch], key=float)
            self.ingest(batch, channel, conv_type, source, ctx)
            next_cursor = (response.get('response_metadata') or {}).get('next_cursor')
            if response.get('has_more') and not next_cursor:
                raise RuntimeError('incomplete_slack_pagination')
            if next_cursor:
                page_cursor = next_cursor
                continue
            page_cursor, done = None, True
            break
        with self.rt.transaction(self.db):
            if error:
                count = conv['error_count'] + 1
                self.db.execute('UPDATE intake_convs SET error=?,error_count=?,next_at=?,wanted_since=CASE WHEN ?>=3 '
                                'THEN NULL ELSE wanted_since END WHERE channel=?',
                                (error, count, now + min(ERROR_BACKOFF_MAX * 4, 60 * 2 ** min(count, 6)), count, channel))
            elif done:
                self.db.execute('UPDATE intake_convs SET cursor=?,page_cursor=NULL,page_high=NULL,wanted_since=NULL,'
                                'last_read_at=?,error=NULL,error_count=0,next_at=NULL WHERE channel=?', (high, now, channel))
            else:
                self.db.execute('UPDATE intake_convs SET page_cursor=?,page_high=?,last_read_at=? WHERE channel=?',
                                (page_cursor, high, now, channel))
        return done and not error

    # ---- per-message processing ----
    def running_watches(self):
        if self._watch_cache is None:
            self._watch_cache = [w for w in (json.loads(r[0]) for r in self.db.execute(
                "SELECT data FROM watches WHERE json_extract(data,'$.state')='running'"))]
        return self._watch_cache

    def covering_watch(self, channel, root, m, conv_type=None, cfg=None):
        """A running watch whose poller will deliver this exact message (same conversation, eligible author). P39: with
        DM routing on, a NEW top-level DM / group-DM message is never covered — the watches no longer capture it
        (slack_followup.ingest), intake routes it (refs first, one capturing task, else <owner key>-inbox; F06: no double)."""
        ts = float(m.get('ts') or 0)
        if root is None and (channel.startswith('D') or conv_type == 'mpim') and dm_routing(cfg or {}):
            return None
        for w in self.running_watches():
            if w.get('kind') == 'task_inbox':
                continue
            same = False
            if w.get('scope') == 'channel' and w.get('channel') == channel:
                same = root is None or root in ((w.get('fetch_state') or {}).get('roots') or [])
            elif w.get('channel') == channel and w.get('thread'):
                same = root is not None and root == w['thread']
            elif root is None and channel.startswith('D') and (w.get('channel') == channel or w.get('dm') == channel) \
                    and not w.get('thread'):
                # P36: a NEW top-level DM message is delivered only by a WHOLE-DM watch (thread is None) — never claimed
                # as "covered" merely because a watch for a DIFFERENT / OLD thread happens to name this DM. A whole-DM
                # watch (a running follow-up watch) still delivers the person's follow-ups; once it stops, a new top-level
                # message is re-triaged by the router (bound_task) instead of a stale binding.
                same = True
            if not same or ts <= float(w.get('since') or 0):
                continue
            if w.get('deadline') is not None and ts > float(w['deadline']):
                continue
            try:
                if self.rt.eligible(w, m):
                    return w
            except Exception:  # noqa: BLE001 - a malformed watch never covers anything
                continue
        return None

    def delegate(self, m, ctx):
        """P42.1: delegate_app, and its user is a person, not that app's own bot user (users.info, see app_people)."""
        via = self.core.delegate_app(m, ctx.get('cfg'))
        return via if via and (ctx.get('state') or {}).get('app_people', {}).get(m['user']) else None

    def app_people(self, messages, ctx):
        """P42.1: is_bot of each new user id behind a delegate-named app post, once per id (kept in the intake state).
        Before the ingest transaction: a backoff defers the batch to the next cycle; an API error = a bot this time."""
        known = ctx.setdefault('state', {}).setdefault('app_people', {})
        for user in sorted({m['user'] for m in messages if self.core.delegate_app(m, ctx.get('cfg'))} - set(known)):
            try:
                info = self.call('users.info', {'user': user}, ctx['now'], ctx['state']).get('user') or {}
            except EndpointBackoff:
                raise BudgetExhausted('users.info') from None
            except RuntimeError as exc:  # e.g. user_not_found / missing_scope: asked again on the next such post
                ctx['state']['app_people_error'] = _slack_code(exc)
                continue
            known[user] = info.get('is_bot') is False and not info.get('deleted')

    def verdict(self, m, channel, conv_type, root, ctx):
        """'accept' or the filter reason. Our agent is checked BEFORE the owner (agent posts use his token)."""
        subtype = m.get('subtype')
        user = str(m.get('user') or '')
        if subtype not in ALLOWED_SUBTYPES or user == SLACKBOT:
            return 'system_noise'
        meta = m.get('metadata') if isinstance(m.get('metadata'), dict) else {}
        profile = m.get('bot_profile') if isinstance(m.get('bot_profile'), dict) else {}
        if meta.get('event_type') == self.core.agent_event_type or m.get('app_id') == self.core.app_id or profile.get('app_id') == self.core.app_id \
                or user == self.core.bot_user or self.db.execute('SELECT 1 FROM agent_sends WHERE channel=? AND ts=?',
                                                  (channel, m.get('ts'))).fetchone():
            return 'our_agent'
        cfg = ctx.get('cfg') or {}
        bot_only = cfg.get('identity') == 'bot'  # 0.77.0: a bot-only deployment answers what is said to it
        addressed = conv_type != 'channel' or f'<@{self.core.bot_user}>' in str(m.get('text') or '')
        if user == self.core.owner_slack_user and not (bot_only and addressed):
            return f'{self.core.key}_authored'  # (said to the bot, the owner's words are an instruction to it)
        if bot_only and not addressed and channel not in ctx.get('oncall', ()):
            return 'no_bot_mention'
        bot = (bool(m.get('bot_id')) or subtype == 'bot_message' or not USER_RE.fullmatch(user)) \
            and not self.delegate(m, ctx)  # P42.1: a person's own app posting as them is that person
        if bot and not (conv_type == 'channel' and channel in ctx['oncall'] and root is None):
            return 'bot_outside_oncall'
        if not bot:
            w = self.covering_watch(channel, root, m, conv_type, ctx.get('cfg'))
            if w is not None:
                return 'covered_by_watch'
        return 'accept'

    def ingest(self, messages, channel, conv_type, source, ctx):
        stats, now = ctx['stats'], ctx['now']
        ordered = sorted(messages, key=lambda m: float(m.get('ts') or 0))
        self.app_people(ordered, ctx)  # P42.1
        with self.rt.transaction(self.db):
            for m in ordered:
                ts = str(m.get('ts') or '')
                if not TS_RE.fullmatch(ts) or float(ts) < ctx['baseline']:
                    continue
                thread_ts = m.get('thread_ts')
                root = thread_ts if thread_ts and thread_ts != ts and TS_RE.fullmatch(str(thread_ts)) else None
                if source == 'channel_history' and root and m.get('subtype') != 'thread_broadcast':
                    continue  # replies reach a task only through watches
                user = str(m.get('user') or '')
                kind = 'human' if self.delegate(m, ctx) else 'bot' if (  # P42.1
                    m.get('bot_id') or m.get('subtype') == 'bot_message' or not USER_RE.fullmatch(user)) \
                    else 'agent' if m.get('app_id') else 'human'
                cur = self.db.execute('INSERT OR IGNORE INTO intake_seen (channel,ts,source,conv_type,thread_root,user,'
                                      'sender_kind,digest,reason,seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                                      (channel, ts, source, conv_type, root, user or None, kind, digest_message(m),
                                       'pending', now))
                if cur.rowcount != 1:
                    continue
                stats['processed'] += 1
                reason = self.verdict(m, channel, conv_type, root, ctx)
                burst_id = None
                if reason == 'accept':
                    burst_id, reason = self.add_to_burst(m, channel, conv_type, root, kind, ctx)
                    stats['accepted'] += 1
                else:
                    stats['filtered'][reason] = stats['filtered'].get(reason, 0) + 1
                self.db.execute('UPDATE intake_seen SET reason=?,burst_id=? WHERE channel=? AND ts=?',
                                (reason, burst_id, channel, ts))

    def _envelope(self, m, root, burst_id, via=None):
        if via:  # P42.1: classified as the person (their trust class, as if typed); the app is recorded as `via`
            m = {k: v for k, v in m.items() if k not in ('bot_id', 'app_id', 'bot_profile')
                 and (k, v) != ('subtype', 'bot_message')}
        env = self.rt.message_envelope({}, m, root)
        if env is None:
            env = self.rt.envelopes.envelope('slack_message', 'human', 'colleague', 'none', 'intake_default', root)
        env['burst_id'] = burst_id
        env['kind'] = 'slack_message'
        if via:
            env['via'] = via
        if env.get('trust') == self.core.key:  # never routed (filtered) -- defensive: intake events never carry the owner authority
            env['authority'] = 'none'
        return env

    def add_to_burst(self, m, channel, conv_type, root, kind, ctx, smoke_scenario=None):
        """Inside the ingest transaction. Returns (burst_id, how)."""
        now, cfg, ts = ctx['now'], ctx['cfg'], m['ts']
        user = str(m.get('user') or '')
        sender = user if kind != 'bot' else 'bot:' + str(m.get('bot_id') or m.get('username') or 'unknown')[:40]
        text = message_text(m)
        refs_t, refs_p = self.core.task_refs(text), pr_refs(text)
        fp = service = status = None
        if kind == 'bot':
            fp, service, status = alert_fingerprint(m, text)
            window = cfg['alert_dedupe_minutes'] * 60
            prior = self.db.execute("SELECT * FROM intake_bursts WHERE alert_fp=? AND state!='expired' ORDER BY created_at "
                                    'DESC LIMIT 1', (fp,)).fetchone()
            if prior is not None and float(ts) - float(prior['first_ts']) <= window and float(ts) >= float(prior['first_ts']):
                if prior['state'] == 'open':
                    self._append(prior, ts, refs_t, refs_p, now, alert_state=status)
                    return prior['burst_id'], 'burst'
                self.db.execute('UPDATE intake_bursts SET dup_count=dup_count+1,dup_last_ts=?,alert_state=coalesce(?,alert_state),'
                                'updated_at=? WHERE burst_id=?', (ts, status, now, prior['burst_id']))
                return prior['burst_id'], 'alert_duplicate'
            key = f'alert:{fp}'
        else:
            key = f'{sender}|{channel}:{root or ""}'
        open_burst = self.db.execute("SELECT * FROM intake_bursts WHERE state='open' AND conv_key=? ORDER BY created_at DESC "
                                     'LIMIT 1', (key,)).fetchone()
        if open_burst is not None:
            # P39 (F08): a human DM / group-DM burst still waits its settle delay — a fragment within that delay of the
            # previous one joins it (it used to seal the first burst early and split one request into two events).
            merge = cfg['burst_seconds']
            if kind == 'human' and conv_type in ('im', 'mpim') and cfg.get('dm_settle_seconds'):
                merge = max(merge, cfg['dm_settle_seconds'])
            if float(ts) - float(open_burst['last_ts']) <= merge:
                self._append(open_burst, ts, refs_t, refs_p, now, alert_state=status)
                return open_burst['burst_id'], 'burst'
            # Both messages arrived in one poll but the sliding window had closed in between: seal, start anew.
            self.db.execute("UPDATE intake_bursts SET state='sealed',sealed_at=?,updated_at=? WHERE burst_id=?",
                            (now, now, open_burst['burst_id']))
        burst_id = 'in' + uuid.uuid4().hex[:14]
        handling = load_handling(self.rt.envelopes.people_path()).get(user, {}) if kind != 'bot' else {}
        if smoke_scenario == 'strategic':
            handling = {'request_style': 'strategic_directive', 'auto_execute': False}
        env = self._envelope(m, root or ts, burst_id, self.delegate(m, ctx) if kind == 'human' else None)
        auto = handling.get('auto_execute')
        self.db.execute('INSERT INTO intake_bursts (burst_id,conv_key,channel,conv_type,thread_root,sender,sender_kind,'
                        'first_ts,last_ts,msg_count,refs,task_refs,pr_refs,alert_fp,alert_service,alert_state,envelope,'
                        'person_style,auto_execute,state,smoke,smoke_scenario,created_at,updated_at) '
                        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (burst_id, key, channel, conv_type, root, sender, kind, ts, ts, 1, json.dumps([ts]),
                         json.dumps(refs_t), json.dumps(refs_p), fp, service, status, json.dumps(env, ensure_ascii=False),
                         handling.get('request_style') or 'standard', None if auto is None else int(bool(auto)), 'open',
                         int(bool(smoke_scenario)), smoke_scenario, now, now))
        return burst_id, 'burst'

    def _append(self, b, ts, refs_t, refs_p, now, alert_state=None):
        refs = json.loads(b['refs'] or '[]')
        if ts not in refs and len(refs) < REFS_CAP:
            refs.append(ts)
        merged_t = sorted(set(json.loads(b['task_refs'] or '[]')) | set(refs_t))
        merged_p = sorted(set(json.loads(b['pr_refs'] or '[]')) | set(refs_p))
        self.db.execute('UPDATE intake_bursts SET last_ts=?,msg_count=msg_count+1,refs=?,task_refs=?,pr_refs=?,'
                        'alert_state=coalesce(?,alert_state),updated_at=? WHERE burst_id=?',
                        (max(b['last_ts'], ts, key=float), json.dumps(refs), json.dumps(merged_t), json.dumps(merged_p),
                         alert_state, now, b['burst_id']))

    # ---- seal / route / deliver (no Slack reads; delivery adds the P3.1 read-receipt emoji) ----
    def seal_due(self, now, cfg, state):
        window = cfg['burst_seconds']
        settle = cfg.get('dm_settle_seconds') or 0  # P3.3: extra wait before a human DM burst is dispatched (0 = off)
        stall = max(STALL_SECONDS, 3 * cfg['poll_seconds'])
        covered = float(state.get('covered_until') or 0)
        sealed = []
        for b in self.db.execute("SELECT burst_id,last_ts,conv_type,sender_kind FROM intake_bursts WHERE "
                                 "state='open'").fetchall():
            # Alerts and channel / oncall bursts seal as soon as the burst window closes; a HUMAN DM / group-DM burst
            # waits the settle delay after its last message so the owner's own quick reply in that conversation lands first.
            human_dm = settle and b['sender_kind'] == 'human' and b['conv_type'] in ('im', 'mpim')
            end = float(b['last_ts']) + (max(window, settle) if human_dm else window)
            if now >= end and (covered >= end or now >= end + stall):
                sealed.append(b['burst_id'])
        if sealed:
            with self.rt.transaction(self.db):
                for burst_id in sealed:
                    self.db.execute("UPDATE intake_bursts SET state='sealed',sealed_at=?,updated_at=? WHERE burst_id=? "
                                    "AND state='open'", (now, now, burst_id))
        return sealed

    def route_due(self, now, config):
        self._watch_cache = None  # P39: routing (in-flight claims) reads the watches as they are now
        rows = list(self.db.execute("SELECT * FROM intake_bursts WHERE state='sealed' ORDER BY created_at LIMIT ?",
                                    (ROUTE_BATCH,)))
        if now >= self._next_waiting:
            self._next_waiting = now + WAITING_RETRY_SECONDS
            rows += list(self.db.execute("SELECT * FROM intake_bursts WHERE state='waiting' AND updated_at<=? "
                                         'ORDER BY created_at LIMIT ?', (now - WAITING_RETRY_SECONDS, ROUTE_BATCH)))
        results = []
        for b in rows:
            try:
                results.append(self.route_one(b, now, config))
            except Exception as exc:  # noqa: BLE001 - one bad burst never blocks the others
                with self.rt.transaction(self.db):
                    self.db.execute("UPDATE intake_bursts SET state='waiting',error=?,updated_at=? WHERE burst_id=?",
                                    ('route_error:' + _slack_code(exc), now, b['burst_id']))
                results.append((b['burst_id'], 'error'))
        return results

    def route_one(self, b, now, config):
        cfg = config['intake']
        max_age = config['dispatch']['max_event_age_hours'] * 3600
        if now - float(b['last_ts']) > max_age:
            with self.rt.transaction(self.db):
                self.db.execute("UPDATE intake_bursts SET state='expired',error=coalesce(error,'too_old'),updated_at=? "
                                'WHERE burst_id=?', (now, b['burst_id']))
            return b['burst_id'], 'expired'
        task_id, route, reason = self.route_burst(b, cfg, now)
        return b['burst_id'], self.deliver(b, task_id, route, reason, cfg, now)

    def bound_task(self, channel, root, conv_type, now, sender=None):
        if not root and conv_type not in ('im', 'mpim'):
            return None  # a new top-level channel message is a new conversation
        if not root and conv_type in ('im', 'mpim'):
            # P46.3 (a colleague, 2026-10-30): a NEW top-level DM / group-DM message never stays with a task because it
            # awaits this person (an open ask card / in-flight watch bound the whole DM, so the owner's chat reached the
            # task). The answer comes in the ask's own thread or by its buttons; anything else goes to <owner key>-inbox,
            # which sees the open asks (R-0012).
            return None
        # A reply inside a bound thread (an intake binding, or a task's thread watch): stays with that task.
        key = binding_key(conv_type, root, None, sender)
        row = self.db.execute('SELECT task_id FROM intake_bindings WHERE channel=? AND thread_root=? AND '
                              '(expires_at IS NULL OR expires_at>?)', (channel, key, now)).fetchone()
        if row and self.rt.registry.task_dir(row['task_id']).is_dir():
            return row['task_id'], 'intake_binding'
        watches = [json.loads(r[0]) for r in self.db.execute(
            "SELECT data FROM watches WHERE json_extract(data,'$.channel')=? OR json_extract(data,'$.dm')=?",
            (channel, channel))]
        for w in sorted(watches, key=lambda x: -float(x.get('started') or 0)):
            if w.get('kind') in ('task_inbox', 'decision') or w.get('smoke') or w.get('state') == 'stopped':
                continue
            tid = self.rt.registry.task_id_for_cwd(w.get('cwd')) if (
                w.get('channel') == channel and w.get('thread') == root) else None
            if tid and self.rt.registry.task_dir(tid).is_dir():
                return tid, 'watch:' + w['id']
        card = ask_thread_card(self.db, channel, root)  # P46.3: an ask card's thread belongs to the asking task
        tid = card and self._watch_task(card['watch_id'], stopped=False)
        return (tid, 'ask_thread:' + card['id']) if tid and self._live_task(tid) else None

    def _watch_task(self, watch_id, stopped=True):
        """The task of a watch (None for a stopped one unless `stopped`: the owner stopped following that conversation)."""
        try:
            w = self.rt.load(self.db, watch_id) if watch_id else {}
        except Exception:  # noqa: BLE001 - a malformed watch belongs to no task
            return None
        return self.rt.registry.task_id_for_cwd(w.get('cwd')) if w and (stopped or w.get('state') != 'stopped') else None

    def pr_index(self, cfg, now):
        built, index = self._pr_index
        if now - built < PR_INDEX_TTL:
            return index
        index = {}
        root = self.rt.registry.root()
        skip = self.intake_tasks(cfg)
        try:
            # P39: skip a renamed task's old-name symlink (task-rename) — the same workspace under a second name
            entries = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith('.') and p.name not in skip
                       and self.rt.registry.task_id_for_cwd(p) == p.name]
        except OSError:
            entries = []
        for task in entries:
            for name in ('goal.md', 'status.md', 'context.md', 'decisions.md'):
                try:
                    with (task / name).open(encoding='utf-8', errors='replace') as stream:
                        text = stream.read(65536)
                except OSError:
                    continue
                for ref in pr_refs(text):
                    index.setdefault(ref, set()).add(task.name)
        self._pr_index = (now, index)
        return index

    def ref_task(self, refs_t, refs_p, cfg, now):
        root = self.rt.registry.root()
        skip = self.intake_tasks(cfg)
        found = set()
        for ref in refs_t:
            try:
                for path in list(root.glob(ref + '-*')) + [root / ref]:
                    if path.is_dir() and path.name not in skip:
                        found.add(path.name)
            except OSError:
                continue
        if len(found) == 1:
            return found.pop(), self.core.ticket_ref_reason
        if refs_p:
            index = self.pr_index(cfg, now)
            hits = set()
            for ref in refs_p:
                if len(index.get(ref, ())) == 1:
                    hits |= index[ref]
            if len(hits) == 1:
                return hits.pop(), 'pr_link'
        return None

    def route_burst(self, b, cfg, now):
        """(task_id, route, reason): bound conversation > ticket ref / PR link > oncall alert > <owner key>-inbox. P39: a NEW
        top-level DM / group-DM message: explicit ref > the task whose conversation window it falls in > <owner key>-inbox (new
        topic, or an explicit ref without a workspace). P46.3: an open ask no longer captures it (bound_task)."""
        if not b['smoke']:
            refs_t, refs_p = json.loads(b['task_refs'] or '[]'), json.loads(b['pr_refs'] or '[]')
            dm = b['conv_type'] in ('im', 'mpim') and dm_routing(cfg)
            if dm and b['thread_root']:
                bound = self.bound_task(b['channel'], b['thread_root'], b['conv_type'], now, b['sender'])
                if bound:
                    return bound[0], 'bound', bound[1]
            if dm:  # a new top-level message, or a reply under a message no task binds (e.g. the task's own top-level post)
                ref = self.ref_task(refs_t, refs_p, cfg, now)
                if ref:
                    return ref[0], 'ref', ref[1]
                if refs_t:  # names a ticket no workspace has yet: <owner key>-inbox creates it, the window never takes it
                    return cfg['inbox_task'], 'inbox', 'dm_unresolved_ref'
                claims = self.dm_claims(b['channel'], b['sender'], now, cfg, after=float(b['first_ts']))
                if len(claims) == 1:
                    task_id, reason = next(iter(claims.items()))
                    return task_id, 'bound', reason
                return cfg['inbox_task'], 'inbox', 'dm_conflict' if claims else 'dm_new_topic'
            bound = self.bound_task(b['channel'], b['thread_root'], b['conv_type'], now, b['sender'])
            if bound:
                return bound[0], 'bound', bound[1]
            ref = self.ref_task(refs_t, refs_p, cfg, now)
            if ref:
                return ref[0], 'ref', ref[1]
        if b['alert_fp']:
            return cfg['oncall_task'], 'oncall', 'oncall_alert'
        return cfg['inbox_task'], 'inbox', 'default_inbox'

    def _live_task(self, task_id):
        return bool(task_id) and self.rt.registry.task_dir(task_id).is_dir() and \
            not self.rt.card_threads.task_closed(self.rt, self.db, task_id)

    def dm_claims(self, channel, sender, now, cfg, after=None):
        """P39 rules 4-6: {task_id: reason} of the tasks that may capture a NEW top-level message of `sender` in this
        DM / group DM: the conversation window (the task that last wrote here within intake.dm_conversation_minutes,
        `dm_conversation`). Closed tasks never capture. P46.3: an in-flight ask to that person (`dm_inflight:watch` /
        `dm_inflight:ask_card`) no longer captures the whole DM; its answer comes in its own thread or by its buttons."""
        claims = {}
        minutes = cfg.get('dm_conversation_minutes', 30)
        if minutes:
            for r in self.db.execute('SELECT task_id, peer, last_at FROM dm_windows WHERE channel=? AND released_at IS NULL '
                                     'AND last_at>=? ORDER BY last_at DESC', (channel, now - minutes * 60)).fetchall():
                if after is not None and not float(r['last_at']) < after <= float(r['last_at']) + minutes * 60:
                    break  # the person wrote BEFORE this task did (or long after): not a reply in its conversation
                if r['task_id'] in self.intake_tasks(cfg):
                    break  # <owner key>-inbox's own conversation: routed as an inbox DM (with the person's task hint)
                if r['peer'] and sender and r['peer'] != sender and not channel.startswith('D'):
                    continue  # a group DM window holds only for the person the task answered
                if self._live_task(r['task_id']):
                    claims.setdefault(r['task_id'], 'dm_conversation')
                break  # rule 4: one window per DM — the task that wrote most recently
        return claims

    def person_tasks(self, channel, sender, now):
        """P39: the hint for <owner key>-inbox on a new top-level DM — this person's current tasks (request bindings, ask
        cards, watches naming them, recent conversations here), newest first, ids / titles / states only."""
        if not sender or not USER_RE.fullmatch(str(sender)):
            return []
        found = {}

        def add(task_id, why, at):
            if not task_id or not self.rt.registry.task_dir(task_id).is_dir():
                return
            item = found.setdefault(task_id, {'task_id': task_id, 'why': [], 'at': 0.0})
            if why not in item['why']:
                item['why'].append(why)
            item['at'] = max(item['at'], float(at or 0))
        for r in self.db.execute('SELECT task_id, created_at FROM intake_bindings WHERE peer=? AND error IS NULL AND '
                                 '(expires_at IS NULL OR expires_at>?)', (sender, now)).fetchall():
            add(r['task_id'], 'request', r['created_at'])
        for r in self.db.execute('SELECT task_id, last_at FROM dm_windows WHERE channel=? AND released_at IS NULL AND '
                                 'last_at>=?', (channel, now - DM_BINDING_SECONDS)).fetchall():
            add(r['task_id'], 'conversation', r['last_at'])
        for w in self.running_watches():
            if sender in (w.get('peers') or []) and w.get('kind') == 'peer':
                add(self.rt.registry.task_id_for_cwd(w.get('cwd')), 'watching', w.get('started'))
        for r in self.db.execute("SELECT watch_id, responders, created_at FROM ask_cards WHERE state IN ('sending','open') "
                                 'AND dry=0').fetchall():
            if sender in json.loads(r['responders'] or '[]') and r['watch_id']:
                try:
                    add(self.rt.registry.task_id_for_cwd(self.rt.load(self.db, r['watch_id']).get('cwd')), 'ask_card',
                        r['created_at'])
                except Exception:  # noqa: BLE001
                    continue
        out = []
        for item in sorted(found.values(), key=lambda x: -x['at'])[:RELATED_TASKS_MAX]:
            tid = item['task_id']
            linear = self.db.execute('SELECT state FROM task_linear_reads WHERE task_id=?', (tid,)).fetchone()
            out.append({'task_id': tid, 'title': self.rt.registry.read_title(self.rt.registry.task_dir(tid)),
                        'why': item['why'], 'closed': self.rt.card_threads.task_closed(self.rt, self.db, tid),
                        'linear_state': linear[0] if linear else None})
        return out

    def open_asks(self, sender, now):
        """P46.3 (R-0012): the tasks waiting on this person, newest first: an open ask card to them (`ask_card`, with its
        question), an in-flight peer watch on them (`watch`, before its fixed deadline) and an agent post as the owner / bot
        that @-mentioned them in the last OPEN_ASK_DAYS days (`mention`, agent_sends.mentions). Closed tasks wait on
        nobody. Metadata only; intake-context reads a post's first line live."""
        if not sender or not USER_RE.fullmatch(str(sender)):
            return []
        found = {}

        def add(task_id, kind, channel, ts, thread, since, asked=None):
            key = (task_id, channel, thread or ts)
            if self._live_task(task_id) and (asked or since > found.get(key, {}).get('since', -1)):
                found[key] = {'task_id': task_id, 'kind': kind, 'channel': channel, 'ts': ts, 'thread': thread,
                              'since': float(since), 'asked': asked}
        for w in self.running_watches():
            if w.get('kind') == 'peer' and w.get('fixed_deadline') and now < float(w['fixed_deadline']) and \
                    sender in (w.get('peers') or []):
                add(self.rt.registry.task_id_for_cwd(w.get('cwd')), 'watch', w['channel'], w.get('thread') or w['since'],
                    w.get('thread'), float(w['since']))
        for r in self.db.execute('SELECT channel, ts, created, mentions FROM agent_sends WHERE created>? AND mentions '
                                 'LIKE ?', (now - OPEN_ASK_DAYS * 86400, f'%"{sender}"%')):
            record = json.loads(r['mentions'])
            if sender in record.get('users', ()):
                add(record.get('task'), 'mention', r['channel'], r['ts'], None, r['created'])
        for r in self.db.execute("SELECT * FROM ask_cards WHERE state='open' AND dry=0 AND smoke=0 AND expire_at>? AND ts IS "
                                 'NOT NULL AND watch_id IS NOT NULL', (now,)).fetchall():
            if sender in json.loads(r['responders'] or '[]'):
                add(self._watch_task(r['watch_id']), 'ask_card', r['channel'], r['ts'], r['thread'], r['created_at'],
                    first_line(r['question']))
        return sorted(found.values(), key=lambda x: -x['since'])[:OPEN_ASKS_MAX]

    def release_closed(self, now):
        """P39 rule 5: a task that closed (terminal marker / Linear Done-Canceled / archived / workspace gone) no
        longer holds anything of a person's DM: its request bindings expire, its conversation windows are released and
        its idle DM peer watches with nothing unhandled stop (stop_reason task_closed). Returns the released tasks."""
        tasks = {r[0] for r in self.db.execute('SELECT DISTINCT task_id FROM intake_bindings WHERE error IS NULL AND '
                                               '(expires_at IS NULL OR expires_at>?)', (now,))}
        tasks |= {r[0] for r in self.db.execute('SELECT DISTINCT task_id FROM dm_windows WHERE released_at IS NULL')}
        dm_watches = [w for w in self.running_watches() if w.get('kind') == 'peer' and
                      (str(w.get('channel') or '').startswith('D') or w.get('dm'))]
        for w in dm_watches:
            tasks.add(self.rt.registry.task_id_for_cwd(w.get('cwd')))
        try:
            config = self.config()
            skip = self.rt.worker_config.long_lived_ids(config)
        except ValueError:
            return []
        released = []
        for task_id in sorted(t for t in tasks if t and t not in skip):
            if not self.rt.card_threads.task_closed(self.rt, self.db, task_id, config):
                continue
            with self.rt.transaction(self.db):
                self.db.execute("UPDATE intake_bindings SET expires_at=?,error='released_task_closed' WHERE task_id=? AND "
                                'error IS NULL AND (expires_at IS NULL OR expires_at>?)', (now, task_id, now))
                self.db.execute("UPDATE dm_windows SET released_at=?,release_reason='task_closed' WHERE task_id=? AND "
                                'released_at IS NULL', (now, task_id))
                for w in dm_watches:
                    if self.rt.registry.task_id_for_cwd(w.get('cwd')) != task_id or self.db.execute(
                            "SELECT 1 FROM events WHERE watch_id=? AND status!='handled' LIMIT 1", (w['id'],)).fetchone():
                        continue
                    current = self.rt.load(self.db, w['id'])
                    if current.get('state') == 'running':
                        current.update(state='stopped', stop_reason='task_closed', stopped_at=now)
                        self.rt.save(self.db, current)
            released.append(task_id)
        if released:
            self._watch_cache = None
        return released

    def deliver(self, b, task_id, route, reason, cfg, now):
        rt = self.rt
        path = rt.registry.task_dir(task_id)
        if not path.is_dir():
            return self._waiting(b, task_id, route, reason, 'task_missing', now)
        with rt.transaction(self.db):
            rt.registry.ensure_task(self.db, task_id, now)
            self.db.execute("UPDATE tasks SET state='active',title=coalesce(title,?),updated_at=? WHERE task_id=? AND "
                            "state='missing'", (rt.registry.read_title(path), now, task_id))
        row = self.db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if not row['current_session']:
            return self._waiting(b, task_id, route, reason, 'no_current_session', now)
        role = rt.worker_config.intake_role({'intake': cfg}, task_id)
        refs_file = self.write_refs_file(path, b, task_id, route, reason, role, now, cfg)
        env = json.loads(b['envelope']) if b['envelope'] else rt.envelopes.envelope(
            'slack_message', 'human', 'colleague', 'none', 'intake_default', b['thread_root'] or b['first_ts'])
        env['burst_id'] = b['burst_id']
        sender = 'slack:' + str(b['sender'] or 'unknown')
        with rt.transaction(self.db):
            w = rt.ensure_inbox_watch(self.db, task_id, str(path), row['current_provider'], row['current_session'], now)
            stamp = f'{now:.6f}'
            digest = hashlib.sha256(json.dumps({'local': 'intake', 'path': str(refs_file)}, sort_keys=True).encode()).hexdigest()
            eid = hashlib.sha256(f'{w["id"]}/{rt.LOCAL_CHANNEL}/{stamp}/{digest}'.encode()).hexdigest()[:24]
            self.db.execute('INSERT INTO events (id,watch_id,channel,ts,author,digest,received,status) VALUES (?,?,?,?,?,?,?,?)',
                            (eid, w['id'], rt.LOCAL_CHANNEL, stamp, sender, digest, now, 'pending'))
            self.db.execute('INSERT OR IGNORE INTO event_sources VALUES (?,?)', (eid, stamp))
            rt.envelopes.store(self.db, eid, env, now)
            self.db.execute('INSERT INTO local_messages VALUES (?,?,?,?,?,?)', (eid, 'intake', task_id, str(refs_file), sender, now))
            self.db.execute("UPDATE intake_bursts SET state='routed',route=?,route_reason=?,target_task=?,event_id=?,refs_file=?,"
                            'routed_at=?,updated_at=?,error=NULL WHERE burst_id=?',
                            (route, reason, task_id, eid, str(refs_file), now, now, b['burst_id']))
            # P39 rule 5: the other person's messages never extend a binding (pre-P39 a whole-DM binding slid 48 h on each
            # of them).
        # P3.3: the conversation plane does NOT react at ingest. An incoming message is reacted to (✅ handled + provider)
        # only when the agent actually replies to that person as the owner — see _post_as_<owner key> / reply_receipt — so a message
        # that the agent only cards the owner about, logs, or the owner skips, never gets a misleading emoji.
        return 'routed'

    def _waiting(self, b, task_id, route, reason, why, now):
        with self.rt.transaction(self.db):
            self.db.execute("UPDATE intake_bursts SET state='waiting',route=?,route_reason=?,target_task=?,error=?,updated_at=? "
                            'WHERE burst_id=?', (route, reason, task_id, why, now, b['burst_id']))
        return 'waiting:' + why

    def write_refs_file(self, task_path, b, task_id, route, reason, role, now, cfg):
        cli = f'python3 {Path(self.rt.__file__).resolve()} --db {self.db_path}'
        refs = json.loads(b['refs'] or '[]')
        root = b['thread_root']
        where = {'im': self.core.msg('intake.Intake.write_refs_file.im'), 'mpim': self.core.msg('intake.Intake.write_refs_file.mpim'), 'channel': self.core.msg('intake.Intake.write_refs_file.channel')}.get(b['conv_type'], b['conv_type'])
        g = guards(b['person_style'], None if b['auto_execute'] is None else bool(b['auto_execute']))
        via = json.loads(b['envelope'] or '{}').get('via')  # P42.1: a person's own app posting as them
        lines = [f'# Intake burst {b["burst_id"]} (refs only, no message bodies)', '',
                 f'- burst: `{b["burst_id"]}`' + (' · 🧪 TEST smoke-intake' if b['smoke'] else ''),
                 f'- route: {route} ({reason}) → task `{task_id}`',
                 f'- conversation: {where} `{b["channel"]}`' + (f' · thread `{root}`' if root else ' · top level'),
                 f'- sender: `{b["sender"]}` ({b["sender_kind"]})' + (f' · via `{via}` (their own app)' if via else ''),
                 f'- handling: request_style={b["person_style"] or "standard"} · auto_execute='
                 f'{"unset" if b["auto_execute"] is None else bool(b["auto_execute"])}'
                 + (f' · **strategic directive: never auto-execute, never auto-reply — brief {self.core.name} only (code-enforced)**'
                    if g['strategic'] else f' · **auto_execute=false: no task launch without {self.core.name} (code-enforced)**'
                    if not g['may_auto_execute'] else ''),
                 f"- messages: {b['msg_count']} · first {self.core.local_iso(b['first_ts'])} · last {self.core.local_iso(b['last_ts'])}"]
        for ts in refs[:REFS_CAP]:
            lines.append(f"  - `{ts}` {(self.core.permalink(b['channel'], ts, root) if not b['smoke'] else '(synthetic)')}")
        if json.loads(b['task_refs'] or '[]') or json.loads(b['pr_refs'] or '[]'):
            lines.append(f'- refs: tasks {", ".join(json.loads(b["task_refs"] or "[]")) or "-"} · PRs '
                         f'{", ".join(json.loads(b["pr_refs"] or "[]")) or "-"}')
        if b['alert_fp']:
            lines.append(f'- alert: fingerprint `{b["alert_fp"]}` · service `{b["alert_service"]}` · status '
                         f'{b["alert_state"] or "?"} · repeats are merged for the dedupe window')
        lines += ['', f'Read the messages (live from Slack, never stored): `{cli} intake-context --burst {b["burst_id"]}`']
        if role == 'inbox':
            lines.append('Triage per goal.md; record it with `triage-record --burst ...`; then resolve this event.')
            if route == 'inbox' and reason in INBOX_DM_REASONS:
                why = {'dm_new_topic': self.core.msg('intake.Intake.write_refs_file.dm_new_topic'),
                       'dm_conflict': self.core.msg('intake.Intake.write_refs_file.dm_conflict'),
                       'dm_unresolved_ref': self.core.msg('intake.Intake.write_refs_file.dm_unresolved_ref')}[reason]
                lines += ['', self.core.msg('intake.Intake.write_refs_file.1', why=why), '']
                related = self.person_tasks(b['channel'], b['sender'], now)
                for t in related:
                    lines.append(f'- `{t["task_id"]}` {t["title"] or ""} · {"/".join(t["why"])}'
                                 + (f' · {self.core.tracker.display} {t["linear_state"]}' if t['linear_state'] else '')
                                 + (self.core.msg('intake.Intake.write_refs_file.2') if t['closed'] else ''))
                if not related:
                    lines.append(self.core.msg('intake.Intake.write_refs_file.3'))
                lines += ['', self.core.msg('intake.Intake.write_refs_file.4', cli=cli)]
            asks = self.open_asks(b['sender'], now) if b['sender_kind'] == 'human' and not b['smoke'] and \
                self.rt.rule_registry.active('inbox.open_asks') else []
            if asks:  # P46.3 (R-0012): metadata only; intake-context shows what each one asked
                lines += ['', self.core.msg('intake.Intake.write_refs_file.5'), '']
                lines += [self.core.msg('intake.Intake.write_refs_file.6', task_id=a['task_id'], value=self.core.ask_kind_zh[a['kind']], permalink=self.core.permalink(a['channel'], a['ts'], a['thread']), local_iso=self.core.local_iso(a['since'])) for a in asks]
                lines += ['', self.core.msg('intake.Intake.write_refs_file.7', cli=cli)]
        elif role == 'oncall':
            lines.append('Oncall policy per goal.md (read-only investigation first); `triage-record --burst ...`; resolve.')
        else:
            lines.append('A new message in a conversation bound to this task (or naming it). Continue the task within its '
                         'authorization; reply in that thread if needed; resolve this event.')
            rule = self.rt.rule_registry.active('send.relevance_ack')
            handed = rule and self.core.handed_as_answer(self.rt, self.db, b, task_id, cfg, now, route, reason)
            window = rule and not handed and by_window(self.db, b, reason)
            if rule and (handed or window or str(reason).startswith('ask_thread:')):  # P46.3 (R-0013)
                lines += ['', '## Why this reached you (R-0013)', '',
                          (f'{self.core.key}-inbox handed it to you as a possible answer to your ask to this person'
                           if handed else self.core.window_why[0].upper() + self.core.window_why[1:] if window else
                           'It is a reply in the thread of your ask card') +
                          ' (intake-context: why_you_got_it). ' + rule['statement'] + ' ' + rule['enforcement']['hint']
                          + f' Hand back: `{cli} handoff --event <this event id> --task {cfg["inbox_task"]}`.']
        if b['smoke']:
            lines += ['', '## 🧪 TEST content (synthetic, not a Slack message)', '', self.core.smoke_text.get(b['smoke_scenario'] or
                                                                                                  'request', '')]
        directory = Path(task_path) / 'history' / 'intake'
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f'{utc_stamp(now)}-{b["burst_id"]}.md'
        n = 2
        while target.exists():
            target = directory / f'{utc_stamp(now)}-{b["burst_id"]}-{n}.md'
            n += 1
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write('\n'.join(lines) + '\n')
        return target

    # ---- bindings -> real watches once the task has a session ----
    def materialize_bindings(self, now):
        rows = self.db.execute('SELECT * FROM intake_bindings WHERE watch_id IS NULL AND want_watch=1 AND error IS NULL AND '
                               '(expires_at IS NULL OR expires_at>?) LIMIT 20', (now,)).fetchall()
        done = []
        for binding in rows:
            task = self.db.execute('SELECT * FROM tasks WHERE task_id=?', (binding['task_id'],)).fetchone()
            if task is None or not task['current_session']:
                continue
            done.append(self.bind_watch(binding, task['current_provider'], task['current_session'], task['path'], now))
        return done

    def bind_watch(self, binding, provider, session, task_path, now):
        rt = self.rt
        channel, root = binding['channel'], binding['thread_root'] or None
        peers = [binding['peer']] if binding['peer'] and USER_RE.fullmatch(binding['peer']) else []
        try:
            if legacy_whole_dm_key(binding['thread_root']):
                raise ValueError('p39_whole_dm_binding_run_dm-bindings-migrate')  # never again a whole-DM watch
            if not peers:
                raise ValueError('binding_has_no_human_peer')
            since = float(binding['since'] or now)
            # F06: a reply intake already took in this thread (routed by this binding before the watch existed) is
            # never delivered a second time by the new watch.
            taken = self.db.execute("SELECT max(CAST(last_ts AS REAL)) FROM intake_bursts WHERE channel=? AND "
                                    "thread_root=? AND state IN ('open','sealed','waiting','routed')",
                                    (channel, binding['thread_root'])).fetchone()[0]
            since = max(since, float(taken or 0))
            kw = dict(session=session, provider=provider, cwd=task_path, channel=channel, thread=root, peers=peers,
                      mode='waiting', since=f'{since:.6f}', lifecycle='idle', now=now)
            w = rt.register(self.db, **kw)
            with rt.transaction(self.db):
                self.db.execute('UPDATE intake_bindings SET watch_id=? WHERE channel=? AND thread_root=?',
                                (w['id'], channel, binding['thread_root']))
            return w['id']
        except Exception as exc:  # noqa: BLE001 - routing by binding still works without the watch
            with rt.transaction(self.db):
                self.db.execute('UPDATE intake_bindings SET error=? WHERE channel=? AND thread_root=?',
                                ((str(exc) if isinstance(exc, ValueError) else type(exc).__name__)[:120], channel,
                                 binding['thread_root']))
            return None


def _cfg(rt):
    return rt.worker_config.load()[0]


def burst(db, ident):
    text = str(ident or '').strip()
    if BURST_ID_RE.fullmatch(text):
        row = db.execute('SELECT * FROM intake_bursts WHERE burst_id=?', (text,)).fetchone()
    elif EVENT_ID_RE.fullmatch(text):
        row = db.execute('SELECT * FROM intake_bursts WHERE event_id=?', (text,)).fetchone()
    else:
        raise ValueError('burst_must_be_a_burst_id_or_its_event_id')
    if row is None:
        raise ValueError('unknown_burst')
    return row


def _handling(rt, b):
    """Burst guard facts: captured at burst creation, OR-ed with the registry now (the stricter one wins)."""
    style, auto = b['person_style'] or 'standard', None if b['auto_execute'] is None else bool(b['auto_execute'])
    if b['sender'] and USER_RE.fullmatch(str(b['sender'])):
        now = load_handling(rt.envelopes.people_path()).get(b['sender'], {})
        if now.get('request_style') == 'strategic_directive':
            style = 'strategic_directive'
        if now.get('auto_execute') is False:
            auto = False
    return style, auto, guards(style, auto)


def binding_key(conv_type, thread_root, first_ts, sender):
    """intake_bindings.thread_root for a conversation: the request's thread — its thread, or (a top-level DM / group
    DM message, a channel request) the thread under the request message itself (its first ts). P39: never a whole DM
    ('' — pre-P39 im) or one sender's whole group-DM top level ('u:<sender>' — pre-P39 mpim); those legacy keys are
    converted by `dm-bindings-migrate`."""
    return thread_root or first_ts


def legacy_whole_dm_key(key):
    return key == '' or str(key or '').startswith('u:')


def _reply_thread(b):
    if b['thread_root']:
        return b['thread_root']
    if b['conv_type'] in ('im', 'mpim'):
        return None
    return b['first_ts']  # a channel top-level request (mention, alert): answer in its thread


def handed_by_intake(route, reason, cfg):
    """P46.3: <owner key>-inbox / <owner key>-oncall handed this burst to its task (`handoff`)."""
    return route == 'handoff' and reason in (f'handoff_from:{cfg["inbox_task"]}', f'handoff_from:{cfg["oncall_task"]}')


def ask_thread_card(db, channel, root, watch_id=None):
    """P46.3: the latest ask card whose thread this is (its own thread, or the one it was posted in; optionally of this
    watch), else None."""
    return db.execute("SELECT * FROM ask_cards WHERE channel=? AND coalesce(thread,ts)=? AND dry=0 AND smoke=0 AND "
                      "state!='failed' AND watch_id=coalesce(?,watch_id) ORDER BY created_at DESC",
                      (channel, root, watch_id)).fetchone()


def by_window(db, b, reason=None):
    """P46.3 (R-0013; orchestrator 2026-10-30): the burst reached its task only through the task's conversation window —
    a new top-level DM message, or a reply in a thread that is not one of our agents' posts (the owner's own chat, the
    person's own message). A reply under an agent's post is in the task's thread: unchanged."""
    root = b['thread_root']
    return (reason or b['route_reason']) == 'dm_conversation' and (not root or not db.execute(
        "SELECT 1 FROM agent_sends WHERE channel=? AND ts=? UNION ALL SELECT 1 FROM intake_seen WHERE channel=? AND ts=? "
        "AND reason='our_agent' LIMIT 1", (b['channel'], root, b['channel'], root)).fetchone())


class AlreadyReplied(ValueError):
    """The burst already has a reply / ack as the owner that no later message of the requester followed."""

    def __init__(self, prior):
        self.prior = prior
        parts = [f"{s['kind']} {s['state']} at {s['at']} by {s['by'] or 'an_earlier_run'}"
                 + (f" ts {s['ts']}" if s['ts'] else '') for s in prior[-3:]]
        super().__init__(f'{AGAIN_ERROR}: ' + '; '.join(parts))


def _public_sends(sends):
    return [{k: s[k] for k in ('kind', 'state', 'at', 'ts', 'by')} for s in sends]


def _conversation(b):
    """SQL filter on `events e LEFT JOIN event_sources s` for the burst's conversation: its reply thread, else the DM /
    group-DM top level."""
    thread = _reply_thread(b)
    return ('e.channel=? AND s.root=?', (b['channel'], thread)) if thread else ('e.channel=?', (b['channel'],))


def _followups(db, b):
    """The requester's later messages a watch delivered in the burst's conversation (continuations that got no burst of
    their own), oldest first: {id, ts, top} (`top`: a DM / group-DM top-level message, not a thread reply). Only the
    requester's own messages count — never the owner's or another person's — and nothing after the requester's NEXT burst
    here (that burst has its own id and its own answer)."""
    thread, last = _reply_thread(b), float(b['last_ts'])
    nxt = db.execute("SELECT min(CAST(first_ts AS REAL)) FROM intake_bursts WHERE channel=? AND sender=? AND "
                     "burst_id!=? AND state IN ('open','sealed','waiting','routed') AND CAST(first_ts AS REAL)>? AND "
                     + ('thread_root=?' if thread else 'thread_root IS NULL'),
                     (b['channel'], b['sender'], b['burst_id'], last, *([thread] if thread else []))).fetchone()[0]
    where, args = _conversation(b)
    out = []
    for r in db.execute('SELECT e.id, e.ts, s.root FROM events e LEFT JOIN event_sources s ON s.event_id=e.id '
                        'WHERE e.author=? AND ' + where, (b['sender'], *args)):
        if TS_RE.fullmatch(str(r['ts'])) and float(r['ts']) > last and (nxt is None or float(r['ts']) < nxt):
            out.append({'id': r['id'], 'ts': float(r['ts']), 'top': r['root'] in (None, r['ts'])})
    return sorted(out, key=lambda f: f['ts'])


def _own_scope(b, task_id):
    """The task the burst was handed to (launch / forward / oncall incident) answers on its own: only ITS earlier
    sends make a repeat, not the triager's ack or reply before the handoff. For everyone else every reply / ack does
    (None)."""
    return task_id if task_id and task_id == b['handled_task'] and task_id != b['target_task'] else None


def _never_posted(exc):
    """P38 (F29): the post certainly never reached Slack — Slack refused it (ok:false, HTTP 4xx) or no connection was
    made (refused, DNS / name resolution, no route). Anything else (timeout, reset, 5xx, a killed run) may have
    landed."""
    if re.fullmatch(r'Slack API error: [a-z_]+', str(exc)):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return 400 <= exc.code < 500
    # Only a URLError's reason proves the request never left: urllib wraps connect-time failures; the same OSError
    # raised raw comes while reading the response, after Slack may already have posted.
    err = exc.reason if isinstance(exc, urllib.error.URLError) else None
    return isinstance(err, (ConnectionRefusedError, socket.gaierror)) or (
        isinstance(err, OSError) and err.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN))


def _bind(rt, db, b, task_id, who, now, want_watch=True, mark_handled=True):
    """Bind the burst's REQUEST to `task_id` (router rule 1) and close its not-yet-routed siblings in that thread.
    P39 rule 1: the request's own thread (root = the request message ts for a top-level DM / group-DM / channel
    message), never the whole DM; replies in it reach the task (a thread watch on it, and this binding once that watch
    is gone). Other top-level messages of the same person are routed one by one (route_burst) — F12: they are never
    marked covered here, so none is silently lost. No expiry: released when the task closes (Intake.release_closed)."""
    key = binding_key(b['conv_type'], b['thread_root'], b['first_ts'], b['sender'])
    peer = b['sender'] if b['sender'] and USER_RE.fullmatch(str(b['sender'])) else None
    watch = bool(want_watch and peer and not b['smoke'])
    with rt.transaction(db):
        db.execute('INSERT OR REPLACE INTO intake_bindings (channel,thread_root,task_id,burst_id,conv_type,peer,since,'
                   'want_watch,watch_id,error,created_at,created_by,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (b['channel'], key, task_id, b['burst_id'], b['conv_type'], peer, b['last_ts'], int(watch), None, None,
                    now, who, None))
        if watch:
            # Not-yet-routed bursts IN the request thread: the thread watch (since = this burst's last ts) delivers them.
            db.execute("UPDATE intake_bursts SET state='covered',error='bound_to_'||?,updated_at=? WHERE channel=? AND "
                       "thread_root=? AND state IN ('open','sealed','waiting') AND burst_id!=?",
                       (task_id, now, b['channel'], key, b['burst_id']))
        if mark_handled:
            db.execute('UPDATE intake_bursts SET handled_task=?,updated_at=? WHERE burst_id=?',
                       (task_id, now, b['burst_id']))
    return db.execute('SELECT * FROM intake_bindings WHERE channel=? AND thread_root=?', (b['channel'], key)).fetchone()


def _slugify(text):
    raw = re.sub(r'[^a-z0-9]+', '-', str(text).lower()).strip('-')
    return '-'.join([w for w in raw.split('-') if w][:10])


def _smoke_workspace(rt, title, brief, now):
    stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))
    path = rt.registry.smoke_root() / f'{SMOKE_TASK_PREFIX}{stamp}-{uuid.uuid4().hex[:4]}'
    path.mkdir(parents=True, mode=0o700)
    (path / 'history' / 'notes').mkdir(parents=True)
    (path / 'goal.md').write_text(f'# TEST {title}\n\nThrowaway workspace created by smoke-intake. Nothing here is real '
                                  'work; never touch anything outside this directory and never send Slack messages.\n\n'
                                  f'## Brief\n\n{brief}\n', encoding='utf-8')
    (path / 'status.md').write_text(f'<!-- rev: 1 · last-writer: smoke-intake · at: {rt.task_memory.utc_iso(now)} -->\n'
                                    '# Status\n\n- TEST workspace created by smoke-intake (intake-launch).\n',
                                    encoding='utf-8')
    for name in ('AGENTS.md', 'CLAUDE.md'):
        (path / name).write_text('# TEST workspace\n\nRead status.md. Only do what the brief asks. Never send Slack '
                                 'messages. Never touch files outside this directory.\n', encoding='utf-8')
    return path


def _conv_type(rt, db, channel):
    if str(channel).startswith('D'):
        return 'im'
    row = db.execute('SELECT conv_type FROM intake_convs WHERE channel=?', (channel,)).fetchone()
    return 'mpim' if row and row['conv_type'] == 'mpim' else 'channel'


def dm_bindings_migrate(rt, db, apply=False, now=None):
    """P39 install step: the pre-P39 whole-DM bindings (thread_root '' for a DM, 'u:<sender>' for one person's whole
    group-DM top level) become bindings of the REQUEST each was made for (its burst's thread, else the request message
    itself); their whole-DM watches stop when nothing is unhandled on them (stop_reason p39_request_binding — else they
    are left running: with DM routing on they no longer capture new top-level messages and expire 48 h after their
    task's last own message). The new request-thread watch starts from the migration time (nothing old is re-read).
    Other tasks' whole-DM watches are only reported. Dry run unless `apply`; one transaction; idempotent."""
    now = time.time() if now is None else now
    legacy = db.execute("SELECT * FROM intake_bindings WHERE (thread_root='' OR thread_root LIKE 'u:%') AND (error IS "
                        "NULL OR error='p39_whole_dm_binding_run_dm-bindings-migrate') ORDER BY channel, created_at").fetchall()
    watches = [json.loads(r[0]) for r in db.execute("SELECT data FROM watches WHERE json_extract(data,'$.state')='running'")]

    def occupying():
        pairs = set()
        for r in db.execute("SELECT task_id, channel FROM intake_bindings WHERE (thread_root='' OR thread_root LIKE "
                            "'u:%') AND error IS NULL AND (expires_at IS NULL OR expires_at>?)", (now,)):
            pairs.add((r['task_id'], r['channel']))
        for r in db.execute("SELECT data FROM watches WHERE json_extract(data,'$.state')='running'"):
            w = json.loads(r[0])
            if w.get('kind', 'peer') == 'peer' and ((str(w.get('channel')).startswith('D') and not w.get('thread')) or
                                                     w.get('dm')):
                pairs.add((rt.registry.task_id_for_cwd(w.get('cwd')), w.get('dm') or w.get('channel')))
        return sorted([t or '-', c] for t, c in pairs)
    report = {'ok': True, 'dry_run': not apply, 'at': now, 'occupying_before': occupying(), 'bindings': [],
              'other_whole_dm_watches': []}
    binding_watches = set()
    with rt.transaction(db):
        for r in legacy:
            b = db.execute('SELECT first_ts, thread_root FROM intake_bursts WHERE burst_id=?', (r['burst_id'],)).fetchone()
            new_key = (b['thread_root'] or b['first_ts']) if b else None
            item = {'channel': r['channel'], 'task_id': r['task_id'], 'peer': r['peer'], 'legacy_key': r['thread_root'],
                    'request_thread': new_key, 'watch_id': r['watch_id'], 'watch_action': None,
                    'binding_action': 'convert' if new_key else 'expire_no_request_found'}
            if r['watch_id']:
                binding_watches.add(r['watch_id'])
                row = db.execute('SELECT data FROM watches WHERE id=?', (r['watch_id'],)).fetchone()
                w = json.loads(row[0]) if row else None
                unhandled = db.execute("SELECT count(*) FROM events WHERE watch_id=? AND status!='handled'",
                                       (r['watch_id'],)).fetchone()[0] if w else 0
                item['watch_action'] = ('missing' if w is None else 'already_' + w['state'] if w['state'] != 'running'
                                        else 'left_running_unhandled_events' if unhandled else 'stop')
                if apply and item['watch_action'] == 'stop':
                    w.update(state='stopped', stop_reason='p39_request_binding', stopped_at=now)
                    rt.save(db, w)
            if apply:
                if new_key and not db.execute('SELECT 1 FROM intake_bindings WHERE channel=? AND thread_root=?',
                                              (r['channel'], new_key)).fetchone():
                    db.execute('INSERT INTO intake_bindings (channel,thread_root,task_id,burst_id,conv_type,peer,since,'
                               'want_watch,watch_id,error,created_at,created_by,expires_at) VALUES '
                               "(?,?,?,?,?,?,?,?,NULL,NULL,?,'p39_migration',NULL)",
                               (r['channel'], new_key, r['task_id'], r['burst_id'], r['conv_type'], r['peer'],
                                f'{now:.6f}', int(bool(r['peer']) and (r['expires_at'] is None or r['expires_at'] > now)),
                                now))
                db.execute("UPDATE intake_bindings SET expires_at=?,error='p39_migrated_to_request_thread' WHERE "
                           'channel=? AND thread_root=?', (now, r['channel'], r['thread_root']))
            report['bindings'].append(item)
        for w in watches:
            if w['id'] in binding_watches or w.get('kind', 'peer') != 'peer':
                continue
            if (str(w.get('channel')).startswith('D') and not w.get('thread')) or w.get('dm'):
                report['other_whole_dm_watches'].append({
                    'watch_id': w['id'], 'task_id': rt.registry.task_id_for_cwd(w.get('cwd')), 'dm': w.get('dm') or
                    w.get('channel'), 'peers': w.get('peers'), 'lifecycle': w.get('lifecycle'),
                    'fixed_deadline': w.get('fixed_deadline'), 'action': 'left_running'})
    report['occupying_after'] = occupying() if apply else None
    return report


def _card_question(b, first, body, limit=1500):
    """P3.1 copy standard: one plain sentence first (code), then the agent's text; the source link is in the footer."""
    head = ('🧪 TEST · ' if b['smoke'] else '') + first + '\n'
    body = str(body).strip()
    if len(head) + len(body) > limit:
        raise ValueError(f'card_body_too_long_max_{limit - len(head)}_characters')
    return head + body


class IntakeCore:
    """The owner-bound half of the intake: the owner's Slack user, bot and app, the workspace archive URL, the
    channel names, the ticket pattern and time zone; the owner's private notes and the send guard; the
    store, the request helpers and the owner-facing hints the engine (Intake, which holds one) uses; the
    commands in the owner's words and the deployment's workspace hook."""

    def __init__(self, language, *, tz, owner_key, owner_name, owner_slack_user, bot_user, app_id, archive_url, known_channels, ticket_ref_re, agent_event_type, ticket_ref_reason, run_id_env, workspace_bootstrap, owner_user, ask_owner, tracker=None):
        self.language = language
        self.tracker = tracker or _trackers.current()  # N2: the deployment's tracker (`kage.adapters.trackers`)
        self.tz = tz
        self.owner_slack_user = owner_slack_user
        self.bot_user = bot_user
        self.app_id = app_id
        self.archive_url = archive_url
        self.known_channels = known_channels
        self.ticket_ref_re = ticket_ref_re
        self.agent_event_type = agent_event_type
        self.ticket_ref_reason = ticket_ref_reason
        self.run_id_env = run_id_env
        self.workspace_bootstrap = workspace_bootstrap
        self.owner_user = owner_user
        self.ask_owner = ask_owner
        self.key, self.name = owner_key, owner_name
        self.mention_query = f'<@{self.owner_slack_user}>'
        self.dispositions = ('forward', 'launch', 'answer', 'ask_requester', 'brief', f'card_{self.key}', 'log', 'ignore',
                             'investigate', 'escalate')
        self.strategic_only = ('brief', 'log', 'ignore', f'card_{self.key}')     # strategic directives: never auto-execute or reply
        self.brief_options = [self.msg('intake.brief_options.1'), self.msg('intake.brief_options.2'), self.msg('intake.brief_options.3'), self.msg('intake.brief_options.4')]
        self.draft_options = [self.msg('intake.draft_options.1'), self.msg('intake.draft_options.2'), self.msg('intake.draft_options.3')]
        self.incident_options = [self.msg('intake.incident_options.1'), self.msg('intake.incident_options.2'), self.msg('intake.incident_options.3')]
        self.smoke_text = {
                 'request': (self.msg('intake.smoke_text.request')),
                 'strategic': (self.msg('intake.smoke_text.strategic')),
                 'alert': self.msg('intake.smoke_text.alert'),
             }
        self.ddl = f'''
  CREATE TABLE IF NOT EXISTS intake_seen (
    channel TEXT NOT NULL, ts TEXT NOT NULL, source TEXT NOT NULL, conv_type TEXT, thread_root TEXT, user TEXT,
    sender_kind TEXT, digest TEXT, reason TEXT NOT NULL, burst_id TEXT, seen_at REAL NOT NULL,
    PRIMARY KEY(channel, ts));
  CREATE INDEX IF NOT EXISTS intake_seen_time ON intake_seen(seen_at);
  CREATE TABLE IF NOT EXISTS intake_convs (
    channel TEXT PRIMARY KEY, conv_type TEXT NOT NULL, cursor TEXT, page_cursor TEXT, page_high TEXT,
    wanted_since REAL, last_read_at REAL, error TEXT, error_count INTEGER NOT NULL DEFAULT 0, next_at REAL);
  CREATE TABLE IF NOT EXISTS intake_fetch_queue (
    channel TEXT NOT NULL, ts TEXT NOT NULL, thread_root TEXT, conv_type TEXT, source TEXT NOT NULL,
    queued_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(channel, ts));
  CREATE TABLE IF NOT EXISTS intake_bursts (
    burst_id TEXT PRIMARY KEY, conv_key TEXT NOT NULL, channel TEXT NOT NULL, conv_type TEXT, thread_root TEXT,
    sender TEXT, sender_kind TEXT, first_ts TEXT NOT NULL, last_ts TEXT NOT NULL, msg_count INTEGER NOT NULL,
    refs TEXT NOT NULL, task_refs TEXT, pr_refs TEXT, alert_fp TEXT, alert_service TEXT, alert_state TEXT,
    dup_count INTEGER NOT NULL DEFAULT 0, dup_last_ts TEXT, envelope TEXT, person_style TEXT, auto_execute INTEGER,
    state TEXT NOT NULL, route TEXT, route_reason TEXT, target_task TEXT, handled_task TEXT, event_id TEXT,
    refs_file TEXT, smoke INTEGER NOT NULL DEFAULT 0, smoke_scenario TEXT, created_at REAL NOT NULL, sealed_at REAL,
    routed_at REAL, updated_at REAL NOT NULL, error TEXT);
  CREATE INDEX IF NOT EXISTS intake_bursts_state ON intake_bursts(state, conv_key);
  CREATE INDEX IF NOT EXISTS intake_bursts_alert ON intake_bursts(alert_fp, created_at);
  CREATE INDEX IF NOT EXISTS intake_bursts_event ON intake_bursts(event_id);
  CREATE INDEX IF NOT EXISTS intake_bursts_time ON intake_bursts(created_at);
  CREATE TABLE IF NOT EXISTS intake_bindings (
    channel TEXT NOT NULL, thread_root TEXT NOT NULL, task_id TEXT NOT NULL, burst_id TEXT, conv_type TEXT,
    peer TEXT, since TEXT, want_watch INTEGER NOT NULL DEFAULT 1, watch_id TEXT, error TEXT,
    created_at REAL NOT NULL, created_by TEXT, expires_at REAL, PRIMARY KEY(channel, thread_root));
  CREATE TABLE IF NOT EXISTS intake_briefs (
    card_event TEXT PRIMARY KEY, burst_id TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT, session TEXT,
    task_id TEXT, created_at REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS triage_records (
    id TEXT PRIMARY KEY, burst_id TEXT, event_id TEXT, sender TEXT, channel TEXT, intent TEXT NOT NULL,
    clarity TEXT NOT NULL, disposition TEXT NOT NULL, target_task TEXT, confidence REAL, basis TEXT,
    needs_{self.key} INTEGER NOT NULL DEFAULT 0, person_style TEXT, note TEXT, recorded_by TEXT, created_at REAL NOT NULL,
    correction TEXT, corrected_by TEXT, corrected_at REAL, smoke INTEGER NOT NULL DEFAULT 0);
  CREATE INDEX IF NOT EXISTS triage_records_burst ON triage_records(burst_id);
  CREATE INDEX IF NOT EXISTS triage_records_time ON triage_records(created_at);
  -- P38 (F29): every reply / ack AS {self.name.upper()} to a burst, reserved BEFORE the post (sending -> sent | delivery_unknown; a
  -- definite non-delivery deletes it), so a fresh session, a crash or an unknown delivery never answers twice.
  -- `horizon` = the newest Slack ts of the conversation the sending session could have seen (see _horizon).
  CREATE TABLE IF NOT EXISTS intake_sends (
    id TEXT PRIMARY KEY, burst_id TEXT NOT NULL, kind TEXT NOT NULL, task_id TEXT, actor TEXT, channel TEXT, ts TEXT,
    state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, horizon REAL);
  CREATE INDEX IF NOT EXISTS intake_sends_burst ON intake_sends(burst_id, kind);
  -- P38 (F29): the newest message a session read live for a burst (intake-context / intake-reconcile), never a body.
  CREATE TABLE IF NOT EXISTS intake_reads (
    burst_id TEXT NOT NULL, actor TEXT NOT NULL, seen_ts REAL NOT NULL, read_at REAL NOT NULL,
    PRIMARY KEY(burst_id, actor));
  -- P39 rule 4/5: the conversation window — when a task last wrote (as {self.name}) into a DM / group DM. Only the task's OWN
  -- messages open or extend it (poll_schedule.note_window); `peer` = whom it answered (NULL = the whole conversation);
  -- released when the task closes.
  CREATE TABLE IF NOT EXISTS dm_windows (
    channel TEXT NOT NULL, task_id TEXT NOT NULL, peer TEXT, last_ts TEXT NOT NULL, last_at REAL NOT NULL, kind TEXT,
    released_at REAL, release_reason TEXT, PRIMARY KEY(channel, task_id));
  CREATE INDEX IF NOT EXISTS dm_windows_recent ON dm_windows(channel, last_at);
'''
        self.ask_kind_zh = {'ask_card': self.msg('intake.ask_kind_zh.ask_card'), 'watch': self.msg('intake.ask_kind_zh.watch'), 'mention': self.msg('intake.ask_kind_zh.mention')}
        self.window_why = (f"it reached you only through your conversation window (you posted in this DM in the last minutes) and is not in a thread of yours: it may be {self.name}'s own chat with this person")
        self.where_zh = {'im': self.msg('intake.where_zh.im'), 'mpim': self.msg('intake.where_zh.mpim'), 'channel': self.msg('intake.where_zh.channel')}

    def msg(self, key, **values):
        return locale.tracker_message(self.language, key, self.tracker, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def initialize(self, db):
        db.executescript(self.ddl)
        # P38 (F29): an intake_sends table created before `horizon` existed gets it here (additive, idempotent, race-safe).
        if 'horizon' not in {r[1] for r in db.execute('PRAGMA table_info(intake_sends)')}:
            try:
                db.execute('ALTER TABLE intake_sends ADD COLUMN horizon REAL')
            except Exception as exc:  # noqa: BLE001 - another process added it first
                if 'duplicate column' not in str(exc):
                    raise
            db.commit()

    def resolve_channels(self, entries):
        """(ids, unresolved): channel IDs pass through, known `#names` map to IDs, anything else is reported."""
        ids, unresolved = [], []
        for entry in entries or []:
            value = self.known_channels.get(str(entry).lower(), entry)
            if CHANNEL_ID_RE.fullmatch(str(value)):
                if value not in ids:
                    ids.append(value)
            else:
                unresolved.append(entry)
        return ids, unresolved

    def permalink(self, channel, ts, root=None):
        url = f"{self.archive_url}/{channel}/p{str(ts).replace('.', '')}"
        return url + (f'?thread_ts={root}&cid={channel}' if root and root != ts else '')

    def task_refs(self, text):
        return sorted({ref.upper() for ref in self.ticket_ref_re.findall(str(text or ''))})

    def local_iso(self, ts):
        try:
            return dt.datetime.fromtimestamp(float(ts), self.tz).isoformat(timespec='seconds')
        except (TypeError, ValueError, OSError):
            return None

    def require_language(self, rt, db, user_id, texts, *, words=(), what='message', lookup=False):
        """The send guard (safety net), before any post or state change. Refused: mostly Chinese to someone whose latest
        message here has no Han or is clearly Latin; no Han to someone whose message is mostly Chinese; a clear Latin language
        other than their clear one. Not written here: Chinese to someone who reads none; no Han to a Chinese-only reader.
        Too little text (a link, a command) is never refused. Returns the reader's (langs, share), or (None, None).
        P47: the language part is R-0009 (send.reply_language); first, R-0011 (send.<owner key>_notes) refuses a text that copies
        the owner's private notes about this person."""
        quoted = user_id != self.owner_slack_user and rt.rule_registry.active(f'send.{self.key}_notes') and self.quoted_notes(rt, user_id, texts)
        if quoted:
            rt.rule_registry.refuse(f'send.{self.key}_notes', f"{self.key}_notes_quoted: this {what} to {person_name(rt, db, user_id) or user_id} copies a phrase of {self.name}'s private notes about them ({quoted} characters)")
        outs = [m for m in (text_language(rt, t) for t in texts if t) if m[0]]
        if user_id == self.owner_slack_user or not USER_RE.fullmatch(str(user_id or '')) or not (outs or lookup):
            return None, None
        langs, why, name, share = reply_language(rt, db, user_id, words)
        their = min(langs) if langs and share is not None else None
        for out, out_share in outs if langs else ():
            if share is None:  # they have not written here: what they read
                bad = (out_share >= ZH_SHARE and 'zh' not in langs) or (not out_share and langs == {'zh'})
            else:
                bad = (out_share >= ZH_SHARE and (not share or their in LATIN)) or (not out_share and share >= ZH_SHARE) \
                    or (out in LATIN and their in LATIN and out != their)
            if bad and rt.rule_registry.active('send.reply_language'):
                raise ValueError(f'reply_in_their_language: {name or user_id}: {why}; rewrite this {what} in {_in(langs)}')
        return langs, share

    def owner_notes(self, rt, user_id):
        """the owner's notes about one person (`<owner key>_view`: summary_zh, traits, normalized_note), [] when none. Read only here;
        the handling cache, envelopes and the rule engine still never see <owner key>_view."""
        path = rt.envelopes.people_path()
        try:
            st = path.stat()
        except OSError:
            return []
        key = (str(path), st.st_mtime_ns, st.st_size)
        if _NOTES_CACHE.get('key') != key:
            out = {}
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                raw = {}
            for person in (raw.get('people') if isinstance(raw, dict) else None) or []:
                view = person.get(f'{self.key}_view') if isinstance(person, dict) else None
                if not isinstance(view, dict):
                    continue
                traits = view.get('traits') if isinstance(view.get('traits'), list) else []
                notes = [str(x).strip() for x in (view.get('summary_zh'), *traits, view.get('normalized_note'))
                         if isinstance(x, str) and x.strip()]
                for ident in person.get('identities') or []:
                    if notes and isinstance(ident, dict) and ident.get('type') == 'slack':
                        out[str(ident.get('value'))] = notes
            _NOTES_CACHE.clear()
            _NOTES_CACHE.update(key=key, value=out)
        return _NOTES_CACHE['value'].get(user_id, [])

    def notes_line(self, rt, user_id):
        """The background line for intake-context / context, only while R-0011 (the send check that protects it) is on."""
        notes = self.owner_notes(rt, user_id) if rt.rule_registry.active(f'send.{self.key}_notes') else []
        text = f"{self.name}'s notes (background only; never quote or reveal): " + ' · '.join(notes)
        return (text[:NOTES_MAX - 1] + '…' if len(text) > NOTES_MAX else text) if notes else None

    def quoted_notes(self, rt, user_id, texts):
        """Length of the first verbatim fragment of the owner's notes about this person in `texts` (after NFKC, case folding and
        dropping spaces / punctuation; at least NOTE_QUOTE weight, about 12 Han or 24 Latin letters), else 0."""
        hay = [_plain(t) for t in texts if t]
        for note in self.owner_notes(rt, user_id):
            n = _plain(note)
            weight, end = 0, 0
            for start in range(len(n)):
                while end < len(n) and weight < NOTE_QUOTE:
                    weight, end = weight + (2 if rt.CJK_RE.match(n[end]) else 1), end + 1
                if weight < NOTE_QUOTE:
                    break
                if any(n[start:end] in h for h in hay):
                    return end - start
                weight -= 2 if rt.CJK_RE.match(n[start]) else 1
        return 0

    def delegate_app(self, m, cfg):
        """P42.1: the app name when this may be a person's own app posting AS them (Coder's `coder-<name>` with their user
        token): bot_id / app_id, a U… / W… `user` (not our bot, not the owner unless the deployment is bot-only), not our
        app, the name (bot_profile.name, else username) matching an `intake.delegate_bot_names` fnmatch pattern, any case
        ([] = off; ["*"] = any app). Else None."""
        profile = m.get('bot_profile') if isinstance(m.get('bot_profile'), dict) else {}
        name, user = str(profile.get('name') or m.get('username') or ''), str(m.get('user') or '')
        # 0.78.3 (F14): with `intake.identity: bot` no agent posts with the owner's token, so the owner's own app posts
        # (their user token through an API client) are the owner's too
        owner_excluded = user == self.owner_slack_user and (cfg or {}).get('identity') != 'bot'
        if not (m.get('bot_id') or m.get('app_id')) or not USER_RE.fullmatch(user) or user == self.bot_user or owner_excluded \
                or self.app_id in (m.get('app_id'), profile.get('app_id')):
            return None
        names = (cfg or {}).get('delegate_bot_names') or ()
        return name if name and any(fnmatch.fnmatchcase(name.lower(), str(n).lower()) for n in names) else None

    def caller(self, rt, db, b, roles=('target', 'handled'), with_task=False):
        """'<owner key>' for a manual run (no agent identity), else 'provider:session' of the burst task's current session — or of
        its running headless run (P38: <owner key>-inbox / <owner key>-oncall run every event in a fresh session that becomes current
        only when it succeeds). `with_task` also returns the task whose session matched (None for the owner)."""
        provider, session = rt.claude.actor()
        if not session:
            return (self.key, None, None, None) if with_task else (self.key, None, None)
        tasks = [b['target_task'] if 'target' in roles else None, b['handled_task'] if 'handled' in roles else None]
        for task_id in [t for t in tasks if t]:
            row = db.execute('SELECT current_provider,current_session FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            current = (row['current_provider'], row['current_session']) if row else None
            if (provider, session) in (current, rt.registry.running_session(db, task_id)):
                found = (f'{provider}:{session}', provider, session)
                return (*found, task_id) if with_task else found
        raise ValueError('caller_must_be_the_current_session_of_the_burst_task')

    def _verified_card(self, db, card_event, burst_id, kind):
        """(option_id, brief row) of the owner's verified decision on an intake card of this burst."""
        if not EVENT_ID_RE.fullmatch(str(card_event or '')):
            raise ValueError('decision_event_must_be_a_card_event_id')
        brief = db.execute('SELECT * FROM intake_briefs WHERE card_event=?', (card_event,)).fetchone()
        if brief is None or brief['burst_id'] != burst_id or brief['kind'] != kind:
            raise ValueError(f'decision_event_is_not_a_{kind}_card_of_this_burst')
        decision = db.execute('SELECT choice FROM decisions WHERE event_id=?', (card_event,)).fetchone()
        if decision is None:
            raise ValueError(f'{self.key}_has_not_decided_this_card_yet')
        choice = str(decision['choice'])
        return (choice[4:] if choice.startswith('opt:') else choice), brief

    @_owner_kwargs
    def record_triage(self, rt, db, b, *, intent, clarity, disposition, target_task=None, confidence=None, basis=None,
                      needs_owner=False, note=None, recorded_by=_OWNER_DEFAULT, now=None, enforce=True):
        recorded_by = self.key if recorded_by is _OWNER_DEFAULT else recorded_by
        now = time.time() if now is None else now
        if intent not in INTENTS:
            raise ValueError('intent_must_be_one_of_' + '_'.join(INTENTS))
        if clarity not in CLARITIES:
            raise ValueError('clarity_must_be_clear_partial_or_vague')
        if disposition not in self.dispositions:
            raise ValueError('disposition_must_be_one_of_' + '_'.join(self.dispositions))
        if confidence is not None:
            confidence = float(confidence)
            if not 0 <= confidence <= 1:
                raise ValueError('confidence_must_be_between_0_and_1')
        for value, name, limit in ((basis, 'basis', 200), (note, 'note', 300)):
            if value is not None and (len(str(value)) > limit or any(ord(c) < 32 and c not in '\t' for c in str(value))):
                raise ValueError(f'{name}_must_be_one_line_of_at_most_{limit}_characters')
        rt.asks.check_secret_free(basis, note)
        if target_task is not None and not rt.worker_config.TASK_ID_RE.fullmatch(str(target_task)):
            raise ValueError('target_task_must_be_a_task_id')
        style, _, g = _handling(rt, b)
        if enforce and g['strategic'] and disposition not in self.strategic_only:
            raise ValueError(f'strategic_directive_only_brief_log_ignore_or_card_{self.key}_until_{self.key}_decides')
        if enforce and not g['may_auto_execute'] and disposition in NO_AUTO_REFUSED:
            raise ValueError(f'auto_execute_false_requires_a_{self.key}_decision_before_launch')
        record_id = 'tr' + uuid.uuid4().hex[:14]
        with rt.transaction(db):
            db.execute(f'INSERT INTO triage_records (id,burst_id,event_id,sender,channel,intent,clarity,disposition,target_task,confidence,basis,needs_{self.key},person_style,note,recorded_by,created_at,smoke) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (record_id, b['burst_id'], b['event_id'], b['sender'], b['channel'], intent, clarity, disposition,
                        target_task, confidence, basis, int(bool(needs_owner or disposition in ('brief', f'card_{self.key}', 'escalate'))),
                        style, note, recorded_by, now, int(bool(b['smoke']))))
        return record_id

    def triage_record_cli(self, rt, db, ident, **kw):
        b = burst(db, ident)
        who, _, _ = self.caller(rt, db, b)
        record_id = self.record_triage(rt, db, b, recorded_by=who, **kw)
        return {'ok': True, 'record_id': record_id, 'burst_id': b['burst_id'], 'disposition': kw['disposition']}

    def triage_correct(self, rt, db, record_id, disposition=None, note=None, now=None):
        """the owner's correction of a triage record (never an agent session): kept next to the original."""
        now = time.time() if now is None else now
        if rt._agent_session() or os.environ.get(self.run_id_env):
            raise ValueError(f'only_{self.key}_corrects_triage_records')
        if not RECORD_ID_RE.fullmatch(str(record_id)):
            raise ValueError('invalid_record_id')
        if disposition is not None and disposition not in self.dispositions:
            raise ValueError('disposition_must_be_one_of_' + '_'.join(self.dispositions))
        if note is not None and len(note) > 300:
            raise ValueError('note_must_be_at_most_300_characters')
        rt.asks.check_secret_free(note)
        with rt.transaction(db):
            if db.execute('SELECT 1 FROM triage_records WHERE id=?', (record_id,)).fetchone() is None:
                raise ValueError('unknown_triage_record')
            db.execute(f"UPDATE triage_records SET correction=?,corrected_by='{self.key}',corrected_at=? WHERE id=?",
                       (json.dumps({'disposition': disposition, 'note': note}, ensure_ascii=False), now, record_id))
        return {'ok': True, 'record_id': record_id, 'correction': {'disposition': disposition, 'note': note}}

    def _ask_view(self, rt, item, reads=None):
        """One open ask as its reader sees it: the task, what was asked (an ask card's question, else the first line of the
        post, read live while `reads` lasts; never stored), where (link) and since when."""
        asked, thread = item['asked'], item['thread']
        if not asked and reads and reads[0] > 0:
            reads[0] -= 1
            try:
                found = rt.slack.api_call('conversations.replies', rt.api_identity(), params={
                    'channel': item['channel'], 'ts': item['ts'], 'oldest': item['ts'], 'latest': item['ts'],
                    'inclusive': 'true', 'limit': 2})
                m = next((x for x in found.get('messages') or [] if isinstance(x, dict) and x.get('ts') == item['ts']), None)
            except Exception:  # noqa: BLE001 - unreadable: the link still says where
                m = None
            if m:
                asked, thread = first_line(message_text(m)), m.get('thread_ts') or thread
        return {'task_id': item['task_id'], 'kind': item['kind'], 'asked': asked,
                'link': self.permalink(item['channel'], item['ts'], thread), 'since': self.local_iso(item['since'])}

    def open_asks_view(self, rt, db, sender, now, task_id=None):
        reads = [OPEN_ASK_READS]
        return [self._ask_view(rt, a, reads) for a in Intake(db, rt.DEFAULT_DB, rt, core=self).open_asks(sender, now)
                if task_id in (None, a['task_id'])]

    def handed_as_answer(self, rt, db, b, task_id, cfg, now, route=None, reason=None):
        """P46.3 (R-0013): handed over by an intake task to a task that has an open ask to the sender — a candidate answer
        (a P39 continuation hand-over, with no ask, is not)."""
        return bool(task_id) and handed_by_intake(route or b['route'], reason or b['route_reason'], cfg) and any(
            a['task_id'] == task_id for a in Intake(db, rt.DEFAULT_DB, rt, core=self).open_asks(b['sender'], now))

    def why_you_got_it(self, rt, db, b, task_id, cfg, now, card=None):
        """P46.3 (R-0013): why a task got this message — <owner key>-inbox handed it over as a possible answer to the task's ask, it
        is a reply in the thread of the task's ask card (`card`), or it came only through the task's conversation window
        (by_window) — the ask(s) it may answer, the task's own post there, and the check. None else."""
        rule = rt.rule_registry.active('send.relevance_ack')
        handed = b is not None and self.handed_as_answer(rt, db, b, task_id, cfg, now)
        window = b is not None and not handed and b['target_task'] == task_id and by_window(db, b)
        if b is not None and str(b['route_reason'] or '').startswith('ask_thread:'):
            card = db.execute('SELECT * FROM ask_cards WHERE id=?', (b['route_reason'][len('ask_thread:'):],)).fetchone()
        if not rule or not task_id or task_id in (cfg['inbox_task'], cfg['oncall_task']) or not (handed or card or window):
            return None
        asks = self.open_asks_view(rt, db, b['sender'], now, task_id) if handed or window else [self._ask_view(rt, {
            'task_id': task_id, 'kind': 'ask_card', 'channel': card['channel'], 'ts': card['ts'], 'thread': card['thread'],
            'since': card['created_at'], 'asked': first_line(card['question'])})]
        post = window and db.execute('SELECT last_ts, last_at FROM dm_windows WHERE channel=? AND task_id=?',
                                     (b['channel'], task_id)).fetchone()
        return {'why': (f'{self.key}-inbox handed this message to you as a possible answer to your ask' if handed else
                        self.window_why if window else 'a reply in the thread of your ask card'), 'your_asks': asks,
                **({'your_post': {'link': self.permalink(b['channel'], post['last_ts']), 'at': self.local_iso(post['last_at'])}}
                   if post else {}),
                'check': f'{rule["statement"]} {rule["enforcement"]["hint"]}',
                **({'intake_reply': 'needs --relevant (you checked that it is for you)'} if handed or window else {})}

    def intake_context(self, rt, db, ident, now=None):
        """The burst's messages read live from Slack (never stored), plus handling guards and allowed actions."""
        now = time.time() if now is None else now
        b = burst(db, ident)
        who, _, _, acting = self.caller(rt, db, b, with_task=True)
        style, auto, g = _handling(rt, b)
        handling = load_handling(rt.envelopes.people_path()).get(b['sender'], {}) if b['sender'] else {}
        refs = json.loads(b['refs'] or '[]')
        out = {'ok': True, 'burst_id': b['burst_id'], 'event_id': b['event_id'], 'smoke': bool(b['smoke']),
               'conversation': {'channel': b['channel'], 'type': b['conv_type'], 'thread_root': b['thread_root'],
                                'reply_thread': _reply_thread(b)},
               'sender': {'id': b['sender'], 'kind': b['sender_kind'], 'request_style': style, 'auto_execute': auto,
                          'tone_hint': handling.get('tone_hint'), 'via': json.loads(b['envelope'] or '{}').get('via')},
               'guards': g, 'route': {'route': b['route'], 'reason': b['route_reason'], 'target_task': b['target_task'],
                                      'handled_task': b['handled_task']},
               'alert': {'fingerprint': b['alert_fp'], 'service': b['alert_service'], 'status': b['alert_state'],
                         'repeats': b['dup_count']} if b['alert_fp'] else None,
               'envelope': json.loads(b['envelope']) if b['envelope'] else None, 'reader': who, 'messages': [],
               # P38 (F29): what an earlier run already posted AS THE OWNER for this burst (a fresh session has no memory of it).
               f'replied_as_{self.key}': _public_sends(self._sends(db, b))}
        if b['route'] == 'inbox' and b['conv_type'] in ('im', 'mpim') and b['route_reason'] in INBOX_DM_REASONS:
            # P39: a new top-level DM routed to <owner key>-inbox — the person's current tasks, to decide `handoff` vs new request.
            out['related_tasks'] = Intake(db, rt.DEFAULT_DB, rt, core=self).person_tasks(b['channel'], b['sender'], time.time())
            out['handoff'] = 'handoff --event <this event> --task T (a continuation of one of them; the message refs travel)'
        if b['smoke']:
            out['messages'] = [{'ts': ts, 'user': b['sender'], 'origin': 'burst', 'text': self.smoke_text.get(b['smoke_scenario'] or
                                                                                                      'request', '')}
                               for ts in refs[:1]]
        else:
            token = rt.api_identity()
            collected, cfg = {}, _cfg(rt)['intake']

            def add(messages, origin):
                for m in messages or []:
                    if isinstance(m, dict) and m.get('ts') and m['ts'] not in collected:
                        via = self.delegate_app(m, cfg) if m.get('user') == b['sender'] else None  # P42.1: the person
                        collected[m['ts']] = {'ts': m['ts'], 'user': m.get('user'), 'bot': bool(m.get('bot_id')) and not via,
                                              'via': via,
                                              'origin': 'burst' if m['ts'] in refs else origin,
                                              'text': message_text(m)[:4000],
                                              'files': [f.get('id') for f in m.get('files') or [] if isinstance(f, dict)]}
            if b['thread_root']:
                replies = rt.slack.api_call('conversations.replies', token, params={
                    'channel': b['channel'], 'ts': b['thread_root'], 'limit': 60, 'include_all_metadata': 'true'})
                add(replies.get('messages'), 'thread')
            else:
                span = rt.slack.api_call('conversations.history', token, params={
                    'channel': b['channel'], 'oldest': b['first_ts'], 'latest': b['last_ts'], 'inclusive': 'true',
                    'limit': min(100, len(refs) + 20)})
                add(span.get('messages'), 'between')
                before = rt.slack.api_call('conversations.history', token, params={
                    'channel': b['channel'], 'latest': b['first_ts'], 'limit': 5})
                add(before.get('messages'), 'before')
            out['messages'] = sorted(collected.values(), key=lambda m: float(m['ts']))[-80:]
            out['missing'] = [ts for ts in refs if ts not in collected]
            self._note_read(rt, db, b, who, out['messages'], time.time())  # P38 (F29): what this session has now seen
            if b['sender_kind'] == 'human':  # P46.2: the language to reply in, shown before anyone writes to them
                out['reply_language'] = reply_line(rt, db, b['sender'], [m['text'] for m in reversed(out['messages'])
                                                                        if m['user'] == b['sender']])
        notes = self.notes_line(rt, b['sender']) if b['sender_kind'] == 'human' else None
        if notes:  # P47 (R-0011): background, never quoted
            out[f'{self.key}_notes'] = notes
        cfg = _cfg(rt)['intake']
        asks_rule = rt.rule_registry.active('inbox.open_asks')
        if b['sender_kind'] == 'human' and not b['smoke']:  # P46.3: who is waiting on this person (R-0012 / R-0013)
            if acting in (None, cfg['inbox_task'], cfg['oncall_task']) and asks_rule:
                out['open_asks'] = {'person': person_name(rt, db, b['sender']) or b['sender'],
                                    'asks': self.open_asks_view(rt, db, b['sender'], now), 'how': asks_rule['statement']}
            why = self.why_you_got_it(rt, db, b, acting, cfg, now)
            if why:
                out['why_you_got_it'] = why
        out['next'] = self._next_steps(b, g, self._unanswered(db, b, _own_scope(b, acting)))
        return out

    def _next_steps(self, b, g, unanswered=()):
        # P38 (F29): a fresh session re-handling an already answered burst reads this before it tries to answer again.
        again = self._answered_note(unanswered)
        if g['strategic']:
            return (f'Strategic directive: brief {self.name} only (`intake-brief`). No launch, no reply, no ack until {self.name} picks a button; then follow goal.md (research -> intake-launch --decision-event; draft -> intake-draft then intake-reply --decision-event).') + again
        steps = ['triage-record', 'intake-forward', 'intake-reply', 'intake-follow + ask', 'intake-brief', f'ask-{self.key}']
        if g['may_auto_execute']:
            steps.insert(1, 'intake-launch')
        if b['alert_fp']:
            steps.append('oncall-incident')
        return 'Allowed: ' + ', '.join(steps) + '. Resolve the event after recording the triage.' + again

    def _author_role(self, rt, db, channel, m, sender):
        """P3.3 reconcile role of one live message: our_agent (our post as the owner) / <owner key> / bot / requester / other."""
        user = str(m.get('user') or '')
        meta = m.get('metadata') if isinstance(m.get('metadata'), dict) else {}
        profile = m.get('bot_profile') if isinstance(m.get('bot_profile'), dict) else {}
        ledger = db.execute('SELECT 1 FROM agent_sends WHERE channel=? AND ts=?', (channel, m.get('ts'))).fetchone() is not None
        if meta.get('event_type') == self.agent_event_type or m.get('app_id') == self.app_id or profile.get('app_id') == self.app_id \
                or user == self.bot_user or ledger:
            return 'our_agent'   # a message our runtime sent as the owner (or our bot) — never treated as the owner himself
        if user == self.owner_slack_user:
            return self.key
        if m.get('bot_id') or m.get('subtype') == 'bot_message' or not USER_RE.fullmatch(user):
            return 'bot'
        return 'requester' if sender and user == str(sender) else 'other'

    def reconcile(self, rt, db, ident, now=None):
        """P3.3 deterministic 're-read before acting': what has happened in the burst's conversation since the trigger, read
        live from Slack (never stored). Before the inbox / task session replies AS THE OWNER or cards the owner about an incoming
        message, it must call this so it never acts on something the owner or the recipient already handled. Returns every message
        since the trigger with its author role (<owner key> / requester / our_agent / bot / other) and the flags the session judges
        on; the 'is it already resolved?' judgement is the session's (goal.md), this step is only the re-read."""
        now = time.time() if now is None else now
        b = burst(db, ident)
        who, _, _, acting = self.caller(rt, db, b, with_task=True)
        refs = set(json.loads(b['refs'] or '[]'))
        trigger_ts = b['last_ts']
        out = {'ok': True, 'burst_id': b['burst_id'], 'event_id': b['event_id'], 'smoke': bool(b['smoke']), 'reader': who,
               'conversation': {'channel': b['channel'], 'type': b['conv_type'], 'thread_root': b['thread_root'],
                                'reply_thread': _reply_thread(b)}, 'sender': b['sender'], 'trigger_ts': trigger_ts,
               f'replied_as_{self.key}': _public_sends(self._sends(db, b))}  # P38 (F29): the send ledger, incl. unknown deliveries
        if b['smoke']:
            return {**out, 'messages': [], 'since_trigger': [], f'{self.key}_replied_since': False, 'requester_replied_since': False,
                    'our_agent_replied_since': False, 'latest_author': None, 'guidance': 'Smoke burst: nothing to reconcile.'}
        token = rt.api_identity()
        collected = {}

        def add(messages):
            for m in messages or []:
                if isinstance(m, dict) and m.get('ts') and m['ts'] not in collected:
                    collected[m['ts']] = m
        thread = b['thread_root'] or (b['first_ts'] if b['conv_type'] == 'channel' else None)
        if thread:  # a thread reply watch already catches later replies, but re-read live so the owner's own reply is seen now
            add(rt.slack.api_call('conversations.replies', token, params={
                'channel': b['channel'], 'ts': thread, 'limit': 80, 'include_all_metadata': 'true'}).get('messages'))
        else:  # a DM / group-DM top level: history from the first message onward catches the owner's and the sender's later ones
            add(rt.slack.api_call('conversations.history', token, params={
                'channel': b['channel'], 'oldest': b['first_ts'], 'inclusive': 'true', 'limit': 80,
                'include_all_metadata': 'true'}).get('messages'))
            add(rt.slack.api_call('conversations.history', token, params={
                'channel': b['channel'], 'latest': b['first_ts'], 'limit': 3}).get('messages'))
        # P38 (F29): our own answer found in the conversation settles an unknown delivery; the read is this session's
        # horizon (what it has now seen).
        self._settle_landed(rt, db, b, collected.values(), now)
        self._note_read(rt, db, b, who, collected.values(), now)
        messages = []
        for m in sorted(collected.values(), key=lambda m: float(m['ts'])):
            messages.append({'ts': m['ts'], 'role': self._author_role(rt, db, b['channel'], m, b['sender']), 'user': m.get('user'),
                             'origin': 'trigger' if m['ts'] in refs else
                                       'since' if float(m['ts']) > float(trigger_ts) else 'before',
                             'text': message_text(m)[:4000]})
        since = [m for m in messages if m['origin'] == 'since']
        ours = any(m['role'] == 'our_agent' for m in since)
        again = self._answered_note(self._unanswered(db, b, _own_scope(b, acting)), reconciled=True)
        if ours and not again:  # an answer as the owner after the trigger that a newer requester message (or another task) owns
            again = (f' Our agent already posted as {self.name} after the trigger (our_agent_replied_since, replied_as_{self.key}): read it and answer only what is still open — never repeat it.')
        return {**out, f'replied_as_{self.key}': _public_sends(self._sends(db, b)), 'messages': messages[-80:], 'since_trigger': since,
                f'{self.key}_replied_since': any(m['role'] == self.key for m in since),
                'requester_replied_since': any(m['role'] == 'requester' for m in since),
                'our_agent_replied_since': ours,
                'latest_author': messages[-1]['role'] if messages else None,
                'guidance': (self.msg('intake.reconcile.1', owner=self.name)) + again}

    def _sends(self, db, b, kinds=SEND_KINDS):
        """What this runtime already posted AS THE OWNER for the burst, oldest first: intake_sends (in-flight and unknown
        deliveries too) plus agent_sends ledger rows written by older code. Metadata only, never a body. `horizon`: what the
        sending session had read live (_horizon); a row without one (older code) is taken to have seen only the burst."""
        marks = ','.join('?' * len(kinds))
        last = float(b['last_ts'])
        out, known = [], set()
        for r in db.execute(f'SELECT * FROM intake_sends WHERE burst_id=? AND kind IN ({marks})', (b['burst_id'], *kinds)):
            known.add((r['channel'], r['ts']))
            horizon = r['horizon'] if r['horizon'] is not None else last
            out.append({'kind': r['kind'], 'state': r['state'], 'at': self.local_iso(r['created_at']), 'ts': r['ts'],
                        'by': r['task_id'] or r['actor'], 'task': r['task_id'], 'created': r['created_at'],
                        'horizon': horizon, 'actor': r['actor']})
        for r in db.execute(f'SELECT * FROM agent_sends WHERE ref=? AND kind IN ({marks})', (b['burst_id'], *kinds)):
            if (r['channel'], r['ts']) not in known:
                out.append({'kind': r['kind'], 'state': 'sent', 'at': self.local_iso(r['created']), 'ts': r['ts'], 'by': None,
                            'task': None, 'created': r['created'], 'horizon': last, 'actor': None})
        return sorted(out, key=lambda s: s['created'])

    def _horizon(self, db, b, actor, task_id, now):
        """P38 (F29): how far the sending session had READ the burst's conversation live when it answered (its own
        intake-context / intake-reconcile reads, else just the burst); the owner by hand sees Slack (the send time). What its
        runs were handed is checked per message (_seen_by), so a follow-up neither read nor handed stays answerable."""
        if not actor or actor == self.key:
            return now
        read = db.execute('SELECT seen_ts FROM intake_reads WHERE burst_id=? AND actor=?',
                          (b['burst_id'], actor)).fetchone()
        return max([float(b['last_ts'])] + ([float(read[0])] if read else []))

    def _seen_by(self, db, b, send, f):
        """P38 (F29): had the session behind `send` seen follow-up `f` when it answered? Read live up to its horizon — a
        thread read covers the thread, a DM / group-DM top-level read only top-level messages (thread replies arrive by
        watch, maybe out of order) — or handed to one of its runs before the answer."""
        by_hand = send['actor'] in (None, self.key)
        if f['ts'] <= send['horizon'] and (by_hand or _reply_thread(b) or f['top']):
            return True
        if by_hand or ':' not in str(send['actor']):
            return False
        provider, _, session = send['actor'].partition(':')
        return db.execute("SELECT 1 FROM consumer_runs r JOIN json_each(CASE WHEN json_valid(r.event_ids) THEN r.event_ids "
                          "ELSE '[]' END) j WHERE r.provider=? AND r.session=? AND j.value=? AND r.started_at<=? LIMIT 1",
                          (provider, session, f['id'], send['created'])).fetchone() is not None

    def _note_read(self, rt, db, b, who, messages, now):
        """P38 (F29): remember the newest message `who` read live for the burst (see _horizon). the owner by hand: not needed."""
        stamps = [float(m['ts']) for m in messages if TS_RE.fullmatch(str(m.get('ts') or ''))]
        if who == self.key or not stamps:
            return
        with rt.transaction(db):
            db.execute('INSERT INTO intake_reads (burst_id,actor,seen_ts,read_at) VALUES (?,?,?,?) ON CONFLICT(burst_id,'
                       'actor) DO UPDATE SET seen_ts=max(seen_ts,excluded.seen_ts),read_at=excluded.read_at',
                       (b['burst_id'], who, max(stamps), now))

    def _unanswered(self, db, b, own=None):
        """The earlier sends that make a new one a repeat: those whose session had seen EVERY follow-up of the requester
        so far (_seen_by). A new burst has its own id, and a follow-up an answer had not seen is answered without --again.
        `own`: only that task's sends count (_own_scope)."""
        followups = _followups(db, b)
        return [s for s in self._sends(db, b) if (own is None or s['task'] == own) and
                all(self._seen_by(db, b, s, f) for f in followups)]

    def _answered_note(self, unanswered, reconciled=False):
        """P38 (F29): the guidance intake-context and intake-reconcile give for an already answered burst."""
        if not unanswered:
            return ''
        note = (f' Already answered as {self.name} (replied_as_{self.key}) and nothing newer came from the requester: do not reply or ack again; intake-reply refuses and intake-launch skips the ack unless --again (a deliberate NEW message only).')
        if any(s['state'] != 'sent' for s in unanswered):
            note += (' A sending / delivery_unknown answer was not found among these messages: if it is really not in the '
                     'conversation, resend it with --again.' if reconciled else
                     ' A sending / delivery_unknown answer may not have landed: intake-reconcile marks it sent when it '
                     'finds it; resend with --again only if it is not in the conversation.')
        return note

    def _settle_landed(self, rt, db, b, messages, now):
        """P38 (F29): a `sending` / `delivery_unknown` answer whose post intake-reconcile finds in the conversation (our
        hidden metadata carrying this burst id) did land: mark it sent with that ts. Never releases anything."""
        for m in messages:
            meta = m.get('metadata') if isinstance(m.get('metadata'), dict) else {}
            payload = meta.get('event_payload') if isinstance(meta.get('event_payload'), dict) else {}
            if meta.get('event_type') != self.agent_event_type or payload.get('burst_id') != b['burst_id'] or \
                    payload.get('kind') not in SEND_KINDS or not TS_RE.fullmatch(str(m.get('ts') or '')):
                continue
            with rt.transaction(db):
                if db.execute('SELECT 1 FROM intake_sends WHERE burst_id=? AND ts=? UNION ALL SELECT 1 FROM agent_sends '
                              'WHERE channel=? AND ts=?', (b['burst_id'], m['ts'], b['channel'], m['ts'])).fetchone():
                    continue  # already accounted for
                row = db.execute("SELECT id FROM intake_sends WHERE burst_id=? AND kind=? AND state IN ('sending',"
                                 "'delivery_unknown') ORDER BY created_at LIMIT 1", (b['burst_id'], payload['kind'])
                                 ).fetchone()
                if row:
                    db.execute("UPDATE intake_sends SET state='sent',ts=?,updated_at=? WHERE id=?",
                               (m['ts'], now, row['id']))
                    rt._ledger(db, b['channel'], m['ts'], payload['kind'], b['burst_id'], payload.get('session'))

    def _reserve_send(self, rt, db, b, kind, *, actor, task_id, horizon, now, again=False, own=None):
        """One IMMEDIATE transaction: refuse a repeat (AlreadyReplied) unless `again`, else record the send as `sending`
        BEFORE the post, so a crash or an unknown delivery counts for the next (fresh) session too."""
        send_id = 'is' + uuid.uuid4().hex[:14]
        with rt.transaction(db):
            prior = [] if again else self._unanswered(db, b, own)
            if prior:
                raise AlreadyReplied(prior)
            db.execute('INSERT INTO intake_sends (id,burst_id,kind,task_id,actor,channel,ts,state,created_at,updated_at,'
                       "horizon) VALUES (?,?,?,?,?,?,NULL,'sending',?,?,?)",
                       (send_id, b['burst_id'], kind, task_id, actor, b['channel'], now, now, horizon))
        return send_id

    def _post_as_owner(self, rt, db, b, text, kind, owner, now, *, again=False, own=None, actor=None, task_id=None,
                      window_task=None, words=None):
        """Conversation plane: the owner's user token, no visible marker, hidden agent metadata, ledger entry. P38 (F29): never a
        second answer to the same burst unless `again` — see _reserve_send / _unanswered. P39: a post into a DM / group DM
        opens the conversation window of the task it speaks for (`window_task`, default the acting task)."""
        if b['smoke']:
            raise ValueError('smoke_burst_has_no_slack_conversation')
        text = str(text or '').strip()
        if not text or len(text) > 3000:
            raise ValueError('text_must_be_1_to_3000_characters')
        rt.asks.check_secret_free(text)
        if rt.asks.BROADCAST_RE.search(text):
            raise ValueError('text_must_not_broadcast')
        if b['sender_kind'] == 'human':  # P46.2: in the language the requester wrote in
            self.require_language(rt, db, b['sender'], [text], words=_burst_words(rt, b) if words is None else words)
        body = {'channel': b['channel'], 'text': text, 'unfurl_links': False, 'unfurl_media': False,
                'metadata': rt.agent_metadata(kind, owner, burst_id=b['burst_id'])}
        thread = _reply_thread(b)
        if thread:
            body['thread_ts'] = thread
        prior = [] if again else self._unanswered(db, b, own)
        if prior:  # refused before any Slack call; _reserve_send re-checks inside its transaction
            raise AlreadyReplied(prior)
        token = rt.api_identity(bot=_cfg(rt)['intake'].get('identity') == 'bot')  # 0.77.0: the assistant's voice
        send_id = self._reserve_send(rt, db, b, kind, actor=actor, task_id=task_id, horizon=self._horizon(db, b, actor, task_id, now),
                                now=now, again=again, own=own)
        try:
            sent = rt.slack.api_call('chat.postMessage', token, json_body=body)
        except Exception as exc:
            # P38 (F29): a post that never reached Slack frees the reservation, so a retry may send; one that may have
            # landed stays delivery_unknown and the next session checks the conversation (intake-reconcile) before --again.
            with rt.transaction(db):
                if _never_posted(exc):
                    db.execute("DELETE FROM intake_sends WHERE id=? AND state='sending'", (send_id,))
                else:  # intake-reconcile may already have found it in the conversation (sent): keep that
                    db.execute("UPDATE intake_sends SET state='delivery_unknown',updated_at=? WHERE id=? AND "
                               "state='sending'", (now, send_id))
            raise
        ts = (sent or {}).get('ts')
        speaks_for = window_task or task_id or rt.registry.task_id_for_cwd(owner.get('cwd'))
        with rt.transaction(db):
            rt._ledger(db, b['channel'], ts, kind, b['burst_id'], owner.get('session'),
                       rt.schedule.mentions(rt.asks.MENTION_RE.findall(text), speaks_for))  # P46.3: open asks
            db.execute("UPDATE intake_sends SET state='sent',ts=?,updated_at=? WHERE id=?", (ts, now, send_id))
            rt.schedule.note_window(db, b['channel'], ts, speaks_for, kind,
                                    b['sender'] if USER_RE.fullmatch(str(b['sender'] or '')) else None, now, b['conv_type'])
        # P3.3 conversation-plane reply receipt: the agent just answered this person as the owner, so react ✅ (handled) then the
        # provider emoji on the message(s) it is replying to (the burst's refs). Best effort; never blocks the send.
        # Only on a person's message: on an alert bot's post a ✅ reads as "handled" to the whole oncall channel (audit F10).
        if b['sender_kind'] == 'human':
            rt.reply_receipt(db, [(b['channel'], ts) for ts in json.loads(b['refs'] or '[]')],
                             owner.get('provider') or 'codex', now)
        return ts

    def launch_prompt(self, rt, db_path, b, task_id, brief, *, acked, decision, incident, replied=False):
        cli = f'python3 {Path(rt.__file__).resolve()} --db {db_path}'
        where = {'im': self.msg('intake.launch_prompt.im'), 'mpim': self.msg('intake.launch_prompt.mpim'), 'channel': self.msg('intake.launch_prompt.channel')}.get(b['conv_type'], b['conv_type'])
        lines = [self.msg('intake.launch_prompt.3', task_id=task_id, value=self.msg('intake.launch_prompt.1', owner_key=self.key) if incident else self.msg('intake.launch_prompt.2', owner_key=self.key)),
                 self.msg('intake.launch_prompt.4', where=where, channel=b['channel']) + (f' thread `{b["thread_root"]}`' if b['thread_root'] else '') +
                 self.msg('intake.launch_prompt.5', sender=b['sender'], burst_id=b['burst_id']) + (self.msg('intake.launch_prompt.6') if b['smoke'] else ''),
                 self.msg('intake.launch_prompt.7', cli=cli, burst_id=b['burst_id']),
                 self.msg('intake.launch_prompt.8', owner=self.name)]
        if b['smoke']:
            lines.append(self.msg('intake.launch_prompt.9'))
        elif acked == 'earlier':  # P38 (F29): no second "收到" — an earlier run already answered this burst as the owner
            lines.append(self.msg('intake.launch_prompt.10', owner=self.name, owner_key=self.key))
        elif acked:
            lines.append(self.msg('intake.launch_prompt.11', owner=self.name))
        else:  # P38 (F29): `replied` = an earlier run already posted as the owner for this burst (--no-ack, incident, strategic)
            lines.append((self.msg('intake.launch_prompt.12', owner=self.name, owner_key=self.key)
                          if replied else self.msg('intake.launch_prompt.13')) + self.msg('intake.launch_prompt.14', owner=self.name))
        if decision:
            lines.append(self.msg('intake.launch_prompt.15', owner=self.name, decision=decision, owner_key=self.key))  # P42: the label; the authority is unchanged
        lines.append(self.msg('intake.launch_prompt.16', owner=self.name))
        lines += ['', self.msg('intake.launch_prompt.17'), brief.strip()]
        return '\n'.join(lines)

    def intake_launch(self, rt, db, db_path, ident, title, brief, *, provider='auto', category=None, important=False,
                      ticket=False, ack=True, ack_text=None, decision_event=None, parent=None, slug=None,
                      incident=False, again=False, wait_seconds=90.0, now=None, runner=None, linear=None):
        """§6: bootstrap a new task workspace (--no-launch), bind the source conversation, launch the first session
        headless (run card = the assistant's notification), ack the requester as the owner (unless strategic / --no-ack, or —
        P38 (F29) — the burst was already answered as the owner: `ack_skipped`, unless `again`). `ticket`: also a tracker
        ticket (`linear`: its pre-N2 name, kept for deployments assembled before N2)."""
        now = time.time() if now is None else now
        ticket = bool(ticket or linear)
        b = burst(db, ident)
        who, _, _, acting = self.caller(rt, db, b, with_task=True)
        if b['handled_task'] and b['handled_task'] != b['target_task'] and not incident:
            raise ValueError(f'burst_already_handled_by_{b["handled_task"]}')
        style, _, g = _handling(rt, b)
        basis = None
        if decision_event:
            option, _ = self._verified_card(db, decision_event, b['burst_id'], 'brief')
            if option != 'research':
                raise ValueError(f'{self.key}_did_not_choose_research_on_the_brief_card')
            basis = f'{self.key}_decision:{decision_event}'
        elif not g['may_auto_execute'] and not incident:
            raise ValueError(f'strategic_or_no_auto_requester_needs_{self.key}_research_decision_on_an_intake_brief')
        if not g['may_reply'] or b['smoke'] or b['sender_kind'] == 'bot':
            ack = False  # strategic directives never get an automatic reply; bots and TEST bursts have nobody to ack
        # P38 (F29): the ack is one-per-burst too. A fresh session re-handling a burst an earlier run already answered as
        # the owner (a reply or ack nothing newer from the requester followed) launches without a second "收到".
        earlier = self._unanswered(db, b) if ack and not again else []
        if earlier:
            ack = False
        title = ' '.join(str(title or '').split())
        if not 3 <= len(title) <= 120:
            raise ValueError('title_must_be_3_to_120_characters')
        brief = rt.task_memory.clean_text(brief)
        rt.asks.check_secret_free(title, brief, ack_text)
        if ticket and not self.tracker.tickets:
            raise ValueError('no_tracker_for_a_ticket')
        if ticket and self.tracker.title_language_rule and (rt.CJK_RE.search(title) or rt.CJK_RE.search(brief)):
            raise ValueError(self.tracker.title_language_rule)
        if ack_text is not None and (not ack_text.strip() or len(ack_text) > 200):
            raise ValueError('ack_text_must_be_1_to_200_characters')
        words = None
        if ack and b['sender_kind'] == 'human':  # P46.2: checked before any workspace or session, like the length above
            words = list(_burst_words(rt, b))
            langs, share = self.require_language(rt, db, b['sender'], [ack_text], words=words, what='ack', lookup=True)
            ack_text = ack_text or default_ack(langs, share)
        config = _cfg(rt)
        choice = provider if provider != 'auto' else rt.worker_config.provider_order(config, category)[0]
        reused = False
        if b['smoke']:
            path = _smoke_workspace(rt, title, brief, now)
        else:
            slug = slug or _slugify(title)
            if not slug or slug == 'infra-task':
                raise ValueError('title_has_no_ascii_words_pass_--slug')
            if not ticket:
                expected = rt.registry.root() / f'local-task-{time.strftime("%Y%m%d", time.localtime(now))}-{slug}'
                if expected.exists():
                    raise ValueError('workspace_exists_choose_another_--slug')
            context = '\n'.join([f'Created by intake from Slack burst {b["burst_id"]} ({b["conv_type"]} {b["channel"]}, '
                                 f'sender {b["sender"]}).', f"Source: {self.permalink(b['channel'], b['first_ts'], b['thread_root'])}",
                                 'Message bodies are not stored here; read them with intake-context.'])
            # the deployment hook keeps its pre-N2 keyword `linear` until N2b
            report = self.workspace_bootstrap(title, brief, context, linear=ticket, agent=choice, root=rt.registry.root(),
                                              slug=slug, runner=runner)
            path = Path(report['workspace'])
            reused = bool(report.get('reused_workspace'))
        task_id = rt.registry.task_id_for_cwd(str(path))
        if not task_id:
            raise ValueError('bootstrapped_workspace_is_not_under_the_tasks_root')
        binding = _bind(rt, db, b, task_id, who, now)
        row = db.execute('SELECT current_session FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        result = {'ok': True, 'burst_id': b['burst_id'], 'task_id': task_id, 'workspace': str(path), 'reused': reused,
                  'binding': {'channel': binding['channel'], 'thread_root': binding['thread_root']}, 'ack_ts': None}
        if reused and row and row['current_session']:
            sent = rt.send_to_task(db, db_path, task_id, '\n'.join([
                f'intake: Slack burst {b["burst_id"]} belongs to this task (reused workspace).',
                f'Read it: intake-context --burst {b["burst_id"]}', '', brief]), from_task=b['target_task'])
            result.update(launched=None, forwarded_event=sent.get('event'))
        else:
            prompt = self.launch_prompt(rt, db_path, b, task_id, brief, acked='earlier' if earlier else ack,
                                   decision=decision_event, incident=incident,
                                   replied=bool(not b['smoke'] and self._sends(db, b)))
            # P3.2: a research task launched from a brief card joins that card's approvals thread.
            # P39: the RAW provider, so `auto` is never recorded as a pin (the bootstrap above got the concrete choice).
            launched = rt.launch_task(db, db_path, str(path), provider, prompt, important=important, category=category,
                                      parent=parent, allow_existing=reused, wait_seconds=wait_seconds, now=now,
                                      from_card=decision_event, persist=True)
            result['launched'] = {k: launched.get(k) for k in ('dispatch_id', 'run_id', 'provider', 'session', 'state')}
            task = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if task['current_session'] and binding['want_watch']:
                result['watch_id'] = Intake(db, db_path, rt, core=self).bind_watch(binding, task['current_provider'],
                                                                         task['current_session'], task['path'], now)
        if ack:
            owner = {'session': (result.get('launched') or {}).get('session'),
                     'provider': (result.get('launched') or {}).get('provider') or choice, 'cwd': str(path)}
            try:
                result['ack_ts'] = self._post_as_owner(rt, db, b, ack_text or DEFAULT_ACK, 'intake_ack', owner, now,
                                                 again=again, actor=who, task_id=acting, window_task=task_id, words=words)
            except AlreadyReplied as exc:  # a reply landed after the check above (another session): still no second ack
                earlier = exc.prior
        if earlier:
            result['ack_skipped'] = str(AlreadyReplied(earlier))
        result['triage_record'] = self.record_triage(
            rt, db, b, intent='alert' if incident else 'request', clarity='clear',
            disposition='escalate' if incident else 'launch', target_task=task_id,
            basis=basis or ('oncall_incident' if incident else 'intake_launch'), recorded_by=who, now=now,
            enforce=not (decision_event or incident))
        rt.start(db_path)
        return result

    def intake_forward(self, rt, db, db_path, ident, task, text=None, *, bind=True, decision_event=None, now=None):
        """Hand the burst to an existing task (send-to-task semantics: agent message, never authority)."""
        now = time.time() if now is None else now
        b = burst(db, ident)
        who, _, _ = self.caller(rt, db, b)
        _, _, g = _handling(rt, b)
        basis = 'intake_forward'
        if decision_event:
            option, _ = self._verified_card(db, decision_event, b['burst_id'], 'brief')
            if option != 'research':
                raise ValueError(f'{self.key}_did_not_choose_research_on_the_brief_card')
            basis = f'{self.key}_decision:{decision_event}'
        elif not g['may_auto_execute']:
            raise ValueError(f'strategic_or_no_auto_requester_needs_{self.key}_decision_before_handing_it_to_a_task')
        row = rt._resolve_task_arg(db, task)
        if row['task_id'] == b['target_task']:
            raise ValueError('burst_is_already_in_this_task')
        body = [f'intake: Slack burst {b["burst_id"]} ({b["conv_type"]} {b["channel"]}, sender {b["sender"]}) was forwarded to '
                f'this task by {b["target_task"] or "intake"}.', f'Read the messages: intake-context --burst {b["burst_id"]}',
                'Source: ' + (self.permalink(b['channel'], b['first_ts'], b['thread_root']) if not b['smoke'] else '(TEST)')]
        if text:
            body += ['', 'Note from the inbox:', str(text).strip()]
        sent = rt.send_to_task(db, db_path, row['task_id'], '\n'.join(body), from_task=b['target_task'])
        if bind:
            _bind(rt, db, b, row['task_id'], who, now)
        else:
            with rt.transaction(db):
                db.execute('UPDATE intake_bursts SET handled_task=?,updated_at=? WHERE burst_id=?', (row['task_id'], now,
                                                                                                   b['burst_id']))
        record = self.record_triage(rt, db, b, intent='follow_up', clarity='clear', disposition='forward',
                               target_task=row['task_id'], basis=basis, recorded_by=who, now=now,
                               enforce=not decision_event)
        return {'ok': True, 'burst_id': b['burst_id'], 'task_id': row['task_id'], 'event': sent.get('event'),
                'reason': sent.get('reason'), 'bound': bool(bind), 'triage_record': record}

    def handoff(self, rt, db, db_path, event_id, task, note=None, now=None):
        """P39 (F07): the first-class hand-over of ONE received message to the task it belongs to — <owner key>-inbox giving a
        continuation to its task, a bound task giving an off-topic new request back to <owner key>-inbox (or straight to the right
        task). The ORIGINAL Slack refs travel, never a re-typed summary: an intake burst is re-delivered as an intake event
        of the target (which may then intake-context / intake-reply on it) and its request thread is re-bound to the target;
        a message a watch delivered becomes a one-message burst first; the owner's reply in a card thread reaches the target as a
        card reply. The handed event is resolved (disposition handed_off) in the same step. Carries no authority."""
        now = time.time() if now is None else now
        if not EVENT_ID_RE.fullmatch(str(event_id or '')):
            raise ValueError('event_must_be_an_event_id')
        e = db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
        if e is None:
            raise ValueError('unknown_event')
        w = rt.load(db, e['watch_id'])
        provider, session = rt.claude.actor()
        if session:
            rt.require_event_owner(db, e, w)  # the session holding it (watch owner, or the run of its dispatch)
        who = f'{provider}:{session}' if session else self.key
        if e['status'] == 'handled':
            raise ValueError('event_already_handled')
        source_task = w.get('task_id') or rt.registry.task_id_for_cwd(w.get('cwd'))
        target = rt._resolve_task_arg(db, task)['task_id']
        if target == source_task:
            raise ValueError('event_already_belongs_to_this_task')
        if note is not None:
            note = ' '.join(str(note).split())
            if not note or len(note) > 300:
                raise ValueError('note_must_be_one_line_of_at_most_300_characters')
            rt.asks.check_secret_free(note)
        trow = db.execute('SELECT * FROM tasks WHERE task_id=?', (target,)).fetchone()
        if trow is None or not trow['current_session']:
            raise ValueError('target_task_has_no_session_yet_use_intake-forward_or_intake-launch')
        config = _cfg(rt)
        cfg = config['intake']
        local = rt.local_message(db, event_id)
        source = db.execute('SELECT root FROM event_sources WHERE event_id=?', (event_id,)).fetchone()
        root = source[0] if source else e['ts']
        out = {'ok': True, 'from_event': event_id, 'from_task': source_task, 'task_id': target, 'event': None,
               'burst_id': None, 'kind': None}
        if local and local['kind'] == 'intake':
            b = db.execute('SELECT * FROM intake_bursts WHERE event_id=?', (event_id,)).fetchone()
            if b is None:
                raise ValueError('intake_event_without_burst')
            out['kind'] = 'burst'
        elif local:
            raise ValueError('only_slack_messages_can_be_handed_off_use_send-to-task')
        elif e['channel'] in rt.card_threads.decision_channels(rt):
            out['kind'] = 'card_reply'
            found = rt.slack.api_call('conversations.replies', rt.api_identity(), params={
                'channel': e['channel'], 'ts': root, 'oldest': e['ts'], 'latest': e['ts'], 'inclusive': 'true', 'limit': 2,
                'include_all_metadata': 'true'})
            m = next((x for x in found.get('messages') or [] if isinstance(x, dict) and x.get('ts') == e['ts']), None)
            if m is None:
                raise ValueError('source_message_not_found')
            consumer = rt.registry.task_consumer(db, target)
            with rt.transaction(db):
                inbox = rt.ensure_inbox_watch(db, target, trow['path'], consumer[0] or 'codex', consumer[1], now)
                new = hashlib.sha256(f'{inbox["id"]}/{e["channel"]}/{e["ts"]}/{e["digest"]}'.encode()).hexdigest()[:24]
                db.execute('INSERT OR IGNORE INTO events (id,watch_id,channel,ts,author,digest,received,status) VALUES '
                           '(?,?,?,?,?,?,?,?)', (new, inbox['id'], e['channel'], e['ts'], e['author'], e['digest'], now,
                                                 'pending'))
                db.execute('INSERT OR IGNORE INTO event_sources VALUES (?,?)', (new, root))
                rt.envelopes.store(db, new, rt.envelopes.lookup(db, event_id) or rt.message_envelope(inbox, m, root), now)
                db.execute("INSERT INTO card_reply_targets (channel,reply_ts,task_id,source,reason,event_id,at) VALUES "
                           "(?,?,?,'handoff','handoff',?,?) ON CONFLICT(channel,reply_ts) DO UPDATE SET task_id=excluded.task_id,"
                           "reason='handoff',event_id=excluded.event_id", (e['channel'], e['ts'], target, new, now))
                db.execute("UPDATE events SET status='handled',disposition='handed_off' WHERE id=? AND status!='handled'",
                           (event_id,))
                rt.claude.settle_claims(db, [event_id], 'handed_off', now)
            out['event'] = new
            rt.start(db_path)
            return out
        else:
            if not USER_RE.fullmatch(str(e['author'] or '')):
                raise ValueError('only_a_persons_message_can_be_handed_off')
            out['kind'] = 'message'
            conv_type = _conv_type(rt, db, e['channel'])
            thread_root = root if root and root != e['ts'] else None
            burst_id = 'in' + uuid.uuid4().hex[:14]
            env = rt.envelopes.lookup(db, event_id) or rt.envelopes.envelope('slack_message', 'human', 'colleague', 'none',
                                                                             'intake_default', thread_root or e['ts'])
            env['burst_id'] = burst_id
            handling = load_handling(rt.envelopes.people_path()).get(e['author'], {})
            auto = handling.get('auto_execute')
            if not guards(handling.get('request_style') or 'standard', None if auto is None else bool(auto))[
                    'may_auto_execute'] and target != cfg['inbox_task']:
                raise ValueError(f'strategic_or_no_auto_requester_hand_it_to_{self.key}-inbox_only')  # before any write
            with rt.transaction(db):
                db.execute('INSERT INTO intake_bursts (burst_id,conv_key,channel,conv_type,thread_root,sender,sender_kind,'
                           'first_ts,last_ts,msg_count,refs,task_refs,pr_refs,envelope,person_style,auto_execute,state,'
                           'route,route_reason,created_at,sealed_at,updated_at) VALUES '
                           "(?,?,?,?,?,?,'human',?,?,1,?,'[]','[]',?,?,?,'sealed','handoff',?,?,?,?)",
                           (burst_id, f'{e["author"]}|{e["channel"]}:{thread_root or ""}', e['channel'], conv_type,
                            thread_root, e['author'], e['ts'], e['ts'], json.dumps([e['ts']]),
                            json.dumps(env, ensure_ascii=False), handling.get('request_style') or 'standard',
                            None if auto is None else int(bool(auto)), 'handoff_of_event:' + event_id, now, now, now))
            b = db.execute('SELECT * FROM intake_bursts WHERE burst_id=?', (burst_id,)).fetchone()
        _, _, g = _handling(rt, b)
        if not g['may_auto_execute'] and target != cfg['inbox_task']:
            raise ValueError(f'strategic_or_no_auto_requester_hand_it_to_{self.key}-inbox_only')
        delivered = Intake(db, db_path, rt, core=self).deliver(b, target, 'handoff', 'handoff_from:' + str(source_task or 'session'),
                                                    cfg, now)
        if delivered != 'routed':
            raise ValueError('handoff_not_delivered:' + str(delivered))
        b = db.execute('SELECT * FROM intake_bursts WHERE burst_id=?', (b['burst_id'],)).fetchone()
        engine = Intake(db, db_path, rt, core=self)
        holder = engine.bound_task(b['channel'], b['thread_root'], b['conv_type'], now, b['sender']) if b['thread_root'] \
            else None
        if not b['smoke'] and (holder is None or holder[0] in (target, cfg['inbox_task']) or not engine._live_task(holder[0])):
            # One message travels; a live task's OWN request thread is never taken from it by handing off one reply in it.
            _bind(rt, db, b, target, who, now, mark_handled=False)  # its request thread now belongs to the target
        with rt.transaction(db):
            db.execute("UPDATE events SET status='handled',disposition='handed_off' WHERE id=? AND status!='handled'",
                       (event_id,))
            rt.claude.settle_claims(db, [event_id], 'handed_off', now)
        out.update(event=b['event_id'], burst_id=b['burst_id'], triage_record=self.record_triage(
            rt, db, b, intent='follow_up', clarity='clear', disposition='forward', target_task=target, basis='handoff',
            note=note, recorded_by=who, now=now, enforce=False))
        rt.start(db_path)
        return out

    def _source_footer(self, b, extra=None):
        link = self.msg('intake._source_footer.1') if b['smoke'] else self.msg('intake._source_footer.2', permalink=self.permalink(b['channel'], b['first_ts'], b['thread_root']))
        return link + (f' · {extra}' if extra else '')

    def _post_card(self, rt, db, db_path, b, kind, first, body, options, now, priority='normal', never_auto=True, footer=None,
                   task_thread=False, cwd=None):
        # P3.2: brief / draft cards are each a fresh top-level root (task_thread=False), so a task launched from one joins its
        # own thread, not a shared inbox thread. An incident card is posted for the incident task (cwd) and establishes its
        # root (task_thread=True), so that task's later cards thread under the incident card.
        provider, session = rt.claude.actor()
        if not session:
            raise ValueError('cards_are_posted_by_the_inbox_or_oncall_session_itself')
        self.caller(rt, db, b)
        rt.buttons.check_copy(body, 'card_body', first_line_max=None)  # agent text: no ids, hashes or jargon
        if cwd is None:
            task = db.execute('SELECT path FROM tasks WHERE task_id=?', (b['target_task'],)).fetchone()
            cwd = task['path'] if task else os.getcwd()
        question = _card_question(b, first, body)
        card = self.ask_owner(rt)(db, db_path, session, provider, cwd, question, options, never_auto=never_auto, now=now,
                           priority=priority, footer=footer or self._source_footer(b), task_thread=task_thread)
        with rt.transaction(db):
            db.execute('INSERT INTO intake_briefs (card_event,burst_id,kind,digest,session,task_id,created_at) VALUES '
                       '(?,?,?,?,?,?,?)', (card['event_id'], b['burst_id'], kind, digest_text(body) if kind == 'draft' else None,
                                           f'{provider}:{session}', b['target_task'], now))
        return card

    def intake_brief(self, rt, db, db_path, ident, body, now=None):
        """Strategic / vague directive: an assistant brief card to the owner ONLY (no external post of any kind)."""
        now = time.time() if now is None else now
        b = burst(db, ident)
        body = rt.task_memory.clean_text(body)
        rt.asks.check_secret_free(body)
        who = person_name(rt, db, b['sender'])
        who = (who + (' ' if who[-1].isascii() else '')) if who else self.msg('intake.intake_brief.1')
        card = self._post_card(rt, db, db_path, b, 'brief',
                          self.msg('intake.intake_brief.3', who=who, get=self.where_zh.get(b['conv_type'], self.msg('intake.intake_brief.2'))), body,
                          self.brief_options, now, footer=self._source_footer(b, self.msg('intake.intake_brief.4')))
        who, _, _ = self.caller(rt, db, b)
        record = self.record_triage(rt, db, b, intent='directive', clarity='vague', disposition='brief', **{f'needs_{self.key}': True}, basis='person:request_style=' + _handling(rt, b)[0], recorded_by=who, now=now)
        return {'ok': True, 'burst_id': b['burst_id'], 'card_event': card['event_id'], 'card_ts': card['thread_ts'],
                'options': ['self', 'draft', 'research', 'ignore'], 'triage_record': record,
                'next': (f"Resolve the intake event as decision. {self.name}'s click arrives as a decision event: research -> intake-launch --decision-event CARD; draft -> intake-draft, then intake-reply --decision-event DRAFT_CARD; self / ignore -> triage-record --disposition log.")}

    def intake_draft(self, rt, db, db_path, ident, text, now=None):
        """A draft reply shown to the owner on a card; only an approved draft (same text) may be sent with intake-reply."""
        now = time.time() if now is None else now
        b = burst(db, ident)
        if b['smoke']:
            raise ValueError('smoke_burst_has_no_slack_conversation')
        text = str(text or '').strip()
        if not text or len(text) > 1100:
            raise ValueError('draft_must_be_1_to_1100_characters')
        rt.asks.check_secret_free(text)
        if b['sender_kind'] == 'human':  # P46.2: never ask the owner to approve a draft intake-reply would refuse
            self.require_language(rt, db, b['sender'], [text], words=_burst_words(rt, b), what='draft')
        who = person_name(rt, db, b['sender']) or self.msg('intake.intake_draft.1')
        card = self._post_card(rt, db, db_path, b, 'draft', self.msg('intake.intake_draft.2', who=who), text,
                          self.draft_options, now, footer=self._source_footer(b, self.msg('intake.intake_draft.3')))
        return {'ok': True, 'burst_id': b['burst_id'], 'card_event': card['event_id'], 'digest': digest_text(text),
                'next': self.msg('intake.intake_draft.next', owner=self.name)}

    def intake_reply(self, rt, db, db_path, ident, text, decision_event=None, now=None, *, again=False, relevant=False):
        """Answer as the owner in the burst's conversation. Strategic requesters: only a the owner-approved draft, verbatim.
        P38 (F29): refused with AGAIN_ERROR when the burst was already answered and the requester wrote nothing the earlier
        answer could not see (a retry, a crash, an unknown delivery, a fresh re-dispatch), unless `again` (a deliberate
        further message). The task the burst was handed to is held only to its own earlier answers (_own_scope).
        P46.3 (R-0013): a task that <owner key>-inbox / <owner key>-oncall handed this burst to as a possible answer to its ask, or that
        got it only through its conversation window (by_window), replies only with `relevant` (it checked that the message is
        for it), before anything is posted or recorded."""
        now = time.time() if now is None else now
        b = burst(db, ident)
        who, provider, session, acting = self.caller(rt, db, b, with_task=True)
        cfg = _cfg(rt)['intake']
        if acting and acting not in (cfg['inbox_task'], cfg['oncall_task']) and not relevant and \
                rt.rule_registry.active('send.relevance_ack'):
            if self.handed_as_answer(rt, db, b, acting, cfg, now):
                rt.rule_registry.refuse('send.relevance_ack', f'handed_off_message_needs_--relevant: {cfg["inbox_task"]} '
                                        f'handed this message to {acting} as a possible answer to its ask; check that it '
                                        'answers it')
            if acting == b['target_task'] and by_window(db, b):  # the orchestrator's decision on Needs the owner 3
                rt.rule_registry.refuse('send.relevance_ack', f"window_message_needs_--relevant: this message reached {acting} only through its conversation window and is not in a thread of its own; it may be {self.name}'s own chat with this person, check that it is for you")
        _, _, g = _handling(rt, b)
        basis = 'intake_reply'
        if decision_event:
            option, brief = self._verified_card(db, decision_event, b['burst_id'], 'draft')
            if option != 'send':
                raise ValueError(f'{self.key}_did_not_approve_sending_this_draft')
            if brief['digest'] != digest_text(text):
                raise ValueError('reply_text_differs_from_the_approved_draft')
            basis = f'{self.key}_decision:{decision_event}'
        elif not g['may_reply']:
            raise ValueError(f'strategic_directive_reply_requires_a_{self.key}_approved_draft')
        task = db.execute('SELECT path FROM tasks WHERE task_id=?', (b['handled_task'] or b['target_task'],)).fetchone()
        owner = {'session': session, 'provider': provider or 'codex', 'cwd': task['path'] if task else os.getcwd()}
        ts = self._post_as_owner(rt, db, b, text, 'intake_reply', owner, now, again=again, own=_own_scope(b, acting), actor=who,
                           task_id=acting)
        record = self.record_triage(rt, db, b, intent='question', clarity='clear', disposition='answer', basis=basis,
                               recorded_by=who, now=now, enforce=False)
        return {'ok': True, 'burst_id': b['burst_id'], 'ts': ts, 'thread': _reply_thread(b), 'triage_record': record}

    def intake_follow(self, rt, db, db_path, ident, now=None):
        """Watch the burst's conversation from the calling inbox/oncall session (follow-ups + `ask` cards)."""
        now = time.time() if now is None else now
        b = burst(db, ident)
        who, provider, session = self.caller(rt, db, b, roles=('target',))
        if not session:
            raise ValueError('intake_follow_is_for_the_inbox_or_oncall_session')
        if not _handling(rt, b)[2]['may_reply']:
            raise ValueError(f'strategic_directive_never_follow_or_ask_the_requester_without_{self.key}')
        if b['smoke']:
            raise ValueError('smoke_burst_has_no_slack_conversation')
        binding = _bind(rt, db, b, b['target_task'], who, now, mark_handled=False)
        task = db.execute('SELECT path FROM tasks WHERE task_id=?', (b['target_task'],)).fetchone()
        wid = Intake(db, db_path, rt, core=self).bind_watch(binding, provider, session, task['path'], now)
        if wid is None:
            error = db.execute('SELECT error FROM intake_bindings WHERE channel=? AND thread_root=?',
                               (binding['channel'], binding['thread_root'])).fetchone()[0]
            raise ValueError('follow_watch_failed: ' + str(error))
        hint = (f'ask --dm {b["sender"]} …' if b['conv_type'] == 'im' else
                f'ask --channel {b["channel"]} --thread {_reply_thread(b) or b["first_ts"]} …')
        rt.start(db_path)
        return {'ok': True, 'burst_id': b['burst_id'], 'watch_id': wid, 'ask': hint}

    def oncall_incident(self, rt, db, db_path, ident, title, brief, summary, *, provider='auto', category='infra', slug=None,
                        wait_seconds=90.0, now=None, runner=None):
        """Big incident: an incident task (child of <owner key>-oncall) plus a HIGH-PRIORITY ask-<owner key> card."""
        now = time.time() if now is None else now
        b = burst(db, ident)
        if not b['alert_fp'] and b['route'] != 'oncall':
            raise ValueError('oncall_incident_needs_an_oncall_burst')
        summary = str(summary or '').strip()
        if not summary or len(summary) > 800:
            raise ValueError('summary_must_be_1_to_800_characters')
        # The card text is checked before the task is launched, so a refused card never leaves a second task behind.
        rt.buttons.check_copy(summary, 'summary', first_line_max=None)
        rt.buttons.check_copy(title, 'title', first_line_max=None)
        # P38 (F49): so is the card's destination (Slack-verified private, cached): a refused card leaves no running incident
        # task behind, and a retry never launches a second one. ask_<owner key> re-checks it from the cache.
        decisions = rt.buttons.load_config()
        rt.buttons.require_private_destination(rt, db, rt.buttons.card_route(decisions, now)['channel'], 'incident_card',
                                               config=decisions)
        config = _cfg(rt)
        launched = self.intake_launch(rt, db, db_path, ident, title, brief, provider=provider, category=category, important=True,
                                 ack=False, parent=config['intake']['oncall_task'] if rt.registry.task_dir(
                                     config['intake']['oncall_task']).is_dir() else None, slug=slug, incident=True,
                                 wait_seconds=wait_seconds, now=now, runner=runner)
        short = ' '.join(str(title).split())
        short = short if len(short) <= 60 else short[:59] + '…'
        extra = self.msg('intake.oncall_incident.1', task_id=launched['task_id']) + (self.msg('intake.oncall_incident.2', alert_service=b['alert_service'], dup_count=b['dup_count']) if b['alert_fp'] else '')
        # P3.2: the incident card belongs to the incident task and establishes its approvals-thread root (the task's own
        # headless cards thread under it); high priority, so if it ever threads it also broadcasts to the channel.
        card = self._post_card(rt, db, db_path, b, 'incident', self.msg('intake.oncall_incident.3', short=short),
                          summary, self.incident_options, now, priority='high', never_auto=False, footer=self._source_footer(b, extra),
                          task_thread=True, cwd=str(launched['workspace']))
        return {**launched, 'incident_card_event': card['event_id'], 'incident_card_ts': card['thread_ts']}

    def intake_status(self, rt, db, now=None):
        now = time.time() if now is None else now
        engine = Intake(db, rt.DEFAULT_DB, rt, core=self)
        state = engine.load_state()
        try:
            cfg = _cfg(rt)['intake']
            config_error = None
        except ValueError as exc:
            cfg, config_error = None, str(exc)
        counts = {r[0]: r[1] for r in db.execute('SELECT state, count(*) FROM intake_bursts GROUP BY state')}
        filtered = {r[0]: r[1] for r in db.execute('SELECT reason, count(*) FROM intake_seen WHERE seen_at>? GROUP BY reason',
                                                   (now - 86400,))}
        tasks = {}
        if cfg:
            for role in ('inbox', 'oncall'):
                row = db.execute('SELECT current_provider,current_session FROM tasks WHERE task_id=?',
                                 (cfg[f'{role}_task'],)).fetchone()
                tasks[role] = {'task_id': cfg[f'{role}_task'], 'exists': rt.registry.task_dir(cfg[f'{role}_task']).is_dir(),
                               'current': dict(row) if row else None}
        hour = sum(c for at, c in state.get('calls_last_hour') or [] if at > now - 3600)
        return {'ok': True, 'enabled': bool(cfg and cfg['enabled']), 'config_error': config_error,
                'bootstrapped': bool(cfg and tasks['inbox']['exists']), 'tasks': tasks, 'status': state.get('status'),
                'baseline': state.get('baseline'), 'next_cycle_at': state.get('next_cycle_at'),
                'covered_until': state.get('covered_until'), 'last_cycle': state.get('last_cycle'),
                'api_calls_last_hour': hour, 'backoff': state.get('backoff') or {},
                'unresolved_channels': state.get('unresolved_channels') or [], 'errors': state.get('errors'),
                'last_error': state.get('last_error'), 'bursts_by_state': counts, 'messages_last_24h_by_reason': filtered,
                'fetch_queue': db.execute('SELECT count(*) FROM intake_fetch_queue').fetchone()[0]}

    def intake_probe(self, rt, queries=None):
        """Read-only: run each discovery query once (page 1) and report counts by conversation type; no text."""
        cfg = _cfg(rt)['intake']
        queries = list(queries or []) or [*cfg['dm_queries'], self.mention_query]
        if len(queries) > 6:
            raise ValueError('at_most_6_queries')
        token = rt.api_identity()
        out = []
        for query in queries:
            try:
                response = rt.slack.api_call('search.messages', token, params={'query': query, 'count': 20,
                                                                               'sort': 'timestamp', 'sort_dir': 'desc'})
            except Exception as exc:  # noqa: BLE001
                out.append({'query': query, 'ok': False, 'error': _slack_code(exc)})
                continue
            block = response.get('messages') if isinstance(response.get('messages'), dict) else {}
            matches = [m for m in block.get('matches') or [] if isinstance(m, dict)]
            types = {}
            replies = mentions = 0
            for match in matches:
                kind = match_conv_type(match)
                types[kind] = types.get(kind, 0) + 1
                root = parse_permalink_thread(match.get('permalink'))
                replies += bool(root and root != match.get('ts'))
                mentions += self.mention_query in str(match.get('text') or '')
            newest = max((float(m['ts']) for m in matches if TS_RE.fullmatch(str(m.get('ts')))), default=None)
            out.append({'query': query, 'ok': True, 'total': (block.get('paging') or {}).get('total', block.get('total')),
                        'page_matches': len(matches), 'by_conversation_type': types, 'thread_replies': replies,
                        f'mentioning_{self.key}': mentions, 'newest_age_seconds': round(time.time() - newest) if newest else None})
        return {'ok': True, 'queries': out,
                'hint': 'A DM-discovery query should return mostly im/mpim matches; adjust intake.dm_queries otherwise.'}

    def intake_push(self, rt, db, db_path, channel, ts, user, thread_ts=None, now=None):
        """0.77.0 bot intake: the receiver's refs for an @mention of the bot or a DM to it (`intake-push`). Only with
        `intake.identity: bot`: the message is read back with the bot token and takes the same filter -> burst path as
        a polled one (`intake_seen` dedups); the refs carry no authority, the read-back message decides."""
        now = time.time() if now is None else now
        if not (CHANNEL_ID_RE.fullmatch(str(channel)) and TS_RE.fullmatch(str(ts)) and USER_RE.fullmatch(str(user))
                and (thread_ts is None or TS_RE.fullmatch(str(thread_ts)))):
            return {'ok': False, 'outcome': 'refused', 'reason': 'invalid_refs'}
        cfg = _cfg(rt)['intake']
        if not cfg['enabled'] or cfg.get('identity') != 'bot':
            return {'ok': True, 'outcome': 'skipped', 'reason': 'bot_intake_off'}
        engine = Intake(db, db_path, rt, core=self)
        state = engine.load_state()
        if state.get('baseline') is None:
            return {'ok': True, 'outcome': 'skipped', 'reason': 'intake_not_started'}
        token = rt.api_identity(bot=True)
        params = {'channel': channel, 'latest': ts, 'oldest': ts, 'inclusive': 'true', 'limit': 1}
        found = rt.slack.api_call('conversations.replies' if thread_ts else 'conversations.history', token,
                                  params={**params, 'ts': thread_ts} if thread_ts else params)
        m = next((x for x in found.get('messages') or [] if isinstance(x, dict) and x.get('ts') == ts), None)
        if m is None:
            return {'ok': False, 'outcome': 'refused', 'reason': 'message_not_found'}
        if str(m.get('user') or '') != user:
            return {'ok': False, 'outcome': 'refused', 'reason': 'author_mismatch'}
        control = getattr(getattr(rt, 'LOOP_CORE', None), 'control', None)
        if control is not None and m.get('thread_ts') in (None, '', ts) and user in _control.owner_users(rt) and \
                channel in rt.card_threads.decision_channels(rt) and control.card_channel_on(rt)[0]:
            return {'ok': True, 'outcome': 'skipped', 'reason': 'control_entry'}  # the control entry takes it
        oncall, _ = self.resolve_channels(cfg['oncall_channels'])
        ctx = {'subscribed': set(), 'oncall': set(oncall), 'now': now, 'cfg': cfg, 'baseline': float(state['baseline']),
               'stats': {'processed': 0, 'accepted': 0, 'filtered': {}}, 'state': state}
        engine.ingest([m], channel, 'im' if channel.startswith('D') else 'channel', 'push', ctx)
        row = db.execute('SELECT reason, burst_id FROM intake_seen WHERE channel=? AND ts=?', (channel, ts)).fetchone()
        if row is None:
            return {'ok': True, 'outcome': 'skipped', 'reason': 'before_intake_baseline'}
        out = {'ok': True, 'outcome': 'accepted' if row['burst_id'] else 'filtered', 'reason': row['reason'],
               'burst_id': row['burst_id']}
        if row['burst_id'] and cfg.get('ack_on_receipt', True):
            out['receipt'] = self.ack_receipt(rt, token, channel, ts)
        return out

    @staticmethod
    def ack_receipt(rt, token, channel, ts):
        """0.78.2 (F12): a message the bot accepted gets 👀 at once, as the bot (the run's own receipts and ✅ follow
        later). Best effort and idempotent: never raises, `already_reacted` counts as done."""
        try:
            rt.slack.api_call('reactions.add', token, json_body={'channel': channel, 'timestamp': ts, 'name': ACK_EMOJI})
            return 'added'
        except Exception as exc:  # noqa: BLE001 - a receipt never blocks intake
            if 'already_reacted' in str(exc):
                return 'already'
            return 'failed:' + (rt.safe_error(exc) if hasattr(rt, 'safe_error') else type(exc).__name__)

    def smoke_intake(self, rt, db, db_path, scenario='request', wait=True, timeout=900.0, poll=3.0, now=None):
        """Post-install E2E with a SYNTHETIC burst (no Slack read): through the real filter -> burst -> router ->
        delivery path into the live <owner key>-inbox (request / strategic) or <owner key>-oncall (alert) session."""
        if scenario not in ('request', 'strategic', 'alert'):
            raise ValueError('scenario_must_be_request_strategic_or_alert')
        now = time.time() if now is None else now
        config = _cfg(rt)
        cfg = config['intake']
        role = 'oncall' if scenario == 'alert' else 'inbox'
        task_id = cfg[f'{role}_task']
        row = db.execute('SELECT current_session FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if not rt.registry.task_dir(task_id).is_dir() or not row or not row['current_session']:
            raise ValueError(f'run_intake-bootstrap_first_{role}_task_has_no_current_session')
        engine = Intake(db, db_path, rt, core=self)
        state = engine.load_state()
        baseline = min(float(state.get('baseline') or now), now - 1)
        ctx = {'subscribed': set(), 'oncall': {SMOKE_ONCALL}, 'now': now, 'cfg': cfg, 'baseline': baseline,
               'stats': {'processed': 0, 'accepted': 0, 'filtered': {}}, 'state': state}
        if scenario == 'alert':
            # A per-run service name (letters only: digits/hex are volatile in the fingerprint) keeps runs apart.
            nonce = ''.join(chr(ord('g') + int(c, 16)) for c in uuid.uuid4().hex[:6])
            text = self.smoke_text['alert'].replace('smoke-intake-test', f'smoke-intake-{nonce}')
            messages = [{'ts': f'{now - 2:.6f}', 'bot_id': 'BSMOKE', 'username': 'smoke-alerts', 'subtype': 'bot_message',
                         'text': text},
                        {'ts': f'{now - 1:.6f}', 'bot_id': 'BSMOKE', 'username': 'smoke-alerts', 'subtype': 'bot_message',
                         'text': text.replace('[Triggered]', '[Re-Triggered]')}]
            channel, conv_type = SMOKE_ONCALL, 'channel'
        else:
            messages = [{'ts': f'{now - 1:.6f}', 'user': SMOKE_USER, 'text': '(synthetic)'}]
            channel, conv_type = SMOKE_DM, 'im'
        def ingest_one(m):
            with rt.transaction(db):
                db.execute("INSERT OR IGNORE INTO intake_seen (channel,ts,source,conv_type,user,sender_kind,digest,reason,"
                           "seen_at) VALUES (?,?,'smoke',?,?,?,?,'pending',?)",
                           (channel, m['ts'], conv_type, m.get('user'), 'bot' if m.get('bot_id') else 'human',
                            digest_message(m), now))
                verdict = engine.verdict(m, channel, conv_type, None, ctx)
                if verdict != 'accept':
                    raise ValueError('smoke_message_was_filtered_' + verdict)
                burst_id, how = engine.add_to_burst(m, channel, conv_type, None, 'bot' if m.get('bot_id') else 'human', ctx,
                                                    smoke_scenario=scenario)
                db.execute('UPDATE intake_seen SET reason=?,burst_id=? WHERE channel=? AND ts=?',
                           (how, burst_id, channel, m['ts']))
            return burst_id, how

        burst_id, _ = ingest_one(messages[0])
        with rt.transaction(db):  # a synthetic burst is complete: seal it now instead of waiting for the window
            db.execute("UPDATE intake_bursts SET state='sealed',sealed_at=?,updated_at=? WHERE burst_id=? AND state='open'",
                       (now, now, burst_id))
        b = db.execute('SELECT * FROM intake_bursts WHERE burst_id=?', (burst_id,)).fetchone()
        outcome = engine.route_one(b, now, config)[1]
        for m in messages[1:]:  # alert: the repeat arrives after routing and must be merged, not delivered again
            _, how = ingest_one(m)
            if how != 'alert_duplicate':
                raise ValueError('smoke_alert_repeat_was_not_deduplicated')
        b = db.execute('SELECT * FROM intake_bursts WHERE burst_id=?', (burst_id,)).fetchone()
        report = {'ok': outcome == 'routed', 'test': True, 'scenario': scenario, 'burst_id': burst_id, 'route': b['route'],
                  'target_task': b['target_task'], 'event_id': b['event_id'], 'msg_count': b['msg_count'],
                  'dup_count': b['dup_count'], 'delivery': outcome}
        rt.start(db_path)
        if not wait or outcome != 'routed':
            return {**report, 'next': f'slack_followup.py intake-status; dispatch-status --task {task_id}'}
        end = time.time() + timeout
        while time.time() < end:
            e = db.execute('SELECT status FROM events WHERE id=?', (b['event_id'],)).fetchone()
            child = db.execute('SELECT handled_task FROM intake_bursts WHERE burst_id=?', (burst_id,)).fetchone()['handled_task']
            busy = child and db.execute("SELECT count(*) FROM dispatches WHERE task_key=? AND state IN ('waiting','running')",
                                        (child,)).fetchone()[0]
            if e and e['status'] == 'handled' and not busy:
                break
            time.sleep(poll)
        b = db.execute('SELECT * FROM intake_bursts WHERE burst_id=?', (burst_id,)).fetchone()
        records = [dict(r) for r in db.execute(f'SELECT id,intent,clarity,disposition,target_task,needs_{self.key} FROM triage_records WHERE burst_id=? ORDER BY created_at', (burst_id,))]
        briefs = [dict(r) for r in db.execute('SELECT card_event,kind FROM intake_briefs WHERE burst_id=?', (burst_id,))]
        report.update(event_status=(db.execute('SELECT status FROM events WHERE id=?', (b['event_id'],)).fetchone() or [None])[0],
                      triage=records, briefs=briefs, handled_task=b['handled_task'],
                      external_posts=db.execute("SELECT count(*) FROM agent_sends WHERE ref=? AND kind IN "
                                                "('intake_ack','intake_reply')", (burst_id,)).fetchone()[0])
        if b['handled_task']:
            path = rt.registry.task_dir(b['handled_task'])
            status_md = path / 'status.md'
            report['child'] = {'task_id': b['handled_task'], 'workspace': str(path),
                               'status_md_line': status_md.exists() and '- smoke-intake ok' in status_md.read_text(encoding='utf-8'),
                               'dispatches': [dict(r) for r in db.execute(
                                   'SELECT dispatch_id,state,outcome,card_ts FROM dispatches WHERE task_key=?',
                                   (b['handled_task'],))]}
        if scenario == 'request':
            report['ok'] = bool(report['event_status'] == 'handled' and report.get('child', {}).get('status_md_line'))
        elif scenario == 'strategic':
            report['ok'] = bool(report['event_status'] == 'handled' and any(x['kind'] == 'brief' for x in briefs)
                                and not b['handled_task'] and not report['external_posts'])
        else:
            report['ok'] = bool(report['event_status'] == 'handled' and b['dup_count'] >= 1)
        return report


# The owner's intake playbook (K14p): what an owner pack provides for the long-lived inbox / oncall tasks.
PLAYBOOK_NAMES = ('POLICY_BEGIN', 'POLICY_END', 'ADDENDA_HEADING', 'ADDENDA_BEGIN', 'ADDENDA_END', 'BLOCKERS_BEGIN',
                  'BLOCKERS_END', 'CARDS_BEGIN', 'CARDS_END', 'INTERACTION_BEGIN', 'INTERACTION_END', 'LIFECYCLE_BEGIN',
                  'LIFECYCLE_END', 'FEEDBACK_BEGIN', 'FEEDBACK_END', 'HANDOFF_BEGIN', 'HANDOFF_END', 'ROLE_TITLE',
                  'rules_policy_block', 'blockers_policy_block', 'cards_policy_block', 'interaction_policy_block',
                  'lifecycle_policy_block', 'feedback_policy_block', 'handoff_policy_block', 'inbox_goal', 'oncall_goal',
                  'role_status', 'role_launch_prompt', 'intake_bootstrap')
PLAYBOOK_CONTEXT = ('references', 'rules_path', 'bootstrap_key', 'read_config', 'run_bootstrap', 'default_inbox')


def intake_playbook(pack, **context):
    """The owner's intake playbook from an owner pack: `pack.intake_playbook(**context)` — the code-managed goal.md
    markers (BEGIN / END pairs: a file-format contract with the goal.md files on disk and with the access policy's
    inbox sync) and the 7 policy blocks of the long-lived inbox / oncall tasks, their goal templates, titles, first
    status and launch prompt, and intake_bootstrap. The deployment passes its references directory, inbox rules file,
    workspace bootstrap and default inbox task, and the engine's bootstrap key and config reader (PLAYBOOK_CONTEXT).
    None when there is no pack or it has no intake playbook; a playbook without one of PLAYBOOK_NAMES is refused."""
    make = getattr(pack, 'intake_playbook', None)
    if make is None:
        return None
    if sorted(context) != sorted(PLAYBOOK_CONTEXT):
        raise TypeError('intake_playbook needs exactly: ' + ', '.join(PLAYBOOK_CONTEXT))
    playbook = make(**context)
    missing = [name for name in PLAYBOOK_NAMES if not hasattr(playbook, name)]
    if missing:
        raise ValueError('intake_playbook_incomplete: ' + ', '.join(missing))
    return playbook


def unavailable_playbook(reason='no_intake_playbook'):
    """O1c2: what a deployment without an intake playbook binds in its place. The goal-file markers are neutral strings
    (an owner's own goal.md never holds them), ROLE_TITLE is empty, and every playbook function refuses with
    `intake_playbook_unavailable` (the reason), so intake cannot run on a deployment whose owner pack has none."""
    def refuse(*_args, **_kwargs):
        raise ValueError(f'intake_playbook_unavailable: {reason}')
    names = {}
    for name in PLAYBOOK_NAMES:
        if name.endswith(('_BEGIN', '_END')):
            names[name] = f'<!-- kage-{name.lower().replace("_", "-")} -->'
        elif name == 'ADDENDA_HEADING':
            names[name] = '## Addenda'
        elif name == 'ROLE_TITLE':
            names[name] = {}
        else:
            names[name] = refuse
    return types.SimpleNamespace(**names)


def run_workspace_bootstrap(bootstrap, title, brief, context, *, agent, root, ticket=False, slug=None, date=None,
                            folder_name=None, runner=None, linear=None):
    """The deployment's workspace bootstrap script (`bootstrap`, its path) with --no-launch (never a terminal tab); returns
    its JSON report. A new ticket starts Todo (In Progress only while a session runs). `folder_name`: a local-only
    workspace with exactly this name (the plain-named long-lived tasks). O1c2: a deployment without a bootstrap script
    is refused before anything runs (`workspace_bootstrap_unavailable`). `linear`: `ticket`'s pre-N2 name."""
    ticket = bool(ticket or linear)
    if runner is None and not Path(bootstrap).is_file():
        raise ValueError('workspace_bootstrap_unavailable: this deployment has no workspace bootstrap script')
    runner = runner or subprocess.run
    with tempfile.TemporaryDirectory(prefix='intake-bootstrap-') as tmp:
        brief_file, context_file = Path(tmp) / 'brief.md', Path(tmp) / 'context.md'
        brief_file.write_text(brief, encoding='utf-8')
        context_file.write_text(context, encoding='utf-8')
        cmd = [sys.executable, str(bootstrap), '--no-launch', '--title', title, '--brief-file', str(brief_file),
               '--context-file', str(context_file), '--root', str(root), '--agent', agent]
        if not ticket:
            cmd.append('--no-ticket')
        else:
            cmd += ['--state', 'Todo']  # tickets start Todo, not the bootstrap default In Progress
        if slug:
            cmd += ['--slug', slug]
        if date:
            cmd += ['--date', date]
        if folder_name:
            cmd += ['--folder-name', folder_name]
        result = runner(cmd, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        detail = re.sub(r'[^\w .:-]+', ' ', (result.stderr or '').strip().splitlines()[-1] if result.stderr else '')[:160]
        raise ValueError('workspace_bootstrap_failed: ' + (detail or f'exit {result.returncode}'))
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise ValueError('workspace_bootstrap_returned_invalid_json') from None
