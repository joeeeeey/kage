# Rule-check plans (`rule-check --plan FILE`)

Written by `kage init` (Kage @version@) for @name@'s deployment. An agent describes an access or account change as a
plan; code resolves the facts and the active rules decide who must approve it. The plan says what is intended, never
who approved it.

## The plan file

A JSON object with schema `kage/@key@.rules-plan/v1`:

| Field | Required | What |
|---|---|---|
| `schema` | yes | `kage/@key@.rules-plan/v1` |
| `category` | yes | `access` |
| `kind` | yes | `person`, `group`, `workload`, `external` or `agent_ops` |
| `action` | yes | `grant`, `change`, `tighten`, `revoke`, `delete`, `rotate`, `resend`, `repair`, `create_account`, … |
| `system` | yes | one of the access pack's systems (`source_control`, `chat`, `email`, `cloud_iam`, `database`, …) |
| `recipient` | no | `email`, `principal` or `group` of whom it is for |
| `source` | no | the event (`event_id`) or message (`channel`, `ts`) that asked for it |
| `scope` | no | `roles`, `resources`, `environment`, `level`, `justification`, `purpose`, `irreversible`, … |
| `spend` | no | `recurring_usd_month`, `one_time_usd` |
| `temporary` | no | `expires_at` (YYYY-MM-DD) or `days` |
| `credential` | no | `type` and `delivery` (`secure_link` or `owner_handles`; never the value) |
| `task`, `notes` | no | the task id; a short note |

Unknown fields refuse the plan. `rule-check --help` and `rules show` show the active rules.

## Outcomes

- `auto`: act, then `rule-outcome --check ID --result executed|failed|abandoned`.
- `one_tap` / `@key@`: a card went to @name@ (`--card`); act only after the decision event, when
  `rule-status --check ID --plan FILE` says the plan is executable.
- `clarify`: ask the requester whom or what the request names.
- `route`: the change belongs to the system's own admin; say so.

`references/rules-example.json` is a neutral example spec: every access change asks @name@ until @name@ installs
rules of their own (`rules install`).
