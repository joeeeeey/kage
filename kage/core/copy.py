"""The copy standard for text the owner reads: no internal ids or jargon, a short first line, no pointer to another card
without its link, and an ask that starts with what the owner has to do.

Agent-written text is refused before anything is written (`check`, with the standard in the error so the agent can
rewrite it); relayed text the code cannot send back is scrubbed instead (`scrub`). The id shapes are the engine's own
record formats; the card-reference pattern and the ask verbs come from the owner's catalog (`copy.*`). The agent-facing
wording (the standards and two problem texts) is the deployment's to choose; the defaults below name the owner.
"""
import re

from kage import locale

FIRST_LINE_MAX = 120
ID_PATTERNS = (
    (re.compile(r'(?<![A-Za-z0-9])AR-\d+'), 'a rule id (AR-…)'),
    (re.compile(r'(?<![A-Za-z0-9])Q-\d{2}(?!\d)'), 'an open-question id (Q-…)'),
    (re.compile(r'(?<![A-Za-z0-9])(?:rc|rp|rq|gp|bk)[0-9a-f]{14}(?![0-9a-f])'), 'an internal record id (rc… / rp… / bk…)'),
    (re.compile(r'(?i)never[-_ ]?auto'), '"never-auto"'),
    (re.compile(r'(?<![0-9A-Za-z])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9A-Za-z])', re.I),
     'a session / uuid'),
    (re.compile(r'(?<![0-9A-Za-z])[0-9a-f]{16,}(?![0-9A-Za-z])', re.I), 'a hash or event id (16+ hex characters)'),
)
LINK_RE = re.compile(r'https?://')
TEXTS = {
    'card_ref': ("points {owner} to another card without its link: {found} (say here what they have to decide, or put "
                 "that card's link on the same line)"),
    'ask_verb': 'the ask must start with what {owner} has to do: ',
    'standard': ('Write the first line as ONE plain sentence: what the agent wants to do, to whom or what, how far (names, '
                 'not ids); at most two short lines after it (why {owner} has to decide; whether it can be undone). Rule '
                 '/ check / record ids, hashes, session ids, "never-auto" and field labels stay on the dashboard or in '
                 'task files.'),
    'ask_standard': ('The ask is the first line {owner} reads: verb + object + link + how to answer. Never send them to '
                     'another card without its link on the same line; the task\'s own note stays on the dashboard.'),
}


class CopyStandard:
    def __init__(self, language, *, owner_name, owner_key, active=lambda: True, texts=None):
        """`active`: whether the standard is enforced right now (a rule switch); `texts`: the deployment's wording of
        TEXTS (same keys; `{owner}` / `{found}` placeholders as there)."""
        self.ask_verbs = locale.words(language, 'copy.ask_verbs')
        self.card_ref_re = re.compile(locale.pattern(language, 'copy.card_ref'))
        self.owner, self.key, self.active = owner_name, owner_key, active
        self.texts = {**TEXTS, **(texts or {})}

    def text(self, name, **values):
        return self.texts[name].format(owner=self.owner, **values)

    def problems(self, text, first_line_max=FIRST_LINE_MAX, refs=False):
        text = str(text or '')
        problems = []
        for pattern, what in ID_PATTERNS:
            found = pattern.search(text)
            if found:
                problems.append(f'contains {what}: {found.group(0)[:24]}')
        for line in text.split('\n') if refs else ():
            found = self.card_ref_re.search(line)
            if found and not LINK_RE.search(line):
                problems.append(self.text('card_ref', found=found.group(0)))
                break
        first = text.strip().split('\n', 1)[0].strip()
        if first_line_max and len(first) > first_line_max:
            problems.append(f'first line is {len(first)} characters (max {first_line_max})')
        return problems

    def check(self, text, field='question', first_line_max=FIRST_LINE_MAX, refs=True, ask=False):
        """Refuse owner-facing text that breaks the standard (agent-written; checked before any write). `refs` refuses a
        pointer to another card without its link; `ask` must start with one of the ask verbs. Off while not active."""
        if not self.active():
            return text
        problems = self.problems(text, first_line_max, refs=refs)
        if ask and not str(text or '').strip().startswith(self.ask_verbs):
            problems.append(self.text('ask_verb') + ' / '.join(self.ask_verbs))
        if problems:
            raise ValueError(f'{self.key}_facing_{field}_breaks_the_card_copy_standard: ' + '; '.join(problems) + '. '
                             + self.text('ask_standard' if ask else 'standard'))
        return text

    @staticmethod
    def scrub(text, first_line_max=FIRST_LINE_MAX):
        """Relayed text the code cannot send back: ids and jargon dropped, the first line shortened. Never used for
        agent input (that is refused with `check`)."""
        text = str(text or '')
        for pattern, _ in ID_PATTERNS:
            text = pattern.sub('…', text)
        lines = text.strip().split('\n')
        if first_line_max and len(lines[0]) > first_line_max:
            lines[0] = lines[0][:first_line_max - 1] + '…'
        return '\n'.join(lines)
