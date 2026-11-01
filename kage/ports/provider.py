"""The provider port: what the engine needs from an agent CLI (Claude Code, Codex, …). One adapter per provider in
kage.adapters.providers; adding a provider adds one module.

- `build_command(mode, session, prompt, model, effort, yolo_args, run_dir, settings=None, extra_args=(),
  system_prompt=None, disallowed=())`: the exact argv of a dispatched run (`mode` fresh / resume).
- `parse_run_log(events_text, stderr_text='', last_message=None)`: a run's summary from its raw stdout (JSONL) and
  stderr — {'session', 'final', 'cli_ok', 'errors', 'retry_categories'}; tolerant of partial files.
- `prompt_layout(profile, mode)`: how a run's prompt is laid out (single / split / constant).
Later steps add liveness, delivery, probes and error classes.
"""
import json

# Tools that wait for a human at the terminal: a dispatched run never gets them (they would block it).
INTERACTIVE_TOOLS = ('AskUserQuestion', 'ExitPlanMode')


def events(text):
    """The JSON objects of a JSONL text, in order; blank, partial and non-object lines are skipped."""
    for line in (text or '').splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        yield event


def run_info(last_message):
    return {'session': None, 'final': last_message, 'cli_ok': None, 'errors': [], 'retry_categories': []}


def finish(info, completed, failed, stderr_text):
    """cli_ok once the stream said how the run ended; the stderr tail last."""
    if completed or failed:
        info['cli_ok'] = completed and not failed
    if stderr_text:
        info['errors'].append(stderr_text[-4000:])
    return info


class Provider:
    name = None

    def build_command(self, mode, session, prompt, model, effort, yolo_args, run_dir, settings=None, extra_args=(),
                      system_prompt=None, disallowed=()):
        raise NotImplementedError

    def parse_run_log(self, events_text, stderr_text='', last_message=None):
        raise NotImplementedError

    def prompt_layout(self, profile, mode):
        """One message, instructions first, unless a provider can do better."""
        return 'single'


class Providers:
    """A deployment's providers by name (claude, else codex) and the dispatcher's calls on them. `claude` / `codex`:
    callables that return the deployment's provider objects at call time (a replaced one is the one used)."""

    def __init__(self, *, claude, codex):
        self.claude, self.codex = claude, codex

    def _provider(self, name):
        return self.claude() if name == 'claude' else self.codex()

    def lean_claude(self, profile, cwd, home=None):
        """(settings, extra argv, summary) of a task class's lean-context profile; see kage.adapters.providers.claude."""
        return self.claude().lean(profile, cwd, home)

    def prompt_layout(self, profile, provider, mode):
        """single / split / constant; see kage.ports.provider."""
        return self._provider(provider).prompt_layout(profile, mode)

    def build_command(self, provider, mode, session, prompt, model, effort, yolo_args, run_dir, settings=None,
                      extra_args=(), system_prompt=None, disallowed=()):
        """Exact CLI argv for a dispatched run; see kage.adapters.providers.claude / .codex."""
        return self._provider(provider).build_command(mode, session, prompt, model, effort, yolo_args, run_dir, settings,
                                                      extra_args, system_prompt, disallowed)

    def parse_run_log(self, provider, events_text, stderr_text='', last_message=None):
        """Summarise a run from its raw stdout (JSONL) and stderr; tolerant of partial files."""
        return self._provider(provider).parse_run_log(events_text, stderr_text, last_message)
