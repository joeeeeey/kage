# Plan: Lark / Feishu as a second chat platform

Status: proposal (no code yet). Base: `v0.79.3`.

Kage speaks one chat platform today: Slack. This plan does two things:

- it lists everything that ties the engine to Slack;
- it orders the work to put Slack behind a platform-neutral **chat port** and to add **Lark** (international,
  `open.larksuite.com`) and **Feishu** (mainland China, `open.feishu.cn`) as a second adapter.

The two brands share one API, so "Lark" below means both.

The approach is the one already used for agent providers (`kage/ports/provider.py`) and issue trackers
(`kage/adapters/trackers.py`):
1. Define a small port.
2. Move the Slack specifics behind it with **byte-identical behaviour for Slack deployments**, pinned by the
   existing tests.
3. Add the second adapter.

Every phase below is its own release. Phases 0–3 change nothing a Slack user can see.

## 1. Where Slack lives today

Measured on `v0.79.3`. Counts are approximate grep hits.

| Area | What is Slack-specific | Size |
|---|---|---|
| Adapter package `kage/adapters/slack/` | transport, Socket Mode runner, receiver, Block Kit cards, asks, decisions, destinations guard, card replies and threads, stuck-claim cards, health notices, avatars, previews | 16 modules, ~3,700 lines |
| Runtime handle | `rt.slack.api_call('<method>', …)` with Web API method names, called from core and policy | ~76 call sites in 15 files |
| Ids and ordering | message `ts` / `thread_ts` used as ids **and** compared as `float(ts)`; event ids hashed from `watch/channel/ts`; `C…`/`D…`/`U…` id regexes and `startswith('D')` checks | ~700 `ts`, ~140 `thread_ts`, ~75 id-shape checks in 20 files |
| Polling | `conversations.history` / `conversations.replies` paging with `oldest` / `latest` cursors (`core/loop.py`, `core/intake.py`, `core/control.py`) | the main receive path |
| Search | `search.messages` with a user token for mention discovery (`core/intake.py`) | 1 phase of intake |
| Sending | `chat.postMessage` / `chat.update` / ephemeral; `agent_sends(channel, ts)` ledger; hidden message `metadata` marking agent-sent messages; "as owner" posting with a user token | ~27 post + ~16 update sites in 11 files |
| Cards | Block Kit built in core as well as in the adapter (`core/dispatch.py`, `escalation.py`, `commands.py`, `learning.py`, `blockers.py`); action-id prefixes `sf_*` read back on click | ~44 hand-built sites |
| Reactions | emoji names as status (`eyes`, `white_check_mark`, provider receipt emoji) | ~10 files |
| Formatting | mrkdwn, `<@U…>` mentions, `<!here>`, `<url\|label>` escapes, permalinks `…/archives/C/p…` built and parsed | ~13 + ~9 files |
| Users | `users.info`, `users.lookupByEmail`, `users.list`; guest / deactivated checks in the access policy | ~7 files |
| Profile | `identities.slack`, `assistant.slack`, `SLACK_ID_RE`, `*.slack.com` workspace URL; `org.channels` must be Slack ids | `kage/profile.py` |
| Onboarding | app manifest and scopes, `xoxb-`/`xapp-`/`xoxp-` token shapes, `auth.test` / `apps.connections.open` checks, DM-code owner proof via the bot's IMs, wizard copy | ~12 files in `kage/onboard/` |
| Doctor | `slack.*_identity`, `slack.*_scopes`, `receiver.websockets`, `cards.destination_private` | `onboard/doctor.py` |
| Storage | ~45 tables with `channel` / `*_ts` columns; values `slack_message`, `slack_button`, `source='slack'`, `slack_write` | `core/store.py` and module DDL |
| Console | `slack_link`, `slack_url`, avatar routes `/api/avatars/(U…)`, page JS (`ops.js`, `control.js`, `app.js`, `trace.js`) | ~100 hits per language |
| Locale / prompts | ~44 keys per catalog name Slack; agent prompts say "Slack refs", "Slack burst" | `kage/locale/*.json`, `core/prompts.py` |
| CLI | `--channel`, `--ts`, `--thread-ts`, `--card-ts`, "Slack link or CHANNEL/TS" help | `kage/cli.py` |

Several pieces are **already neutral**, and the plan copies their pattern:
- core classes with a thin Slack wrapper: `core.decisions.ReplyReader` + `SlackReplies`, `core.asks.Asks` +
  `SlackAsks`, `core.envelope.Envelopes` + `SlackEnvelopes`, `core.provider_health.ProviderHealth(post=…)` +
  `HealthNotices`;
- `core.authority.Authority`, which takes its id regexes as parameters;
- the write guard `core.mode.WriteGuard`, which is injected into the transport;
- `LOCAL` and `console` sources, which show that core already tolerates non-Slack channel values.

## 2. Lark capabilities that matter

Everything below is drawn from the Lark open platform docs. Items marked *verify* must be confirmed against the
official docs or a test tenant before they are relied on.

| Need | Slack today | Lark equivalent |
|---|---|---|
| Bot auth | bot token `xoxb-` (static) | self-built app `app_id` + `app_secret` → `tenant_access_token`, **expires (~2 h, *verify*)**, refreshed by the adapter |
| Acting as the owner | user token `xoxp-` | `user_access_token` via OAuth / device flow, refresh token (*verify* lifetime). Prefer bot-only (see §5). |
| Receive without a public URL | Socket Mode (`apps.connections.open`) | **long connection** (WebSocket) event subscription; events and card callbacks arrive on it; each event type must also be subscribed in the developer console |
| Incoming message | `message.im`, `app_mention`, `message.groups` | `im.message.receive_v1`. `chat_type` `p2p` / `group`; `mentions[]` (compare with the bot's `open_id`); `root_id` (thread root), parent id, `thread_id`; `create_time` |
| Card click | `block_actions` payload | `card.action.trigger` callback. Acknowledge within **3 s**; `action.value` is our payload; update via the callback token (≤ 2 uses / 30 min) or `PATCH` the message |
| Send / reply / thread | `chat.postMessage` with `thread_ts` | `POST /open-apis/im/v1/messages` (to `oc_` chat or `ou_` user); `POST …/messages/:id/reply` with `reply_in_thread=true`; idempotency key (dedupe ~1 h) |
| Edit | `chat.update` | text/post: `PUT …/messages/:id`; card: `PATCH …/messages/:id` (≤ 14 days old, ≤ 30 KB) |
| Read history / thread | `conversations.history` / `.replies` | `GET /open-apis/im/v1/messages?container_id_type=chat\|thread&container_id=…` (page ≤ 50); `mget` ≤ 50 ids. **The bot cannot list p2p chats it is not in.** |
| Search | `search.messages` (user token) | message search exists; bot-identity support and scope *verify*. **Design for not needing search** (§5). |
| Reactions | `reactions.add` with emoji names | `POST …/messages/:id/reactions` with `emoji_type` (e.g. `THUMBSUP`, `DONE`, `OnIt`, `Typing`, `CrossMark`); removal needs the stored `reaction_id` |
| Mentions | `<@U…>` | text/post `<at user_id="ou_…">name</at>`; card markdown `<at id=ou_…></at>` |
| Ids | `T…`, `U…`, `C…`/`G…`/`D…`, `ts` `1712345678.123456` | `ou_…` (open_id, per app), `on_…` (union_id), `oc_…` chat, `om_…` message, `omt_…` thread; **opaque, not orderable**; order by `create_time` |
| Cards | Block Kit | Card JSON **schema 2.0** (`header`, `body.elements`, `button` with `behaviors:[{type:"callback", value}]`), ≤ 200 elements |
| Users | `users.info`, `users.lookupByEmail` | `contact/v3/users/:id`; lookup by email via `contact/v3/users/batch_get_id` (bot scope *verify*); message read APIs return the sender name |
| Hidden metadata | `metadata.event_type` on messages | none. The send ledger is the only source of "agent sent this". |

## 3. Target design

### 3.1 The chat port (`kage/ports/chat.py`)

A small interface, shaped after `ports/provider.py`. The names below are the proposal; final names come in the
Phase 1 PR.

```text
ChatRef        platform-neutral reference: (platform, conversation, message, thread_root)
                 conversation/message/thread_root are opaque strings; never parsed by core
ChatMessage    ref, author (person id), text (plain), mentions [person ids], created_at (float seconds),
               kind (human | bot | agent_sent | system), raw (adapter-private)
Person         id, display name, email (if known), is_bot, is_guest/external, deactivated

Chat (port)
  identity()                         -> bot person, workspace/tenant id
  send(conversation, text|card, thread_root=None, idempotency_key=None) -> ChatRef
  update(ref, text|card)
  react(ref, status)                 status is a neutral word: seen, working, replied, approved, rejected, …
  unreact(ref, status)
  history(conversation, since=None, limit) -> [ChatMessage] ordered by created_at
  thread(ref, since=None)            -> [ChatMessage]
  person(person_id) / person_by_email(email)
  permalink(ref) / parse_link(url) -> ChatRef | None
  is_direct(conversation) / is_private(conversation)   (replaces startswith('D') and the destinations guard)
  write_endpoints                    classification used by the observe-only WriteGuard
  render_card(CardModel) / render_text(TextModel)      (adapter renders; core never builds platform markup)
  receiver()                         -> a runner yielding neutral events (message, card_action, reaction)
```

`CardModel` is the neutral card model that `rt.buttons.card_model` already half-produces. It holds a title, body
lines, fields, buttons (`id`, `label`, `style`, `value`), a context/footer line and a decided/retired state. Slack
renders it to Block Kit and Lark to card schema 2.0.

### 3.2 Ids, ordering and storage

- **Core treats ids as opaque strings.** Every `float(ts)` and `max(…, key=float)` is replaced by the message's
  `created_at`, which the adapter supplies. For Slack, `created_at` equals `float(ts)`, so behaviour is identical.
- **Columns keep their names.** The store only adds columns and never renames them. `channel` / `ts` columns hold
  opaque refs and gain a `platform` column, default `slack`. A later release may add neutral aliases.
- **Event ids:** sha256 over `platform/watch/conversation/message/digest`. For Slack the hash input stays exactly
  as it is today (no `platform` prefix), so existing ids do not change.
- **Stored literals** stay as they are for Slack rows: `slack_message`, `slack_button`, `source='slack'`. New
  rows from Lark use `lark_message`, `lark_button`, `source='lark'`, and readers accept both.

### 3.3 Profile and configuration

- Add `identities.lark` (`tenant`, `user` = owner `open_id`) and `assistant.lark` (`app_id`, bot `open_id`) next
  to the existing Slack blocks. `channel` in `kage.answers/v1` becomes `slack | lark`, and `brand: lark |
  feishu` selects the API host.
- `org.channels` values are validated by the active adapter's id shape, not by `SLACK_ID_RE`.
- Secrets (`ports/secrets.py`): `lark-app-id`, `lark-app-secret`, optional `lark-encrypt-key` /
  `lark-verification-token` (webhook mode only), and optional `lark-user-refresh-token`.
- Selection: `profile.channel` → `kage/adapters/chat.py` `for_name()`, like `trackers.for_name()`. Assembly
  units stop hard-coding `kage.adapters.slack.X` and ask the selected adapter.

### 3.4 Token refresh (shared with rotating Slack tokens)

Lark's `tenant_access_token` expires, and so do Slack app-level tokens when token rotation is enabled. So one
**refreshing credential** helper goes into the secrets port, used by both adapters. It:
- caches the access token with its expiry;
- refreshes it ahead of expiry and on `invalid/expired token` errors;
- serialises refreshes across processes with a file lock;
- never logs a token.

## 4. TODO, in release order

Each item lists its acceptance check. Slack deployments must see no behaviour change in Phases 0–3: the existing
suite stays green, and a recorded-traffic twin test compares Slack API calls before and after.

### Phase 0 — guard rails (small)
- [ ] `tests/test_boundaries.py`: fail if `kage/core`, `kage/policy` or `kage/observe` import
      `kage.adapters.slack`. Each step also lowers an allow-list of the remaining Slack method names, `ts`
      parsing, Block Kit keys and id regexes in those packages, so the count can only go down.
- [ ] Recover or replace the generator for `kage/assembly/*.py`, which says "generated, do not edit" but is not in
      the repo. Either check the generator in or drop the header and maintain the units by hand.
- [ ] Record a Slack call trace fixture: method, params minus tokens, and order, for the main flows (intake DM,
      @mention, card post + click, thread reply, dispatch run card). Later phases diff against it.

### Phase 1 — the chat port with Slack behind it (no behaviour change)
- [ ] `kage/ports/chat.py`: `ChatRef`, `ChatMessage`, `Person`, `CardModel`, `Chat` (§3.1).
- [ ] `kage/adapters/slack/chat.py`: `SlackChat` implementing the port over the existing transport.
- [ ] `kage/adapters/chat.py`: `for_name('slack')`, plus `use()` / `current()`, like trackers.
- [ ] Hub wiring: `rt.chat` next to `rt.slack`. `rt.slack` stays as a deprecated alias until Phase 3 ends.
- Accept: suite green; call-trace fixture identical.

### Phase 2 — move core off Slack, module by module
In order of risk, one PR each:
- [ ] **Sending.** Route `chat.postMessage` / `chat.update` (dispatch, escalation, commands, control, intake,
      blockers, lifecycle, approvers, mode, access) through `rt.chat.send` / `update`. The send ledger
      `agent_sends` records the returned `ChatRef`.
- [ ] **Cards.** Replace the ~44 hand-built Block Kit sites (dispatch run, failure and breaker cards, escalation,
      commands, learning, blockers) with `CardModel` + `render_card`. Keep the action-id contract (`sf_*`) as
      the button `id`.
- [ ] **Reactions.** A neutral status vocabulary (`seen`, `working`, `replied`, `approved`, `rejected`, plus one
      receipt status per provider). The Slack map keeps today's emoji names, and `RECEIPT_EMOJI` becomes a
      per-adapter table.
- [ ] **Formatting.** `<@U…>`, `<!here>`, escapes and mrkdwn move into `render_text`. Core passes mentions as
      person ids.
- [ ] **Permalinks.** `permalink()` / `parse_link()` in the adapter; the console uses `ref_url` instead of
      `slack_link` / `slack_url` (keep the old key in JSON for one release).
- [ ] **Ids and ordering.** Remove `float(ts)`, `\d+\.\d+` checks and `startswith('D')`; use `created_at`,
      `is_direct()` and adapter id validators. `core.authority` already takes injected regexes.
- [ ] **Reading.** Watches, `loop.fetch` / `incremental`, `event_context`, `verified_root`, intake history and
      thread reads, and control polling go through `history()` / `thread()`. Cursors become `(created_at, ref)`
      pairs; Slack's are numerically identical.
- [ ] **Users.** `person()` / `person_by_email()` in the access policy, approvers, people and onboarding apply.
      Guest, deactivated and email-mismatch checks read `Person` fields.
- [ ] **Hidden metadata.** For "agent sent this", rely on the send ledger first. Slack's `metadata` stays as a
      second signal in the Slack adapter.
- [ ] **Locale and prompts.** Keys that name Slack get a `{platform}` placeholder, or move into adapter copy.
      Agent prompts say "chat refs". Keep a golden test that the Slack rendering is byte-identical.
- [ ] **CLI.** Accept `--ref <platform>:<conversation>/<message>` and links, and keep `--channel/--ts` as
      Slack-only aliases.
- Accept per PR: the suite and the call-trace fixture are identical, and the Phase 0 allow-list shrinks.

### Phase 3 — receiver and onboarding behind the port
- [ ] The receiver yields neutral events (`message`, `card_action`, `reaction`) from `Chat.receiver()`. The Slack
      Socket Mode runner becomes the Slack implementation. The decide, card-reply, control and intake handlers
      take neutral events.
- [ ] Onboarding: `channel` choice in the answers and the wizard; per-adapter manifest/app-setup instructions,
      credential checks and doctor checks (`chat.identity`, `chat.scopes`, `receiver.connected`,
      `cards.destination_private`).
- [ ] DM-code owner proof through `Chat.history()` on the bot's direct conversations. Keep today's skips for
      system conversations, such as Slack's built-in bot DM, unreadable DMs and deleted users.
- [ ] Refreshing credentials in the secrets port (§3.4), first used by Slack rotating app tokens.
- Accept: a fresh Slack `kage init` against the fake server works, and a real-workspace smoke run is unchanged.

### Phase 4 — the Lark adapter
- [ ] `kage/adapters/lark/transport.py`: HTTPS client for `open.larksuite.com` / `open.feishu.cn` (`brand`),
      `tenant_access_token` refresh, error-code mapping (rate limit, permission, not found), the offline guard
      and write-endpoint classification for the WriteGuard.
- [ ] `kage/adapters/lark/receiver.py`: long-connection client.
  - Reconnect with backoff, dedupe events on `message_id` (`event_id` for callbacks), liveness.
  - Answer `card.action.trigger` within 3 s and do the work after the acknowledgement.
  - Implementation choice: speak the long-connection protocol directly (stdlib only, like the Slack Socket Mode
    runner), or run the official SDK or CLI as a subprocess. Pick one after a spike. The engine is
    standard-library only today.
- [ ] `kage/adapters/lark/chat.py`: `LarkChat` port implementation:
  - send / reply-in-thread / update / react (store `reaction_id`) / history / thread / mget;
  - person lookup, permalinks, `is_direct` (`chat_type == p2p`), `is_private`;
  - card schema 2.0 rendering of `CardModel`.
- [ ] Mention detection: `mentions[].id == bot open_id`. Group messages without a mention are ignored unless they
      are in a thread the bot is part of (same rule as Slack today).
- [ ] Owner proof: DM code in the p2p chat with the bot. Optionally, OAuth user token proof.
- [ ] Destinations guard: cards only to the owner's p2p chat or a private group whose members are owner,
      approvers and bot.
- [ ] Onboarding copy: create a self-built app, enable the bot, add the scopes, subscribe the events
      (`im.message.receive_v1`, `card.action.trigger`, reactions, bot added/removed), enable the long connection
      and the callback configuration, then publish a version. Equivalent to the Slack manifest step, but done by
      hand in the console (Lark has no manifest import).
- [ ] Doctor: token refresh works, bot info, scopes present (if the API exposes them; otherwise a probe per
      capability), long connection connected, event subscriptions seen.
- Accept: the adapter unit suite plus the parameterised core suite pass on the Lark fake (Phase 5).

### Phase 5 — tests and fakes
- [ ] `tests/fakes/lark.py`: an HTTP-level fake for the IM, contact and auth endpoints, plus a long-connection
      fake that emits events and callbacks. It mirrors `test_transport.FakeSlack` and the Socket Mode fake.
- [ ] Parameterise the core flow tests on the platform (`test_intake`, `test_control_slack`,
      `test_b3_card_roots`, `test_loop`, `test_followup`, `test_escalation`, `test_dispatch`, `test_approvers`,
      `test_people`, `test_mode`, `test_observe_*`).
- [ ] Twin the adapter-level tests (`test_transport`, `test_cards`, `test_asks`, `test_decisions`,
      `test_envelope`, `test_receiver`, `test_socket_mode`, `test_serve_receiver`, `test_card_threads`).
- [ ] Twin the onboarding tests (`test_tokens`, `test_identity`, `test_init`, `test_doctor`,
      `test_owner_transfer`).
- [ ] Contract test: one shared scenario list, such as "DM → task → card → click → receipt", run against both
      fakes, compared on neutral events and outcomes.
- [ ] Real-tenant smoke (manual, documented in `docs/`): a free Lark/Feishu test tenant and a throwaway
      self-built app. Covers wizard, DM code, receiver connected, one card click, then cleanup. This mirrors the
      real-workspace check done for Slack onboarding.

### Phase 6 — console and docs
- [ ] Console: neutral `ref_url`; avatars through `Chat.person()`; per-platform labels; the pages show the active
      platform.
- [ ] Docs: `docs/platforms.md` (port contract, adapter checklist), README install section for Lark, a fictional
      Lark example deployment next to `examples/northwind-mira/`.

## 5. Design decisions to make early

1. **Pull vs push.** Slack intake polls `conversations.history` and runs mention search with a user token. On
   Lark a bot cannot list p2p chats it is not in, and bot search is uncertain.
   - Proposal: Lark intake is **push-only**, from the long connection, scoped to what the bot can see:
     @mentions, DMs to the bot, and threads it is in.
   - Consequence: a Lark deployment does not watch the owner's own DMs with other people. This matches how a bot
     identity already works on Slack when there is no user token.
2. **Acting as the owner.** Slack's `as_owner` voice posts with a user token. Lark supports user tokens, but
   they need OAuth and refresh, and the semantics differ.
   - Proposal: Lark ships with the `assistant` voice only. `as_owner` is a later, opt-in step.
3. **Long-connection implementation.** Native stdlib client vs official SDK / CLI subprocess. Decide after a
   one-day spike that measures:
   - reconnect behaviour;
   - callback acknowledgement latency (must be < 3 s);
   - dependency policy.
4. **Card updates after a click.** Two options:
   - the callback token, which is fast but limited to 2 uses and 30 min;
   - `PATCH` on the message, which needs a scope and works for 14 days.
   Proposal: the callback token for the immediate "decided" state, and `PATCH` for later updates (retire,
   outcome).
5. **Ordering and dedupe.** Order by `create_time`. Dedupe on `message_id` for messages and `event_id` for
   callbacks; never on Slack-style `ts`.
6. **Per-app user ids.** `open_id` differs per app. Store `union_id` too, so people records survive an app
   change; the owner transfer and people registry must key on the neutral person id.
7. **Brand.** One adapter with `brand: lark | feishu` (host only). Do not fork it.

## 6. Risks

- **The size of Phase 2.** Hundreds of sites in core. Mitigation: one concept per PR, the call-trace fixture and
  the shrinking allow-list.
- **Hidden behaviour tied to Slack quirks.** Examples: `ts` doubling as time, and thread roots inferred from
  permalinks. Mitigation: characterisation tests before each move, the same rule the engine already follows.
- **Lark API details not yet verified.** Token TTLs, bot search, scope names, rate limits. Mitigation: the Phase 4
  spike, plus the real-tenant smoke before release.
- **Rate limits.** Lark limits are per app and per API (*verify*). The transport needs the same 429 / backoff
  handling the Slack transport has, including for reactions.

## 7. Out of scope here

Other platforms (Teams, Discord, Mattermost) should become possible after Phases 0–3, but are not planned. So are
message-level encryption keys for webhook mode and multi-tenant (store) apps.
