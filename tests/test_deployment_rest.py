import contextlib
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from kage.core import dispatch, intake, trace
from kage.observe import dashboard
from kage.ports import provider


class WorkspaceBootstrapTest(unittest.TestCase):
    def test_the_command_line_and_the_report(self):
        seen = []

        def runner(cmd, **kw):
            seen.append((cmd, Path(cmd[cmd.index('--brief-file') + 1]).read_text(), kw['timeout']))
            return types.SimpleNamespace(returncode=0, stdout='{"workspace": "/w"}', stderr='')
        report = intake.run_workspace_bootstrap('/deploy/bootstrap.py', 'Title', 'the brief', 'ctx', linear=True,
                                                agent='claude', root='/tasks', slug='s', folder_name='x-inbox', runner=runner)
        self.assertEqual(report, {'workspace': '/w'})
        cmd, brief, timeout = seen[0]
        self.assertEqual(cmd[1:3], ['/deploy/bootstrap.py', '--no-launch'])
        self.assertIn('--state', cmd)
        self.assertEqual(cmd[-4:], ['--slug', 's', '--folder-name', 'x-inbox'])
        self.assertEqual((brief, timeout), ('the brief', 180))

    def test_failures_are_named(self):
        bad = lambda cmd, **kw: types.SimpleNamespace(returncode=2, stdout='', stderr='x\nlast <line>!')  # noqa: E731
        with self.assertRaisesRegex(ValueError, 'workspace_bootstrap_failed: last  line '):
            intake.run_workspace_bootstrap('/b.py', 't', 'b', 'c', linear=False, agent='codex', root='/r', runner=bad)
        junk = lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout='not json', stderr='')  # noqa: E731
        with self.assertRaisesRegex(ValueError, 'returned_invalid_json'):
            intake.run_workspace_bootstrap('/b.py', 't', 'b', 'c', linear=False, agent='codex', root='/r', runner=junk)


class CommandLinesTest(unittest.TestCase):
    def test_trace_exit_codes_in_the_owners_language(self):
        def run(error):
            def trace_ref(path, ref):
                if error:
                    raise error(ref)
                return {'ref': ref}
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = trace.cli_main(['C0TEST/1'], language='en', default_db=Path('/nonexistent.sqlite'),
                                      description='d', trace_ref=trace_ref, render_text=lambda r: 'text ' + r['ref'])
            return code, out.getvalue(), err.getvalue()
        self.assertEqual(run(None), (0, 'text C0TEST/1\n', ''))
        self.assertEqual(run(ValueError)[0], 2)
        self.assertEqual(run(KeyError), (1, '', 'No record of this message was found\n'))

    def test_dashboard_serves_on_loopback_until_interrupted(self):
        made = []

        class Server:
            server_port = 12345

            def __init__(self, address, db, enrich_profiles, design_html):
                made.append((address, str(db), enrich_profiles, str(design_html)))

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                made.append('closed')
        out = io.StringIO()
        umask = os.umask(0o022)
        try:
            with contextlib.redirect_stdout(out):
                dashboard.cli_main(['--no-enrichment'], description='d', default_db=Path('/x.sqlite'),
                                   default_design_html='/d.html', default_port=9, server_class=Server)
        finally:
            os.umask(umask)
        self.assertEqual(made, [(('127.0.0.1', 9), '/x.sqlite', False, '/d.html'), 'closed'])
        self.assertIn('http://127.0.0.1:12345', out.getvalue())


class RcExportsAndProvidersTest(unittest.TestCase):
    def test_literal_exports_last_one_wins_expansions_skipped(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        (home / '.a').write_text('export ONE="1"\nexport TWO="$HOME/x"\n')
        (home / '.b').write_text("export ONE='2'  # later\nexport lower=1\n")
        self.assertEqual(dispatch.rc_exports(['ONE', 'TWO', 'NONE'], home, rc_files=('.a', '.b', '.missing')), {'ONE': '2'})

    def test_providers_are_looked_up_at_call_time(self):
        calls = []
        fake = lambda name: types.SimpleNamespace(  # noqa: E731
            prompt_layout=lambda profile, mode: (name, mode), lean=lambda profile, cwd, home: (name, cwd),
            build_command=lambda *a: (name, a[0]), parse_run_log=lambda *a: (name, a[0]))
        current = {'claude': fake('claude-1')}
        p = provider.Providers(claude=lambda: current['claude'], codex=lambda: fake('codex'))
        self.assertEqual(p.prompt_layout({}, 'claude', 'fresh'), ('claude-1', 'fresh'))
        current['claude'] = fake('claude-2')
        self.assertEqual(p.lean_claude({}, '/cwd'), ('claude-2', '/cwd'))
        self.assertEqual(p.build_command('codex', 'resume', None, 'p', 'm', 'e', [], '/run'), ('codex', 'resume'))
        with self.assertRaises(KeyError):
            p.parse_run_log('other', 'log')


if __name__ == '__main__':
    unittest.main()
