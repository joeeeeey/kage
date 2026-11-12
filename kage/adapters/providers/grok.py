"""Grok CLI headless runs. Interactive append is deliberately unsupported."""
from __future__ import annotations
import json
from kage.ports import provider as port

# Shell-route tool ids and their daemon-route names (Grok 1.0.67 accepts all of them).
INTERACTIVE_TOOLS = ('ask_user_question', 'enter_plan_mode', 'exit_plan_mode', 'AskQuestion', 'CreatePlan', 'SwitchMode')

DEFAULTS = {'model': 'grok-4.7', 'effort': 'high', 'yolo_args': ['--always-approve']}


def result_events(text):
    """Accept both a whole JSON object and compact JSONL, ignoring incomplete output."""
    try:
        value = json.loads(text or '')
    except ValueError:
        yield from (e for e in port.events(text) if isinstance(e, dict))
    else:
        if isinstance(value, dict):
            yield value


class Grok(port.Provider):
    name = 'grok'

    def build_command(self, mode, session, prompt, model, effort, yolo_args, run_dir, settings=None, extra_args=(),
                      system_prompt=None, disallowed=()):
        if not session:
            raise ValueError('grok_requires_session_id')
        cmd = ['grok', '-p', prompt, '-r' if mode == 'resume' else '-s', session,
               '--output-format', 'json', '-m', model, '--reasoning-effort', effort,
               '--no-auto-update', *yolo_args, *extra_args]
        if system_prompt:
            cmd += ['--rules', system_prompt]
        return cmd + ['--disallowed-tools', ','.join(dict.fromkeys((*INTERACTIVE_TOOLS, *disallowed)))]

    def parse_run_log(self, events_text, stderr_text='', last_message=None):
        info = port.run_info(last_message)
        completed = failed = False
        for event in result_events(events_text):
            info['session'] = event.get('sessionId') or info['session']
            if isinstance(event.get('usage'), dict):
                info['usage'] = event['usage']
            error = (event.get('error') or event.get('isError') or event.get('is_error')
                     or (event.get('message') if event.get('type') == 'error' else None))
            if error or event.get('stopReason') in ('Error', 'Failed', 'Cancelled'):
                failed = True
                info['errors'].append(str(error or event.get('stopReason'))[-2000:])
            if isinstance(event.get('text'), str) and event.get('sessionId'):
                info['final'] = event['text']
                completed = event.get('stopReason') in (None, 'EndTurn', 'end_turn', 'Stop', 'stop')
                if not completed:
                    failed = True
                    info['errors'].append(str(event.get('stopReason')))
        return port.finish(info, completed, failed, stderr_text)
