"""Slack side of ask cards: responders are Slack users, text is mrkdwn with `<@U…>` mentions, the card is Block Kit.

The asked text mentions only the responders (they are prefixed when the text names nobody) and never broadcasts;
labels are plain text; nothing may look like a credential (kage.core.secrets).
"""
import re

from kage.core import asks as core
from kage.core.secrets import check_secret_free

ASK_PREFIX = 'sf_ask:'
ASK_CONFIRM_PREFIX = 'sf_askconfirm:'
ACTIONS_BLOCK_ID = 'sf_ask_actions'
HINT_BLOCK_ID = 'sf_ask_hint'
RESULT_BLOCK_ID = 'sf_ask_result'
CONFIRM_ACTIONS_BLOCK_ID = 'sf_askconfirm_actions'
USER_RE = re.compile(r'[UW][A-Z0-9]+')
MENTION_RE = re.compile(r'<@([UW][A-Z0-9]+)(?:\|[^>]*)?>')
BROADCAST_RE = re.compile(r'<!(?:here|channel|everyone)[^>]*>|(?<![\w<])@(?:here|channel|everyone)\b')


def mentions(users):
    return ' '.join(f'<@{u}>' for u in users)


def escape(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def note_part(note):
    return f'（{escape(note)}）' if note else ''


def validate_responders(responders):
    responders = list(dict.fromkeys(responders or []))
    if not 1 <= len(responders) <= core.MAX_RESPONDERS or any(not USER_RE.fullmatch(str(u)) for u in responders):
        raise ValueError('ask_requires_1_to_5_responder_user_ids')
    return responders


def validate_labels(specs, parse_option):
    """Ask labels are plain text: no mentions, links or @ at all (stricter than decision cards)."""
    for spec in specs or []:
        _, label, _ = parse_option(spec)
        if core.LABEL_FORBIDDEN_RE.search(label):
            raise ValueError('ask_option_labels_must_be_plain_text_without_mentions_or_links')
        check_secret_free(label)


def prepare_text(text, responders, max_len=core.MAX_TEXT, field='text'):
    """Owner-voice text: mention only the responders (prefixed if absent), never broadcast."""
    text = str(text or '').strip()
    if not text or len(text) > max_len:
        raise ValueError(f'{field}_must_be_1_to_{max_len}_characters')
    check_secret_free(text)
    if BROADCAST_RE.search(text):
        raise ValueError(f'{field}_must_not_broadcast')
    mentioned = set(MENTION_RE.findall(text))
    if mentioned - set(responders):
        raise ValueError(f'{field}_may_only_mention_responders')
    if not mentioned:
        text = mentions(responders) + ' ' + text
    return text


def button(text, action_id, value, style=None):
    element = {'type': 'button', 'text': {'type': 'plain_text', 'text': text, 'emoji': True},
               'action_id': action_id, 'value': value}
    if style:
        element['style'] = style
    return element


def closed_blocks(text, line):
    return [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}},
            {'type': 'context', 'block_id': RESULT_BLOCK_ID, 'elements': [{'type': 'mrkdwn', 'text': line}]}]


class SlackAsks:
    def __init__(self, asks, *, owner_user):
        self.asks, self.owner_user = asks, owner_user

    def card_blocks(self, card_id, text, options, lang=None, hint=True):
        # Labels render as authored; approve-kind is primary, reject-kind danger, one style max.
        elements = [button(o['label'], f'{ASK_PREFIX}{card_id}:{o["id"]}', card_id,
                           {'approve': 'primary', 'reject': 'danger'}.get(o['kind'])) for o in options]
        blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}},
                  {'type': 'actions', 'block_id': ACTIONS_BLOCK_ID, 'elements': elements}]
        if hint:
            blocks.append({'type': 'context', 'block_id': HINT_BLOCK_ID,
                           'elements': [{'type': 'mrkdwn', 'text': self.asks.phrase(lang, 'hint')}]})
        return blocks

    def result_line(self, lang, option, actor, via, at, note=None):
        values = dict(icon=core.KIND_ICON[option['kind']], label=escape(option['label']), note=note_part(note),
                      clock=self.asks.clock(at))
        if via == self.asks.override_via:
            return self.asks.phrase(lang, 'override', **values)
        return self.asks.phrase(lang, 'chose', who=f'<@{actor}>', **values)

    def confirm_text(self, lang, author, option, note):
        mention = '' if author == self.owner_user else f'<@{author}> '
        return self.asks.phrase(lang, 'confirm', mention=mention, label=escape(option['label']), note=note_part(note))

    def confirm_blocks(self, proposal_id, text, lang):
        return [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}},
                {'type': 'actions', 'block_id': CONFIRM_ACTIONS_BLOCK_ID, 'elements': [
                    button(self.asks.phrase(lang, 'confirm_yes'), ASK_CONFIRM_PREFIX + 'yes', proposal_id, 'primary'),
                    button(self.asks.phrase(lang, 'confirm_no'), ASK_CONFIRM_PREFIX + 'no', proposal_id)]}]
