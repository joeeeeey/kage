"""Intake core (K11a): the pure helpers of the intake — channel names, permalinks, message text and digests,
ticket / PR references, alert fingerprints, local times — the people-registry handling fields and display
names, the reader-language gate (reply in the language the person wrote in; a send-boundary safety net) and
the owner's private notes guard. `rt` is the loaded runtime module (Slack transport, rule registry, people
path).
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
import unicodedata
import urllib.error
import urllib.parse

from kage import locale

TS_RE = re.compile(r'\d+\.\d+')


USER_RE = re.compile(r'[UW][A-Z0-9]{2,}')


CHANNEL_ID_RE = re.compile(r'[CDG][A-Z0-9]{2,}')


AUTH_ERRORS = ('invalid_auth', 'not_authed', 'token_revoked', 'token_expired', 'account_inactive', 'missing_scope',
               'not_allowed_token_type', 'unexpected_slack_identity', 'expected_human_user_token')


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


class IntakeCore:
    """The owner-bound half of the intake core: the owner's Slack user, bot and app, the workspace archive URL,
    the channel names, the ticket pattern and time zone; the owner's private notes and the send guard."""

    def __init__(self, language, *, tz, owner_key, owner_name, owner_slack_user, bot_user, app_id, archive_url, known_channels, ticket_ref_re):
        self.language = language
        self.tz = tz
        self.owner_slack_user = owner_slack_user
        self.bot_user = bot_user
        self.app_id = app_id
        self.archive_url = archive_url
        self.known_channels = known_channels
        self.ticket_ref_re = ticket_ref_re
        self.key, self.name = owner_key, owner_name

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

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
