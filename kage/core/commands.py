"""Commands (K14e): what agents and the receiver call — asks to colleagues in the owner's voice (post, answer,
propose / confirm, sweep, smokes), the hand-off of an event to a task, the Claude hooks, the task inbox and local
events (notes, send-to-task), the memory guard, a dispatched run's question hook, run views and card actions,
terminal takeover / hand-back, the run tail, launching a task and the dispatch smoke. Everything else — the
deployment's paths (tail wrapper, iTerm script) among it — is reached through the runtime (`rt`).
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

from kage import locale

_OWNER_DEFAULT = object()  # a parameter whose default is the owner's value, filled in at call time


INBOX_KIND = 'task_inbox'


INBOX_RETIRED = 'superseded_by_newer_session'  # P38: stop_reason of a long-lived task's older, idle inbox watch


BREAKER_ID_RE = re.compile(r'b[0-9a-f]{15}')


VIEW_STEPS = 8


EPHEMERAL_LIMIT = 2900


SESSION_PREFIX_RE = re.compile(r'[0-9a-f][0-9a-f-]{3,35}')


def _live_runs(db, disp, key):
    """The task's runs still marked running whose process lives (an unknown start time counts as alive, as in `_tend`);
    a dead one is finalized here at once, in case the supervisor is down (finalize is idempotent)."""
    live = []
    for run in [dict(r) for r in db.execute("SELECT * FROM consumer_runs WHERE task=? AND state='running'", (key,))]:
        if not run['pid_start'] or disp.alive(run['pid'], run['pid_start']):
            live.append(run)
        else:
            disp.finalize(run['run_id'])
    return live


TAIL_CHAIN_WAIT_SECONDS = 900


class CommandsCore:
    """The owner-bound half of the commands: the owner's Slack user, the dashboard URL, the runtime's env and
    tool names, the words in the owner's language, and the runtime (`rt`) every name it does not hold is
    reached through — the deployment's paths and the names the tests replace among them."""

    def __init__(self, language, *, owner_key, owner_name, owner_slack_user, bot_user, dashboard_url, hops_env, run_id_env, no_iterm_env, tail_command, rt):
        self.language = language
        self.owner_slack_user = owner_slack_user
        self.bot_user = bot_user
        self.dashboard_url = dashboard_url
        self.hops_env = hops_env
        self.run_id_env = run_id_env
        self.no_iterm_env = no_iterm_env
        self.tail_command = tail_command
        self.rt = rt
        self.key, self.name = owner_key, owner_name
        self.smoke_step1 = (self.msg('commands.smoke_step1.1'))
        self.smoke_step2 = (self.msg('commands.smoke_step2.1'))

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def _identity_token(self, identity, user_token=None):
        if identity == 'bot':
            return self.rt.api_identity(bot=True)
        return user_token or self.rt.api_identity()

    def _post_ask(self, db, owner, watch_id, channel, thread, responders, text, stored, confirm_mode, reminder, lang, hint,
                  remind, expire, now, user_token, smoke=False, identity='user', live_card=False):
        card_id = uuid.uuid4().hex[:20]
        with self.rt.transaction(db):
            self.rt._ask_limits(db, owner['session'], self.rt.claude.provider(owner), channel, thread, responders, now, smoke)
            db.execute('INSERT INTO ask_cards (id,watch_id,channel,thread,owner_session,provider,responders,options,'
                       'confirm_mode,state,question,lang,reminder_text,hint,remind_after,expire_after,created_at,smoke,'
                       'post_identity,dry) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (card_id, watch_id, channel, thread, owner['session'], self.rt.claude.provider(owner),
                        json.dumps(responders), json.dumps(stored, ensure_ascii=False), confirm_mode, 'sending', text,
                        lang, reminder, int(bool(hint)), remind, expire, now, int(bool(smoke)), identity,
                        int(bool(smoke and not live_card))))
        if smoke and not live_card:
            # Dry smoke: synthesized in the DB exactly as a real ask card, no Slack post; ask_sweep never reminds / expires it.
            ts = f'{int(now)}.{int(hashlib.sha256(("dryask" + card_id).encode()).hexdigest()[:8], 16) % 1000000:06d}'
            with self.rt.transaction(db):
                db.execute("UPDATE ask_cards SET ts=?,state='open',remind_at=?,expire_at=? WHERE id=?",
                           (ts, now + remind, now + expire, card_id))
                self.rt._ledger(db, channel, ts, 'ask_card', card_id, owner['session'])
            return card_id, ts
        # Conversation plane: the owner's own account, no visible marker; hidden metadata only.
        body = {'channel': channel, 'text': text, 'unfurl_links': False, 'unfurl_media': False,
                'blocks': self.rt.asks.card_blocks(card_id, text, self.rt.buttons.card_model(stored), lang, hint),
                'metadata': self.rt.agent_metadata('ask_card', owner, card_id=card_id, smoke='true' if smoke else None)}
        if thread:
            body['thread_ts'] = thread
        try:
            sent = self.rt.slack.api_call('chat.postMessage', user_token, json_body=body)
        except Exception as exc:
            with self.rt.transaction(db):
                db.execute("UPDATE ask_cards SET state='failed',last_error=? WHERE id=?", (self.rt.safe_error(exc), card_id))
            raise ValueError('ask_post_failed_' + self.rt.safe_error(exc)) from None
        with self.rt.transaction(db):
            db.execute("UPDATE ask_cards SET ts=?,state='open',remind_at=?,expire_at=? WHERE id=?",
                       (sent['ts'], now + remind, now + expire, card_id))
            self.rt._ledger(db, channel, sent['ts'], 'ask_card', card_id, owner['session'])
            # P46.3: an ask card no longer opens the asking task's whole-DM conversation window (P39): its answer comes in
            # its own thread or by its buttons, and the owner's chat with the person in the DM stays his (a colleague, 2026-10-30).
        return card_id, sent['ts']

    def ask(self, db, *, responders, text, options, channel=None, thread=None, dm=None, confirm_mode='auto',
            remind_after=_OWNER_DEFAULT, expire_after=_OWNER_DEFAULT, reminder_text=None, lang='zh',
            hint=True, now=None):
        """Owner session asks colleagues in an already-followed thread/DM; all guards precede writes."""
        remind_after = self.rt.asks.DEFAULT_REMIND if remind_after is _OWNER_DEFAULT else remind_after
        expire_after = self.rt.asks.DEFAULT_EXPIRE if expire_after is _OWNER_DEFAULT else expire_after
        now = time.time() if now is None else now
        provider, session = self.rt.claude.actor()
        if not session:
            raise ValueError('ask_requires_the_owning_session_identity')
        if bool(dm) == bool(channel or thread):
            raise ValueError('use_either_channel_and_thread_or_dm')
        if dm and not self.rt.asks.USER_RE.fullmatch(str(dm)):
            raise ValueError('invalid_dm_user')
        if not dm and not (re.fullmatch(r'[CG][A-Z0-9]+', str(channel)) and re.fullmatch(r'\d+\.\d+', str(thread))):
            raise ValueError('ask_requires_exact_channel_and_thread')
        responders, text, stored, reminder, remind, expire = self.rt._ask_inputs(
            responders, text, options, confirm_mode, remind_after, expire_after, reminder_text, lang)
        w, channel, thread = self.rt._covering_watch(db, provider, session, channel, thread, dm)
        self.rt.require_owner(w)
        self.rt._ask_limits(db, session, provider, channel, thread, responders, now)
        # P46.2: the owner's words in the language each responder wrote here; the card's own words (hint, reminder, result)
        # follow their latest message (zh / en), else the fallback (--lang; en for someone who reads no Chinese)
        votes = set()
        for u in responders:
            langs, share = self.rt.intake.require_language(self.rt, db, u, [text, reminder_text, *(o['label'] for o in stored)],
                                                   words=self.rt.intake.own_words(self.rt, channel, thread, u), lookup=True)
            votes.add('zh' if langs == {'zh'} else 'en' if share is not None or (langs and 'zh' not in langs) else lang)
        if lang != ('en' if 'en' in votes else 'zh'):
            lang = 'en' if 'en' in votes else 'zh'
            reminder = reminder if reminder_text else self.rt.asks.prepare_text(
                self.rt.asks.phrase(lang, 'reminder', mentions=self.rt.asks.mentions(responders)), responders, 300, 'reminder_text')
        identity = self.rt.audience_identity(responders)
        card_id, _ = self._post_ask(db, w, w['id'], channel, thread, responders, text, stored, confirm_mode, reminder,
                               lang, hint, remind, expire, now, self._identity_token(identity), identity=identity)
        return {**self.rt.ask_summary(db, card_id),
                'next': 'Answers arrive as events with context.ask_answer (authority none). Typed answers: ask-propose.'}

    def _update_ask_card(self, db, card_id, user_token, line, fallback=False):
        """Replace the buttons with a result line as the owner; if editing fails, reply in the thread instead."""
        card = self.rt._ask_card(db, card_id)
        owner = self.rt._ask_owner(db, card)
        receipt, error = None, None
        try:
            # Only the identity that posted a message can edit it (NULL = legacy the owner-token card).
            self.rt.slack.api_call('chat.update', self._identity_token(card['post_identity'] or 'user', user_token), json_body={
                'channel': card['channel'], 'ts': card['ts'], 'text': card['question'],
                'blocks': self.rt.asks.closed_blocks(card['question'], line),
                'metadata': self.rt.agent_metadata('ask_card', owner, card_id=card_id)})
            stage = 'card_updated'
        except Exception as exc:
            stage, error = 'card_update_failed', self.rt.safe_error(exc)
            if fallback:
                try:
                    sent = self.rt.slack.api_call('chat.postMessage', self._identity_token(
                            self.rt.audience_identity(json.loads(card['responders'])), user_token), json_body={
                        'channel': card['channel'], 'thread_ts': self.rt._ask_root(card), 'text': line,
                        'metadata': self.rt.agent_metadata('ask_result', owner, card_id=card_id),
                        'unfurl_links': False, 'unfurl_media': False})
                    stage, receipt = 'result_posted', sent['ts']
                except Exception as again:
                    stage, error = 'result_failed', self.rt.safe_error(again)
        with self.rt.transaction(db):
            db.execute('UPDATE ask_cards SET card_stage=?,receipt_ts=coalesce(?,receipt_ts),last_error=? WHERE id=?',
                       (stage, receipt, error, card_id))
            self.rt._ledger(db, card['channel'], receipt, 'ask_result', card_id, owner['session'])
        return stage

    def _update_ask_proposal_message(self, db, user_token, card, p, line=None, text=None):
        """Best effort: drop confirmation buttons; record the stage, never raise."""
        if not p['message_ts']:
            return
        option = self.rt._ask_option(card, p['option_id'])
        body = text or self.rt.asks.confirm_text(card['lang'], p['proposer_user'], option, p['note'])
        blocks = ([{'type': 'section', 'text': {'type': 'mrkdwn', 'text': body}}] if line is None
                  else self.rt.asks.closed_blocks(body, line))
        try:
            identity = (p['post_identity'] if 'post_identity' in p.keys() else None) or 'user'
            self.rt.slack.api_call('chat.update', self._identity_token(identity, user_token), json_body={
                'channel': card['channel'], 'ts': p['message_ts'], 'text': body, 'blocks': blocks})
            stage, error = 'message_updated', None
        except Exception as exc:
            stage, error = 'message_update_failed', self.rt.safe_error(exc)
        with self.rt.transaction(db):
            db.execute('UPDATE ask_proposals SET message_stage=?,last_error=? WHERE id=?', (stage, error, p['id']))

    def _record_ask_answer(self, db, card_id, option, actor, via, now, user_token, note=None, source_ts=None, proposal_id=None):
        with self.rt.transaction(db):
            card = self.rt._ask_card(db, card_id)
            if card['state'] != 'open':
                return None
            answer = {'option_id': option['id'], 'label': option['label'], 'kind': option['kind'], 'actor': actor,
                      'via': via, 'at': now, 'note': note, 'source_ts': source_ts, 'proposal_id': proposal_id}
            event = None
            if card['watch_id']:
                w = self.rt.load(db, card['watch_id'])
                event = self.rt._queue_owner_event(db, w, card['channel'], card['ts'], self.rt._ask_root(card), actor,
                                           {'ask_answer': card_id, 'option': option['id'], 'actor': actor, 'via': via,
                                            'at': now}, now,
                                           self.rt.interaction_envelope(w, 'ask_answer', actor, self.rt._ask_root(card), now))
            db.execute("UPDATE ask_cards SET state='answered',answer=?,answer_event=? WHERE id=?",
                       (json.dumps(answer, ensure_ascii=False), event, card_id))
            closed = [dict(r) for r in db.execute(
                "SELECT * FROM ask_proposals WHERE card_id=? AND status='pending' AND id IS NOT ?", (card_id, proposal_id))]
            db.execute("UPDATE ask_proposals SET status='superseded',closed_reason='card_answered' "
                       "WHERE card_id=? AND status='pending' AND id IS NOT ?", (card_id, proposal_id))
        stage = self._update_ask_card(db, card_id, user_token, self.rt.asks.result_line(card['lang'], option, actor, via, now, note),
                                 fallback=True)
        for p in closed:
            self._update_ask_proposal_message(db, user_token, card, p, self.rt.asks.phrase(card['lang'], 'closed'))
        return {'ok': True, 'duplicate': False, 'card_id': card_id, 'option_id': option['id'], 'label': option['label'],
                'kind': option['kind'], 'actor': actor, 'via': via, 'stage': stage, 'answer_event': event,
                'authority': 'none'}

    def ask_answer(self, db, card_id, option_id, actor, channel, message_ts, actor_team=None, now=None):
        """A card button click (receiver only). Non-responders change nothing and get a private nudge."""
        now = time.time() if now is None else now
        if self.rt._agent_session():
            return self.rt._fail('agent_session_cannot_decide')
        if not (self.rt.asks.CARD_ID_RE.fullmatch(str(card_id)) and self.rt.buttons.OPTION_ID_RE.fullmatch(str(option_id))
                and self.rt.asks.USER_RE.fullmatch(str(actor)) and re.fullmatch(r'[CDG][A-Z0-9]+', str(channel))
                and re.fullmatch(r'\d+\.\d+', str(message_ts))):
            return self.rt._fail('invalid_request')
        card = self.rt._ask_card(db, card_id)
        if card is None:
            return self.rt._fail('unknown_card')
        if card['channel'] != channel or card['ts'] != message_ts:
            return self.rt._fail('card_mismatch')
        lang = card['lang']
        if self.rt._external(actor_team):
            return self.rt._fail('external_team', ephemeral=self.rt.asks.phrase(lang, 'external'))
        try:
            option = self.rt._ask_option(card, option_id)
        except ValueError:
            return self.rt._fail('invalid_option')
        responders = json.loads(card['responders'])
        if card['state'] != 'open':
            answer = json.loads(card['answer']) if card['answer'] else {}
            if card['state'] == 'answered' and (answer.get('actor'), answer.get('option_id')) == (actor, option['id']):
                if card['card_stage'] not in ('card_updated', 'result_posted'):
                    self._update_ask_card(db, card_id, self.rt.api_identity(), self.rt.asks.result_line(
                        lang, option, actor, answer['via'], answer['at'], answer.get('note')), fallback=True)
                return {**self.rt.ask_summary(db, card_id), 'duplicate': True, 'option_id': option['id'], 'via': answer['via']}
            return self.rt._fail('already_handled', ephemeral=self.rt.asks.phrase(lang, 'handled'))
        if actor in responders:
            via = 'button'
        elif actor == self.owner_slack_user:
            via = f'{self.key}_override'
        else:
            return self.rt._fail('not_responder', ephemeral=self.rt.asks.phrase(lang, 'not_responder', mentions=self.rt.asks.mentions(responders)))
        result = self._record_ask_answer(db, card_id, option, actor, via, now, self.rt.api_identity())
        return result or self.rt._fail('already_handled', ephemeral=self.rt.asks.phrase(lang, 'handled'))

    def _ask_source(self, user_token, card, source_ts):
        root = self.rt._ask_root(card)
        found = self.rt.slack.api_call('conversations.replies', user_token, params={
            'channel': card['channel'], 'ts': root, 'oldest': source_ts, 'latest': source_ts,
            'inclusive': 'true', 'limit': 2})
        source = next((m for m in found.get('messages', []) if m.get('ts') == source_ts), None)
        if source is None and card['channel'].startswith('D'):
            found = self.rt.slack.api_call('conversations.history', user_token, params={
                'channel': card['channel'], 'oldest': source_ts, 'latest': source_ts, 'inclusive': 'true', 'limit': 1})
            source = next((m for m in found.get('messages', []) if m.get('ts') == source_ts), None)
        elif source is not None and (source.get('thread_ts') or source['ts']) != root:
            source = None
        return source

    def ask_propose(self, db, card_id, option_id, source_ts, note=None, now=None):
        """Owner maps a typed answer to an option: confirm when it could trigger a change, else echo."""
        now = time.time() if now is None else now
        if not self.rt.asks.CARD_ID_RE.fullmatch(str(card_id)) or not re.fullmatch(r'\d+\.\d+', str(source_ts)):
            raise ValueError('invalid_card_id_or_source_ts')
        card = self.rt._ask_card(db, card_id)
        if card is None:
            raise ValueError('unknown_ask_card')
        if card['state'] != 'open':
            raise ValueError('ask_card_not_open')
        w = self.rt.load(db, card['watch_id'])
        self.rt.require_owner(w)
        option = self.rt._ask_option(card, option_id)
        note = self.rt.buttons.validate_note(note)
        self.rt.asks.check_secret_free(note)
        if float(source_ts) <= float(card['ts']):
            raise ValueError('source_must_follow_the_card')
        responders = json.loads(card['responders'])
        for u in responders:  # P46.2: the note is posted as the owner (echo / confirmation / result line)
            self.rt.intake.require_language(self.rt, db, u, [note], what='note',
                                    words=self.rt.intake.own_words(self.rt, card['channel'], self.rt._ask_root(card), u))
        user_token = self.rt.api_identity()
        source = self._ask_source(user_token, card, source_ts)
        if source is None:
            raise ValueError('source_not_in_card_conversation')
        author = str(source.get('user') or '')
        if source.get('bot_id') or source.get('app_id') or source.get('subtype') not in (None, 'file_share') \
                or not self.rt.asks.USER_RE.fullmatch(author):
            raise ValueError('source_must_be_a_hand_typed_human_message')
        if author not in responders and author != self.owner_slack_user:
            raise ValueError('source_author_is_not_a_responder')
        lang, root = card['lang'], self.rt._ask_root(card)
        identity = self.rt.audience_identity(responders)
        if not self.rt.asks.needs_confirmation(card['confirm_mode'], option):
            text = self.rt.asks.phrase(lang, 'echo', label=self.rt.asks.escape(option['label']))
            try:
                sent = self.rt.slack.api_call('chat.postMessage', self._identity_token(identity, user_token), json_body={
                    'channel': card['channel'], 'thread_ts': root, 'text': text,
                    'metadata': self.rt.agent_metadata('ask_echo', w, card_id=card_id), 'unfurl_links': False, 'unfurl_media': False})
            except Exception as exc:
                raise ValueError('echo_post_failed_' + self.rt.safe_error(exc)) from None
            with self.rt.transaction(db):
                self.rt._ledger(db, card['channel'], sent['ts'], 'ask_echo', card_id, w['session'])
                db.execute('UPDATE ask_cards SET receipt_ts=? WHERE id=?', (sent['ts'], card_id))
            via = f'{self.key}_override' if author == self.owner_slack_user and self.owner_slack_user not in responders else 'text_echo'
            result = self._record_ask_answer(db, card_id, option, author, via, now, user_token, note, source_ts)
            return {**(result or self.rt.ask_summary(db, card_id)), 'mode': 'echo', 'echo_ts': sent['ts']}
        pid = uuid.uuid4().hex
        with self.rt.transaction(db):
            db.execute('INSERT INTO ask_proposals (id,card_id,option_id,note,source_ts,proposer_user,owner_session,'
                       'proposed_at,status,post_identity) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (pid, card_id, option['id'], note, source_ts, author, w['session'], now, 'pending', identity))
        text = self.rt.asks.confirm_text(lang, author, option, note)
        try:
            sent = self.rt.slack.api_call('chat.postMessage', self._identity_token(identity, user_token), json_body={
                'channel': card['channel'], 'thread_ts': root, 'text': text,
                'blocks': self.rt.asks.confirm_blocks(pid, text, lang), 'unfurl_links': False, 'unfurl_media': False,
                'metadata': self.rt.agent_metadata('ask_proposal', w, card_id=card_id, proposal_id=pid)})
        except Exception as exc:
            with self.rt.transaction(db):
                db.execute("UPDATE ask_proposals SET status='failed',closed_reason='post_failed',last_error=? WHERE id=?",
                           (self.rt.safe_error(exc), pid))
            raise ValueError('confirmation_post_failed_' + self.rt.safe_error(exc)) from None
        with self.rt.transaction(db):
            db.execute('UPDATE ask_proposals SET message_ts=? WHERE id=?', (sent['ts'], pid))
            self.rt._ledger(db, card['channel'], sent['ts'], 'ask_proposal', pid, w['session'])
            older = [dict(r) for r in db.execute(
                "SELECT * FROM ask_proposals WHERE card_id=? AND status='pending' AND id!=?", (card_id, pid))]
            db.execute("UPDATE ask_proposals SET status='superseded',closed_reason='newer_proposal' "
                       "WHERE card_id=? AND status='pending' AND id!=?", (card_id, pid))
        for p in older:
            self._update_ask_proposal_message(db, user_token, card, p, self.rt.asks.phrase(lang, 'closed'))
        return {'ok': True, 'mode': 'confirm', 'proposal_id': pid, 'message_ts': sent['ts'], 'card_id': card_id,
                'option_id': option['id'], 'label': option['label'], 'kind': option['kind'], 'note': note,
                'source_author': author, 'superseded': [p['id'] for p in older], 'authority': 'none',
                'next': 'Wait for the confirmation click; the answer is information, not authorization.'}

    def ask_confirm(self, db, pid, answer, actor, channel, message_ts, actor_team=None, now=None):
        """"✅ 对" / "✏️ 不对" on a typed-answer confirmation (receiver only)."""
        now = time.time() if now is None else now
        if self.rt._agent_session():
            return self.rt._fail('agent_session_cannot_decide')
        if not (re.fullmatch(r'[0-9a-f]{32}', str(pid)) and answer in ('yes', 'no') and self.rt.asks.USER_RE.fullmatch(str(actor))
                and re.fullmatch(r'[CDG][A-Z0-9]+', str(channel)) and re.fullmatch(r'\d+\.\d+', str(message_ts))):
            return self.rt._fail('invalid_request')
        p = db.execute('SELECT * FROM ask_proposals WHERE id=?', (pid,)).fetchone()
        if p is None:
            return self.rt._fail('unknown_proposal')
        card = self.rt._ask_card(db, p['card_id'])
        if card is None or card['channel'] != channel or p['message_ts'] != message_ts:
            return self.rt._fail('proposal_mismatch')
        lang = card['lang']
        if self.rt._external(actor_team):
            return self.rt._fail('external_team', ephemeral=self.rt.asks.phrase(lang, 'external'))
        if actor not in (p['proposer_user'], self.owner_slack_user):
            return self.rt._fail('not_proposal_author', ephemeral=self.rt.asks.phrase(lang, 'not_responder', mentions=self.rt.asks.mentions([p['proposer_user']])))
        status = p['status']
        if (status, answer) in (('confirmed', 'yes'), ('rejected', 'no')):
            return {'ok': True, 'duplicate': True, 'proposal_id': pid, 'status': status, 'card_id': card['id']}
        if status != 'pending' or card['state'] != 'open':
            if status == 'pending':
                with self.rt.transaction(db):
                    db.execute("UPDATE ask_proposals SET status='superseded',closed_reason='card_closed' WHERE id=?", (pid,))
            return self.rt._fail('already_handled', ephemeral=self.rt.asks.phrase(lang, 'handled'))
        user_token = self.rt.api_identity()
        option = self.rt._ask_option(card, p['option_id'])
        if answer == 'yes':
            with self.rt.transaction(db):
                if self.rt._ask_card(db, card['id'])['state'] != 'open':
                    return self.rt._fail('already_handled', ephemeral=self.rt.asks.phrase(lang, 'handled'))
                db.execute("UPDATE ask_proposals SET status='confirmed',decided_by=?,decided_at=? WHERE id=? AND status='pending'",
                           (actor, now, pid))
            responders = json.loads(card['responders'])
            via = f'{self.key}_override' if actor == self.owner_slack_user and self.owner_slack_user not in responders else 'text_confirmed'
            result = self._record_ask_answer(db, card['id'], option, actor, via, now, user_token, p['note'], p['source_ts'], pid)
            self._update_ask_proposal_message(db, user_token, card, self.rt._proposal_row(db, pid), self.rt.asks.phrase(lang, 'confirmed', clock=self.rt.asks.clock(now)))
            return {**(result or self.rt.ask_summary(db, card['id'])), 'proposal_id': pid, 'answer': 'yes'}
        with self.rt.transaction(db):
            w = self.rt.load(db, card['watch_id'])
            event = self.rt._queue_owner_event(db, w, channel, message_ts, self.rt._ask_root(card), actor,
                                       {'ask_proposal_rejected': pid, 'actor': actor}, now,
                                       self.rt.interaction_envelope(w, 'ask_answer', actor, self.rt._ask_root(card), now))
            db.execute("UPDATE ask_proposals SET status='rejected',decided_by=?,decided_at=?,rejected_event=? WHERE id=?",
                       (actor, now, event, pid))
        self._update_ask_proposal_message(db, user_token, card, self.rt._proposal_row(db, pid), text=self.rt.asks.phrase(lang, 'confirm_cancelled'))
        return {'ok': True, 'answer': 'no', 'duplicate': False, 'proposal_id': pid, 'status': 'rejected',
                'card_id': card['id'], 'rejected_event': event, 'authority': 'none'}

    def ask_sweep(self, db, now=None, token=None):
        """Due-time only: one reminder at remind_at, expiry at expire_at. Cheap when nothing is due."""
        now = time.time() if now is None else now
        due = db.execute("SELECT * FROM ask_cards WHERE state='open' AND dry=0 AND "
                         '(expire_at<=? OR (reminded_at IS NULL AND remind_at<=?)) '
                         'ORDER BY expire_at LIMIT 20', (now, now)).fetchall()
        if not due:
            return []
        user_token = token or self.rt.api_identity()
        done = []
        for card in due:
            owner = self.rt._ask_owner(db, card)
            if card['expire_at'] <= now:
                with self.rt.transaction(db):
                    current = self.rt._ask_card(db, card['id'])
                    if current['state'] != 'open':
                        continue
                    event = None
                    if card['watch_id']:
                        w = self.rt.load(db, card['watch_id'])
                        event = self.rt._queue_owner_event(db, w, card['channel'], card['ts'], self.rt._ask_root(card), self.bot_user,
                                                   {'ask_expired': card['id'], 'at': now}, now,
                                                   self.rt.interaction_envelope(w, 'ask_answer', self.bot_user, self.rt._ask_root(card), now,
                                                                        system=True))
                    db.execute("UPDATE ask_cards SET state='expired',expired_event=? WHERE id=?", (event, card['id']))
                    db.execute("UPDATE ask_proposals SET status='superseded',closed_reason='card_expired' "
                               "WHERE card_id=? AND status='pending'", (card['id'],))
                self._update_ask_card(db, card['id'], user_token, self.rt.asks.phrase(card['lang'], 'expired'))
                done.append((card['id'], 'expired'))
                continue
            with self.rt.transaction(db):
                # Mark before posting: a failed or slow post never turns into repeated reminders.
                if db.execute('UPDATE ask_cards SET reminded_at=? WHERE id=? AND reminded_at IS NULL AND state=?',
                              (now, card['id'], 'open')).rowcount != 1:
                    continue
            try:
                # The reminder mentions exactly the responders: the owner-only -> the assistant.
                sent = self.rt.slack.api_call('chat.postMessage', self._identity_token(
                        self.rt.audience_identity(json.loads(card['responders'])), user_token), json_body={
                    'channel': card['channel'], 'thread_ts': self.rt._ask_root(card), 'text': card['reminder_text'],
                    'metadata': self.rt.agent_metadata('ask_reminder', owner, card_id=card['id']),
                    'unfurl_links': False, 'unfurl_media': False})
                with self.rt.transaction(db):
                    db.execute('UPDATE ask_cards SET reminder_ts=? WHERE id=?', (sent['ts'], card['id']))
                    self.rt._ledger(db, card['channel'], sent['ts'], 'ask_reminder', card['id'], owner['session'])
            except Exception as exc:
                with self.rt.transaction(db):
                    db.execute('UPDATE ask_cards SET last_error=? WHERE id=?', (self.rt.safe_error(exc), card['id']))
            done.append((card['id'], 'reminded'))
        return done

    def smoke_ask(self, db, db_path, session, provider, cwd, text, options=None, confirm_mode='auto',
                  remind_after=_OWNER_DEFAULT, expire_after=_OWNER_DEFAULT, lang='zh', now=None, live_card=False):
        """TEST ask card in the decision channel, the owner as responder, with its own watch. Dry by default (no real Slack
        card, no reminders / expiry, no cleanup); `live_card` posts a real TEST ask card for receiver E2E."""
        remind_after = self.rt.asks.DEFAULT_REMIND if remind_after is _OWNER_DEFAULT else remind_after
        expire_after = self.rt.asks.DEFAULT_EXPIRE if expire_after is _OWNER_DEFAULT else expire_after
        now = time.time() if now is None else now
        uuid.UUID(session)
        provider = self.rt.claude.provider({'provider': provider})
        if not Path(cwd).expanduser().is_dir():
            raise ValueError('task_cwd_missing')
        label = self.msg('commands.smoke_ask.1') if lang == 'zh' else '🧪 TEST · smoke-ask (end-to-end test)'
        options = options or [self.msg('commands.smoke_ask.2'), self.msg('commands.smoke_ask.3')]
        responders, body, stored, reminder, remind, expire = self.rt._ask_inputs(
            [self.owner_slack_user], f'{label} {text}', options, confirm_mode, remind_after, expire_after, None, lang, smoke=True)
        channel = self.rt.buttons.load_config()['decision_channel']
        identity = self.rt.audience_identity(responders)  # the owner is the only responder: the assistant
        if live_card:  # P38 (F49): a live TEST ask card lands in the owner's decision channel: same guard (dry posts nothing)
            self.rt.buttons.require_private_destination(self.rt, db, channel, 'smoke_ask')
        owner = {'session': session, 'provider': provider, 'cwd': str(Path(cwd).expanduser().absolute())}
        card_id, ts = self._post_ask(db, owner, None, channel, None, responders, body, stored, confirm_mode, reminder, lang,
                                True, remind, expire, now, self._identity_token(identity), smoke=True, identity=identity,
                                live_card=live_card)
        child = self.rt.register(db, session=session, provider=provider, cwd=cwd, channel=channel, thread=ts,
                         peers=[self.owner_slack_user], mode='decision', since=ts, now=now, registry_note=False)
        with self.rt.transaction(db):
            child = self.rt.load(db, child['id'])
            child.update(smoke=True, label='TEST smoke-ask')
            if not live_card:
                child['dry'] = True  # a dry TEST watch is never polled against Slack (fetch short-circuits)
            self.rt.save(db, child)
            db.execute('UPDATE ask_cards SET watch_id=? WHERE id=?', (child['id'], card_id))
        self.rt.start(db_path)
        return {**self.rt.ask_summary(db, card_id), 'test': True, 'dry': not live_card, 'session': session, 'provider': provider,
                'cleanup': None if not live_card else f'slack_followup.py smoke-ask-cleanup --card-id {card_id}'}

    def smoke_ask_cleanup(self, db, card_id, now=None):
        """Stop only a smoke-ask watch, cancel the card and settle leftovers; real cards are refused."""
        now = time.time() if now is None else now
        card = self.rt._ask_card(db, card_id)
        if card is None or not card['smoke']:
            raise ValueError('not_a_smoke_ask_card')
        with self.rt.transaction(db):
            w = self.rt.load(db, card['watch_id']) if card['watch_id'] else None
            if w is not None and not w.get('smoke'):
                raise ValueError('not_a_smoke_watch')
            settled = []
            if w is not None:
                settled = [r[0] for r in db.execute("SELECT id FROM events WHERE watch_id=? AND status!='handled'", (w['id'],))]
                db.execute("UPDATE events SET status='handled',disposition='irrelevant' WHERE watch_id=? AND status!='handled'",
                           (w['id'],))
                self.rt.claude.settle_claims(db, settled, 'smoke_cleanup', now)
                w.update(state='stopped', stop_reason='smoke_cleanup', stopped_at=now)
                self.rt.save(db, w)
                if self.rt.claude.provider(w) == 'claude':
                    self.rt.claude.resolved(db, w['session'])
            db.execute("UPDATE ask_cards SET state='cancelled' WHERE id=? AND state IN ('sending','open')", (card_id,))
            db.execute("UPDATE ask_proposals SET status='superseded',closed_reason='smoke_cleanup' "
                       "WHERE card_id=? AND status='pending'", (card_id,))
        return {'ok': True, 'card_id': card_id, 'watch_id': w['id'] if w else None, 'stopped': w is not None,
                'settled_events': settled, 'slack_card': f'{card["channel"]}/{card["ts"]} (left in Slack, labeled TEST)'}

    def handoff(self, db, wid, session, cwd, provider=None, owner_pid=None):
        uuid.UUID(session)
        cwd = str(Path(cwd).expanduser().absolute())
        if not Path(cwd).is_dir():
            raise ValueError('task_cwd_missing')
        with self.rt.transaction(db):
            w = self.rt.load(db, wid)
            old_provider = self.rt.claude.provider(w)
            new_provider = old_provider if provider is None else self.rt.claude.provider({'provider': provider})
            if (new_provider, session) != (old_provider, w['session']):
                if db.execute("SELECT 1 FROM events WHERE watch_id=? AND status!='handled'", (wid,)).fetchone():
                    raise ValueError('reconcile_pending_events_before_handoff')
            w.update(session=session, provider=new_provider, cwd=cwd,
                     owner_pid=owner_pid, owner_birth=self.rt.pid_birth(owner_pid))
            self.rt.save(db, w)
        # A handoff makes the new owner the task's current session (registry only).
        if not w.get('smoke'):
            self.rt._registry_hook(db, self.rt.registry.note_success, new_provider, session, cwd, time.time(), 'handoff')
        return w

    def claude_hook(self, db, db_path, bind=False):
        data = json.load(sys.stdin)
        session = data.get('session_id', '')
        uuid.UUID(session)
        if data.get('hook_event_name') not in ('SessionStart', 'FileChanged'):
            raise ValueError('unsupported_claude_hook_event')
        target = self.rt.claude.ensure_path(db_path, session)
        if bind:
            if data.get('hook_event_name') != 'SessionStart':
                raise ValueError('binding_requires_session_start')
            # Create once so the native filesystem watcher can subscribe before events.
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    json.dump({'provider': 'claude', 'session_id': session, 'generation': 0}, stream)
            except FileExistsError:
                if target.is_symlink():
                    raise ValueError('invalid_claude_wake_file')
            envfile = os.environ.get('CLAUDE_ENV_FILE')
            if envfile:
                with open(envfile, 'a') as stream:
                    stream.write('\nexport CLAUDE_CODE_SESSION_ID=' + shlex.quote(session) + '\n')
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'watchPaths': [str(target)]}}))
            return
        if data['hook_event_name'] == 'FileChanged' and data.get('file_path') != str(target):
            return  # dynamic watchPaths can coexist with unrelated FileChanged hooks
        # Identity comes from native hook input, never from wake-file contents.
        os.environ['CLAUDE_CODE_SESSION_ID'] = session
        batch = self.rt.claude.wait(db, session, timeout=0)
        if batch:
            print(self.rt.claude_batch_prompt(db, batch, db_path), file=sys.stderr)
            sys.exit(2)  # asyncRewake wakes the original CLI; no new model process

    def ensure_inbox_watch(self, db, task_id, task_path, provider, session, now):
        """Inside a transaction: the local inbox watch of (task, session) for notes and agent messages.

        State `inbox` is neither running (never polled) nor stopped: due_watches selects it only
        when it has pending events, and delivery is provider-aware exactly like any other watch.
        """
        uuid.UUID(session)
        self.rt.claude.provider({'provider': provider})
        wid = hashlib.sha256(f'inbox/{task_id}/{provider}/{session}'.encode()).hexdigest()[:20]
        row = db.execute('SELECT data FROM watches WHERE id=?', (wid,)).fetchone()
        if row:
            w = json.loads(row[0])
            if w['state'] == 'stopped':
                w.update(state='inbox', reopened_at=now)
                w.pop('stop_reason', None)
                self.rt.save(db, w)
            return w
        w = dict(id=wid, session=session, provider=provider, cwd=str(task_path), channel=self.rt.LOCAL_CHANNEL, thread=None,
                 peers=[], dm=None, mode='observe', lifecycle='until_stop', scope='thread', context_all=False,
                 kind=INBOX_KIND, state='inbox', started=now, since=str(now), deadline=None, next_poll=now + 10 ** 9,
                 step=0, polls=0, owner_pid=None, owner_birth=None, last_error=None, cadence_activity_ts=now,
                 cadence_version=self.rt.schedule.VERSION, label=self.msg('commands.ensure_inbox_watch.1'), task_id=task_id)
        self.rt.save(db, w)
        # P38: a long-lived task gets a new session per event, so retire its older inbox watches once nothing is open on
        # them (a stopped inbox watch reopens if its session is ever current again).
        for row in db.execute("SELECT data FROM watches w WHERE json_extract(data,'$.kind')=? AND "
                              "json_extract(data,'$.task_id')=? AND json_extract(data,'$.state')='inbox' AND id!=? AND NOT "
                              "EXISTS (SELECT 1 FROM events e WHERE e.watch_id=w.id AND e.status!='handled')",
                              (INBOX_KIND, task_id, wid)).fetchall():
            old = json.loads(row[0])
            old.update(state='stopped', stop_reason=INBOX_RETIRED, stopped_at=now)
            self.rt.save(db, old)
        return w

    def enqueue_local_event(self, db, task_id, task_path, provider, session, kind, path, sender, env, now, hold=None):
        """Pending event on the task inbox of `session`; body stays in the workspace file."""
        with self.rt.transaction(db):
            w = self.ensure_inbox_watch(db, task_id, task_path, provider, session, now)
            ts = f'{now:.6f}'
            digest = hashlib.sha256(json.dumps({'local': kind, 'path': str(path)}, sort_keys=True).encode()).hexdigest()
            eid = hashlib.sha256(f"{w['id']}/{self.rt.LOCAL_CHANNEL}/{ts}/{digest}".encode()).hexdigest()[:24]
            db.execute('INSERT INTO events (id,watch_id,channel,ts,author,digest,received,status,receipt) '
                       'VALUES (?,?,?,?,?,?,?,?,?)', (eid, w['id'], self.rt.LOCAL_CHANNEL, ts, sender, digest, now,
                                                      'dispatched' if hold else 'pending', hold))
            db.execute('INSERT OR IGNORE INTO event_sources VALUES (?,?)', (eid, ts))
            self.rt.envelopes.store(db, eid, env, now)
            db.execute('INSERT INTO local_messages VALUES (?,?,?,?,?,?)', (eid, kind, task_id, str(path), sender, now))
        return eid, w['id']

    def _resolve_task_arg(self, db, value):
        """--task accepts a task id or a workspace directory (an existing workspace gets its task row)."""
        text = str(value)
        if '/' in text:
            tid = self.rt.registry.task_id_for_cwd(str(Path(text).expanduser().absolute()))
            if not tid:
                raise ValueError('directory_is_not_a_task_workspace')
        else:
            tid = text
            if db.execute('SELECT 1 FROM tasks WHERE task_id=?', (tid,)).fetchone() is None:
                path = self.rt.registry.task_dir(tid)
                found = self.rt.registry.task_id_for_cwd(str(path)) if path.is_dir() else None
                if found != tid and not (found and path.is_symlink()):
                    raise ValueError('unknown_task')
                tid = found  # P39: a renamed task's old name (its symlink, task-rename) is the new task, never a 2nd row
        with self.rt.transaction(db):
            self.rt.registry.ensure_task(db, tid, time.time())
        return self.rt._task_row(db, tid)

    def note(self, db, db_path, task, text, now=None):
        """Write a task note; a non-current author also wakes the current session with an agent_message event."""
        now = time.time() if now is None else now
        row = self._resolve_task_arg(db, task)
        provider, session = self.rt.claude.actor()
        author = f'{provider}:{session}' if session else self.key
        current = (row['current_provider'], row['current_session']) if row['current_session'] else None
        self.rt.asks.check_secret_free(text)
        path = self.rt.task_memory.write_note(row['path'], row['task_id'], author, text, now,
                                      f'{current[0]}:{current[1]}' if current else None)
        result = {'ok': True, 'task_id': row['task_id'], 'file': str(path), 'author': author, 'event': None}
        if current is None:
            return {**result, 'reason': 'task_without_current_session_note_kept_in_history'}
        if session and current == (provider, session):
            return {**result, 'reason': 'author_is_the_current_session_merge_it_yourself'}
        env = (self.rt.envelopes.envelope('agent_message', 'agent', 'agent', 'none', 'local_note', None, 0,
                                  {'provider': provider, 'session': session, 'task_cwd': os.getcwd()}) if session else
               self.rt.envelopes.envelope('agent_message', 'human', self.key, 'none', f'local_note_{self.key}', None, 0))
        eid, wid = self.enqueue_local_event(db, row['task_id'], row['path'], current[0], current[1], 'note', path,
                                       'agent:' + author if session else self.key, env, now)
        self.rt.start(db_path)
        return {**result, 'event': eid, 'watch_id': wid, 'to': f'{current[0]}:{current[1]}'}

    def send_to_task(self, db, db_path, task, text, from_task=None, now=None):
        """Any agent may message any task (never authority); revival and the circuit breaker apply."""
        now = time.time() if now is None else now
        row = self._resolve_task_arg(db, task)
        provider, session = self.rt.claude.actor()
        sender = f'{provider}:{session}' if session else self.rt.registry.actor_label((provider, None))
        from_task = from_task or self.rt.registry.task_id_for_cwd(os.getcwd())
        if from_task == row['task_id']:
            raise ValueError('cannot_send_to_own_task_use_note')
        self.rt.asks.check_secret_free(text)
        from_key = from_task or (f'session:{provider}:{session}' if session else sender)
        try:
            hops = max(0, int(os.environ.get(self.hops_env) or 0)) + 1
        except ValueError:
            hops = 1
        disp = self.rt.make_dispatcher(db, db_path)
        state, breaker_id, trips = disp.breaker_check(from_key, row['task_id'], now)
        if state == 'stopped':
            raise ValueError(f'agent_pair_stopped_by_{self.key}_for_24h')
        path = self.rt.task_memory.write_message(row['path'], row['task_id'], sender, from_task, text, now, hops)
        if state == 'tripped':
            breaker_id = disp.trip_breaker(from_key, row['task_id'], trips, now)
        current = (row['current_provider'], row['current_session']) if row['current_session'] else None
        result = {'ok': True, 'task_id': row['task_id'], 'file': str(path), 'from': from_key, 'hops': hops, 'event': None,
                  'round_trips_last_hour': trips, 'breaker': breaker_id if state in ('paused', 'tripped') else None,
                  'authority': 'none'}
        message_id = uuid.uuid4().hex[:16]
        eid = None
        if current is None:
            result['reason'] = 'task_without_current_session_message_kept_in_history'
        elif session and current == (provider, session):
            result['reason'] = 'sender_is_the_target_current_session_no_self_wake'
        else:
            env = self.rt.envelopes.envelope('agent_message', 'agent', 'agent', 'none', 'local_agent_message', None, hops,
                                     {'provider': provider, 'session': session, 'task_cwd': os.getcwd()})
            hold = 'breaker:' + breaker_id if state in ('paused', 'tripped') else None
            eid, wid = self.enqueue_local_event(db, row['task_id'], row['path'], current[0], current[1], 'message', path,
                                           'agent:' + sender, env, now, hold)
            result.update(event=eid, watch_id=wid, to=f'{current[0]}:{current[1]}', held_by_breaker=bool(hold))
        with self.rt.transaction(db):
            db.execute('INSERT INTO agent_messages VALUES (?,?,?,?,?,?,?,?,?)',
                       (message_id, from_key, row['task_id'], sender, eid, str(path), hops,
                        breaker_id if state in ('paused', 'tripped') else None, now))
        if eid:
            self.rt.start(db_path)
        return result

    def memory_guard(self, db, action, task, expect=None, now=None):
        now = time.time() if now is None else now
        row = self._resolve_task_arg(db, task)
        if action == 'check':
            return self.rt.task_memory.check(row['path'], expect)
        writer = self.rt._actor_label() or self.key
        return self.rt.task_memory.bump(row['path'], writer, now, expect)

    def dispatch_hook(self, db, db_path, run_id, stdin=None):
        """PreToolUse hook of dispatched Claude runs: a terminal question becomes an ask-<owner key> card; always deny."""
        reason = (self.msg('commands.dispatch_hook.1', owner_key=self.key))
        if self.rt.dispatch.KEEP_WARM_ID_RE.fullmatch(str(run_id)):  # P40.1: a keep-warm refresh may run no tool at all
            reason = 'keep-warm run: no tool may run; finish now.'
        try:
            data = json.load(stdin or sys.stdin)
            tool, session = data.get('tool_name'), str(data.get('session_id') or '')
            run = db.execute('SELECT * FROM consumer_runs WHERE run_id=?', (str(run_id),)).fetchone()
            if run and run['state'] == 'running' and run['provider'] == 'claude' and run['session'] == session \
                    and tool in self.rt.dispatch.INTERACTIVE_TOOLS:
                question, options = self.rt.dispatch.hook_question(tool, data.get('tool_input'))
                try:
                    self.rt.asks.check_secret_free(question)
                except ValueError:
                    question, options = self.msg('commands.dispatch_hook.2'), []
                # The session cannot fix a relayed question: drop ids / jargon instead of refusing (P3.1 copy standard).
                card = getattr(self.rt, f'ask_{self.key}')(db, db_path, session, 'claude', run['task_path'], self.rt.buttons.scrub_copy(question), options,
                                trusted_owner=True, lint=False,
                                footer=self.msg('commands.dispatch_hook.3', dashboard_url=self.dashboard_url, run_id=run_id))
                reason = (self.msg('commands.dispatch_hook.4', owner=self.name, event_id=card['event_id']))
        except Exception as exc:  # noqa: BLE001 - the hook must always answer "deny"
            self.rt._meta_error(db, 'dispatch_hook_error', exc)
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                                 'permissionDecisionReason': reason}}, ensure_ascii=False))

    def tail_command_text(self, target, db_path):
        """Shell line typed into the new iTerm tab: `<tail_command> <run-id>` (validated ids only)."""
        try:
            linked = self.rt.TAIL_LINK.resolve() == self.rt.TAIL_WRAPPER.resolve()
        except OSError:
            linked = False
        db = ['--db', shlex.quote(str(db_path))] if Path(db_path).expanduser().resolve() != self.rt.DEFAULT_DB.expanduser().resolve() else []
        if not os.access(self.rt.TAIL_WRAPPER, os.X_OK):  # e.g. a patch install that dropped the exec bit
            return ' '.join([shlex.quote(sys.executable), '-I', shlex.quote(str(Path(self.rt.__file__).resolve())), *db, 'tail',
                             shlex.quote(str(target))])
        return ' '.join([shlex.quote(str(self.rt.TAIL_LINK if linked else self.rt.TAIL_WRAPPER)), *db, shlex.quote(str(target))])

    def open_iterm_tab(self, workdir, command_text):
        """A NEW iTerm2 tab on this Mac via the workspace skill's launch_iterm2.scpt (its own activate only)."""
        # Test harnesses set this: a unit test must never open a real terminal tab on the owner's Mac.
        if os.environ.get(self.no_iterm_env) == '1':
            raise OSError('iterm_disabled_by_test_harness')
        return subprocess.run([self.rt.OSASCRIPT, str(self.rt.ITERM_SCRIPT), str(workdir), command_text], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=15)

    def _log_dispatch_action(self, db, dispatch_id, run_id, action, actor, result, now):
        try:
            with self.rt.transaction(db):
                db.execute('INSERT INTO dispatch_actions (dispatch_id,run_id,action,actor,result,at) VALUES (?,?,?,?,?,?)',
                           (dispatch_id, run_id, action, actor, result, now))
        except sqlite3.Error as exc:
            self.rt._meta_error(db, 'dispatch_action_log_error', exc)

    def dispatch_iterm(self, db, db_path, d, actor, now=None):
        """[📺 在 iTerm 里看]: open `<tail_command> <run-id>` in a new iTerm2 tab on the owner's Mac."""
        now = time.time() if now is None else now
        run = self.rt._latest_run(db, dispatch_id=d['dispatch_id'])
        target = run['run_id'] if run else d['dispatch_id']  # no run yet: <tail_command> waits for the first one
        command_text = self.tail_command_text(target, db_path)
        manual = self.msg('commands.dispatch_iterm.1', escape=self.rt.buttons.escape(command_text))
        if sys.platform != 'darwin' or not self.rt.ITERM_SCRIPT.is_file():
            self._log_dispatch_action(db, d['dispatch_id'], run and run['run_id'], 'iterm', actor, 'unavailable', now)
            return self.rt._fail('iterm_unavailable', dispatch_id=d['dispatch_id'], ephemeral=self.msg('commands.dispatch_iterm.2') + manual)
        workdir = d['task_path'] if d['task_path'] and Path(d['task_path']).is_dir() else str(Path.home())
        try:
            proc = self.rt.open_iterm_tab(workdir, command_text)
            opened = proc.returncode == 0
            detail = 'opened' if opened else f'osascript_exit_{proc.returncode}'
        except subprocess.TimeoutExpired:
            opened, detail = False, 'osascript_timeout'
        except OSError as exc:
            opened, detail = False, 'osascript_' + type(exc).__name__
        self._log_dispatch_action(db, d['dispatch_id'], run and run['run_id'], 'iterm', actor, detail, now)
        if not opened:
            return self.rt._fail('iterm_open_failed', dispatch_id=d['dispatch_id'], detail=detail,
                         ephemeral=self.msg('commands.dispatch_iterm.3', detail=detail) + manual)
        what = f'run `{run["run_id"]}`' if run else self.msg('commands.dispatch_iterm.4')
        return {'ok': True, 'action': 'iterm', 'dispatch_id': d['dispatch_id'], 'run_id': run and run['run_id'],
                'command': command_text, 'ephemeral': self.msg('commands.dispatch_iterm.5', what=what, escape=self.rt.buttons.escape(command_text))}

    def run_view_text(self, db, d, now=None, steps=VIEW_STEPS):
        """[查看]: a private Chinese summary of the dispatch's latest run (masked steps, no bodies)."""
        now = time.time() if now is None else now
        task = db.execute('SELECT title FROM tasks WHERE task_id=?', (d['task_id'],)).fetchone() if d['task_id'] else None
        name = self.rt.buttons.escape(d['task_id'] or d['task_key'])
        title = self.msg('commands.run_view_text.1', escape=self.rt.buttons.escape(task[0])) if task and task[0] else ''
        head = self.msg('commands.run_view_text.2', name=name, title=title)
        run = self.rt._latest_run(db, dispatch_id=d['dispatch_id'])
        if run is None:
            plan = json.loads(d['plan'] or '[]')
            first = plan[min(d['step'], len(plan) - 1)] if plan else {}
            return (self.msg('commands.run_view_text.3', head=head, escape=self.rt.buttons.escape(d['state']), get=self.rt.dispatch.PROVIDER_NAME.get(first.get('provider'), first.get('provider') or '?'), len=len(self.rt.dispatch_held(db, d['dispatch_id']))))
        handled, total = self.rt._run_counts(db, run)
        elapsed = self.rt.run_log.duration(self.rt._run_elapsed(run, now))
        started = self.rt.dispatch._clock(run['started_at']) if run.get('started_at') else '?'
        if run['state'] == 'running':
            state = self.msg('commands.run_view_text.4', elapsed=elapsed, started=started)
        else:
            label = self.rt.OUTCOME_LABEL.get(run.get('outcome'), self.rt.buttons.escape(run.get('outcome') or '?'))
            reason = self.rt.dispatch.ERROR_LABELS.get(run.get('error_class') or '', '')
            state = f'{label}' + (self.msg('commands.run_view_text.5', reason=reason) if reason and run.get('outcome') != 'success' else '') + self.msg('commands.run_view_text.6', elapsed=elapsed)
        lines = [head, f"run `{run['run_id']}` · {self.rt.buttons.escape(self.rt._run_who(run))}", self.msg('commands.run_view_text.7', state=state),
                 self.msg('commands.run_view_text.8', handled=handled, total=total)]
        recent = []
        if run.get('log_dir') and Path(run['log_dir']).is_dir():
            if run['state'] == 'running':
                self.rt.run_log.update(run['log_dir'], run['provider'])
            recent = [ln for ln in self.rt.run_log.read_lines(run['log_dir']) if self.rt.run_log.is_step_line(ln)][-steps:]
            if not recent and (Path(run['log_dir']) / self.rt.run_log.EVENTS).exists():  # older run without readable.log
                rendered, _ = self.rt.run_log.render_text(run['provider'], self.rt.dispatch._read_tail(Path(run['log_dir']) / self.rt.run_log.EVENTS))
                recent = [ln for ln in rendered if self.rt.run_log.is_step_line(ln)][-steps:]
        footer = self.msg('commands.run_view_text.9', tail_command=self.tail_command, run_id=run['run_id'], dashboard_url=self.dashboard_url, run_id2=run['run_id'])
        if recent:
            body = [self.rt.buttons.escape(self.rt.run_log.short(ln, 170)) for ln in recent]
            text = '\n'.join(lines + [self.msg('commands.run_view_text.10', len=len(body)), '```' + '\n'.join(body) + '```', footer])
            while len(text) > EPHEMERAL_LIMIT and len(body) > 1:
                body = body[1:]
                text = '\n'.join(lines + [self.msg('commands.run_view_text.11', len=len(body)), '```' + '\n'.join(body) + '```', footer])
            return text[:EPHEMERAL_LIMIT]
        return '\n'.join(lines + [self.msg('commands.run_view_text.12'), footer])[:EPHEMERAL_LIMIT]

    def dispatch_view(self, db, db_path, d, actor, now=None):
        now = time.time() if now is None else now
        text = self.run_view_text(db, d, now)
        run = self.rt._latest_run(db, dispatch_id=d['dispatch_id'])
        self._log_dispatch_action(db, d['dispatch_id'], run and run['run_id'], 'view', actor, 'shown', now)
        return {'ok': True, 'action': 'view', 'dispatch_id': d['dispatch_id'], 'run_id': run and run['run_id'],
                'ephemeral': text}

    def dispatch_action(self, db, db_path, kind, ident, choice, actor, channel, message_ts, now=None):
        """Receiver-only: run-card / failure-card / breaker-card buttons. All checks precede any write."""
        now = time.time() if now is None else now
        if self.rt._agent_session() or os.environ.get(self.run_id_env):
            return self.rt._fail('agent_session_cannot_decide')
        pattern = self.rt.DISPATCH_ID_RE if kind == 'dispatch' else BREAKER_ID_RE
        if not (pattern.fullmatch(str(ident)) and self.rt._ids_valid(actor, channel, message_ts)):
            return self.rt._fail('invalid_request')
        try:
            config = self.rt.buttons.load_config()
        except ValueError:
            return self.rt._fail('invalid_decision_config')
        if self.rt.buttons.authorize(config, actor, now) is None:
            return self.rt._fail('not_authorized')
        disp = self.rt.make_dispatcher(db, db_path)
        if kind == 'breaker':
            row = db.execute('SELECT * FROM agent_breakers WHERE id=?', (ident,)).fetchone()
            if row is None:
                return self.rt._fail('unknown_breaker')
            if (row['card_channel'], row['card_ts']) != (channel, message_ts):
                return self.rt._fail('card_mismatch')
            if choice not in ('continue', 'stop'):
                return self.rt._fail('invalid_choice')
            return disp.breaker_decide(ident, choice, actor, now)
        d = db.execute('SELECT * FROM dispatches WHERE dispatch_id=?', (ident,)).fetchone()
        if d is None:
            return self.rt._fail('unknown_dispatch')
        if d['card_channel'] != channel or message_ts not in (d['card_ts'], d['fail_card_ts']):
            return self.rt._fail('card_mismatch')
        d = dict(d)
        if choice == 'iterm':
            return self.dispatch_iterm(db, db_path, d, actor, now)
        if choice == 'view':
            return self.dispatch_view(db, db_path, d, actor, now)
        modes = {'retry': 'auto', 'retry_codex': 'codex', 'retry_claude': 'claude'}
        if choice not in ('stop', 'resume', *modes):
            return self.rt._fail('invalid_choice')
        if choice == 'stop':
            result = disp.stop(ident, actor, now)
        elif choice == 'resume':
            result = disp.resume(ident, actor, now)
        else:
            result = disp.retry(ident, modes[choice], actor, now)
        self._log_dispatch_action(db, ident, d['run_id'], choice, actor,
                             'duplicate' if result.get('duplicate') else 'ok' if result.get('ok') else 'failed', now)
        return result

    def takeover_task(self, db, target):
        """Task key of a task id, a task workspace directory or a session id / unique prefix (Claude or Codex): the task the
        session is current for, else the task of its latest run."""
        text = str(target or '').strip()
        try:
            return self._resolve_task_arg(db, text)['task_id']
        except ValueError:
            if '/' in text or not SESSION_PREFIX_RE.fullmatch(text.lower()):
                raise ValueError(self.msg('commands.takeover_task.1', text=text)) from None
        like = text.lower() + '%'
        rows = (db.execute('SELECT current_provider, current_session, task_id FROM tasks WHERE current_session LIKE ? '
                           'ORDER BY updated_at DESC', (like,)).fetchall() +
                db.execute('SELECT provider, session, task FROM consumer_runs WHERE session LIKE ? '
                           'ORDER BY started_at DESC', (like,)).fetchall())
        sessions = sorted({f'{r[0]} {r[1]}' for r in rows})
        if len(sessions) > 1:
            raise ValueError(self.msg('commands.takeover_task.3', text=text, join=self.msg('commands.takeover_task.2').join(sessions)))
        if not sessions:
            raise ValueError(self.msg('commands.takeover_task.4', text=text))
        return rows[0][2]

    def takeover(self, db, db_path, target, wait=20.0, force=False, sleep=time.sleep):
        """P43 `takeover` (fsess take): hold the task like the owner's ⏹ (`Dispatcher.hold`, reason <owner key>_takeover), SIGTERM
        its run, wait `wait` s for it to end (SIGKILL only with `force`), mark the run card, pin the session as the task's
        current one and say how to resume it. the owner's terminal only; idempotent."""
        getattr(self.rt, f'{self.key}_terminal_only')()
        disp = self.rt.make_dispatcher(db, db_path)
        key = self.takeover_task(db, target)
        for run in [dict(r) for r in db.execute("SELECT * FROM consumer_runs WHERE task=? AND state='running'", (key,))]:
            disp.capture_session(run)  # a fresh Codex run's thread id, as soon as the CLI reported it
        provider, session = self.rt._takeover_session(db, disp, key)
        running = db.execute("SELECT 1 FROM consumer_runs WHERE task=? AND state='running'", (key,)).fetchone()
        if not session and not running:
            raise ValueError(self.msg('commands.takeover.1', key_value=key))
        card = (disp._task_dispatch(key, ('running',)) or
                disp._task_dispatch(key, ('waiting', 'paused', 'failed', f'needs_{self.key}')))
        killed, fresh = [], (disp.paused(key) or {}).get('reason') != self.rt.dispatch.TAKEOVER_REASON
        if fresh:
            killed = disp.hold(key, self.rt.dispatch.TAKEOVER_ACTOR, self.rt.dispatch.TAKEOVER_REASON, disp.clock())

        def settle(seconds):  # a run `start` reserved before the hold (a lease without pid) is waited for and stopped too
            deadline = time.monotonic() + max(0.0, seconds)
            while True:
                live, lease = _live_runs(db, disp, key), disp.lease(key)
                for run in [r for r in live if not r['stop_requested']]:
                    disp._signal(run, signal.SIGTERM, 1, disp.clock())
                    killed.append(run['run_id'])
                starting = [lease['run_id']] if lease and lease['holder_kind'] == 'headless' and lease['pid'] is None \
                    and disp.lease_valid(lease) else []
                if not (live or starting) or time.monotonic() >= deadline:
                    return live, starting
                sleep(0.25)
        live, starting = settle(wait)
        if live and force:
            for run in live:
                disp._signal(run, signal.SIGKILL, 1, disp.clock())
            live, starting = settle(self.rt.TAKEOVER_KILL_SECONDS)
        if card and (fresh or killed):
            disp._card_status(card['dispatch_id'], 'paused', actor=self.rt.dispatch.TAKEOVER_ACTOR)
        if not session:  # a fresh Codex run stopped before its thread id was bound: finalize read it from the log
            provider, session = self.rt._takeover_session(db, disp, key)
        if not session:
            raise ValueError(self.msg('commands.takeover.2', key_value=key, key_value2=key))
        task = disp.task_for_key(key) or {}
        if task.get('task_id') and (fresh or task.get('current') != (provider, session)):
            # the owner works on in this session: the handback resumes it (audited, current_source manual)
            with self.rt.transaction(db):
                self.rt.registry.set_current(db, key, provider, session, self.rt.dispatch.TAKEOVER_ACTOR, self.rt.dispatch.TAKEOVER_REASON)
        return {'ok': True, 'task_id': key, 'provider': provider, 'session': session,
                'cwd': disp._run_cwd({'task_path': task.get('path'), 'task_id': task.get('task_id')},
                                     {'mode': 'resume', 'provider': provider, 'session': session}),
                'killed_runs': killed, 'still_running': [r['run_id'] for r in live] + starting,
                'held_events': self.rt._held_count(db, disp, key),
                'resume_command': shlex.join(['codex', 'resume', session] if provider == 'codex' else
                                             ['claude', '--resume', session]),
                'handback_command': shlex.join([sys.executable, str(Path(self.rt.__file__).resolve()), '--db', str(db_path),
                                                'handback', '--task', key])}

    def handback(self, db, db_path, target):
        """P43 `handback` (fsess back): end the hold the way ▶️ does (`Dispatcher.release`); the held events resume the
        task's current session (the one the owner worked in) headless. the owner's terminal only; idempotent."""
        getattr(self.rt, f'{self.key}_terminal_only')()
        disp = self.rt.make_dispatcher(db, db_path)
        key = self.takeover_task(db, target)
        task = disp.task_for_key(key) or {}
        provider, session = task.get('current') or (None, None)
        keep, released, mode = None, bool(disp.paused(key)), 'resume' if session else None
        if released:
            if _live_runs(db, disp, key):
                raise ValueError(self.msg('commands.handback.1', key_value=key))
            if session and disp.probe.check(provider, session)['state'] in self.rt.liveness.LIVE:  # never two writers
                raise ValueError(self.msg('commands.handback.2', provider=provider, value=session[:self.rt.dispatch.SESSION_ID_CHARS]))
            plan = disp.plan_for(task, task['current'], disp.config()) if task.get('current') else None
            keep = disp.release(key, self.rt.dispatch.TAKEOVER_ACTOR, disp.clock(), plan=plan)
            if keep:
                disp._card_status(keep, 'resumed', actor=self.rt.dispatch.TAKEOVER_ACTOR)
            if plan:  # what the restart runs: a long-lived task / a session past the context cap starts fresh (P38)
                provider, session, mode = plan[0]['provider'], plan[0]['session'], plan[0]['mode']
        return {'ok': True, 'task_id': key, 'provider': provider, 'session': session, 'mode': mode, 'released': released,
                'restarted': keep, 'held_events': self.rt._held_count(db, disp, key)}

    def _tail_one(self, db, run, raw, follow, out, poll, sleep, clock):
        self.rt._tail_print(out, self.rt._tail_header(run))
        run_dir = Path(run['log_dir']) if run.get('log_dir') else None
        if run_dir is None or not run_dir.is_dir():
            self.rt._tail_print(out, self.msg('commands._tail_one.1'))
            text, code = self.rt._tail_outcome(db, run, clock())
            self.rt._tail_print(out, text)
            return code
        path = run_dir / (self.rt.run_log.EVENTS if raw else self.rt.run_log.READABLE)
        offset, settle = 0, 0
        while True:
            if not raw:
                self.rt.run_log.update(run_dir, run['provider'])
            text, offset = self.rt.run_log.read_from(path, offset)
            for line in text.splitlines():
                self.rt._tail_print(out, self.rt.run_log.mask(line) if raw else line)
            current = self.rt._latest_run_by_id(db, run['run_id']) or run
            exited = (run_dir / 'exit.json').exists()
            if not follow:
                break
            if current['state'] != 'running':
                if not raw:  # older runs never got a closing line; finish() is idempotent
                    self.rt.run_log.finish(run_dir, self.rt._run_exit_info(run_dir), run['provider'])
                text, offset = self.rt.run_log.read_from(path, offset)
                for line in text.splitlines():
                    self.rt._tail_print(out, self.rt.run_log.mask(line) if raw else line)
                run = current
                break
            if exited:
                settle += 1
                if settle > 10:  # the process is gone but nobody recorded the outcome (supervisor down?)
                    run = current
                    self.rt._tail_print(out, self.msg('commands._tail_one.2'))
                    break
            sleep(poll)
        run = self.rt._latest_run_by_id(db, run['run_id']) or run
        text, code = self.rt._tail_outcome(db, run, clock())
        self.rt._tail_print(out, text)
        return code if follow else 0

    def run_tail(self, db, db_path, target, raw=False, follow=True, out=None, poll=1.0, sleep=time.sleep, clock=time.time,
                  chain_wait=TAIL_CHAIN_WAIT_SECONDS):
        """Print a run's readable log (or the masked raw stream) and follow it until the run ends.

        TARGET is a run id (that run only), a dispatch id or a task id/workspace (its latest run, then
        any retry / fallback / follow-up run of the same dispatch or task while one is pending).
        Exit code: 0 success (or --no-follow), 1 failed/stopped/unresolved, 3 nothing to show.
        """
        out = out or sys.stdout
        kind, ref = self.rt._tail_resolve(db, target)
        seen = set()
        run = self.rt._tail_latest(db, kind, ref)
        deadline = clock() + chain_wait
        if run is None:
            if not follow or not self.rt._tail_active(db, kind, ref):
                self.rt._tail_print(out, self.msg('commands.run_tail.1'))
                return 3
            self.rt._tail_print(out, self.msg('commands.run_tail.2'))
            while run is None and self.rt._tail_active(db, kind, ref) and clock() < deadline:
                sleep(poll)
                run = self.rt._tail_latest(db, kind, ref)
            if run is None:
                self.rt._tail_print(out, self.msg('commands.run_tail.3'))
                return 3
        while True:
            code = self._tail_one(db, run, raw, follow, out, poll, sleep, clock)
            seen.add(run['run_id'])
            if not follow or kind == 'run':
                return code
            nxt, deadline = None, clock() + chain_wait
            while self.rt._tail_active(db, kind, ref) and clock() < deadline:
                candidate = self.rt._tail_latest(db, kind, ref)
                if candidate and candidate['run_id'] not in seen:
                    nxt = candidate
                    break
                sleep(poll)
            if nxt is None:
                return code
            self.rt._tail_print(out, '')
            self.rt._tail_print(out, self.msg('commands.run_tail.4'))
            run = nxt

    def launch_task(self, db, db_path, task_dir, provider, prompt, *, important=False, category=None, parent=None,
                    allow_existing=False, wait_seconds=90.0, now=None, dispatcher=None, from_card=None, persist=True):
        """Start the FIRST session of a task headless (P2 intake) and register it as the current session.

        P3.2: `from_card` is the escalation event of the card this task is launched from (an intake brief, a decision card):
        the new task joins that card's approvals thread (inherit_approvals_thread), so its cards thread under the same root.
        P39: `persist` records this launch's explicit provider (never `auto`), important and category as the task's launch
        preference, which later system re-dispatches honour (Dispatcher.launch)."""
        now = time.time() if now is None else now
        if provider not in ('auto', *self.rt.claude.PROVIDERS):
            raise ValueError('provider_must_be_auto_codex_or_claude')
        prompt = self.rt.task_memory.clean_text(prompt)
        self.rt.asks.check_secret_free(prompt)
        path = Path(task_dir).expanduser().absolute()
        if not path.is_dir():
            raise ValueError('task_cwd_missing')
        tid = self.rt.registry.task_id_for_cwd(str(path))
        if not tid:
            raise ValueError('task_dir_must_be_a_workspace_under_the_tasks_root')
        with self.rt.transaction(db):
            self.rt.registry.ensure_task(db, tid, now)
        row = self.rt._task_row(db, tid)
        if row['current_session'] and not allow_existing:
            raise ValueError('task_already_has_a_current_session_use_send_to_task_or_--allow-existing')
        if category is not None and not self.rt.worker_config.CATEGORY_RE.fullmatch(category):
            raise ValueError('invalid_category')
        if parent:
            with self.rt.transaction(db):
                self.rt.registry.link_parent(db, tid, parent, self.rt.registry.actor_label(self.rt.claude.actor()), session=self.rt.claude.actor()[1],
                                     reason='launch-task', now=now)
        if from_card:
            try:
                self.rt.inherit_approvals_thread(db, tid, from_card, now)
            except Exception as exc:  # noqa: BLE001 - threading is best effort; launch must proceed
                self.rt._meta_error(db, 'approvals_watch_error', exc)
        disp = dispatcher or self.rt.make_dispatcher(db, db_path)
        task = disp.task_for_key(tid)
        dispatch_id = disp.launch(task, provider, prompt, important=important, category=category, now=now, persist=persist)
        run_id = disp.start(dispatch_id, now=now)
        if run_id is None:
            self.rt.start(db_path)  # at capacity or busy: the supervisor starts it as soon as it can
        deadline = time.time() + max(0.0, float(wait_seconds))
        while True:
            d = disp._dispatch(dispatch_id)
            run = disp._run(d['run_id']) if d['run_id'] else None
            if run and run['provider'] == 'codex' and not run['session']:
                disp.capture_session(run)
                run = disp._run(run['run_id'])
            if (run and run['session']) or d['state'] not in self.rt.dispatch.ACTIVE or time.time() >= deadline:
                break
            time.sleep(0.5)
        row = self.rt._task_row(db, tid)
        return {'ok': True, 'task_id': tid, 'dispatch_id': dispatch_id, 'state': d['state'], 'run_id': d['run_id'],
                'provider': run['provider'] if run else json.loads(d['plan'])[0]['provider'],
                'session': run['session'] if run else json.loads(d['plan'])[0].get('session'),
                'current_session': {'provider': row['current_provider'], 'session': row['current_session']},
                'parent': parent, 'log_dir': run['log_dir'] if run else None,
                'next': f'The run is headless; progress and failures arrive as {self.rt.DEPLOYMENT.owner.assistant_name} cards. '
                        'Use send-to-task to add input.'}

    def smoke_dispatch_workspace(self, provider, now=None):
        now = time.time() if now is None else now
        stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))
        path = self.rt.registry.smoke_root() / f'{self.rt.registry.SMOKE_PREFIX}{stamp}-{provider}-{uuid.uuid4().hex[:4]}'
        path.mkdir(parents=True, mode=0o700)
        (path / 'history' / 'notes').mkdir(parents=True)
        (path / 'goal.md').write_text(f'# TEST smoke-dispatch ({provider})\n\nThrowaway workspace for a dispatcher '
                                      'end-to-end test. Nothing here is real work; never touch anything outside this '
                                      'directory and never send Slack messages.\n', encoding='utf-8')
        (path / 'status.md').write_text(f'<!-- rev: 1 · last-writer: smoke-dispatch · at: {self.rt.task_memory.utc_iso(now)} -->\n# Status\n\n- TEST workspace created by smoke-dispatch.\n', encoding='utf-8')
        for name in ('AGENTS.md', 'CLAUDE.md'):
            (path / name).write_text('# TEST workspace\n\nRead status.md. Only do what the dispatched event asks. '
                                     'Never send Slack messages. Never touch files outside this directory.\n',
                                     encoding='utf-8')
        return path

    def smoke_dispatch(self, db, db_path, provider, scenario='revive', wait=True, timeout=900.0, poll=3.0):
        """End-to-end helper after install: TEST task under /tmp, dead current session, synthetic event.

        revive:   launch-task starts the first session (headless) and it exits -> it is dead; a
                  synthetic agent message then must be consumed by `--resume` of that session.
        fallback: the current session is a never-existing UUID of `provider`, so resume fails with
                  session_missing and the other provider must take over in a FRESH session.
        Requires the installed supervisor (it does the revival); posts 🧪 TEST cards only.
        """
        if provider not in self.rt.claude.PROVIDERS or scenario not in ('revive', 'fallback'):
            raise ValueError('provider_codex_or_claude_and_scenario_revive_or_fallback')
        path = self.smoke_dispatch_workspace(provider)
        tid = self.rt.registry.task_id_for_cwd(str(path))
        report = {'ok': True, 'test': True, 'task_id': tid, 'workspace': str(path), 'scenario': scenario,
                  'provider': provider}
        self.rt.start(db_path)
        end = time.time() + timeout
        if scenario == 'revive':
            launched = self.rt.launch_task(db, db_path, str(path), provider, self.smoke_step1, wait_seconds=120)
            report['launch'] = {k: launched[k] for k in ('dispatch_id', 'run_id', 'session', 'state')}
            while wait and time.time() < end:
                d = db.execute('SELECT state FROM dispatches WHERE dispatch_id=?', (launched['dispatch_id'],)).fetchone()
                if d and d['state'] not in self.rt.dispatch.ACTIVE:
                    break
                time.sleep(poll)
        else:
            with self.rt.transaction(db):
                self.rt.registry.ensure_task(db, tid, time.time())
                self.rt.registry.set_current(db, tid, provider, str(uuid.uuid4()), 'smoke-dispatch', 'fallback scenario: missing session')
        sent = self.send_to_task(db, db_path, tid, self.smoke_step2, from_task='smoke-dispatch-driver')
        report['event'] = sent['event']
        report['dead_current'] = sent.get('to')
        if not wait or not sent['event']:
            return {**report, 'next': f'slack_followup.py dispatch-status --task {tid}'}
        while time.time() < end:
            e = db.execute('SELECT status FROM events WHERE id=?', (sent['event'],)).fetchone()
            busy = db.execute("SELECT count(*) FROM dispatches WHERE task_key=? AND state IN ('waiting','running')",
                              (tid,)).fetchone()[0]
            if e and e['status'] == 'handled' and not busy:
                break
            time.sleep(poll)
        e = db.execute('SELECT status FROM events WHERE id=?', (sent['event'],)).fetchone()
        row = self.rt._task_row(db, tid)
        status_text = (path / 'status.md').read_text(encoding='utf-8')
        report.update(event_status=e['status'] if e else None,
                      status_md_line=('- smoke-dispatch ok' in status_text), status_rev=self.rt.task_memory.check(path)['rev'],
                      current_session={'provider': row['current_provider'], 'session': row['current_session']},
                      runs=[{k: r[k] for k in ('run_id', 'provider', 'mode', 'trigger', 'outcome', 'error_class', 'exit_code')}
                            for r in db.execute('SELECT * FROM consumer_runs WHERE task=? ORDER BY started_at', (tid,))],
                      routes=[dict(r) for r in db.execute('SELECT decision,reason,to_provider,to_session FROM dispatch_routes '
                                                          'WHERE task_key=? ORDER BY id', (tid,))])
        report['ok'] = report['event_status'] == 'handled'
        return report
