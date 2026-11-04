# Deployment wiring defaults

An `instance.json` still needs an `assembly` section. Its `values` object is now optional: every value used by the
generated units has an owner-scoped default in `kage.defaults.assembly_values`. Explicit values win, including empty
strings, nulls, and entire objects. In particular an explicit `worker_config.DEFAULTS` is never recursively merged
with Kage's defaults. Existing deployments that specify every value retain the same effective values.

The default paths separate owners under `~/.config/kage/<key>/` and `~/.local/share/kage/state-<key>/`. The default
worker configuration leaves dispatch, intake, owner-DM reading, routines, rule proposals, and feedback learning off.
These defaults prepare onboarding; they are not the observe-only mode or a substitute for its send-path guard.

Unknown requested values still raise `AssemblyError`. Policy and playbook packs still need lock entries and matching
hashes: `default.access` is the reserved neutral policy-pack name, and `<owner key>.playbook` remains the personal
pack name. Missing packs refuse assembly; they never become empty policies. The workspace bootstrap and tracker
helper paths name optional deployment integrations, which onboarding must provide or keep disabled.

## Neutral packs

`kage.packs` ships two templates: `default.access` (an organization pack with generic vocabularies and no
organization checks) and `default.playbook` (the dispatch rules every run needs: memory, event resolution, asking
the owner, other tasks, status.md, authority and secrets; no intake playbook). A template is never loaded from Kage.
Onboarding copies it into the deployment, for example `deploy/packs/default/access.py`, and pins its sha256 in
`kage.lock` `packs` like any other pack, so a missing or altered copy still refuses. With the neutral access pack
no rule can widen anything: a plan no owner rule covers goes to the owner. An organization's own pack replaces it.

The `rule_engine` unit binds an organization pack's own checks and vocabularies (its `CHECKS`, `PRODUCTS`, `_LIMITS`,
`_bundle_items`) under their names only when the pack defines them; a deployment with its own pack keeps every
name it had.

## A deployment without modules of its own

A deployment that declares no `slack_transport` module gets Kage's own leaf (`kage/assembly/standalone/`): the
Slack client over the transport, its tokens only from the owner's secrets store (`kage.ports.secrets`: the macOS
Keychain, service `kage.<key>`, else 0600 files in `~/.config/kage/<key>/secrets/`), and no trusted bot until
onboarding records the verified one. A declared module always wins and keeps its own token variables and
files. An owner pack without an intake playbook binds `kage.core.intake.unavailable_playbook()`: intake
refuses with `intake_playbook_unavailable`, everything else assembles. A missing workspace bootstrap script is
refused before anything runs, and the default tracker helper is none. `context` on a local event (a note, an agent
message) needs no Slack identity. The instance profile still needs `paths.inbox_rules`.

`tests/test_minimal_deployment.py` is the acceptance proof: a deployment made only of the templates assembles,
answers `--help`, fails `doctor --offline` until its state directory exists and its bot and app-level tokens are
stored, then passes, and completes note → context → resolve with a scratch HOME and no network.

## Secrets and the owner's identity (O1c3)

- `kage.onboard.tokens.admit` checks a pasted token with Slack (bot: `auth.test` names a bot; app-level:
  `apps.connections.open`; user: a person of the bot's workspace) and only then stores it. Another workspace or
  kind is refused before anything is written. No token is ever in an answers file, the deployment, git, argv, a log
  or a report; they hold `kage-secret://<key>/<name>` at most.
- `kage.onboard.identity` records the owner as `kage.identity/v1` in the private state directory, proven by the user
  token's own identity or by a one-time code the owner sends to the bot as a direct message (read back with the
  bot's own token). A typed id is never accepted, and a bot token's `auth.test` never proves the owner.
- `doctor` adds `secrets.port` (the store holds the bot and app-level tokens; presence only) and `owner.identity`
  (the profile against the record; a profile that predates it is reported as adopted).
- `KAGE_SECRETS_BACKEND=file` (or `memory`) keeps test harnesses on a scratch HOME away from the user's Keychain.

Kage still does not provide `init` or a workspace bootstrap. Each is a separate onboarding step.
None of this alters existing profiles, stores, configuration files or services.
