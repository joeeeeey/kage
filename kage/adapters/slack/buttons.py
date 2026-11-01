"""The decision buttons, assembled for one owner: the card options and typed-reply reading (kage.core.decisions,
kage.adapters.slack.decisions), who may decide, card routing and the private-destination guard (kage.core.authority,
kage.adapters.slack.destinations), the card blocks and the copy standard (kage.adapters.slack.cards, kage.core.copy).

A deployment builds one `DecisionButtons` with its owner (Slack user, key, name, language, time zone), the environment
variable and default path of its decisions config, its refresh command and report advice, its wording of the copy
standard and the rule switch that enforces it. The hooks are the deployment's own names, called at call time, so a
replaced function (a test's patch) is the one the guard and the cards reach: `load_config`, `check`,
`record_refusal`, `private` (the destination guard's callbacks) and `default_model` (the buttons of a card stored
without options).
"""
from __future__ import annotations
import os
from pathlib import Path
import re

from kage.adapters.slack import cards as _cards
from kage.adapters.slack import decisions as _slack_decisions
from kage.adapters.slack import destinations as _destinations
from kage.core import authority as _authority
from kage.core import copy as _copy
from kage.core import decisions as _decisions

USER_RE = re.compile(r'U[A-Z0-9]+')
CHANNEL_RE = re.compile(r'[CG][A-Z0-9]+')
DM_RE = re.compile(r'D[A-Z0-9]+')  # a resolved bot↔user direct-message conversation id
# The engine's tables that store a card destination: (table, column, time column, extra condition).
STORED_DESTINATIONS = (('escalation_cards', 'channel', 'created', ''), ('dispatches', 'card_channel', 'created_at', ''),
                       ('agent_breakers', 'card_channel', 'tripped_at', ''),
                       ('blocker_guard', 'channel', 'created_at', ''),  # old rows only (the guard is gone)
                       ('automation_gaps', 'card_channel', 'created_at', ''),
                       ('automation_gaps', 'stage2_channel', 'stage2_posted_at', ''),
                       ('stuck_claims', 'card_channel', 'created_at', ''), ('ask_cards', 'channel', 'created_at', 'smoke=1'),
                       ('agent_sends', 'channel', 'created', "kind='provider_health_notice'"))
# Commands spawned for interactive clicks must not inherit an agent identity.
AGENT_ENV = ('CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID', 'CLAUDECODE')

MIN_OPTIONS, MAX_OPTIONS, MAX_LABEL, MAX_NOTE = (_decisions.MIN_OPTIONS, _decisions.MAX_OPTIONS, _decisions.MAX_LABEL,
                                                 _decisions.MAX_NOTE)
KINDS, KIND_ICON, OPTION_ID_RE = _decisions.KINDS, _decisions.KIND_ICON, _decisions.OPTION_ID_RE
MENTION_RE, SLACK_TOKEN_RE = _slack_decisions.MENTION_RE, _slack_decisions.SLACK_TOKEN_RE
parse_option, clean_label, excerpt = _decisions.parse_option, _slack_decisions.clean_label, _slack_decisions.excerpt
resolve_choice, normalize_choice = _decisions.resolve_choice, _decisions.normalize_choice
public_option, validate_note = _decisions.public_option, _decisions.validate_note
SCOPES = _authority.SCOPES
ooo_in_window, delegates_now, card_route = _authority.ooo_in_window, _authority.delegates_now, _authority.card_route
decision_channels, authorize = _authority.decision_channels, _authority.authorize
(DESTINATION_META, DESTINATION_BLOCKED, DESTINATION_REFUSALS, DESTINATION_TTL, DESTINATION_ERROR_TTL,
 DESTINATION_STALE_OK, DESTINATION_LEGACY_DAYS) = (
    _destinations.DESTINATION_META, _destinations.DESTINATION_BLOCKED, _destinations.DESTINATION_REFUSALS,
    _destinations.DESTINATION_TTL, _destinations.DESTINATION_ERROR_TTL, _destinations.DESTINATION_STALE_OK,
    _destinations.DESTINATION_LEGACY_DAYS)
PRIVATE_VERDICTS = _destinations.PRIVATE_VERDICTS
TRANSIENT_SLACK_ERRORS, REFUSALS_KEPT = _destinations.TRANSIENT_SLACK_ERRORS, _destinations.REFUSALS_KEPT
DestinationRefused, transient_failure = _destinations.DestinationRefused, _destinations.transient_failure
(ACTION_PREFIX, CONFIRM_PREFIX, ACTIONS_BLOCK_ID, HINT_BLOCK_ID, OUTCOME_BLOCK_ID, CONFIRM_BLOCK_ID, FOOTER_BLOCK_ID,
 REPLY_BLOCK_ID, METADATA_TYPE, MAX_RULE_FIELD) = (
    _cards.ACTION_PREFIX, _cards.CONFIRM_PREFIX, _cards.ACTIONS_BLOCK_ID, _cards.HINT_BLOCK_ID, _cards.OUTCOME_BLOCK_ID,
    _cards.CONFIRM_BLOCK_ID, _cards.FOOTER_BLOCK_ID, _cards.REPLY_BLOCK_ID, _cards.METADATA_TYPE, _cards.MAX_RULE_FIELD)
escape, _button, footer_block, reply_block = _cards.escape, _cards.button, _cards.footer_block, _cards.reply_block
rule_field, receipt_metadata, decided_blocks = _cards.rule_field, _cards.receipt_metadata, _cards.decided_blocks
retired_blocks, proposal_closed_blocks = _cards.retired_blocks, _cards.proposal_closed_blocks
COPY_FIRST_LINE_MAX, COPY_PATTERNS, LINK_RE = _copy.FIRST_LINE_MAX, _copy.ID_PATTERNS, _copy.LINK_RE
_DEFAULT = object()  # an argument left out (the card's own hint, the owner as the mention)


class DecisionButtons:
    """The owner's decision buttons: `reader` / `replies` (options and typed replies), `authority` / `destinations`
    (who decides, where a card may go), `cards` (blocks, receipts, outcomes) and `copy` (the copy standard)."""

    def __init__(self, language, *, tz, tz_name, owner_user, owner_key, owner_name, config_env, default_config,
                 refresh_command, report_next, copy_texts, copy_active, hooks):
        self.language, self.owner_user, self.config_env, self.default_config = language, owner_user, config_env, default_config
        self.reader = _decisions.ReplyReader(language)
        self.replies = _slack_decisions.SlackReplies(self.reader)
        self.default_pair = _decisions.default_pair(language)
        self.authority = _authority.Authority(owner_user=owner_user, owner_key=owner_key, user_re=USER_RE,
                                              channel_re=CHANNEL_RE, dm_re=DM_RE)
        self.destinations = _destinations.Destinations(
            self.authority, owner_user=owner_user, owner_key=owner_key, load_config=hooks['load_config'],
            stored_destinations=STORED_DESTINATIONS, refresh_command=refresh_command, report_next=report_next,
            hooks={'check': hooks['check'], 'record_refusal': hooks['record_refusal'], 'private': hooks['private']})
        self.cards = _cards.DecisionCards(language, tz=tz, tz_name=tz_name, owner_user=owner_user,
                                          default_model=hooks['default_model'])
        self.copy = _copy.CopyStandard(language, owner_name=owner_name, owner_key=owner_key, active=copy_active,
                                       texts=copy_texts)

    def validate_options(self, specs, recommend=None):
        """No specs: the default pair (stored as []). Otherwise 2-4 options replace it."""
        return _decisions.validate_options(specs, recommend, clean_label)

    def card_model(self, stored):
        """Buttons on a card, each with its `choice` (action_id suffix). Reads every stored format."""
        return _decisions.card_model(stored, self.default_pair)

    def parse_reply(self, text, model, strict=False):
        """The owner's thread reply -> {'option', 'phrase', 'via', 'guidance', 'reason'}; see kage.core.decisions."""
        return self.replies.parse_reply(text, model, strict)

    def config_path(self):
        return Path(os.environ.get(self.config_env) or self.default_config).expanduser()

    def load_config(self, path=None):
        """Missing file means owner-only in the owner's DM (never a public channel); malformed config fails closed."""
        return self.authority.load_config(path if path else self.config_path())

    def require_private_destination(self, rt, db, channel, kind, config=None, configured=True):
        """The guard every owner-facing bot post passes right before chat.postMessage; see
        kage.adapters.slack.destinations.Destinations.require."""
        return self.destinations.require(rt, db, channel, kind, config, configured)

    def hint_block(self, text=_DEFAULT):
        return self.cards.hint_block(self.cards.hint if text is _DEFAULT else text)

    def card_blocks(self, text, event_id, model=None, footer=None, hint=_DEFAULT, body_blocks=None):
        """Section (the owner-facing text), buttons, optional footer (task name, dashboard link), hint; see
        kage.adapters.slack.cards.DecisionCards.card_blocks."""
        return self.cards.card_blocks(text, event_id, model, footer, self.cards.hint if hint is _DEFAULT else hint,
                                      body_blocks)

    def rule_card_blocks(self, draft, mention=_DEFAULT):
        """The body of a rule-distillation card; see kage.adapters.slack.cards.DecisionCards.rule_card_blocks."""
        return self.cards.rule_card_blocks(draft, self.owner_user if mention is _DEFAULT else mention)
