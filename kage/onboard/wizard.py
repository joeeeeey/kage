"""The `kage init` wizard (O1c4): about ten questions on a terminal, each with a default, then the Slack app, the tokens
and the owner's proof. It writes the answers file (kage.onboard.answers) and the verified records; `kage init` then
generates the deployment from them (kage.onboard.init).

Tokens are read without echo, checked with Slack and only then stored (kage.onboard.tokens.admit); none is printed,
written to the answers or passed in argv. The owner is proven by a one-time code DMed to the bot, or by the user token
in the `as_owner` voice (kage.onboard.identity); a typed Slack id is never asked for. Everything that talks to the
person or to Slack is injected (`io`, `call`, `sleep`, `clock`), so tests run it end to end with fakes.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path
import pwd
import time

from kage import profile
from kage.onboard import answers as _answers
from kage.onboard import identity, init, manifest, tokens
from kage.ports import secrets

TRIES = 3
POLL_SECONDS = 5
SCOPES_STEP = '''
  The bot lacks scopes: {missing}
  The app was not made from the manifest above, or not reinstalled after it changed. In Slack:
    a. https://api.slack.com/apps → this app → App Manifest: replace it with the manifest above, Save
    b. Install App → Reinstall to {team}, Allow
    c. OAuth & Permissions → copy the Bot User OAuth Token (xoxb-…) and paste it below'''


class WizardError(ValueError):
    """The wizard stopped (said why); the answers file was not written."""


class TerminalIO:
    def ask(self, prompt, default=''):
        shown = f'{prompt} [{default}]: ' if default else f'{prompt}: '
        value = input(shown).strip()
        return value or default

    def secret(self, prompt):
        return getpass.getpass(f'{prompt} (not shown): ').strip()

    def say(self, text=''):
        print(text, flush=True)


def default_name():
    try:
        gecos = pwd.getpwuid(os.getuid()).pw_gecos.split(',')[0].strip()
    except (KeyError, AttributeError):
        gecos = ''
    return gecos or getpass.getuser()


def default_timezone():
    try:
        link = os.readlink('/etc/localtime')
    except OSError:
        return 'UTC'
    marker = 'zoneinfo/'
    return link.split(marker, 1)[1] if marker in link else 'UTC'


def default_language(environ=None):
    lang = (environ or os.environ).get('LANG', '')
    return 'zh-CN' if lang.lower().startswith('zh') else 'en'


def _choice(io, prompt, options, default):
    for _ in range(TRIES):
        value = io.ask(f"{prompt} ({' / '.join(options)})", default)
        if value in options:
            return value
        io.say(f'  please answer one of: {", ".join(options)}')
    raise WizardError(f'no_valid_answer: {prompt}')


def _valid(io, prompt, default, ok, hint):
    for _ in range(TRIES):
        value = io.ask(prompt, default)
        if ok(value):
            return value
        io.say(f'  {hint}')
    raise WizardError(f'no_valid_answer: {prompt.strip()}')


def _timezone_ok(name):
    try:
        _answers.fallback_offset(name)
        return True
    except ValueError:
        return False


def questions(io, environ=None):
    """The raw answers (validated by kage.onboard.answers)."""
    name = _valid(io, '1. Your name', default_name(), lambda v: 0 < len(v) <= 80, 'one line, up to 80 characters')
    key = _valid(io, '   A short key for stored names (lower-case, e.g. <key>-inbox)', _answers.default_key(name),
                 lambda v: bool(profile.OWNER_KEY_RE.fullmatch(v)), 'a lower-case word: letters, digits, _')
    short_name = _valid(io, '   How texts name you', name.split()[0], lambda v: 0 < len(v) <= 80, 'one short line')
    io.say('2. Channel: Slack (the only one in this release)')
    language = _choice(io, '3. Language', _answers.LANGUAGES, default_language(environ))
    timezone = _valid(io, '   Timezone', default_timezone(), _timezone_ok, 'an IANA name such as Europe/Berlin')
    voice = _choice(io, '4. Voice: the assistant bot speaks, or as you (needs a user token)', _answers.VOICES,
                    'assistant')
    cards = _valid(io, '5. Where approval cards go: dm (your DM with the bot) or a private channel id', 'dm',
                   lambda v: v == 'dm' or bool(_answers.CARD_CHANNEL_RE.fullmatch(v)), 'dm, or a channel id such as C0000EXAMPLE')
    io.say('6. Task tracker: none (a tracker adapter comes later)')
    root = _valid(io, '7. Task workspaces folder', _answers.DEFAULT_WORKSPACE_ROOT,
                  lambda v: v.startswith(('~/', '/')), 'an absolute path or one starting with ~/')
    return {'schema': _answers.SCHEMA,
            'owner': {'name': name, 'short_name': short_name, 'key': key, 'language': language, 'timezone': timezone},
            'channel': 'slack',
            'slack': {'voice': voice, 'cards': cards,
                      'secrets': {n: secrets.ref(key, n) for n in _answers.REQUIRED_SECRETS.get(voice, ())}},
            'tracker': 'none', 'workspace_root': root}


def _token(io, store, name, label, admit):
    """Read, check and store one token (TRIES attempts); returns admit's result."""
    for _ in range(TRIES):
        value = io.secret(label)
        try:
            return admit(value)
        except tokens.TokenRefused as exc:
            io.say(f'  refused: {exc.code}; nothing was stored')
    raise WizardError(f'token_refused: {name}')


def run(io=None, *, home=None, environ=None, call=tokens.slack_call, store=None, sleep=time.sleep, clock=time.time,
        answers_path=None, prove_timeout=identity.CODE_TTL):
    """The whole wizard. Returns (answers, answers file path)."""
    io = io or TerminalIO()
    home = Path(home or Path.home())
    raw = questions(io, environ)
    try:
        answers = _answers.validate(raw)
    except _answers.AnswersError as exc:
        raise WizardError(f'answers_invalid: {exc}') from None
    key, voice = answers['owner']['key'], answers['slack']['voice']
    state = init.state_dir(key, home)
    init._private_mkdir(state)
    store = store or secrets.for_owner(key, home=home)
    display = f"{answers['owner']['short_name']}'s kage"
    io.say('\n8. Create the Slack app from this manifest:\n')
    io.say(manifest.render(display, as_owner=voice == 'as_owner'))
    io.say()

    def admit_bot(token):
        return tokens.admit(store, tokens.BOT, token, call=call)

    bot = _token(io, store, tokens.BOT, 'Bot token (xoxb-…)', admit_bot)
    for _ in range(TRIES):  # F7: an app not made from the manifest (or not reinstalled) stops here, not later
        if not bot.get('missing_scopes'):
            break
        io.say(SCOPES_STEP.format(missing=', '.join(bot['missing_scopes']), team=bot.get('team') or 'the workspace'))
        bot = _token(io, store, tokens.BOT, 'Bot token after the reinstall (xoxb-…)', admit_bot)
    if bot.get('missing_scopes'):
        raise WizardError('bot_scopes_missing: ' + ', '.join(bot['missing_scopes']))
    try:
        app_id = tokens.bot_app(store.get(tokens.BOT), bot, call=call)
    except tokens.TokenRefused as exc:
        raise WizardError(f'bot_app_unknown: {exc.code}') from None
    identity.record_bot(state, bot, app_id, clock=clock)
    _token(io, store, tokens.APP, 'App-level token (xapp-…, connections:write)',
           lambda t: tokens.admit(store, tokens.APP, t, call=call))
    if voice == 'as_owner':
        user = _token(io, store, tokens.USER, 'User token (xoxp-…)',
                      lambda t: tokens.admit(store, tokens.USER, t, call=call, bot=bot))
        record = identity.prove_by_user_token(state, user, clock=clock)
    else:
        record = _prove_by_dm(io, state, store, bot, display, call=call, sleep=sleep, clock=clock,
                              timeout=prove_timeout)
    io.say(f"  verified: you are {record['user_id']} in {record['team_id']} ({record['method']})")
    path = Path(answers_path or init.config_dir(key, home) / 'answers.json')
    init._private_mkdir(path.parent)
    _answers.write(path, answers)
    io.say(f'  answers written to {path} (no token in it)')
    return answers, path


def _prove_by_dm(io, state, store, bot, display, *, call, sleep, clock, timeout):
    code = identity.new_challenge(state, clock=clock)
    io.say(f'\n9. Prove it is you: send this code to {display} as a direct message in Slack: {code}')
    io.say('   (waiting up to 10 minutes; nobody else sees the code)')
    started = clock()
    while True:
        try:
            return identity.prove_by_dm(state, store.get(tokens.BOT), bot, call=call, clock=clock)
        except tokens.TokenRefused as exc:
            if exc.code != 'identity_code_not_found_in_the_bots_direct_messages' or clock() - started > timeout:
                raise WizardError(f'owner_not_verified: {exc.code}') from None
        sleep(POLL_SECONDS)
