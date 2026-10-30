"""Card gate: the fields every agent card to the owner carries, checked before any row or channel call, and rendered by
code above the agent's own text: the decision (first line), the situation, what was checked, then a PR's four parts or
an infrastructure apply's plan scope.

Each check runs only while its registry rule is active (`rt.rule_registry`: active / refuse); a refusal says what is
wrong, how to fix it and which rule. Every field also passes the runtime's copy lint (`rt.buttons.check_copy`).
Code-built cards pass no gate.

The words come from the owner's language (kage.locale: decision, filler, waiting and time words, failure and apply
words, line labels) and from the owner profile (`key`, the owner's word in identifiers and CLI values, e.g.
`--waiting-on <key>`; `short_name`, how messages name the owner). The gate dict uses neutral keys (`why_owner`).
"""
import re
import time

from kage import locale

WHY_OWNER = ('scope', 'spend', 'give_up')
FAILURE_TYPES = ('auth_missing', 'auth_expired', 'permission')  # blocker types that are a failure to diagnose
DECIDE_MAX, LINE_MAX = 80, 300
PR_KEYS = ('pr_cause', 'pr_why', 'pr_change', 'pr_verify')
PLAN_KEYS = ('plan_pr', 'plan_root', 'plan_resources', 'plan_counts')
FIELDS = ('decide', 'waiting', 'waiting_on', 'why_owner', 'checked', *PR_KEYS, *PLAN_KEYS)
PR_RE = re.compile(r'https?://github\.com/[\w.-]+/[\w.-]+/pull/\d+')
CODE_FILE_RE = re.compile(r'[\w./-]+\.(?:py|tsx?|jsx?|go|tf|hcl|ya?ml|json|sql|rb|java|kt|swift|sh|css|html)\b')
COUNTS_RE = re.compile(r'\s*(\d{1,4})\s*/\s*(\d{1,4})\s*/\s*(\d{1,4})\s*/\s*(\d{1,4})\s*')


def _one(value, key):
    text = ' '.join(str(value or '').split())
    cap = DECIDE_MAX if key == 'decide' else LINE_MAX
    if len(text) > cap:
        raise ValueError(f'{key}_must_be_at_most_{cap}_characters')
    return text


def compose(shown, text):
    """The card text: decision, rendered lines, then the agent's own words (without a decision line, its rule off: the
    agent's words lead)."""
    text = str(text or '').strip()
    return '\n'.join(x for x in ((shown['decide'], *shown['lines'], text) if shown['decide'] else
                                  (text, *shown['lines'])) if x)


class CardGate:
    def __init__(self, *, language, owner_key, owner_name):
        self.language, self.key, self.name = language, owner_key, owner_name
        pat = lambda k: locale.pattern(language, k)  # noqa: E731
        self.waiting_on = (owner_key, 'person', 'system')
        self.decision_re = re.compile(pat('card.decision'))
        self.filler_re = re.compile(pat('card.filler'))
        self.wait_re = re.compile(pat('card.wait'))
        self.since_re = re.compile(pat('card.since'))
        self.owner_named_re = re.compile('(?i)' + pat('card.addressed_to_owner') + '|' + re.escape(owner_key))
        self.failure_re = re.compile(pat('card.failure'))
        self.infra_re = re.compile(pat('card.infra_apply'))
        self.pr_parts = tuple((k, self._msg(f'card.pr.{k}')) for k in PR_KEYS)
        self.plan = tuple((k, self._msg(f'card.plan.{k}')) for k in PLAN_KEYS)
        self.apply_line = self._msg('card.apply_line')

    def _msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def check(self, rt, gate, *, text='', failure=False):
        """{'decide', 'lines'}: the card's first line and the lines code renders under it. `text` is the rest of the
        card (question, operation, about) the detectors read; `failure` marks a blocker failure type. Raises the first
        refusal."""
        reg = rt.rule_registry
        g = {k: _one(gate.get(k), k) for k in FIELDS if k not in ('checked', 'waiting_on', 'why_owner')}
        checked = list(dict.fromkeys(_one(c, 'checked') for c in gate.get('checked') or () if str(c).strip()))
        decide, waiting, on = g['decide'], g['waiting'], gate.get('waiting_on')
        seen = ' '.join([str(text or ''), *g.values(), *checked])
        key, name = self.key, self.name
        if reg.active('card.decision_line'):
            if not decide:
                reg.refuse('card.decision_line', 'card_needs_a_decision_line: no --decide (rule-check: --summary)')
            if not self.decision_re.search(decide) or len(self.filler_re.sub('', decide)) < 4:
                reg.refuse('card.decision_line', f'decision_line_is_vague: "{decide}" does not say what {name} must '
                                                 f'decide')
        if reg.active('card.situation_line'):
            if not waiting or on not in self.waiting_on:
                reg.refuse('card.situation_line', 'card_needs_the_situation: --waiting and --waiting-on are required')
            if not (self.wait_re.search(waiting) and self.since_re.search(waiting)):
                reg.refuse('card.situation_line', 'situation_line_is_incomplete: --waiting must say who waits on whom, '
                                                  'for what, and since when (a time or date)')
            if on == key and not self.owner_named_re.search(waiting):
                reg.refuse('card.situation_line', f'situation_does_not_name_{key}: --waiting-on {key}, but --waiting '
                                                  f'does not say that {name} ({self._msg("you")}) is the one waited on')
        if on == 'person' and gate.get('why_owner') not in WHY_OWNER and reg.active('card.not_waiting_on_person'):
            reg.refuse('card.not_waiting_on_person', f'waiting_on_a_person_is_not_a_card_to_{key}: --waiting-on person '
                                                     f'without --why-{key} scope|spend|give_up')
        need = 2 if failure or self.failure_re.search(seen) else 1
        if len(checked) < need and reg.active('card.checked_first'):
            reg.refuse('card.checked_first', f'card_needs_what_you_checked: {len(checked)} --checked, {need} needed' +
                       (' (a failure card: two independent checks)' if need == 2 else ''))
        if (PR_RE.search(seen) or any(g[k] for k in PR_KEYS)) and reg.active('card.pr_four_parts'):
            missing = [f'--{k.replace("_", "-")} ({label})' for k, label in self.pr_parts if not g[k]]
            if missing:
                reg.refuse('card.pr_four_parts', 'pr_card_needs_four_parts: missing ' + ', '.join(missing))
            if CODE_FILE_RE.search(g['pr_change']):
                reg.refuse('card.pr_four_parts', 'pr_change_names_files: --pr-change says what changes in behaviour, '
                                                 'not which files')
            if not PR_RE.search(seen):
                reg.refuse('card.pr_four_parts', 'pr_card_needs_the_pr_link: put the GitHub PR link in the card or '
                                                 '--about')
        counts = COUNTS_RE.fullmatch(g['plan_counts'])
        if (self.infra_re.search(seen) or any(g[k] for k in PLAN_KEYS)) and reg.active('card.infra_plan_scope'):
            missing = [f'--{k.replace("_", "-")}' for k in PLAN_KEYS if not g[k]]
            if missing or not counts:
                reg.refuse('card.infra_plan_scope', 'infra_card_needs_the_plan_scope: ' + (
                    'missing ' + ', '.join(missing) if missing else '--plan-counts must be C/U/D/R, e.g. 1/2/0/0'))
        for field, value in (('decide', decide), *((k, v) for k, v in g.items() if k != 'decide' and v),
                             *(('checked', c) for c in checked)):
            if value:
                rt.buttons.check_copy(value, field, first_line_max=None)
        lines = [self._msg('card.line.situation', text=waiting)] * bool(waiting)
        lines += [self._msg('card.line.checked', text=self._msg('card.list_separator').join(checked))] * bool(checked)
        lines += [self._msg('card.line.field', label=label, text=g[k]) for k, label in self.pr_parts if g[k]]
        if any(g[k] for k in PLAN_KEYS):
            c = counts.groups() if counts else ('?',) * 4
            lines += [self._msg('card.line.plan', pr=g['plan_pr'], root=g['plan_root']),
                      self._msg('card.line.resources', resources=g['plan_resources']),
                      self._msg('card.line.create_update', create=c[0], update=c[1]),
                      self._msg('card.line.delete_replace', delete=c[2], replace=c[3]), self.apply_line]
        return {'decide': decide, 'lines': lines}

    def open_limit(self, rt, db, task_id, replaced=None, now=None):
        """No new agent card while the task already has cards.max_open_per_task owner cards (its own cards, blockers,
        rule checks: the ones it can close) younger than tasks.stale_card_days waiting for the owner: undecided, buttons
        live, watch not stopped. The card a continuation takes over (`replaced`) does not count, nor one a newer card
        already continues. The intake tasks (worker_config.intake_role) are exempt: each carries many unrelated
        requests."""
        if not task_id or not rt.rule_registry.active('card.open_limit'):
            return
        try:
            config = rt.worker_config.load()[0]
        except Exception:  # noqa: BLE001 - a malformed config: the default intake task ids
            config = rt.worker_config.DEFAULTS
        if rt.worker_config.intake_role(config, task_id):
            return
        cap = rt.card_replies.config(rt).get('max_open_per_task', 3)
        days = rt.task_lifecycle.config(rt)['stale_card_days'] or rt.card_replies.OPEN_CARD_DAYS  # 0 = lapsing off
        since = (time.time() if now is None else now) - days * 86400
        rows = db.execute("SELECT x.event_id FROM escalations x JOIN escalation_cards c ON c.event_id=x.event_id JOIN "
                          "events e ON e.id=x.event_id AND e.disposition=? WHERE c.task_id=? AND c.dry=0 AND "
                          "c.smoke=0 AND c.created>? AND x.stage='watching' AND NOT EXISTS (SELECT 1 FROM decisions d "
                          "WHERE d.event_id=x.event_id) AND "
                          "NOT EXISTS (SELECT 1 FROM watches w WHERE w.id=x.watch_id AND json_extract(w.data,'$.state')="
                          "'stopped') AND NOT EXISTS (SELECT 1 FROM card_replies r WHERE r.event_id=x.event_id AND "
                          "r.stage='retired')", (f'ask_{self.key}', task_id, since)).fetchall()
        waiting = [r[0] for r in rows if r[0] != replaced and not rt.card_replies.continued(rt, db, r[0])]
        if len(waiting) >= cap:
            rt.rule_registry.refuse('card.open_limit', f'too_many_open_cards: task {task_id} already has '
                                                       f'{len(waiting)} cards from the last {days} days waiting for '
                                                       f'{self.name} (cards.max_open_per_task={cap})')
