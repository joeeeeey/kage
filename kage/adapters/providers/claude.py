"""Claude Code (`claude -p --output-format stream-json`): the run's argv, the hook settings that keep a dispatched run
away from interactive tools, the lean-context profile (skill listing, git instructions, prompt-cache TTL, auto memory,
MCP) and its stream-json events."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shlex

from kage.ports import provider as port

SKILL_FILE_LIMIT = 500  # at most this many skill names in one skillOverrides map


def claude_settings(python, script, db_path, run_id):
    """Settings whose PreToolUse hook sends an interactive tool call to the runtime's `dispatch-hook` (which refuses
    it): a dispatched run has no human at its terminal."""
    command = ' '.join(shlex.quote(str(x)) for x in [python, '-I', script, '--db', db_path, 'dispatch-hook',
                                                       '--run-id', run_id])
    return {'hooks': {'PreToolUse': [{'matcher': '|'.join(port.INTERACTIVE_TOOLS),
                                      'hooks': [{'type': 'command', 'command': command, 'timeout': 60}]}]}}


def _skill_name(skill_md, fallback):
    """The `name:` of a SKILL.md frontmatter block, else the directory name."""
    try:
        with open(skill_md, encoding='utf-8', errors='replace') as stream:
            head = stream.read(4096)
    except OSError:
        return None
    block = re.match(r'---[ \t]*\n(.*?)\n---', head, re.S)
    for line in (block.group(1).splitlines() if block else ()):
        key, sep, value = line.partition(':')
        if sep and key.strip() == 'name' and value.strip().strip('"\''):
            return value.strip().strip('"\'')
    return fallback


def discover_skills(cwd, home=None):
    """Names of the personal and project skills Claude Code lists for a session started in `cwd`:
    `$CLAUDE_CONFIG_DIR` (default ~/.claude) skills/ and commands/, plus .claude/skills and .claude/commands of cwd and
    every parent up to the repository root (outside a repository: cwd only). Bundled and plugin skills are not on disk
    here; skillOverrides leaves them as they are."""
    config_dir = Path(home) / '.claude' if home else Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    bases = [config_dir]
    path = Path(cwd).resolve()
    chain = [path, *path.parents]
    top = next((i for i, d in enumerate(chain) if (d / '.git').exists()), 0)
    bases += [d / '.claude' for d in chain[:top + 1]]
    names = set()
    for base in bases:
        try:
            skill_dirs = sorted(base.joinpath('skills').iterdir()) if base.joinpath('skills').is_dir() else []
            commands = sorted(base.joinpath('commands').glob('*.md')) if base.joinpath('commands').is_dir() else []
        except OSError:
            continue
        for entry in skill_dirs:
            name = _skill_name(entry / 'SKILL.md', entry.name) if entry.is_dir() and entry.name != 'synced' else None
            if name:
                names.add(name)
        names.update(c.stem for c in commands)
    return sorted(names)[:SKILL_FILE_LIMIT]


class Claude(port.Provider):
    name = 'claude'

    def __init__(self, *, discover=None):
        """`discover(cwd, home)`: the skill lister lean() uses (default discover_skills; a deployment may route it
        through its own name)."""
        self.discover = discover or discover_skills

    def build_command(self, mode, session, prompt, model, effort, yolo_args, run_dir, settings=None, extra_args=(),
                      system_prompt=None, disallowed=()):
        """The prompt right after -p (documented ordering), the lean flags and --append-system-prompt before
        --settings, the variadic --disallowedTools last (the interactive tools, then a lean class's drop_tools)."""
        cmd = ['claude', '-p', prompt, '--resume' if mode == 'resume' else '--session-id', session,
               '--output-format', 'stream-json', '--verbose', '--model', model, '--effort', effort, *yolo_args,
               *extra_args]
        if system_prompt:
            cmd += ['--append-system-prompt', system_prompt]
        if settings is not None:
            cmd += ['--settings', json.dumps(settings, separators=(',', ':'))]
        return cmd + ['--disallowedTools', *port.INTERACTIVE_TOOLS,
                      *(t for t in disallowed if t not in port.INTERACTIVE_TOOLS)]

    def lean(self, profile, cwd, home=None):
        """(settings, extra argv, summary) of a task class's lean-context profile for a run started in `cwd`. The
        `full` / git / default-TTL profile adds nothing. auto_memory false -> autoMemoryEnabled false; mcp none ->
        --strict-mcp-config (`drop_tools` goes to build_command's --disallowedTools)."""
        settings, args, error = {}, [], None
        mode = profile['skills']
        if mode == 'none':
            args.append('--disable-slash-commands')
        elif mode in ('names', 'keep'):
            keep = set(profile['keep_skills'])
            hidden = 'name-only' if mode == 'names' else 'off'
            try:
                found = self.discover(cwd, home)
            except Exception as exc:  # noqa: BLE001 - an unreadable skill tree only costs the trim (every skill listed)
                found, error = [], type(exc).__name__
            settings['skillOverrides'] = {name: 'on' if name in keep else hidden for name in sorted(set(found) | keep)}
        if not profile['git_context']:
            settings['includeGitInstructions'] = False
        if profile['cache_ttl'] in ('5m', '1h'):
            settings['promptCacheTtl'] = profile['cache_ttl']
        if not profile['auto_memory']:
            settings['autoMemoryEnabled'] = False
        if profile['mcp'] == 'none':
            args.append('--strict-mcp-config')
        summary = {'class': profile['class'], 'lean': profile['enabled'], 'skills': mode,
                   'skill_overrides': len(settings.get('skillOverrides') or ()), 'git_context': profile['git_context'],
                   'cache_ttl': profile['cache_ttl'], 'auto_memory': profile['auto_memory'], 'mcp': profile['mcp'],
                   'drop_tools': len(profile['drop_tools']), **({'skills_error': error} if error else {})}
        return settings, args, summary

    def prompt_layout(self, profile, mode):
        """A fresh run: `constant` (the class's first message is a constant: the instructions are the whole -p
        message, the run context a file), `split` (instructions as --append-system-prompt, the run context as -p) or
        `single`; a resumed run: `single`."""
        if mode != 'fresh':
            return 'single'
        if profile.get('first_message') == 'constant':
            return 'constant'
        return 'split' if profile['rules_in_system_prompt'] else 'single'

    def parse_run_log(self, events_text, stderr_text='', last_message=None):
        info = port.run_info(last_message)
        completed = failed = False
        for event in port.events(events_text):
            kind = event.get('type')
            if kind == 'system' and event.get('subtype') == 'init' and event.get('session_id'):
                info['session'] = info['session'] or event['session_id']
            elif kind == 'system' and event.get('subtype') == 'api_retry':
                info['retry_categories'].append(str(event.get('error') or ''))
                if event.get('error_status'):
                    info['errors'].append(f"HTTP {event['error_status']}")
            elif kind == 'result':
                info['session'] = event.get('session_id') or info['session']
                failed = bool(event.get('is_error')) or event.get('subtype') != 'success'
                completed = not failed
                if isinstance(event.get('result'), str):
                    info['final'] = event['result']
                    if failed:
                        info['errors'].append(event['result'][-2000:])
                info['errors'].extend(str(e)[-500:] for e in event.get('errors') or [])
                if event.get('api_error_status'):
                    info['errors'].append(f"HTTP {event['api_error_status']}")
        return port.finish(info, completed, failed, stderr_text)
