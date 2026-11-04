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

Kage still does not provide `init`, secret storage or a workspace bootstrap. Each is a separate onboarding step.
None of this alters existing profiles, stores, configuration files or services.
