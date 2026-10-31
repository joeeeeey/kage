"""Intake (K11a core, K11b engine): the pure helpers of the intake — channel names, permalinks, message text
and digests, ticket / PR references, alert fingerprints, local times — the people-registry handling fields
and display names, the reader-language gate and the owner's private notes guard; and the engine inside the
supervisor: Slack discovery (search, conversation cursors, exact fetches, per-endpoint backoff, a call
budget), the filter, bursts (alert dedupe, DM settle), deterministic routing, delivery (a refs-only file and
an intake event) and request bindings. `rt` is the loaded runtime module.
"""
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import uuid

from kage import locale

SLACKBOT = 'USLACKBOT'


TS_RE = re.compile(r'\d+\.\d+')


USER_RE = re.compile(r'[UW][A-Z0-9]{2,}')


CHANNEL_ID_RE = re.compile(r'[CDG][A-Z0-9]{2,}')


BURST_ID_RE = re.compile(r'in[0-9a-f]{14}')


EVENT_ID_RE = re.compile(r'[0-9a-f]{24}')


ALLOWED_SUBTYPES = (None, 'file_share', 'thread_broadcast', 'bot_message', 'me_message')


STATE_KEY = 'intake_state'


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


REQUEST_STYLES = ('standard', 'strategic_directive')


HANDLING_FIELDS = ('request_style', 'auto_execute')


DEFAULT_ACK = '收到，在弄～'


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
            self.token = self.rt.api_identity()
            try:
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
        if user == self.core.owner_slack_user:
            return f'{self.core.key}_authored'
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
                                 + (f' · Linear {t["linear_state"]}' if t['linear_state'] else '')
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


class IntakeCore:
    """The owner-bound half of the intake: the owner's Slack user, bot and app, the workspace archive URL, the
    channel names, the ticket pattern and time zone; the owner's private notes and the send guard; the
    store, the request helpers and the owner-facing hints the engine (Intake, which holds one) uses."""

    def __init__(self, language, *, tz, owner_key, owner_name, owner_slack_user, bot_user, app_id, archive_url, known_channels, ticket_ref_re, agent_event_type, ticket_ref_reason):
        self.language = language
        self.tz = tz
        self.owner_slack_user = owner_slack_user
        self.bot_user = bot_user
        self.app_id = app_id
        self.archive_url = archive_url
        self.known_channels = known_channels
        self.ticket_ref_re = ticket_ref_re
        self.agent_event_type = agent_event_type
        self.ticket_ref_reason = ticket_ref_reason
        self.key, self.name = owner_key, owner_name
        self.mention_query = f'<@{self.owner_slack_user}>'
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

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

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
        token): bot_id / app_id, a U… / W… `user` (not our bot, not the owner), not our app, the name (bot_profile.name, else
        username) matching an `intake.delegate_bot_names` fnmatch pattern, any case ([] = off). Else None."""
        profile = m.get('bot_profile') if isinstance(m.get('bot_profile'), dict) else {}
        name, user = str(profile.get('name') or m.get('username') or ''), str(m.get('user') or '')
        if not (m.get('bot_id') or m.get('app_id')) or not USER_RE.fullmatch(user) or user in (self.bot_user, self.owner_slack_user) \
                or self.app_id in (m.get('app_id'), profile.get('app_id')):
            return None
        names = (cfg or {}).get('delegate_bot_names') or ()
        return name if name and any(fnmatch.fnmatchcase(name.lower(), str(n).lower()) for n in names) else None

    def handed_as_answer(self, rt, db, b, task_id, cfg, now, route=None, reason=None):
        """P46.3 (R-0013): handed over by an intake task to a task that has an open ask to the sender — a candidate answer
        (a P39 continuation hand-over, with no ask, is not)."""
        return bool(task_id) and handed_by_intake(route or b['route'], reason or b['route_reason'], cfg) and any(
            a['task_id'] == task_id for a in Intake(db, rt.DEFAULT_DB, rt, core=self).open_asks(b['sender'], now))
