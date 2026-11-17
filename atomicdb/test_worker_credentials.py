"""The account from a .env file, -S with a default (worker build 2026111703).

At startup the worker reads .env from the current directory, else next to
the script; its ATOMICDB_USER / ATOMICDB_PASSWORD make -U / -P optional, and
-U / -P on the command line win over it.  --dotenv PATH reads one specific
file and is exclusive with -U / -P.  The server and process environment are
never read.  The password is never printed.
"""

import importlib.util
import io
import os
import pathlib
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

_SPEC = importlib.util.spec_from_file_location(
    'atomicdb_worker_credentials_under_test',
    pathlib.Path(__file__).resolve().parent.parent / 'Client'
    / 'atomicdb_worker.py')
worker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worker)

SECRET = 's3cr3t-#pass word'


def _args(U=None, P=None, dotenv=None):
    return SimpleNamespace(U=U, P=P, dotenv=dotenv)


class EnvFileParsingTests(SimpleTestCase):

    def test_plain_lines_comments_and_blanks(self):
        text = ('# my worker\n'
                '\n'
                'ATOMICDB_USER=alice\n'
                '   OTHER = value  \n'
                '# ATOMICDB_PASSWORD=commented\n')
        self.assertEqual(worker.parse_env_file(text),
                         {'ATOMICDB_USER': 'alice', 'OTHER': 'value'})

    def test_quotes_keep_hashes_and_spaces(self):
        text = ('ATOMICDB_PASSWORD="s3cr3t-#pass word"\n'
                "OTHER='single # quoted'\n")
        values = worker.parse_env_file(text)
        self.assertEqual(values['ATOMICDB_PASSWORD'], SECRET)
        self.assertEqual(values['OTHER'], 'single # quoted')

    def test_inline_comment_only_after_a_space(self):
        values = worker.parse_env_file('A=abc # note\nB=abc#def\n')
        self.assertEqual(values, {'A': 'abc', 'B': 'abc#def'})

    def test_export_prefix_crlf_and_junk(self):
        text = ('export ATOMICDB_USER=bob\r\n'
                'not a pair\r\n'
                '1BAD=x\r\n'
                'EMPTY=\r\n')
        self.assertEqual(worker.parse_env_file(text),
                         {'ATOMICDB_USER': 'bob', 'EMPTY': ''})


class DotenvFileTests(SimpleTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def write(self, name, text, bom=False):
        path = self.root / name
        data = text.encode('utf-8')
        path.write_bytes((b'\xef\xbb\xbf' if bom else b'') + data)
        return path

    def test_the_account_comes_from_the_file(self):
        path = self.write('.env', 'ATOMICDB_USER=alice\n'
                                  f'ATOMICDB_PASSWORD="{SECRET}"\n', bom=True)
        self.assertEqual(worker.read_dotenv_account(path), ('alice', SECRET))

    def test_the_server_is_not_read_from_the_file(self):
        path = self.write('.env', 'ATOMICDB_USER=alice\n'
                                  'ATOMICDB_PASSWORD=x\n'
                                  'ATOMICDB_SERVER=https://elsewhere\n')
        for a in (_args(dotenv=str(path)), _args()):
            a.S = worker.DEFAULT_SERVER
            problem, _ = worker.resolve_account(a, [path])
            self.assertIsNone(problem)
            self.assertEqual(a.S, 'https://belzedar.duckdns.org')

    def test_a_missing_key_names_the_key_and_the_file_not_values(self):
        path = self.write('.env', f'ATOMICDB_PASSWORD="{SECRET}"\n')
        with self.assertRaises(worker.DotenvError) as caught:
            worker.read_dotenv_account(path)
        message = str(caught.exception)
        self.assertIn('ATOMICDB_USER', message)
        self.assertIn(str(path), message)
        self.assertNotIn('ATOMICDB_PASSWORD', message)
        self.assertNotIn(SECRET, message)

    def test_an_empty_password_counts_as_missing(self):
        path = self.write('.env', 'ATOMICDB_USER=alice\nATOMICDB_PASSWORD=\n')
        with self.assertRaises(worker.DotenvError) as caught:
            worker.read_dotenv_account(path)
        self.assertIn('ATOMICDB_PASSWORD', str(caught.exception))

    def test_the_current_directory_wins_over_the_script_folder(self):
        cwd = self.root / 'cwd'
        script = self.root / 'script'
        cwd.mkdir()
        script.mkdir()
        (script / '.env').write_text('x')
        self.assertEqual(worker.find_dotenv('', [cwd / '.env',
                                                 script / '.env']),
                         script / '.env')
        (cwd / '.env').write_text('x')
        self.assertEqual(worker.find_dotenv('', [cwd / '.env',
                                                 script / '.env']),
                         cwd / '.env')

    def test_no_file_names_where_it_looked(self):
        candidates = [self.root / 'a' / '.env', self.root / 'b' / '.env']
        with self.assertRaises(worker.DotenvError) as caught:
            worker.find_dotenv('', candidates)
        self.assertIn(str(candidates[0]), str(caught.exception))
        self.assertIn(str(candidates[1]), str(caught.exception))
        with self.assertRaises(worker.DotenvError) as caught:
            worker.find_dotenv(str(self.root / 'nope.env'))
        self.assertIn('nope.env', str(caught.exception))

    def test_the_default_paths_are_cwd_then_the_script(self):
        paths = worker.default_dotenv_paths()
        self.assertEqual(paths[0], pathlib.Path.cwd() / '.env')
        self.assertEqual(paths[-1].parent,
                         pathlib.Path(worker.__file__).resolve().parent)


class ResolveAccountTests(SimpleTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.cwd_env = root / 'cwd' / '.env'
        self.script_env = root / 'script' / '.env'
        self.cwd_env.parent.mkdir()
        self.script_env.parent.mkdir()
        self.candidates = [self.cwd_env, self.script_env]

    def test_no_cli_and_no_file_names_both_ways(self):
        problem, note = worker.resolve_account(_args(), self.candidates)
        self.assertIn('missing -U, -P', problem)
        self.assertIn('command line', problem)
        self.assertIn('ATOMICDB_USER and ATOMICDB_PASSWORD in a .env file',
                      problem)
        self.assertIn('--dotenv PATH', problem)
        self.assertIn(str(self.cwd_env), problem)
        self.assertIsNone(note)

    def test_the_command_line_alone_still_works(self):
        a = _args('alice', 'pw')
        self.assertEqual(worker.resolve_account(a, self.candidates),
                         (None, None))
        self.assertEqual((a.U, a.P), ('alice', 'pw'))

    def test_the_automatic_file_makes_the_flags_optional(self):
        self.script_env.write_text('ATOMICDB_USER=alice\n'
                                   f'ATOMICDB_PASSWORD="{SECRET}"\n')
        a = _args()
        problem, note = worker.resolve_account(a, self.candidates)
        self.assertIsNone(problem)
        self.assertEqual((a.U, a.P), ('alice', SECRET))
        self.assertEqual(note,
                         f'note: account alice read from {self.script_env}')

    def test_the_current_directory_file_wins(self):
        self.script_env.write_text('ATOMICDB_USER=script\n'
                                   'ATOMICDB_PASSWORD=s\n')
        self.cwd_env.write_text('ATOMICDB_USER=cwd\nATOMICDB_PASSWORD=c\n')
        a = _args()
        worker.resolve_account(a, self.candidates)
        self.assertEqual((a.U, a.P), ('cwd', 'c'))

    def test_the_command_line_overrides_the_file_and_says_so(self):
        self.cwd_env.write_text('ATOMICDB_USER=file-user\n'
                                f'ATOMICDB_PASSWORD="{SECRET}"\n')
        a = _args('cli', 'cli-pass')
        problem, note = worker.resolve_account(a, self.candidates)
        self.assertIsNone(problem)
        self.assertEqual((a.U, a.P), ('cli', 'cli-pass'))
        self.assertEqual(note, f'note: -U, -P on the command line override '
                               f'{self.cwd_env}')
        self.assertNotIn(SECRET, note)

    def test_a_partial_file_and_a_flag_complete_each_other(self):
        self.cwd_env.write_text(f'ATOMICDB_PASSWORD="{SECRET}"\n')
        a = _args(U='alice')
        problem, note = worker.resolve_account(a, self.candidates)
        self.assertIsNone(problem)
        self.assertEqual((a.U, a.P), ('alice', SECRET))
        self.assertIn('ATOMICDB_PASSWORD read from', note)
        self.assertNotIn(SECRET, note)

    def test_a_partial_file_alone_names_the_missing_key_and_file(self):
        self.cwd_env.write_text(f'ATOMICDB_PASSWORD="{SECRET}"\n')
        problem, _ = worker.resolve_account(_args(), self.candidates)
        self.assertIn('missing -U', problem)
        self.assertIn(f'{self.cwd_env} has no ATOMICDB_USER', problem)
        self.assertNotIn(SECRET, problem)

    def test_explicit_dotenv_and_the_flags_are_exclusive(self):
        for a in (_args(U='alice', dotenv=''), _args(P='pw', dotenv='x')):
            problem, _ = worker.resolve_account(a, self.candidates)
            self.assertIn('cannot be combined', problem)

    def test_explicit_dotenv_needs_both_keys(self):
        other = self.cwd_env.parent / 'other.env'
        other.write_text('ATOMICDB_USER=alice\n')
        problem, _ = worker.resolve_account(_args(dotenv=str(other)),
                                            self.candidates)
        self.assertIn(f'{other} has no ATOMICDB_PASSWORD', problem)

    def test_the_environment_is_never_read(self):
        with mock.patch.dict(os.environ, {'ATOMICDB_USER': 'env',
                                          'ATOMICDB_PASSWORD': 'env'}):
            problem, _ = worker.resolve_account(_args(), self.candidates)
        self.assertIn('missing -U, -P', problem)


class MainCredentialTests(SimpleTestCase):

    def run_main(self, *flags, candidates=None):
        argv = ['atomicdb_worker.py', '--engine', 'my-engine',
                '--no-auto-update', '--no-fetch-syzygy', '-T', '1',
                '--jobs', '1', '--hash', '16', *flags]
        with mock.patch.object(worker.sys, 'argv', argv), \
                mock.patch.object(worker, 'default_dotenv_paths',
                                  return_value=list(candidates or [])), \
                mock.patch.object(worker, 'check_engine_identity',
                                  return_value=None), \
                mock.patch.object(worker, '_file_sha256', return_value=''), \
                mock.patch.object(worker, '_net_sha256', return_value=''), \
                mock.patch.object(worker, 'memory_mb',
                                  return_value=(65536, 32768)), \
                mock.patch.object(worker, '_analysis_slot') as slot, \
                redirect_stdout(io.StringIO()) as out, \
                redirect_stderr(io.StringIO()) as err:
            try:
                worker.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, slot, out.getvalue(), err.getvalue()

    def auth_of(self, slot):
        return next(arg for arg in slot.call_args.args
                    if isinstance(arg, dict) and 'password' in arg)

    def test_a_bare_dotenv_finds_the_file_and_never_prints_the_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = pathlib.Path(tmp) / '.env'
            env.write_text('ATOMICDB_USER=alice\n'
                           f'ATOMICDB_PASSWORD="{SECRET}"\n')
            code, slot, out, err = self.run_main('--dotenv',
                                                 candidates=[env])
        self.assertEqual(code, 0)
        auth = self.auth_of(slot)
        self.assertEqual((auth['username'], auth['password']),
                         ('alice', SECRET))
        self.assertEqual(slot.call_args.args[1].S,
                         'https://belzedar.duckdns.org')
        self.assertIn(f'note: account alice read from {env}', out)
        self.assertNotIn(SECRET, out + err)

    def test_dotenv_with_a_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = pathlib.Path(tmp) / 'worker.env'
            env.write_text('ATOMICDB_USER=bob\nATOMICDB_PASSWORD=pw\n')
            code, slot, _, _ = self.run_main('--dotenv', str(env),
                                             '-S', 'https://other')
        self.assertEqual(code, 0)
        self.assertEqual(self.auth_of(slot)['username'], 'bob')
        self.assertEqual(slot.call_args.args[1].S, 'https://other')

    def test_dotenv_with_U_is_refused(self):
        code, slot, _, err = self.run_main('--dotenv', '-U', 'alice')
        self.assertEqual(code, 2)
        self.assertEqual(slot.call_count, 0)
        self.assertIn('--dotenv cannot be combined with -U or -P', err)

    def test_no_account_at_all_is_refused(self):
        code, slot, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertEqual(slot.call_count, 0)
        self.assertIn('missing -U, -P', err)
        self.assertIn('--dotenv PATH', err)

    def test_the_automatic_env_file_needs_no_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = pathlib.Path(tmp) / '.env'
            env.write_text('ATOMICDB_USER=alice\n'
                           f'ATOMICDB_PASSWORD="{SECRET}"\n')
            code, slot, out, err = self.run_main(candidates=[env])
        self.assertEqual(code, 0)
        auth = self.auth_of(slot)
        self.assertEqual((auth['username'], auth['password']),
                         ('alice', SECRET))
        self.assertIn(f'note: account alice read from {env}', out)
        self.assertNotIn(SECRET, out + err)

    def test_the_command_line_wins_over_the_automatic_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = pathlib.Path(tmp) / '.env'
            env.write_text('ATOMICDB_USER=alice\n'
                           f'ATOMICDB_PASSWORD="{SECRET}"\n')
            code, slot, out, err = self.run_main('-U', 'cli', '-P', 'cp',
                                                 candidates=[env])
        self.assertEqual(code, 0)
        auth = self.auth_of(slot)
        self.assertEqual((auth['username'], auth['password']), ('cli', 'cp'))
        self.assertIn(f'-U, -P on the command line override {env}', out)
        self.assertNotIn(SECRET, out + err)

    def test_a_missing_key_stops_before_anything_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = pathlib.Path(tmp) / '.env'
            env.write_text(f'ATOMICDB_PASSWORD="{SECRET}"\n')
            code, slot, out, err = self.run_main('--dotenv', str(env))
        self.assertEqual(code, 2)
        self.assertEqual(slot.call_count, 0)
        self.assertIn('has no ATOMICDB_USER', err)
        self.assertNotIn(SECRET, out + err)

    def test_the_command_line_account_still_works(self):
        code, slot, out, _ = self.run_main('-U', 'cli', '-P', 'cli-pass')
        self.assertEqual(code, 0)
        self.assertEqual(self.auth_of(slot)['username'], 'cli')
        self.assertNotIn('read from', out)
