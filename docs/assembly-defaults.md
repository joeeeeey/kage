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

This release does not provide `init`, secret storage, a workspace bootstrap, or neutral pack contents. Each is a
separate onboarding step. It does not alter existing profiles, stores, configuration files, or services.
