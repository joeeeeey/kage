"""The files a generated deployment's dispatched runs read (O1c5b), written by `kage init` next to its program.

The assembled units find them from the deployment root (a unit's `__file__` is `<root>/scripts/<unit>.py`):

  SKILL.md                               the event semantics every dispatched prompt cites (`<root>/SKILL.md`)
  references/rules.md                    the rule-check plan file (`rule-check --plan FILE`)
  references/rules-example.json          the access spec the rule engine falls back to before the owner installs
                                         rules: one domain rule, every access change asks the owner
  references/rule-registry-seed/README.md   the registry's seed directory: no rule until the owner approves one
  scripts/dispatch_runner.py             the detached wrapper of one dispatched run: verifies the release kage.lock
                                         pins, then runs kage.core.runner from it

Neutral: the owner's name, key and verified Slack user, the program path and the Kage version are the only values
(the templates are kage/onboard/templates/*.md); no organization word, no person, no token.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from kage.policy.access import SHAPES  # the spec names every never-auto shape

TEMPLATES = Path(__file__).with_name('templates')
RUNNER = 'scripts/dispatch_runner.py'
SKILL = 'SKILL.md'
RULES = 'references/rules.md'
EXAMPLE_SPEC = 'references/rules-example.json'
SEED_README = 'references/rule-registry-seed/README.md'
EXECUTABLE = (RUNNER,)
MARKER_RE = re.compile(r'@[a-z_]+@')

RUNNER_TEXT = '''#!/usr/bin/env python3
"""This deployment's dispatch runner, written by `kage init`: the detached wrapper of ONE dispatched agent run.

The supervisor starts it as `python3 -I scripts/dispatch_runner.py --run-dir DIR -- COMMAND...` (kage.core.runner).
The release kage.lock pins ($KAGE_VERSIONS_DIR, else ~/.local/share/kage/versions) must verify against the lock (its
BUNDLE.json, MANIFEST.sha256 and every file) before any of its code but the checker runs: exit 78 when it does not.
The readable log renders through the deployment's run_log unit when the assembly can make it; rendering is optional,
the run is not."""
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def _release():
    pin = json.loads((ROOT / 'kage.lock').read_text(encoding='utf-8'))['kage']
    versions = Path(os.environ.get('KAGE_VERSIONS_DIR') or Path.home() / '.local/share/kage/versions')
    release = versions / f"{pin['version']}-{pin['commit'][:12]}"
    spec = importlib.util.spec_from_file_location('kage_release_check', release / 'kage' / 'service.py')
    try:
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        problems = check.release_problems(release, pin)
    except (OSError, AttributeError) as exc:
        problems = [f'release_unreadable: {type(exc).__name__}']
    return release, problems


def _run_log():
    try:
        from kage import app
        return app.App(ROOT / 'deploy').leaf('run_log')
    except Exception:  # noqa: BLE001 - rendering is optional; the run itself is not
        return None


def main():
    release, problems = _release()
    if problems:
        print(json.dumps({'error': 'KageRefused', 'detail': f'release {release.name}: ' + ', '.join(problems[:5])}),
              file=sys.stderr)
        return 78
    sys.path.insert(0, str(release))
    from kage.core import runner
    return runner.main(sys.argv[1:], load_run_log=_run_log)


if __name__ == '__main__':
    sys.exit(main())
'''

SEED_TEXT = '''# Rule registry seed

Written by `kage init`. The registry reads `R-NNNN.json` rules from the live registry directory, else from here. A new
deployment starts with none: a rule appears only after its owner approves a proposal (`rule-propose`).
'''


def _render(name, values):
    text = (TEMPLATES / name).read_text(encoding='utf-8')
    for key, value in values.items():
        text = text.replace(f'@{key}@', value)
    left = MARKER_RE.findall(text)
    if left:
        raise ValueError(f'template_marker_left: {name}: {sorted(set(left))}')
    return text.encode('utf-8')


def example_spec(key, owner_user):
    """The neutral access spec: one domain rule that matches every access plan and asks the owner; every never-auto
    shape asks the owner too. Valid for kage.policy.access with the neutral access pack (`kage/<key>.rules/v1`)."""
    return {
        'schema': f'kage/{key}.rules/v1', 'category': 'access', 'version': 1,
        'title_zh': 'Every access change asks the owner',
        'defaults': {'company_domains': ['example.com'], 'owner': owner_user, 'approvers': [owner_user], 'hr': [],
                     'spend': {'recurring_usd_month': 0, 'one_time_usd': 0}, 'temporary_days': 30},
        'never_auto': {shape: {'level': key} for shape in SHAPES},
        'role_catalog': {},
        'rules': [{'id': 'R-01', 'name': 'owner_decides_access', 'title_zh': 'The owner decides every access change',
                   'kind': 'domain', 'level': key, 'trigger': {'category': 'access'}, 'checks': [],
                   'fallback': key}],
    }


def files(*, key, name, owner_user, version, program):
    """{relative path: bytes} of the dispatch files for this owner."""
    values = {'key': key, 'name': name, 'version': version, 'program': program}
    spec = json.dumps(example_spec(key, owner_user), indent=2, ensure_ascii=False) + '\n'
    return {SKILL: _render('SKILL.md', values), RULES: _render('rules.md', values),
            EXAMPLE_SPEC: spec.encode('utf-8'), SEED_README: SEED_TEXT.encode('utf-8'),
            RUNNER: RUNNER_TEXT.encode('utf-8')}
