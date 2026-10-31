"""Decision cards, channel-neutral core: the options of a card and reading the owner's typed reply into one of them.

Options: `ID=LABEL[:KIND]` specs (kinds approve / reject / other), 2-4 of them, or the default approve / reject pair
(stored as []). Every stored format reads back as a model of options, each with its `choice` (the button's action).

Typed replies are read by code, never by a model: a reply counts only when it STARTS with an option label or one of the
language's approve / reject phrases (longest first, so a negated phrase never reads as its positive), followed by the
end or a separator, and maps to exactly one option. A question, a condition right after the phrase, a contradiction, or
(on a verbatim card) any extra text leaves it undecided. The phrases and patterns come from the owner's catalog
(kage.locale `decision.*`); the channel adapter removes its markup before and passes the plain text in.
"""
import json
import re

from kage import locale

KINDS = ('approve', 'reject', 'other')
KIND_ICON = {'approve': '✅', 'reject': '❌', 'other': '🔹'}
MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_LABEL = 30
MAX_NOTE = 300
OPTION_ID_RE = re.compile(r'[a-z0-9_-]{1,24}')


def parse_option(spec):
    """`ID=LABEL[:KIND]` -> (id, label, kind); KIND defaults to other."""
    spec = str(spec)
    if '=' not in spec:
        raise ValueError('option_must_be_ID=LABEL[:KIND]')
    option_id, rest = spec.split('=', 1)
    head, sep, tail = rest.rpartition(':')
    kind = 'other'
    if sep and tail.strip() in KINDS:
        rest, kind = head, tail.strip()
    return option_id.strip(), rest, kind


def validate_options(specs, recommend, clean_label):
    """No specs: the default pair (stored as []). Otherwise 2-4 options replace it. `clean_label` is the channel's."""
    specs = list(specs or [])
    if not specs:
        if recommend is not None:
            raise ValueError('recommend_requires_scenario_options')
        return []
    if not MIN_OPTIONS <= len(specs) <= MAX_OPTIONS:
        raise ValueError('scenario_options_must_be_2_to_4')
    options = []
    for spec in specs:
        option_id, label, kind = parse_option(spec)
        if not OPTION_ID_RE.fullmatch(option_id):
            raise ValueError('option_id_must_match_[a-z0-9_-]{1,24}')
        options.append({'id': option_id, 'label': clean_label(label), 'kind': kind, 'recommended': False})
    if len({o['id'] for o in options}) != len(options):
        raise ValueError('option_ids_must_be_unique')
    if len({o['label'] for o in options}) != len(options):
        raise ValueError('option_labels_must_be_unique')
    if recommend is not None:
        match = [o for o in options if o['id'] == recommend]
        if not match:
            raise ValueError('recommend_must_match_an_option_id')
        match[0]['recommended'] = True
    return options


def default_pair(language):
    return ({'id': 'approve', 'label': locale.message(language, 'decision.approve_label'), 'kind': 'approve',
             'recommended': False, 'choice': 'approve'},
            {'id': 'reject', 'label': locale.message(language, 'decision.reject_label'), 'kind': 'reject',
             'recommended': False, 'choice': 'reject'})


def card_model(stored, pair):
    """Buttons on a card, each with its `choice` (action suffix). Reads every stored format; `pair` = default_pair()."""
    items = json.loads(stored) if isinstance(stored, str) else list(stored or [])
    if items and isinstance(items[0], dict):
        return [{'id': o['id'], 'label': o['label'], 'kind': o['kind'], 'recommended': bool(o.get('recommended')),
                 'choice': f'opt:{o["id"]}'} for o in items]
    # The default pair; older cards may add unlabeled-kind extra buttons opt:1..3.
    return [dict(o) for o in pair] + [
        {'id': str(n), 'label': label, 'kind': 'other', 'recommended': False, 'choice': f'opt:{n}'}
        for n, label in enumerate(items, 1)]


def resolve_choice(choice, model):
    for option in model:
        if option['choice'] == choice:
            return option
    raise ValueError('invalid_choice')


def normalize_choice(choice, model):
    """Owner input: an option id, `opt:<id>`, or approve / reject on default cards."""
    for option in model:
        if choice in (option['choice'], option['id']):
            return option['choice']
    raise ValueError('invalid_choice')


def public_option(option):
    return {k: option[k] for k in ('id', 'label', 'kind', 'recommended')}


def validate_note(note):
    if note is None:
        return None
    note = ' '.join(str(note).split())
    if not note:
        return None
    if len(note) > MAX_NOTE:
        raise ValueError('note_must_be_at_most_300_characters')
    return note


class ReplyReader:
    """The owner's typed reply -> a decision, with the phrases and patterns of the owner's language."""

    def __init__(self, language):
        words = lambda k: locale.words(language, k)  # noqa: E731
        pattern = lambda k: re.compile(locale.pattern(language, k))  # noqa: E731
        self.approve_phrases = words('decision.approve_phrases')
        self.reject_phrases = words('decision.reject_phrases')
        self.recommended_phrases = words('decision.recommended_phrases')
        self.separators = locale.message(language, 'decision.separators')
        self.condition_re, self.question_re = pattern('decision.condition'), pattern('decision.question')
        self.plan_change_re, self.asking_re = pattern('decision.plan_change'), pattern('decision.asking')

    def phrase_at_start(self, text, phrases):
        low = text.lower()
        for phrase in sorted(phrases, key=len, reverse=True):
            if low.startswith(phrase.lower()):
                rest = text[len(phrase):]
                if not rest or rest[0] in self.separators:
                    return phrase, rest
        return None, None

    def reply_class(self, plain):
        """'plan_change' or 'kept' for a reply that is not a clear decision (`plain`: markup already reduced)."""
        return 'plan_change' if self.plan_change_re.search(plain) and not self.asking_re.search(plain) else 'kept'

    def parse_reply(self, body, model, strict=False):
        """`body`: the reply with mentions removed. -> {'option', 'phrase', 'via', 'guidance', 'reason'}; `option` is
        None unless unambiguous. `strict`: the approve option executes verbatim, so any extra text is ambiguous. Text
        after the phrase is guidance for the session, never a condition."""
        body = body.strip().lstrip(self.separators).strip()
        out = {'option': None, 'phrase': None, 'via': None, 'guidance': False, 'reason': 'no_keyword'}
        if not body:
            return out
        labels = sorted((o for o in model if len(o['label']) >= 2), key=lambda o: len(o['label']), reverse=True)
        option = phrase = rest = via = None
        for o in labels:
            found, tail = self.phrase_at_start(body, (o['label'],))
            if found:
                option, phrase, rest, via = o, found, tail, 'label'
                break
        if option is None:
            approve, a_rest = self.phrase_at_start(body, self.approve_phrases)
            reject, r_rest = self.phrase_at_start(body, self.reject_phrases)
            if not approve and not reject:
                return out
            kind = 'approve' if approve and (not reject or len(approve) > len(reject)) else 'reject'
            phrase, rest, via = (approve, a_rest, 'approve') if kind == 'approve' else (reject, r_rest, 'reject')
            recommended = next((o for o in model if o['recommended'] and o['kind'] != 'reject'), None)
            same_kind = [o for o in model if o['kind'] == kind]
            if kind == 'reject':
                option = same_kind[0] if len(same_kind) == 1 else None
            elif phrase in self.recommended_phrases and recommended:
                option = recommended
            elif len(same_kind) == 1:
                # approve = the approve option, unless the card recommends a different one (then it is not clear which)
                option = same_kind[0] if recommended in (None, same_kind[0]) else None
                if option is None:
                    return {**out, 'phrase': phrase, 'via': via, 'reason': 'approve_or_recommended'}
            else:  # no approve-kind option, or several: only the recommended one is clear
                option = recommended if recommended and (not same_kind or recommended in same_kind) else None
            if option is None:
                return {**out, 'phrase': phrase, 'via': via, 'reason': f'no_single_{kind}_option'}
        rest = (rest or '').strip().lstrip(self.separators).strip()
        first_sentence = re.split(r'[。.!！\n]', body, maxsplit=1)[0]
        result = {**out, 'phrase': phrase, 'via': via, 'guidance': bool(rest)}
        if self.question_re.search(first_sentence):
            return {**result, 'reason': 'question'}
        if self.condition_re.match(rest):
            return {**result, 'reason': 'conditional'}
        opposite = self.reject_phrases if option['kind'] == 'approve' else self.approve_phrases
        clause = re.split(r'[，,。.！!;；\n]', rest, maxsplit=1)[0].strip().lower()
        if clause and clause in {p.lower() for p in opposite}:
            return {**result, 'reason': 'contradiction'}
        if strict and rest and option['kind'] == 'approve':
            return {**result, 'reason': 'verbatim_card_extra_text'}
        return {**result, 'option': option, 'reason': 'decided'}
