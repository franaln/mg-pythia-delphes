"""Native shell completion executes no workflow and follows the argparse CLI."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from mgpd.cli import _parser, main
from mgpd.completion import shell_completion, command_parsers


class CompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bash = self.root / 'completion.bash'
        self.zsh = self.root / 'completion.zsh'
        self.bash.write_text(shell_completion(_parser(), 'bash'))
        self.zsh.write_text(shell_completion(_parser(), 'zsh'))

    def bash_candidates(self, *words):
        script = 'source ' + shlex.quote(str(self.bash)) + '\n'
        script += 'COMP_WORDS=(' + ' '.join(shlex.quote(word) for word in words) + ')\n'
        script += f'COMP_CWORD={len(words)-1}\n_mgpd_complete\nprintf "%s\\0" "${{COMPREPLY[@]}}"\n'
        result = subprocess.run(['bash', '--noprofile', '--norc', '-c', script], cwd=self.root,
                                capture_output=True, check=True)
        return [value.decode() for value in result.stdout.split(b'\0') if value]

    def test_bash_commands_and_every_subcommand_option(self):
        commands, _ = command_parsers(_parser())
        self.assertEqual(set(self.bash_candidates('mgpd', '')), set(commands) | {'-h', '--help', '-v', '--version'})
        for command, parser in commands.items():
            expected = {option for action in parser._actions for option in action.option_strings}
            with self.subTest(command=command):
                self.assertEqual(set(self.bash_candidates('mgpd', command, '-')), expected)
        self.assertEqual(self.bash_candidates('mgpd', 'cre'), ['create'])

    def test_bash_choices_equals_and_free_values(self):
        for words, expected in [
            (['create', '--level', 'dete'], ['detector']),
            (['create', '--template', ''], ['proc']),
            (['create', '--mode', 'local-a'], ['local-apptainer']),
            (['create', '--mode=local-a'], ['--mode=local-apptainer']),
            (['create', '--mode', '=', 'local-a'], ['local-apptainer']),
            (['create', '--mode', '='], ['local', 'local-docker', 'local-apptainer', 'condor', 'jupiter']),
            (['merge', '--event-count', 'non'], ['nonempty']),
            (['completion', 'z'], ['zsh']),
            (['analyze', '--example', 'lhe'], ['lhe-dileptons']),
            (['create', '--njobs', ''], []),
            (['create', '--process', ''], []),
        ]:
            with self.subTest(words=words):
                self.assertEqual(self.bash_candidates('mgpd', *words), expected)

    def test_bash_paths_preserve_spaces_and_filter_directories(self):
        (self.root / 'input folder').mkdir()
        (self.root / 'input.yml').write_text('')
        (self.root / 'input archive.tar.gz').write_text('')
        self.assertEqual(self.bash_candidates('mgpd', 'create', '--copy', 'in'), ['input folder'])
        self.assertEqual(set(self.bash_candidates('mgpd', 'prepare', 'in')),
                         {'input folder', 'input.yml', 'input archive.tar.gz'})
        self.assertIn('input.yml', self.bash_candidates('mgpd', 'validate-config', 'in'))
        self.assertEqual(self.bash_candidates('mgpd', 'status', 'in'), ['input folder'])
        self.assertEqual(self.bash_candidates('mgpd', 'analyze', '--from-results', 'in'), ['input folder'])
        self.assertIn('input.yml', self.bash_candidates('mgpd', 'analyze', '--config', 'in'))
        self.assertIn('--mode', self.bash_candidates('mgpd', 'prepare', 'input.yml', ''))
        self.assertIn('input.yml', self.bash_candidates('mgpd', 'prepare', '--mode', 'condor', 'in'))
        self.assertIn('input.yml', self.bash_candidates('mgpd', 'merge', 'input.yml', 'in'))
        self.assertEqual(self.bash_candidates('mgpd', 'prepare', '--', '--'), [])
        self.assertEqual(self.bash_candidates('mgpd', 'prepare', '--output', 'in'), ['input folder'])
        self.assertIn('input archive.tar.gz', self.bash_candidates('mgpd', 'merge', 'in'))

    def test_cli_generates_scripts_without_running_workflows(self):
        with patch('mgpd.cli.prepare_project', side_effect=AssertionError('No preparation')), \
                patch('mgpd.cli.execute_production', side_effect=AssertionError('No execution')):
            for shell in ('bash', 'zsh'):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(['completion', shell]), 0)
                self.assertEqual(output.getvalue(), shell_completion(_parser(), shell))
                self.assertNotIn('==SUPPRESS==', output.getvalue())
                executable = shutil.which(shell)
                if executable:
                    result = subprocess.run([executable, '-n'], input=output.getvalue(), capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaises(SystemExit):
            main(['completion', 'fish'])

    @unittest.skipUnless(shutil.which('zsh'), 'Zsh is not installed')
    def test_zsh_real_interactive_tab_completion(self):
        pid, terminal = pty.fork()
        if pid == 0:
            os.chdir(self.root)
            os.execv(shutil.which('zsh'), ['zsh', '-f'])
        def read(seconds, until=None):
            output = ''
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if select.select([terminal], [], [], min(.03, max(0, deadline-time.monotonic())))[0]:
                    try:
                        output += os.read(terminal, 65536).decode(errors='replace')
                    except OSError:
                        break
                if until and until in output:
                    break
            return output
        try:
            read(.15)
            setup = (f'autoload -Uz compinit; compinit -D; source {shlex.quote(str(self.zsh))}; '
                     "KEYTIMEOUT=1; bindkey -e; function _mgpd_dump { print -r -- __BUFFER__${BUFFER}__END__; }; "
                     "zle -N _mgpd_dump; bindkey '^X' _mgpd_dump; bindkey '^U' kill-whole-line; print __READY__\n")
            os.write(terminal, setup.encode())
            self.assertIn('__READY__\r\n', read(10, '__READY__\r\n'))
            read(.05)
            (self.root / 'sample project').mkdir()
            cases = [('mgpd cre', 'mgpd create '),
                     ('mgpd create --level dete', 'mgpd create --level detector '),
                     ('mgpd create --mode local-a', 'mgpd create --mode local-apptainer '),
                     ('mgpd merge --event-count non', 'mgpd merge --event-count nonempty '),
                     ('mgpd completion z', 'mgpd completion zsh '),
                     ('mgpd create --copy sam', 'mgpd create --copy sample\\ project/')]
            for text, expected in cases:
                with self.subTest(text=text):
                    os.write(terminal, b'\x15' + text.encode()); read(.05)
                    os.write(terminal, b'\t'); read(.2)
                    os.write(terminal, b'\x18')
                    output = read(2, '__END__')
                    match = re.search('__BUFFER__(.*?)__END__', output)
                    self.assertIsNotNone(match, output)
                    self.assertEqual(match.group(1), expected)
        finally:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            os.close(terminal)
