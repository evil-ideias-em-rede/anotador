import io
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from progresso import Waiting, progress, task_label


class ProgressTests(unittest.TestCase):
    def test_heartbeat_while_operation_is_blocked_and_thread_stops(self):
        heartbeat = threading.Event()
        class Stream(io.StringIO):
            def write(self, text):
                result = super().write(text)
                if self.getvalue().count('aguardando') >= 2:
                    heartbeat.set()
                return result
        output = Stream()
        with Waiting('API', stream=output, interval=0.01) as waiting:
            self.assertTrue(heartbeat.wait(2), 'Sem feedback durante a espera')
        self.assertFalse(waiting.thread.is_alive())
        self.assertIn('concluído', output.getvalue())
        self.assertNotIn('\033', output.getvalue())

    def test_failure_cleans_spinner_and_propagates_error(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        output = Terminal()
        with self.assertRaisesRegex(ValueError, 'teste'):
            with Waiting('API', stream=output) as waiting:
                raise ValueError('teste')
        self.assertFalse(waiting.thread.is_alive())
        self.assertIn('\r\033[2K', output.getvalue())
        self.assertIn('interrompido', output.getvalue())
        self.assertTrue(output.getvalue().endswith('\n'))

    def test_progress_counts_completed_items(self):
        output = io.StringIO()
        with patch('sys.stderr', output):
            progress('Falas', 3, 10)
        self.assertIn('3/10 (30%)', output.getvalue())
        self.assertNotIn('100%', output.getvalue())

    def test_label_excludes_input_content_and_credentials(self):
        label = task_label('anotacao', {'dimensao': {'id': 2}, 'token': 'SECRET',
                                       'fala': {'texto': 'PRIVATE'}, 'participante': 'PRIVATE'})
        self.assertIn('2/4', label)
        self.assertNotIn('SECRET', label)
        self.assertNotIn('PRIVATE', label)
