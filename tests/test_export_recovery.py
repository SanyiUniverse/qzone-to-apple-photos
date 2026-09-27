"""Synthetic export recovery checks; never calls Photos or accesses accounts."""
import itertools
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import photos_pipeline as pipeline


class ExportRecoveryTests(unittest.TestCase):
    def test_reused_proof_requires_exact_current_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / 'completed.json'
            reference.write_text('{}')
            row = {'album_id': 'demo-album', 'pic_key': 'demo-photo', 'album': 'demo',
                   'path': '/synthetic/example.jpeg', 'target_epoch': 123,
                   'import_sha256': 'synthetic-digest'}
            current = {'qq_album_id': row['album_id'], 'pic_key': row['pic_key'],
                'asset_id': 'synthetic-asset', 'import_sha256': row['import_sha256'],
                'epoch_seconds': 123, 'album_id': 'synthetic-photos-album',
                'task_album': pipeline.task_album(row), 'filename': 'example.jpeg', 'in_album': True}
            proof = pipeline.export_proof_record(pipeline.key(row), current, reference)
            self.assertTrue(pipeline.reusable_export_proof(row, current, proof))
            for field in ('source_key', 'qq_album_id', 'pic_key', 'asset_id', 'import_sha256',
                          'epoch_seconds', 'album_id', 'task_album', 'filename', 'in_album'):
                with self.subTest(changed_proof_field=field):
                    changed = {**proof, field: False if field == 'in_album' else 'changed'}
                    self.assertFalse(pipeline.reusable_export_proof(row, current, changed))
            for field in ('asset_id', 'import_sha256', 'epoch_seconds', 'album_id',
                          'task_album', 'filename', 'in_album'):
                with self.subTest(changed_native_field=field):
                    changed = {**current, field: False if field == 'in_album' else 'changed'}
                    self.assertFalse(pipeline.reusable_export_proof(row, changed, proof))
            reference.unlink()
            self.assertFalse(pipeline.reusable_export_proof(row, current, proof))

    def run_case(self, case):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'synthetic.dat'
            source.write_bytes(b'synthetic bytes')
            row = {'album_id': 'demo-album', 'pic_key': 'demo-photo', 'album': 'demo',
                   'path': str(source), 'target_epoch': 123,
                   'import_sha256': pipeline.sha(source)}
            assets = {pipeline.key(row): {'asset_id': 'synthetic-asset/L0/001'}}
            calls = []

            def native(helper, journal, operation, requests, extra=None):
                self.assertEqual(operation, 'export')
                calls.append(requests)
                if case == 'wrong_hash' or (case == 'missing_then_success' and len(calls) == 2):
                    payload = b'different bytes' if case == 'wrong_hash' else source.read_bytes()
                    Path(requests[0]['export_dir'], 'export.dat').write_bytes(payload)
                return {'ok': True, 'results': [{'asset_id': requests[0]['asset_id'],
                        'in_album': True, 'epoch_seconds': 123}]}

            with patch.object(pipeline, 'native', native), \
                    patch.object(pipeline.time, 'monotonic', side_effect=itertools.count(0, 31)), \
                    patch.object(pipeline.time, 'sleep'):
                if case == 'missing_then_success':
                    pipeline.export_verify(Path('synthetic-helper'), root / 'journal', [row], assets)
                else:
                    with self.assertRaises(RuntimeError):
                        pipeline.export_verify(Path('synthetic-helper'), root / 'journal', [row], assets)
            proof = pipeline.read(root / 'journal' / 'export-proof.json')
            self.assertEqual(proof['all_passed'], case == 'missing_then_success')
            self.assertEqual(len(calls), 1 if case == 'wrong_hash' else 2)
            if len(calls) == 2:
                self.assertNotEqual(calls[0][0]['export_dir'], calls[1][0]['export_dir'])

    def test_missing_export_retries_once_to_fresh_directory(self):
        self.run_case('missing_then_success')

    def test_wrong_hash_is_never_retried_or_accepted(self):
        self.run_case('wrong_hash')

    def test_empty_success_responses_never_count_as_proof(self):
        self.run_case('always_missing')


if __name__ == '__main__':
    unittest.main()
