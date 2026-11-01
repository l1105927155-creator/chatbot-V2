"""Paid experiment recovery must never replay an existing output."""
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


spec = importlib.util.spec_from_file_location(
    'reply_habits_matrix', Path(__file__).resolve().parents[1] / 'scripts/reply-habits-matrix.py')
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


class RecoveryEvidenceTest(unittest.TestCase):
    def test_existing_output_requires_successful_cleanup(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertFalse(matrix.completed_result(directory))
            (directory / 'result.json').write_text('{}')
            with self.assertRaises(RuntimeError):
                matrix.completed_result(directory)
            (directory / 'cleanup-result.json').write_text(json.dumps({'success': False}))
            with self.assertRaises(RuntimeError):
                matrix.completed_result(directory)
            (directory / 'cleanup-result.json').write_text(json.dumps({'success': True}))
            self.assertTrue(matrix.completed_result(directory))
            (directory / 'cleanup-failure.json').write_text('{}')
            with self.assertRaises(RuntimeError):
                matrix.completed_result(directory)


if __name__ == '__main__':
    unittest.main()
