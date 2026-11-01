#!/usr/bin/env python3
"""Personal-data scan: the engine repository holds no person's or company's identifiers.

gitleaks finds secrets; this finds identifiers that are not secret but must not be here either:

  denylist   a word whose sha256 is listed in tools/leak-denylist.sha256 (names, company words, project and task
             names, real chat ids), or a compound of words joined by - _ . (a project name such as `some-project`,
             lower-cased and joined by -, whose parts alone are ordinary words). Only hashes are stored, so the list
             itself names nobody. Add one with `--hash WORD` (or `--hash some-project`).
  chat-id    a real-looking Slack id (T…, U…, W…, B…, C…, G…, D…, A…, E… with digits). Synthetic ids in tests
             and examples contain TEST, FAKE, EXAMPLE or 0000.
  home-path  an absolute home directory of a named user (/Users/<name>/, /home/<name>/).
  email      an address outside the reserved example domains (noreply addresses in commit trailers pass).

Findings print the file, line, rule and a masked hint, never the matched text.

A denylist word added after a release finds that release's commits too; their findings are listed, commit and word
hash, in tools/leak-history-known.txt (`--known` prints the lines for the current findings of --history), so the
history scan stays strict for every other commit.

  leak_scan.py --tree        every tracked file in the working tree (CI, before a release)
  leak_scan.py --staged      the staged version of every staged file (pre-commit)
  leak_scan.py --history     every added line in every commit, and every commit message (CI)
  leak_scan.py PATH...       the given files
  leak_scan.py --hash WORD   print the denylist line for WORD
Exit status 1 when anything is found, 2 on a usage or git error.
"""
import hashlib
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DENYLIST = ROOT / 'tools/leak-denylist.sha256'
KNOWN = ROOT / 'tools/leak-history-known.txt'
WORD_RE = re.compile(r'[A-Za-z0-9]+')
COMPOUND_RE = re.compile(r'[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)+')
CHAT_ID_RE = re.compile(r'(?<![A-Za-z0-9])[TUWBCGDAE][A-Z0-9]{8,11}(?![A-Za-z0-9])')
SYNTHETIC_RE = re.compile(r'TEST|FAKE|EXAMPLE|0000')
HOME_RE = re.compile(r'/(?:Users|home)/([A-Za-z0-9._-]+)/')
HOME_ALLOWED = {'example', 'user', 'you', 'me', 'runner', 'someone'}
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)')
EMAIL_ALLOWED_RE = re.compile(r'(?:^|\.)(?:example\.(?:com|org|net)|example|test|invalid|localhost'
                              r'|users\.noreply\.github\.com)$', re.I)
NOREPLY_RE = re.compile(r'no-?reply@', re.I)   # commit trailers such as a co-author's noreply address
SKIP_SUFFIXES = ('.png', '.jpg', '.jpeg', '.gif', '.ico', '.gz', '.zip', '.pdf')


def digest(word):
    return hashlib.sha256(word.lower().encode('utf-8')).hexdigest()


def load_denylist(path=DENYLIST):
    hashes = set()
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            if not re.fullmatch(r'[0-9a-f]{64}', line):
                raise ValueError(f'{path.name}: not a sha256 line')
            hashes.add(line)
    return hashes


def mask(text):
    return f'{text[0]}…({len(text)} chars)' if text else '…'


def scan_text(text, denied, keys=False):
    """Yield (line number, rule, masked hint) for every finding in one text (with `keys`, a fourth item: the matched
    text's sha256, for the known-history list)."""
    def found(number, rule, text):
        return (number, rule, mask(text), digest(text)) if keys else (number, rule, mask(text))
    for number, line in enumerate(text.splitlines(), 1):
        for word in WORD_RE.findall(line):
            if digest(word) in denied:
                yield found(number, 'denylist', word)
        for compound in COMPOUND_RE.findall(line):  # every run of 2+ adjacent parts, joined by -
            parts = re.split(r'[-_.]', compound)
            for i in range(len(parts)):
                for j in range(i + 2, min(len(parts), i + 4) + 1):
                    joined = '-'.join(parts[i:j])
                    if digest(joined) in denied:
                        yield found(number, 'denylist', joined)
        for m in CHAT_ID_RE.finditer(line):
            value = m.group(0)
            if sum(c.isdigit() for c in value) >= 2 and not SYNTHETIC_RE.search(value):
                yield found(number, 'chat-id', value)
        for m in HOME_RE.finditer(line):
            if m.group(1).lower() not in HOME_ALLOWED:
                yield found(number, 'home-path', m.group(1))
        for m in EMAIL_RE.finditer(line):
            if not EMAIL_ALLOWED_RE.search(m.group(1)) and not NOREPLY_RE.match(m.group(0)):
                yield found(number, 'email', m.group(0))


def load_known(path=KNOWN):
    """(commit prefix, sha256) pairs of findings in released history (see the module doc)."""
    if not path.is_file():
        return set()
    pairs = set()
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            commit, value = line.split()
            if not re.fullmatch(r'[0-9a-f]{12}', commit) or not re.fullmatch(r'[0-9a-f]{64}', value):
                raise ValueError(f'{path.name}: not a "<commit12> <sha256>" line')
            pairs.add((commit, value))
    return pairs


def git(*args):
    out = subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True)
    if out.returncode:
        raise RuntimeError(f'git {args[0]} failed: {out.stderr.decode(errors="replace").strip()}')
    return out.stdout


def sources(mode, paths):
    """Yield (label, text) pairs to scan."""
    if mode == 'paths':
        for p in paths:
            yield p, Path(p).read_bytes().decode('utf-8', errors='replace')
    elif mode == 'tree':
        for name in git('ls-files', '-z').decode().split('\0'):
            if name and not name.lower().endswith(SKIP_SUFFIXES) and (ROOT / name).is_file():
                yield name, (ROOT / name).read_bytes().decode('utf-8', errors='replace')
    elif mode == 'staged':
        names = git('diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z').decode().split('\0')
        for name in names:
            if name and not name.lower().endswith(SKIP_SUFFIXES):
                yield f'{name} (staged)', git('show', f':{name}').decode('utf-8', errors='replace')
    elif mode == 'history':
        commits = git('rev-list', '--all').decode().split()
        for commit in commits:
            yield f'commit {commit[:12]} message', git('log', '-1', '--format=%B', commit).decode('utf-8', 'replace')
            patch = git('show', '--format=', '--unified=0', '--no-color', commit).decode('utf-8', 'replace')
            added = '\n'.join(line[1:] for line in patch.splitlines()
                              if line.startswith('+') and not line.startswith('+++'))
            yield f'commit {commit[:12]} added lines', added


def main(argv):
    if len(argv) == 2 and argv[0] == '--hash':
        print(digest(argv[1]))
        return 0
    known_mode = argv == ['--known']
    if known_mode:
        argv = ['--history']
    modes = [a for a in argv if a in ('--tree', '--staged', '--history')]
    paths = [a for a in argv if not a.startswith('--')]
    if (not modes and not paths) or (modes and paths) or len(set(argv) - set(modes) - set(paths)):
        print(__doc__, file=sys.stderr)
        return 2
    denied, known = load_denylist(), load_known()
    found, skipped, lines = 0, 0, set()
    try:
        for mode in (m.lstrip('-') for m in modes) if modes else ['paths']:
            for label, text in sources(mode, paths):
                commit = label.split()[1] if mode == 'history' else None
                for number, rule, hint, key in scan_text(text, denied, keys=True):
                    if known_mode:
                        lines.add(f'{commit} {key}')
                        continue
                    if commit and (commit, key) in known:
                        skipped += 1
                        continue
                    print(f'{label}:{number}: {rule} {hint}')
                    found += 1
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    if known_mode:
        print('\n'.join(sorted(lines)))
        return 0
    print(f'leak_scan: {found} finding(s)' + (f' ({skipped} in released history, listed as known)' if skipped else ''),
          file=sys.stderr)
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
