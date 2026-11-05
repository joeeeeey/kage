"""`python3 -m kage --deployment DIR [arguments]`: the deployment's program on the runtime Kage assembles (kage.app).

The arguments are the program's own (`status`, `tail RUN`, `--help`, …; `ask-owner…` is the owner's `ask-<key>…`);
the program name in its usage is the deployment's. An assembly that cannot be made (a switch off, another release pinned, a transitional module missing or
altered) is refused before anything runs: exit 3, one JSON line on stderr. A failing command ends like the program's
own: exit 1 with {"error", "detail"} on stderr (details of unexpected errors withheld).

`python3 -m kage init …` (O1c4) creates or adopts a deployment and runs before any assembly (kage.onboard.init_cli)."""
from __future__ import annotations
import json
from pathlib import Path
import re
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['init']:  # O1c4: onboarding runs before any assembly (there may be no deployment yet)
        from kage.onboard import init_cli
        return init_cli.main(argv[1:])
    if len(argv) < 2 or argv[0] != '--deployment':
        print('usage: python3 -m kage --deployment DIR [arguments] | python3 -m kage init [--help]', file=sys.stderr)
        return 2
    from kage import app
    try:
        assembly = app.App(argv[1])
        runtime = assembly.assemble()
    except app.AssemblyError as exc:
        print(app.refusal(exc), file=sys.stderr)
        return 3
    sys.argv = [Path(assembly.program).name, *app.owner_alias(argv[2:], assembly.deployment.owner.key)]
    try:
        runtime['main']()
    except Exception as exc:  # the program's own last word (the deployment's guard, the same lines)
        api_error = re.fullmatch(r'Slack API error: ([a-z_]+)', str(exc))
        clear = isinstance(exc, ValueError) or api_error or str(exc).startswith('real_slack_disabled')
        detail = str(exc) if clear else 'Operation failed; inspect API permissions/connectivity and watch status.'
        detail += runtime['rule_registry'].tag(detail)
        print(json.dumps({'error': type(exc).__name__, 'detail': detail}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
