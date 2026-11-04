import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from kage.ports import secrets

TOKEN = 'xoxb-' + '1111-2222-AbCdEfGh'


class FakeSecurity:
    """subprocess.run for /usr/bin/security over a dict; records argv and stdin."""

    def __init__(self, write_rc=0, write_stderr='', drop_writes=False):
        self.items, self.calls = {}, []
        self.write_rc, self.write_stderr, self.drop_writes = write_rc, write_stderr, drop_writes

    def __call__(self, argv, input=None, capture_output=True, text=True, timeout=None):
        self.calls.append((list(argv), input))
        done = lambda rc, out='', err='': subprocess.CompletedProcess(argv, rc, out, err)  # noqa: E731
        if argv[1:] == ['-i']:
            words = input.split()
            if self.write_rc:
                return done(self.write_rc, err=self.write_stderr)
            assert words[:2] == ['add-generic-password', '-U'], words
            opts = dict(zip(words[2::2], words[3::2]))
            if not self.drop_writes:
                self.items[(opts['-s'], opts['-a'])] = opts['-w']
            return done(0)
        opts = dict(zip(argv[2::2], argv[3::2]))
        key = (opts.get('-s'), opts.get('-a'))
        if argv[1] == 'find-generic-password':
            return done(0, self.items[key] + '\n') if key in self.items else done(44, err='could not be found')
        if argv[1] == 'delete-generic-password':
            return done(0) if self.items.pop(key, None) is not None else done(44)
        return done(1, err='unknown')


class NamesTest(unittest.TestCase):
    def test_references_carry_no_value(self):
        self.assertEqual(secrets.ref('mira', 'slack-bot-token'), 'kage-secret://mira/slack-bot-token')
        self.assertEqual(secrets.parse_ref('kage-secret://mira/slack-bot-token'), ('mira', 'slack-bot-token'))
        for bad in ('kage-secret://Mira/x', 'kage-secret://mira/x/y', 'file:///etc/x', 'kage-secret://mira/../x', None):
            with self.assertRaises(secrets.SecretError):
                secrets.parse_ref(bad)

    def test_values_are_plain_tokens(self):
        for good in (TOKEN, 'xapp-' + '1-A0B1-123-abc', 'k.e_y~v+a/l=u:e@x%y-z', 'Z' * 2048):
            self.assertEqual(secrets.check_value(good), good)
        for bad in ('', '-starts-with-dash', 'has space', 'tab\there', 'q"uote', "a'b", 'b\\s', 'a#b', 'a$b',
                    'new\nline', 'Z' * 2049, 'ünicode', None, 42):
            with self.assertRaises(secrets.SecretError) as e:
                secrets.check_value(bad)
            if isinstance(bad, str) and bad:
                self.assertNotIn(bad, str(e.exception))


class MemoryStoreTest(unittest.TestCase):
    def test_get_put_delete(self):
        s = secrets.MemoryStore('mira')
        self.assertIsNone(s.get('slack-bot-token'))
        s.put('slack-bot-token', TOKEN)
        self.assertEqual(s.get('slack-bot-token'), TOKEN)
        self.assertTrue(s.has('slack-bot-token'))
        self.assertNotIn(TOKEN, repr(s))
        self.assertTrue(s.delete('slack-bot-token'))
        self.assertFalse(s.delete('slack-bot-token'))
        with self.assertRaises(secrets.SecretError):
            s.put('Bad Name', TOKEN)


class FileStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'cfg' / 'secrets'

    def tearDown(self):
        self.tmp.cleanup()

    def test_private_files_atomic_overwrite(self):
        s = secrets.FileStore('mira', self.root)
        self.assertIsNone(s.get('slack-bot-token'))
        self.assertFalse(self.root.exists())  # a read creates nothing
        s.put('slack-bot-token', TOKEN)
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / 'slack-bot-token').st_mode), 0o600)
        s.put('slack-bot-token', 'xoxb-2')
        self.assertEqual(s.get('slack-bot-token'), 'xoxb-2')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['slack-bot-token'])  # no temp file left
        self.assertTrue(s.delete('slack-bot-token'))
        self.assertFalse(s.delete('slack-bot-token'))

    def test_wider_modes_and_symlinks_are_refused_not_repaired(self):
        s = secrets.FileStore('mira', self.root)
        s.put('a', TOKEN)
        os.chmod(self.root / 'a', 0o644)
        with self.assertRaises(secrets.SecretError):
            s.get('a')
        os.chmod(self.root / 'a', 0o600)
        os.chmod(self.root, 0o755)
        with self.assertRaises(secrets.SecretError):
            s.get('a')
        with self.assertRaises(secrets.SecretError):
            s.put('b', TOKEN)
        os.chmod(self.root, 0o700)
        target = Path(self.tmp.name) / 'elsewhere'
        target.write_text('x')
        os.symlink(target, self.root / 'link')
        with self.assertRaises(secrets.SecretError):
            s.get('link')
        with self.assertRaises(secrets.SecretError):
            s.put('link', TOKEN)
        self.assertEqual(target.read_text(), 'x')


class KeychainStoreTest(unittest.TestCase):
    def test_the_value_goes_only_on_stdin(self):
        fake = FakeSecurity()
        s = secrets.KeychainStore('mira', run=fake)
        s.put('slack-bot-token', TOKEN)
        self.assertEqual(s.get('slack-bot-token'), TOKEN)
        self.assertEqual(fake.items, {('kage.mira', 'slack-bot-token'): TOKEN})
        for argv, stdin in fake.calls:
            self.assertEqual(argv[0], '/usr/bin/security')
            self.assertFalse(any(TOKEN in a for a in argv), argv)
        writes = [stdin for argv, stdin in fake.calls if argv[1:] == ['-i']]
        self.assertEqual(writes, [f'add-generic-password -U -s kage.mira -a slack-bot-token -w {TOKEN}\n'])

    def test_missing_delete_and_a_keychain_file(self):
        fake = FakeSecurity()
        s = secrets.KeychainStore('mira', keychain='/tmp/x/test.keychain-db', run=fake)
        self.assertIsNone(s.get('nothing'))
        self.assertFalse(s.delete('nothing'))
        s.put('a', TOKEN)
        self.assertTrue(fake.calls[-2][1].endswith(' /tmp/x/test.keychain-db\n'))
        self.assertTrue(all(argv[-1] == '/tmp/x/test.keychain-db' for argv, _ in fake.calls if argv[1:] != ['-i']))
        self.assertTrue(s.delete('a'))
        with self.assertRaises(secrets.SecretError):
            secrets.KeychainStore('mira', keychain='/tmp/with space/k', run=fake).put('a', TOKEN)

    def test_write_errors_never_quote_stderr(self):
        fake = FakeSecurity(write_rc=1, write_stderr=f'security: unknown command "{TOKEN[5:]}"')
        with self.assertRaises(secrets.SecretError) as e:
            secrets.KeychainStore('mira', run=fake).put('a', TOKEN)
        self.assertEqual(str(e.exception), 'keychain_write_failed_rc_1')

    def test_a_write_that_does_not_read_back_fails(self):
        with self.assertRaises(secrets.SecretError) as e:
            secrets.KeychainStore('mira', run=FakeSecurity(drop_writes=True)).put('a', TOKEN)
        self.assertEqual(str(e.exception), 'keychain_write_not_read_back')

    def test_the_stdin_line_stays_under_securitys_limit(self):
        fake = FakeSecurity()
        s = secrets.KeychainStore('mira', keychain='/tmp/' + 'k' * 1990, run=fake)
        with self.assertRaises(secrets.SecretError) as e:
            s.put('a', 'Z' * 2048)
        self.assertEqual(str(e.exception), 'keychain_write_line_too_long')
        self.assertEqual(fake.calls, [])


class ForOwnerTest(unittest.TestCase):
    def test_backends(self):
        with tempfile.TemporaryDirectory() as home:
            f = secrets.for_owner('mira', backend='file', home=home)
            self.assertIsInstance(f, secrets.FileStore)
            self.assertEqual(f.root, Path(home) / '.config/kage/mira/secrets')
            self.assertIsInstance(secrets.for_owner('mira', backend='memory'), secrets.MemoryStore)
            self.assertIsInstance(secrets.for_owner('mira', backend='keychain'), secrets.KeychainStore)
            with self.assertRaises(secrets.SecretError):
                secrets.for_owner('mira', backend='vault')
            with self.assertRaises(secrets.SecretError):
                secrets.for_owner('Mira', backend='memory')


if __name__ == '__main__':
    unittest.main()
