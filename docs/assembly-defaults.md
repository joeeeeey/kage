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

None of this alters existing profiles, stores, configuration files or services.

## `kage init` (O1c4)

- `python3 -m kage init` runs before any assembly. On a terminal it is the wizard: about ten questions with defaults,
  the Slack app manifest, the bot and app-level tokens (and the user token for the `as_owner` voice) checked and
  stored, the owner proven (DM code, or the user token), the answers written (`kage.answers/v1`, 0600, references
  only). `--answers FILE` runs without a terminal and needs the referenced secrets in the store.
- The deployment (`~/.local/share/kage/deploy-<key>/`, a private git repository init never pushes) holds the
  profile, the two neutral packs, `kage.lock` (the running release, verified; every switch on Kage) and
  `scripts/kage.py`, the deployment's program. The Slack identities in `owner.json` come only from the verified
  records (`kage.identity/v1`, `kage.bot/v1`), never from the answers. `instance.json` has no `values`.
- Built in a staging directory, checked (profile, lock, packs), published by rename. A re-run prints what would change
  and writes it only with `--apply`; packs are never replaced; a symlink, another owner key, a differing verified
  identity or a pack off its pin refuses. `--adopt DIR` records the onboarding row (`onboarding_steps`) only.
- Tracker none: `org.json` has no ticket pattern; the units compile `Org.ticket_pattern()`, which never matches (O1c5a).

## A generated deployment runs (O1c5)

- Dispatch files (`kage.onboard.dispatch_files`): `SKILL.md` (the event semantics the dispatched prompts cite),
  `references/rules.md` (rule-check plans), `references/rules-example.json` (a neutral spec: every access change asks
  the owner), `references/rule-registry-seed/` (no rule) and `scripts/dispatch_runner.py` (verifies the pinned release,
  then `kage.core.runner`). Owner's name, key, program and Kage version are the only values in them.
- `python3 -m kage --deployment DIR serve dashboard [--port P]`: the console on the loopback interface, the assembled
  units as its hooks, pages rendered with the owner's words, in the owner's language (en or zh-CN, O1e).
- `… serve receiver`: Socket Mode (kage.adapters.slack.socket_mode) with the app-level and bot tokens from the secrets
  store and the workspace and bot from the verified profile; buttons, card-thread replies and the owner's control
  messages go to the deployment's own program. No deployment-specific shortcut.
- `services.json` (`kage.services/v1`, `written_by: kage init`): followup (`run`), dashboard, receiver; labels
  `pro.kage.<key>.<name>`; this Python and the agents' PATH; logs in the state directory.
  `… services plan|start [--offline]|stop|status` loads them into launchd (plists run Kage's launcher from the pinned
  release) only after every required doctor check but `services.running` passes; init itself loads nothing. A
  services.json init did not write refuses: such a deployment keeps its own service tool.
- Test harnesses set `KAGE_NO_AUTOSTART=1` when they run a deployment's program: CLI commands otherwise start a
  supervisor in the background.

## The owner's language in Slack (O1d)

- Card words follow the owner: every command's `--lang` and every core default resolve to
  `kage.locale.card_lang(owner language)` (`zh` for a Chinese owner, `en` for anyone else); a caller may still name one.
- The gates on the agent's owner-facing sentences (`rule-check --card --summary`, rule proposals, gap proposals) check
  `kage.locale.written_for(owner language, text)`: Han for a Chinese owner (unchanged error codes), a letter for
  anyone else (`…in_the_owner_language`). Proposal field names (`title_zh`, …) stay as the wire format.
- `kage init`'s SKILL.md tells dispatched agents the owner's language. Not owner-language: the org tracker's English
  rule, the requester-language checks of replies sent as the owner, the secret-masking patterns.
- The console pages (O1e): `kage/observe/pages/en/` is `pages/zh-CN/` in English — the same files, structure,
  sentinels, element ids and API paths (a parity test guards it); a page change is made in both languages.

## An install's quiet moment keeps the control entry (C4)

- `dispatch.install_hold` (default false): while true no new run starts and nothing is revived, except the control
  task's — the owner's control messages keep being answered during an install. Runs already started finish; held events
  stay pending and are routed after the hold. A deployment that pins its own worker defaults needs the key in them
  (`worker_config.DEFAULTS.dispatch.install_hold: false`) to set it.
- While dispatch cannot run the control task (`dispatch.enabled` false, or the control task paused), a control message
  from Slack gets one short note in its thread at once, and its answer clock waits (a fresh one after the resume). A
  control run that reaches a message after its `timeout` still answers or relays it; other failures stay final.

## Observe-only mode (O2, release A)

- `instance.mode` (`runtime_meta`): `installed` (kage init) → `observe` (`services start` after its doctor gate) →
  `active` (only the owner's own click on the start card) ↔ `paused`; `mode show | observe | pause | resume` from the
  owner's terminal. A store without the row (an adopted deployment) is `active`: nothing changes for it.
- While not `active`: the dispatcher starts no run, and the standalone Slack leaf lets writes through only to the
  owner's verified private card destination; everything else is a `shadow_actions` row (`ShadowedWrite`). Deployments
  with their own Slack transport keep it unguarded.
- The start card (code-owned, button-only, daily while observing): counts per kind and the latest shadow actions,
  [开始干活] / [再看一天].
- Not yet (release B): triage with a no-tools model call in observe mode, the console's `/onboarding` page, owner
  corrections of shadow decisions.
