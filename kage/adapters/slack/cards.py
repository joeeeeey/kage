"""Slack side of decision cards: the Block Kit of a card (text, buttons, footer, hint), its receipt and outcome lines,
the line a typed reply leaves, the rule-distillation card body and the confirm-my-reading proposal.

Block and action ids are stored in posted messages and read back by the receiver: they never change. Every word the
owner reads comes from the owner's catalog (`decision.*`); times are the owner's clock. Text is mrkdwn: labels, notes
and excerpts are escaped where they are rendered.
"""
from __future__ import annotations
import datetime as dt

from kage import locale
from kage.core.decisions import KIND_ICON
from kage.adapters.slack.decisions import escape

ACTION_PREFIX = 'sf_decision:'
CONFIRM_PREFIX = 'sf_confirm:'
ACTIONS_BLOCK_ID = 'sf_decision_actions'
HINT_BLOCK_ID = 'sf_decision_hint'
OUTCOME_BLOCK_ID = 'sf_decision_outcome'
CONFIRM_BLOCK_ID = 'sf_confirm_actions'
FOOTER_BLOCK_ID = 'sf_card_footer'     # the task name / dashboard link under the buttons; kept after a decision
REPLY_BLOCK_ID = 'sf_thread_reply'     # the line a typed reply in the thread leaves
METADATA_TYPE = 'sf_decision'
MAX_RULE_FIELD = 700                   # one rule-card section stays short enough to read at a glance
SUPERSEDED_REASONS = ('newer_proposal', 'card_decision', 'smoke_cleanup')
DEFAULT = object()  # an argument left out: the card's own hint, the owner's mention
RULE_FIELDS = ('scenario', 'default', 'override', 'rule', 'impact')


def button(text, action_id, value, style=None):
    element = {'type': 'button', 'text': {'type': 'plain_text', 'text': text, 'emoji': True},
               'action_id': action_id, 'value': value}
    if style:
        element['style'] = style
    return element


def context_block(block_id, text):
    return {'type': 'context', 'block_id': block_id, 'elements': [{'type': 'mrkdwn', 'text': text}]}


def footer_block(text):
    return context_block(FOOTER_BLOCK_ID, text)


def reply_block(text):
    return context_block(REPLY_BLOCK_ID, text)


def decided_blocks(blocks, outcome):
    """Buttons, hint and an earlier outcome gone; the outcome line added."""
    kept = [b for b in blocks or [] if b.get('type') != 'actions'
            and b.get('block_id') not in (ACTIONS_BLOCK_ID, HINT_BLOCK_ID, OUTCOME_BLOCK_ID)]
    return kept + [outcome]


def retired_blocks(blocks, line, keep_buttons=False):
    """Buttons and hint gone (unless `keep_buttons`), one reply line added; an existing reply line is replaced."""
    kept = [b for b in blocks or [] if b.get('block_id') != REPLY_BLOCK_ID and (keep_buttons or (
        b.get('type') != 'actions' and b.get('block_id') not in (ACTIONS_BLOCK_ID, HINT_BLOCK_ID)))]
    return kept + [reply_block(line)]


def proposal_closed_blocks(text, status_line):
    return [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}},
            {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': status_line}]}]


def receipt_metadata(event_id, choice, actor, provenance='slack_button', proposal_id=None, option=None):
    payload = {'event_id': event_id, 'choice': choice, 'actor': actor, 'provenance': provenance}
    if option:
        payload.update(option_id=option['id'], option_kind=option['kind'])
    if proposal_id:
        payload['proposal_id'] = proposal_id
    return {'event_type': METADATA_TYPE, 'event_payload': payload}


def rule_field(value, field):
    """One owner-facing field of a rule-distillation draft: non-empty plain text, no control characters, bounded."""
    value = ' '.join(str(value or '').split())
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f'rule_{field}_must_be_plain_text')
    if not value or len(value) > MAX_RULE_FIELD:
        raise ValueError(f'rule_{field}_must_be_1_to_{MAX_RULE_FIELD}_characters')
    return value


class DecisionCards:
    """The card text and blocks in the owner's language and clock. `default_model` gives the buttons of a card stored
    without options (the default pair)."""

    def __init__(self, language, *, tz, tz_name, owner_user, default_model):
        self.language, self.tz, self.tz_name, self.owner_user = language, tz, tz_name, owner_user
        self.default_model = default_model
        self.hint = self.msg('decision.card.hint')
        self.keep_hint = self.msg('decision.card.keep_hint')
        self.cancelled = self.msg('decision.card.cancelled')
        self.superseded = {r: self.msg(f'decision.superseded.{r}') for r in SUPERSEDED_REASONS}

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def clock(self, timestamp):
        return dt.datetime.fromtimestamp(timestamp, self.tz).strftime('%H:%M')

    def iso(self, timestamp):
        return dt.datetime.fromtimestamp(timestamp, self.tz).isoformat(timespec='seconds')

    def stamp(self, actor, timestamp):
        return f' · <@{actor}> · {self.clock(timestamp)} ({self.tz_name})'

    def note_suffix(self, option, note):
        if not note:
            return ''
        what = self.msg('decision.note.remark' if option['kind'] == 'reject' else 'decision.note.condition')
        return self.msg('decision.note', what=what, note=escape(note))

    def choices(self, labels):
        return self.msg('decision.choice_joiner').join(self.msg('decision.choice', label=escape(label)) for label in labels)

    # ---- the card ----

    def option_button(self, option, event_id):
        # One style at most: reject-kind is always danger; otherwise the recommended option (or the default approve)
        # is primary.
        if option['kind'] == 'reject':
            style = 'danger'
        elif option['recommended'] or option['choice'] == 'approve':
            style = 'primary'
        else:
            style = None
        text = f'{KIND_ICON[option["kind"]]} {option["label"]}' + (
            self.msg('decision.recommended') if option['recommended'] else '')
        return button(text, ACTION_PREFIX + option['choice'], event_id, style)

    def actions_block(self, event_id, model=None):
        # The value carries only the event id; details stay in the store.
        return {'type': 'actions', 'block_id': ACTIONS_BLOCK_ID,
                'elements': [self.option_button(o, event_id) for o in (model or self.default_model())]}

    def hint_block(self, text=DEFAULT):
        return context_block(HINT_BLOCK_ID, self.hint if text is DEFAULT else text)

    def code_owned_hint(self, model):
        """Hint of a code-owned card: only the option labels count as decisions (other text reaches its task)."""
        return self.msg('decision.card.code_owned_hint', choices=self.choices(o['label'] for o in model))

    def card_blocks(self, text, event_id, model=None, footer=None, hint=DEFAULT, body_blocks=None):
        """Section (the owner-facing text), buttons, optional footer, hint. `body_blocks` (pre-built section / header
        blocks) replaces the single text section for a rich card; `text` stays the notification fallback."""
        head = [dict(b) for b in body_blocks] if body_blocks else [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]
        blocks = head + [self.actions_block(event_id, model)]
        if footer:
            blocks.append(footer_block(footer))
        return blocks + [self.hint_block(hint)]

    # ---- the rule-distillation card ----

    def rule_card_header(self, title):
        """A Slack header (plain_text, at most 150 characters) with the readable scenario name."""
        title = ' '.join(str(title or '').split())
        return {'type': 'header', 'text': {'type': 'plain_text', 'text': self.msg('decision.rule.header', title=title)[:150],
                                           'emoji': True}}

    def rule_card_blocks(self, draft, mention=DEFAULT):
        """Header, intro, scenario, current handling vs the owner's, the proposed rule, its impact. `draft` carries
        already-checked plain fields; no internal ids anywhere in the body."""
        f = {k: escape(rule_field(draft[k], k)) for k in RULE_FIELDS}
        label = lambda k: f'*{self.msg("decision.rule." + k)}*\n{f[k]}'  # noqa: E731
        return [
            self.rule_card_header(draft['title']),
            {'type': 'section', 'text': {'type': 'mrkdwn', 'text': self.msg(
                'decision.rule.intro', mention=f'<@{self.owner_user if mention is DEFAULT else mention}>')}},
            {'type': 'section', 'text': {'type': 'mrkdwn', 'text': label('scenario')}},
            {'type': 'section', 'fields': [{'type': 'mrkdwn', 'text': label('default')},
                                           {'type': 'mrkdwn', 'text': label('override')}]},
            {'type': 'section', 'text': {'type': 'mrkdwn', 'text': label('rule')}},
            {'type': 'section', 'text': {'type': 'mrkdwn', 'text': label('impact')}},
        ]

    def rule_card_fallback(self, draft):
        """The one-line notification fallback (no mention)."""
        return self.msg('decision.rule.fallback', title=escape(' '.join(str(draft.get('title') or '').split())))

    # ---- receipts and outcomes ----

    def receipt_text(self, actor, option, note=None, provenance=None, guidance=False):
        if provenance == 'thread_reply':
            return (self.msg('decision.receipt.reply', actor=f'<@{actor}>', icon=KIND_ICON[option['kind']],
                             label=escape(option['label']))
                    + (self.msg('decision.receipt.guidance') if guidance else ''))
        return self.msg('decision.receipt.chose', actor=f'<@{actor}>', label=escape(option['label']),
                        note=self.note_suffix(option, note))

    def outcome_text(self, option, actor, decided_at, note=None, provenance=None, excerpt=None):
        if option['choice'] in ('approve', 'reject'):
            head = self.msg('decision.outcome.' + option['choice'])
        else:
            head = self.msg('decision.outcome.chose', icon=KIND_ICON[option['kind']], label=escape(option['label']))
        if provenance == 'thread_reply':
            head += (self.msg('decision.outcome.replied', excerpt=excerpt) if excerpt
                     else self.msg('decision.outcome.by_reply'))
        return head + self.note_suffix(option, note) + self.stamp(actor, decided_at)

    def outcome_block(self, option, actor, decided_at, note=None, provenance=None, excerpt=None):
        return context_block(OUTCOME_BLOCK_ID, self.outcome_text(option, actor, decided_at, note, provenance, excerpt))

    # ---- a typed reply in the card's thread ----

    def reply_line(self, excerpt_text, secret=False, code_owned=False, labels=(), plan_change=False):
        """The line under a card once the owner replied in its thread (`excerpt_text` already escaped)."""
        quoted = self.msg('decision.reply.hidden') if secret else self.msg('decision.reply.quoted', excerpt=excerpt_text)
        if code_owned:  # code decides a code-owned card: say what counts (the rest reaches the card's task)
            return self.msg('decision.reply.code_owned', quoted=quoted, choices=self.choices(labels))
        if plan_change:
            return self.msg('decision.reply.plan_change', quoted=quoted)
        return self.msg('decision.reply.handled', quoted=quoted)

    # ---- confirm-my-reading proposals ----

    def proposal_text(self, option, note=None, meaning=None):
        """The owner session's reading of a reply (`meaning`), and the option (with its note) a yes records."""
        picked = f'{KIND_ICON[option["kind"]]} {escape(option["label"])}{self.note_suffix(option, note)}'
        if not meaning:
            return self.msg('decision.proposal.picked', picked=picked)
        return self.msg('decision.proposal.meaning', meaning=escape(meaning), picked=picked)

    def proposal_blocks(self, proposal_id, text):
        return [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}},
                {'type': 'actions', 'block_id': CONFIRM_BLOCK_ID, 'elements': [
                    button(self.msg('decision.proposal.yes'), CONFIRM_PREFIX + 'yes', proposal_id, 'primary'),
                    button(self.msg('decision.proposal.no'), CONFIRM_PREFIX + 'no', proposal_id)]}]

    def confirmed_line(self, actor, decided_at):
        return self.msg('decision.proposal.confirmed') + self.stamp(actor, decided_at)
