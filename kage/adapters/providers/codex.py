"""Codex CLI (`codex exec --json`): the run's argv and its JSONL events."""
from pathlib import Path

from kage.ports import provider as port


class Codex(port.Provider):
    name = 'codex'

    def build_command(self, mode, session, prompt, model, effort, yolo_args, run_dir, settings=None, extra_args=(),
                      system_prompt=None, disallowed=()):
        """`codex exec [OPTIONS] PROMPT`, or `codex exec resume [OPTIONS] SESSION PROMPT` (options documented on
        `resume --help`). The last message goes to `<run_dir>/last.md`."""
        options = [*yolo_args, '--skip-git-repo-check', '--json', '-o', str(Path(run_dir) / 'last.md'),
                   '-m', model, '-c', f'model_reasoning_effort="{effort}"']
        if mode == 'resume':
            return ['codex', 'exec', 'resume', *options, session, prompt]
        return ['codex', 'exec', *options, prompt]

    def parse_run_log(self, events_text, stderr_text='', last_message=None):
        info = port.run_info(last_message)
        completed = failed = False
        for event in port.events(events_text):
            kind = event.get('type')
            if kind == 'thread.started' and event.get('thread_id'):
                info['session'] = info['session'] or event['thread_id']
            elif kind == 'turn.completed':
                completed, failed = True, False
            elif kind == 'turn.failed':
                failed = True
                error = event.get('error') or {}
                info['errors'].append(str(error.get('message') if isinstance(error, dict) else error)[-2000:])
            elif kind == 'error':
                failed = True
                info['errors'].append(str(event.get('message') or '')[-2000:])
            elif kind == 'item.completed':
                item = event.get('item') or {}
                if item.get('type') in ('agent_message', 'assistant_message') and item.get('text') and not last_message:
                    info['final'] = item['text']
        return port.finish(info, completed, failed, stderr_text)
