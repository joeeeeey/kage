"""Slack side of decision cards: Slack markup in labels and replies (`<@U…>`, `<!here>`, `<https://…|label>`), and
the escaped one-line excerpt of a reply that a card shows."""
import re

from kage.core import decisions as core

MENTION_RE = re.compile(r'<[@!#][^>]*>')
SLACK_TOKEN_RE = re.compile(r'<([^<>|]*)(?:\|([^<>]*))?>')


def escape(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def clean_label(label):
    # Button text is plain_text; labels are also echoed in mrkdwn, so strip Slack mention / link tokens here and escape
    # &<> wherever they are rendered.
    label = ' '.join(MENTION_RE.sub('', str(label)).split())
    if any(ord(c) < 32 or ord(c) == 127 for c in label):
        raise ValueError('option_label_must_be_plain_single_line_text')
    if not label or len(label) > core.MAX_LABEL:
        raise ValueError('option_label_must_be_1_to_30_characters')
    return label


def excerpt(text, limit=40):
    """First `limit` characters of a Slack message for a card line: markup reduced to its label, one line, escaped."""
    plain = SLACK_TOKEN_RE.sub(lambda m: m.group(2) or ('@…' if (m.group(1) or '').startswith('@') else '…'),
                               str(text or ''))
    # Slack sends &, <, > escaped; undo that before cutting, escape once at the end.
    plain = ' '.join(plain.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&').split())
    return escape(plain if len(plain) <= limit else plain[:limit] + '…')


class SlackReplies:
    """A ReplyReader fed with Slack text: mentions removed before reading a decision, links and mentions reduced to
    their label before classifying (a URL's `?` is not a question)."""

    def __init__(self, reader):
        self.reader = reader

    def parse_reply(self, text, model, strict=False):
        return self.reader.parse_reply(MENTION_RE.sub(' ', str(text or '')), model, strict)

    def reply_class(self, text):
        return self.reader.reply_class(SLACK_TOKEN_RE.sub(lambda m: m.group(2) or ' ', str(text or '')))
