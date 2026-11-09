"""`serve dashboard` (O1c5c): the console of a generated deployment, on the loopback interface only.

The same server and read models as every deployment's (kage.observe.dashboard): `DashboardCore` gets the assembled
units as its hooks (`_app.leaf`: avatar_cache, poll_schedule, claude_delivery, run_log, worker_config, task_registry,
rule_engine, routines, decision_buttons, blockers, feedback_learning, provider_health, stuck_claims, dispatcher,
cache_report), the trace and the message previews are built here over kage.core.trace and
kage.adapters.slack.previews (previews read Slack through the deployment's own transport leaf), and the pages are
rendered in memory with the owner's own words only. The database is opened read-only, as on any deployment.

Pages exist in English and zh-CN (O1e): an owner whose language has none gets the zh-CN ones (the API text follows
the owner's language catalog).
"""
from __future__ import annotations
from pathlib import Path
import types

from kage.adapters.slack import previews as _previews
from kage.core import trace as _trace
from kage.observe import dashboard as _k
from kage.observe import web as _web

HOOKS = ('avatar_cache', 'poll_schedule', 'claude_delivery', 'run_log', 'worker_config', 'task_registry', 'rule_engine',
         'routines', 'decision_buttons', 'blockers', 'feedback_learning', 'provider_health', 'stuck_claims',
         'dispatcher', 'cache_report')
FALLBACK_PAGES = 'zh-CN'
DEFAULT_PORT = 8777
CHANNEL_EXAMPLE = 'C0000000000'


def service_label(key, name):
    return f'pro.kage.{key}.{name}'


def page_language(language):
    return language if language in _web.languages() else FALLBACK_PAGES


def page_values(assembly):
    d = assembly.deployment
    owner, org = d.owner, d.org
    channels = dict(org.channels)
    first = next(iter(channels.values()), CHANNEL_EXAMPLE)
    return {'owner_name': owner.short_name, 'OWNER_NAME': owner.short_name.upper(), 'owner_key': owner.key,
            'tz_name': owner.timezone, 'tail_command': f'{assembly.spec.get("program", "program")} tail',
            'ticket_ref_reason': assembly.value('intake.ticket_ref_reason'), 'ticket_team': org.tracker_team or '-',
            'feedback_corpus': assembly.value('feedback_learning.DEFAULT_CORPUS_DIR'),
            'service_label': service_label(owner.key, 'followup'), 'trace_global': 'KageTrace',
            'channel_example': first, 'oncall_channel_example': first,
            'channel_names': ' / '.join(channels) or '-',
            'receipt_emoji': ' / '.join(sorted({assembly.value('hub.RECEIPT_EMOJI.claude'),
                                                assembly.value('hub.RECEIPT_EMOJI.codex')}))}


def trace_module(assembly, run_log):
    d = assembly.deployment
    owner = d.owner
    core = _trace.TraceCore(owner.language, tz=owner.tz(), owner_key=owner.key, owner_name=owner.short_name,
                            owner_slack_user=owner.slack_user,
                            ticket_ref_reason=assembly.value('intake.ticket_ref_reason'),
                            ticket_team=d.org.tracker_team, run_log=run_log)

    class Store(_trace.Store):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, core=core, **kwargs)

    return types.SimpleNamespace(
        TRACE=core, Store=Store, INBOX_TASKS=core.inbox_tasks, VIEWS=core.views, LIST_DAYS=_trace.LIST_DAYS,
        LIST_MAX_DAYS=_trace.LIST_MAX_DAYS, build=core.build, in_view=core.in_view, inbox=core.inbox,
        parse_ref=_trace.parse_ref, resolve=_trace.resolve, summary=_trace.summary, trace_ref=core.trace_ref)


def previews_module(assembly, blockers):
    owner = assembly.deployment.owner
    core = _previews.Previews(owner.language, owner_key=owner.key, owner_name=owner.short_name,
                              owner_slack_user=owner.slack_user, slack_workspace=owner.slack_workspace,
                              slack_transport=lambda: assembly.leaf('slack_transport'),
                              mask_codes=lambda text: blockers.mask_codes(text))

    class PreviewService(_previews.PreviewService):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, core=core, **kwargs)

    return types.SimpleNamespace(PREVIEWS=core, PreviewService=PreviewService, PreviewError=_previews.PreviewError)


def build(assembly):
    """(DashboardCore, a DashboardServer class bound to it) for an assembled deployment."""
    d = assembly.deployment
    owner = d.owner
    leaves = {name: assembly.leaf(name) for name in HOOKS}
    language = page_language(owner.language)
    pages = _web.Pages.for_owner(language, page_values(assembly))
    state = Path(assembly.value('hub.DEFAULT_DB')).parent
    core = _k.DashboardCore(
        owner.language, tz=owner.tz(), owner_key=owner.key, owner_name=owner.short_name, web=pages,
        default_design_html=str(Path.home() / state / 'design' / 'architecture-design.html'),
        design_workspace=d.instance.paths.get('workspace_root', '~'), known_channels=dict(d.org.channels),
        tail_command=page_values(assembly)['tail_command'], avatars=leaves['avatar_cache'],
        poll_schedule=leaves['poll_schedule'], previews=previews_module(assembly, leaves['blockers']),
        claude_delivery=leaves['claude_delivery'], run_log=leaves['run_log'], worker_config=leaves['worker_config'],
        task_registry=leaves['task_registry'], rule_engine=leaves['rule_engine'], routines=leaves['routines'],
        decision_buttons=leaves['decision_buttons'], blockers=leaves['blockers'],
        feedback_learning=leaves['feedback_learning'], provider_health=leaves['provider_health'],
        stuck_claims=leaves['stuck_claims'], dispatcher=leaves['dispatcher'], cache_report=leaves['cache_report'],
        trace=trace_module(assembly, leaves['run_log']), process_alive=lambda pid, birth: _k.process_alive(pid, birth))
    shell = _web.ROOT / language  # a real directory for the server's Path(); the rendered pages replace it

    class Server(_k.DashboardServer):
        def __init__(self, address, db_path, enrich_profiles=False, design_html=None):
            super().__init__(address, db_path, shell, enrich_profiles, design_html, core=core)
            self.web = pages
            self.public_origins = d.instance.public_origins

    return core, Server


def main(assembly, runtime, argv):
    core, server_class = build(assembly)
    default_db = Path.home() / assembly.value('hub.DEFAULT_DB')
    _k.cli_main(list(argv), description=f"{assembly.deployment.owner.short_name}'s Kage console (loopback only)",
                default_db=default_db, default_design_html=core.default_design_html, default_port=DEFAULT_PORT,
                server_class=server_class)
    return 0
