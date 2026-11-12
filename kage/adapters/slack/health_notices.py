"""Slack side of provider-health notices: a top-level bot message in the owner's private decision channel, mentioning
the owner, with a context line linking the dashboard's health page; the destination is checked private first and the
send is ledgered (it is the runtime's own message, never ingested as work). 0.78.6: the system status monitor posts its
notices the same way (provider `system`, its own context line)."""
from __future__ import annotations
from kage import locale


class HealthNotices:
    def __init__(self, language, *, owner_user):
        self.language, self.owner_user = language, owner_user

    def post(self, rt, db, provider, kind, text, context_key='health.notice_context', page='/health'):
        """A top-level bot message to the owner (private decision channel only); ledgered so the poller never ingests it."""
        rt.asks.check_secret_free(text)
        config = rt.buttons.load_config()
        channel = config['decision_channel']
        # Config membership alone passed a public decision_channel; Slack must report it private (cached). A
        # refusal lands in notice_error and the notice is retried on the next pass.
        rt.buttons.require_private_destination(rt, db, channel, 'provider_health_notice', config=config)
        body = f'<@{self.owner_user}> {rt.buttons.escape(text)}'
        sent = rt.slack.api_call('chat.postMessage', rt.api_identity(bot=True), json_body={
            'channel': channel, 'text': body, 'unfurl_links': False, 'unfurl_media': False,
            'blocks': [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': body}},
                       {'type': 'context', 'elements': [{'type': 'mrkdwn',
                                                         'text': locale.message(self.language, context_key,
                                                                                link=f'<{rt.DASHBOARD_URL}{page}>')}]}],
            'metadata': rt.agent_metadata('provider_health_notice', None, provider=provider, notice=kind)})
        rt.record_send(db, sent.get('channel') or channel, sent.get('ts'), 'provider_health_notice', provider)
        return sent.get('ts')
