# Grok CLI provider

Grok is a headless provider alongside Claude and Codex. Install a tested Grok CLI on the same host as the Kage
supervisor and expose `grok` on its PATH. The initial integration is tested with Grok 1.0.67. Kage does not install,
update, authenticate or copy credentials for the CLI.

## Configure and launch

Existing deployments retain their provider defaults and fallback order. Explicit `--provider grok` uses
`grok-4.7`, effort `high` and `--always-approve` unless a provider specification is configured:

```sh
python3 scripts/kage.py config set providers.grok '{"model":"grok-4.7","effort":"high","yolo_args":["--always-approve"]}'
python3 scripts/kage.py launch-task --task task-1 --provider grok --prompt-file /srv/task-brief.md
```

Effort is passed as `--reasoning-effort`. The CLI only honors levels advertised by the model's endpoint or its
per-model configuration; check for an ignored-effort warning when validating a gateway.

Suggested opt-in categories are `triage` and `summary` (short classification and read-only summaries). Keep coding,
production changes and existing intake/control routes on their current providers until evaluated separately:

```sh
python3 scripts/kage.py config set routing.categories.triage '"grok"'
python3 scripts/kage.py config set routing.categories.summary '"grok"'
```

Setting a category alone does not change the inbox/oncall/control provider. Those roles have separate provider,
model and effort settings. Configure the complete Grok specification before editing its fields in the console.
To also include Grok as an automatic fallback, explicitly include it in `routing.fallback_order`. After Grok fails, the existing configured Claude/Codex fallback order applies. Grok auth,
unsupported-model and spawn failures switch immediately; transient/rate failures use the existing retry policy.
Interactive-session conflicts, task failures and run timeouts retain their existing stop/handback behavior.

## Custom OpenAI-compatible endpoint

Inject the deployment's key into the supervisor's environment through its existing private secret mechanism:
`XAI_API_KEY` and `GROK_MODELS_BASE_URL=https://gateway.example/v1`. Never place keys in Kage config, prompts or argv.
For Grok 1.0.67, explicitly select API-key auth and bind the dotted model name to the environment credential in
`$GROK_HOME/config.toml` (default `~/.grok/config.toml`):

```toml
[auth]
preferred_method = "api_key"

[endpoints]
models_base_url = "https://gateway.example/v1"
xai_api_base_url = "https://gateway.example/v1"

[model."grok-4.7"]
base_url = "https://gateway.example/v1"
env_key = "XAI_API_KEY"
```

Both discovery and inference must use the intended gateway. `/models` succeeding alone does not prove headless
inference works: validate a fresh turn and a resume that recalls a synthetic fact. Dotted model IDs need TOML
quotes. The model key stays in the process environment; the configuration above contains no secret.

## Delivery and diagnostics

Kage preallocates a UUID, starts `grok -p ... -s UUID --output-format json --no-auto-update`, then resumes using
`-r UUID`. Child tools receive `GROK_SESSION_ID`; stale Claude/Codex identities are removed. Interactive question
and plan tools are removed from the agent toolset. Decisions still go through Kage's cards.

There is no live interactive append transport. While Grok is live or liveness is uncertain, pending events wait;
when it exits, the dispatcher resumes the original session. A session is live while a Grok process names it on its
command line (headless `-s` / `-r`, a TUI resumed by ID) or holds it in `$GROK_HOME/active_sessions.json`; Kage's own
headless runs are also covered by its leases. A Grok process that names another session never blocks this one. One
that names no session and is not registered (a TUI still starting, a long-lived helper) leaves the state unknown, and
events wait until it is gone. An unreadable registry or process scan does not prove that a session is dead.
Disabling dispatch leaves Grok events pending; they never enter the Codex queue.

JSON results supply final text, session ID and usage. Reports distinguish uncached input, cache reads and cache
writes. With JSON output, readable output appears at completion; there is no per-tool streaming display.
