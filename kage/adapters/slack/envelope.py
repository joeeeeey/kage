"""Slack side of the event envelope: reading a Slack message into kage.core.envelope's terms.

What only a Slack message tells: the instance's hidden marker (message metadata `event_type`), the app that posted
(`app_id`), bot posts (`bot_id`, `bot_message`, the assistant's bot user), the author's workspace (`user_team`) and
guest flags (`user_profile.is_restricted` / `is_ultra_restricted`), and which subtypes are hand-typed.
"""
from __future__ import annotations
from kage.core import envelope as core

HAND_TYPED_SUBTYPES = (None, 'file_share', 'thread_broadcast')


class SlackEnvelopes:
    def __init__(self, envelopes, *, workspace, assistant_app, assistant_bot_user, agent_marker):
        self.core = envelopes
        self.workspace, self.app, self.bot, self.marker = workspace, assistant_app, assistant_bot_user, agent_marker

    def agent_sender(self, m):
        """The sender block of a message this instance's agents sent (hidden marker), else None."""
        meta = m.get('metadata') if isinstance(m.get('metadata'), dict) else {}
        if meta.get('event_type') != self.marker:
            return None
        payload = meta.get('event_payload') if isinstance(meta.get('event_payload'), dict) else {}
        return {k: (str(payload[k]) if payload.get(k) is not None else None)
                for k in ('kind', 'provider', 'session', 'task_cwd', 'hops')}

    def person_trust(self, user, m=None, delegates=(), people=None):
        """Trust class of a human Slack user. Names, mentions and text never change it."""
        m = m or {}
        team = m.get('user_team') or m.get('team')
        profile = m.get('user_profile') if isinstance(m.get('user_profile'), dict) else {}
        return self.core.person_trust(
            user, delegates=delegates, other_team=bool(team and team != self.workspace),
            guest=bool(profile.get('is_restricted') or profile.get('is_ultra_restricted')), people=people)

    def classify_message(self, m, *, thread_root=None, ledger_hit=False, delegates=(), people=None):
        """Envelope of an ingested Slack message, in the fixed order (a)..(f).

        (a)  our agent: the hidden marker, our app id, or our send ledger. Checked BEFORE the owner check, so agent
             posts made with the owner's token are agent.
        (a2) another app posting as a human (that person's own agent): agent, with the person's trust class but never
             the owner's; authority none.
        (b)  the owner hand-typed -> <key> / <key>_intent.
        (c)  an active out-of-office delegate -> delegate.
        (d)  other bot / app messages (alert bots...) -> system.
        (e)  a guest, another workspace or a registry external -> external.
        (f)  everyone else -> colleague.
        """
        user = str(m.get('user') or '')
        app = m.get('app_id')
        sender = self.agent_sender(m)
        if sender is not None or app == self.app or ledger_hit:
            rule = 'agent_metadata' if sender is not None else 'agent_app_id' if app == self.app else 'agent_ledger'
            return core.envelope('slack_message', 'agent', 'agent', 'none', rule, thread_root,
                                 core.hops(sender.get('hops')) + 1 if sender else 1, sender)
        bot = bool(m.get('bot_id')) or m.get('subtype') == 'bot_message' or user == self.bot
        human = bool(core.USER_RE.fullmatch(user)) and not bot
        if app and human:
            trust, _ = self.person_trust(user, m, delegates, people)
            return core.envelope('slack_message', 'agent', 'agent' if trust == self.core.owner_trust else trust, 'none',
                                 'agent_foreign_app', thread_root, 1)
        if not human:
            return core.envelope('slack_message', 'system', 'system', 'none', 'system_bot', thread_root)
        trust, rule = self.person_trust(user, m, delegates, people)
        if trust == self.core.owner_trust:
            hand_typed = m.get('subtype') in HAND_TYPED_SUBTYPES
            return core.envelope('slack_message', 'human', trust,
                                 self.core.owner_intent if hand_typed else 'none',
                                 self.core.rule_hand_typed if hand_typed else self.core.rule_non_message, thread_root)
        return core.envelope('slack_message', 'human', trust, 'none', rule, thread_root)
