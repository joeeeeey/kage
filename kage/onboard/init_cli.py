"""`python3 -m kage init …` (O1c4): runs before any assembly, so it works where no deployment exists yet.

  init                          the wizard (a terminal only), then a new deployment
  init --answers FILE [--apply] generate from an answers file; on an existing deployment show what changes, and
                                write it only with --apply
  init --adopt DIR              record that the existing deployment DIR is onboarded; nothing else changes
  init --manifest [--as-owner] [--name NAME]   print the Slack app manifest and the steps

Options: --target DIR (default ~/.local/share/kage/deploy-<key>), --release DIR (a verified release of this Kage when
it does not run from one), --json. Exit 0: done or nothing to change; 1: refused ({"error", "detail"} on stderr);
2: usage.
"""
from __future__ import annotations

import argparse
import json
import sys

from kage.onboard import answers as _answers
from kage.onboard import init, manifest, wizard
from kage.ports import secrets


def parser():
    p = argparse.ArgumentParser(prog='kage init', description='Create or adopt a Kage deployment.')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--answers', metavar='FILE', help='generate from an answers file (kage.answers/v1)')
    mode.add_argument('--adopt', metavar='DIR', help="record an existing deployment (its deploy/ dir) as onboarded")
    mode.add_argument('--manifest', action='store_true', help='print the Slack app manifest and the steps')
    p.add_argument('--apply', action='store_true', help='write the changes to an existing deployment')
    p.add_argument('--target', metavar='DIR', help='the deployment directory (default ~/.local/share/kage/deploy-<key>)')
    p.add_argument('--release', metavar='DIR', help='a verified release of this Kage to pin')
    p.add_argument('--as-owner', action='store_true', help='with --manifest: the variant with the user token')
    p.add_argument('--name', default='kage', help="with --manifest: the app's display name")
    p.add_argument('--json', action='store_true', help='the report as JSON')
    return p


def _refuse(code, detail):
    print(json.dumps({'error': code, 'detail': detail}), file=sys.stderr)
    return 1


def _present(answers):
    """Every referenced secret exists in the owner's store (presence only; no value is read into a report)."""
    key = answers['owner']['key']
    store = secrets.for_owner(key)
    missing = [name for name in answers['slack']['secrets'] if not store.has(name)]
    if missing:
        raise init.InitError('secret_missing: ' + ', '.join(missing) + ' (run the wizard to store it)')


def _print(report, as_json):
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    for step in report['steps']:
        if step['action'] != 'same':
            print(f"{step['action']:7} {step['path']}")
    if report['diff']:
        print(report['diff'], end='' if report['diff'].endswith('\n') else '\n')
    changes = [s for s in report['steps'] if s['action'] in ('create', 'change')]
    if report['applied']:
        print(f"init: {'created' if report['new'] else 'updated'} {report['target']} (Kage {report['release']}, "
              f"{len(changes)} files{', git ' + report['git'] if report['git'] else ''}). Next: "
              f"{report['target']}/scripts/kage.py doctor")
    elif changes:
        print(f'init: {len(changes)} changes not written; re-run with --apply to write them')
    else:
        print(f"init: {report['target']} is up to date")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.manifest:
            print(manifest.render(args.name, as_owner=args.as_owner))
            return 0
        if args.adopt:
            result = init.adopt(args.adopt)
            print(json.dumps(result, indent=2) if args.json else f"init: adopted {result['adopted']} "
                  f"(onboarding row in {result['db']}); nothing else changed")
            return 0
        if args.answers:
            answers = _answers.load(args.answers)
        else:
            if not sys.stdin.isatty():
                return _refuse('InitError', 'init_needs_a_terminal: run it in a terminal, or pass --answers FILE')
            answers, path = wizard.run()
            print(f'init: answers saved to {path}')
        _present(answers)
        report = init.run(answers, target=args.target, apply=args.apply, release=args.release)
    except (init.InitError, _answers.AnswersError, wizard.WizardError, secrets.SecretError) as exc:
        return _refuse(type(exc).__name__, str(exc))
    except KeyboardInterrupt:
        return _refuse('Interrupted', 'stopped; nothing was published')
    _print(report, args.json)
    return 0
