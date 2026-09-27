"""Synthetic native responses only: no Photos, network or personal fixtures."""
import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import photos_pipeline as p
import photos_recover as recovery


def synthetic(root):
    source = root / 'source.jpeg'
    source.write_bytes(b'synthetic original resource')
    row = {'album_id': 'source-album', 'pic_key': 'source-item', 'album': 'synthetic',
           'path': str(source), 'target_epoch': 123, 'target_iso': '1970-01-01T00:02:03+00:00',
           'import_sha256': p.sha(source), 'file_type': 'JPEG', 'status': 'capture_ready',
           'media_payload_unchanged': True}
    asset = {'asset_id': 'synthetic-asset/L0/001', 'filename': source.name,
             'import_sha256': row['import_sha256'], 'task_album': p.task_album(row),
             'album_id': 'photos-album', 'in_album': True, 'epoch_seconds': 123}
    return row, asset


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.row = {'album_id': 'source-album', 'pic_key': 'source-item', 'album': 'synthetic'}
        self.assets = {p.key(self.row): {'asset_id': 'synthetic-asset', 'album_id': 'photos-album',
                                      'task_album': p.task_album(self.row)}}
        self.request = p.membership_request([self.row], self.assets)
        self.result = {'ok': True, 'albums': [{'task_album': p.task_album(self.row),
            'album_id': 'photos-album', 'member_ids': ['synthetic-asset'], 'count': 1,
            'expected_count': 1, 'missing': [], 'extra': []}]}

    def test_current_exact_members_are_preserved(self):
        current, pending = p.classify_membership(self.result, self.request, self.assets)
        self.assertEqual(current, self.assets)
        self.assertEqual(pending, [])

    def test_missing_asset_is_pending_and_never_in_active_ledger(self):
        self.result['albums'][0].update(member_ids=[], count=0, missing=['synthetic-asset'])
        current, pending = p.classify_membership(self.result, self.request, self.assets)
        self.assertEqual(current, {})
        self.assertEqual(pending[0]['source_key'], p.key(self.row))
        self.assertTrue(pending[0]['requires_user_decision'])

    def test_extra_asset_or_replaced_album_blocks_finalization(self):
        for changed in ({'member_ids': ['synthetic-asset', 'unrelated'], 'count': 2, 'extra': ['unrelated']},
                        {'album_id': 'replacement'}):
            value = copy.deepcopy(self.result)
            value['albums'][0].update(changed)
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                p.classify_membership(value, self.request, self.assets)

    def test_dropped_album_or_faked_difference_is_rejected(self):
        for value in ({'ok': True, 'albums': []},
                      {'ok': True, 'albums': [{**self.result['albums'][0], 'missing': ['synthetic-asset']}]}):
            with self.assertRaises(RuntimeError):
                p.classify_membership(value, self.request, self.assets)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.batch = self.root / 'batch-00001'
        self.row, self.asset = synthetic(self.root)
        p.save(self.batch / 'pending.json', {'operation': 'import', 'source_keys': [p.key(self.row)],
                                            'prepared_rows': [self.row]})
        self.group = {'task_album': p.task_album(self.row), 'returned': [
            {'asset_id': self.asset['asset_id'], 'filename': self.asset['filename']}]}
        p.save(self.batch / 'import-result.json', {'imports': [self.group]})

    def tearDown(self):
        self.temp.cleanup()

    def test_default_plan_never_calls_native_or_writes_completion(self):
        with patch.object(p, 'native', side_effect=AssertionError('Native call during plan')):
            plan = recovery.recover(self.batch, Path('synthetic-helper'))
        self.assertFalse(plan['apply'])
        self.assertFalse((self.batch / 'completed.json').exists())
        self.assertFalse(list(self.batch.glob('recovery-*')))

    def test_incomplete_return_ids_block_before_any_native_call(self):
        p.save(self.batch / 'import-result.json', {'imports': []})
        with patch.object(p, 'native', side_effect=AssertionError('Unsafe native call')):
            with self.assertRaisesRegex(RuntimeError, 'Incomplete/nonunique returned IDs'):
                recovery.recover(self.batch, Path('synthetic-helper'), apply=True)
        self.assertFalse((self.batch / 'completed.json').exists())

    def test_crash_after_logged_return_can_recover_complete_ids(self):
        (self.batch / 'import-result.json').unlink()
        (self.batch / 'import-stdout.json').write_text('')
        (self.batch / 'import-stderr.log').write_text(json.dumps({'event': 'import_returned', 'group': self.group}) + '\n')
        rows, _, imported = recovery.recovery_plan(self.batch)
        self.assertEqual(len(rows), 1)
        self.assertEqual(imported['imports'], [self.group])

    def test_conflicting_return_snapshots_are_not_guessed(self):
        changed = copy.deepcopy(self.group)
        changed['returned'][0]['asset_id'] = 'different-asset'
        p.save(self.batch / 'returned-import-groups.json', [changed])
        with self.assertRaisesRegex(RuntimeError, 'Conflicting'):
            recovery.recovery_plan(self.batch)

    def test_verify_batch_requires_explicit_known_ids(self):
        pending = p.read(self.batch / 'pending.json')
        pending['operation'] = 'verify'
        p.save(self.batch / 'pending.json', pending)
        with self.assertRaisesRegex(RuntimeError, 'lacks matching known IDs'):
            recovery.recovery_plan(self.batch)
        pending['known_assets'] = {p.key(self.row): self.asset}
        p.save(self.batch / 'pending.json', pending)
        _, assets, imported = recovery.recovery_plan(self.batch)
        self.assertIsNone(imported)
        self.assertEqual(assets[p.key(self.row)]['asset_id'], self.asset['asset_id'])

    def run_recovery(self, filename='source.jpeg', wrong_date=False, wrong_hash=False):
        calls = []
        def native(helper, directory, operation, requests, extra=None):
            self.assertIn(operation, ('verify', 'export'))
            calls.append(operation)
            results = []
            for item in requests:
                if operation == 'export':
                    Path(item['export_dir'], filename).write_bytes(
                        b'wrong resource' if wrong_hash else Path(self.row['path']).read_bytes())
                results.append({**self.asset, 'filename': filename,
                    'epoch_seconds': 124 if wrong_date else 123})
            return {'ok': True, 'results': results, 'album_counts': []}
        with patch.object(p, 'native', native):
            result = recovery.recover(self.batch, Path('synthetic-helper'), apply=True)
        return result, calls

    def test_apply_uses_only_existing_ids_and_real_export_proof(self):
        result, calls = self.run_recovery()
        self.assertTrue(result['completed'])
        self.assertEqual(calls, ['verify', 'export', 'verify'])
        completed = p.read(self.batch / 'completed.json')
        reference = completed['export_proof_references'][p.key(self.row)]
        self.assertEqual(p.read(Path(reference))['verified'][p.key(self.row)]['export_sha256'],
                         self.row['import_sha256'])

    def test_new_filename_alias_requires_exported_original_bytes(self):
        _, calls = self.run_recovery(filename='new-resource-alias.jpeg')
        self.assertEqual(calls, ['verify', 'export', 'export', 'verify'])
        value = p.read(self.batch / 'completed.json')['assets'][p.key(self.row)]
        self.assertEqual(value['filename'], 'new-resource-alias.jpeg')
        self.assertEqual(value['filename_alias_evidence']['export_sha256'], self.row['import_sha256'])

    def test_wrong_date_cannot_create_completed_proof(self):
        with self.assertRaises(RuntimeError):
            self.run_recovery(wrong_date=True)
        self.assertFalse((self.batch / 'completed.json').exists())

    def test_wrong_alias_resource_cannot_create_completed_proof(self):
        with self.assertRaises(RuntimeError):
            self.run_recovery(filename='new-resource-alias.jpeg', wrong_hash=True)
        self.assertFalse((self.batch / 'completed.json').exists())

    def test_completed_flag_without_real_export_evidence_is_rejected(self):
        p.save(self.batch / 'completed.json', {'export_verified': True,
            'assets': {p.key(self.row): self.asset}})
        p.save(self.batch / 'export-proof.json', {'all_passed': True, 'verified': {}})
        with self.assertRaises(RuntimeError):
            p.resolve_export_reference(self.batch / 'completed.json', p.key(self.row), self.asset)


class FinalLedgerTests(unittest.TestCase):
    def test_missing_after_batch_is_excluded_with_real_proof_for_remaining(self):
        for missing in (False, True):
            with self.subTest(missing_after_batch=missing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                row, asset = synthetic(root)
                manifest, journal = root / 'manifest.json', root / 'journal'
                p.save(manifest, [row])

                def native(helper, batch_dir, operation, requests, extra=None):
                    if operation == 'import':
                        return {'ok': True, 'imports': [{'task_album': p.task_album(row), 'returned': [asset]}]}
                    if operation == 'export':
                        for request in requests:
                            Path(request['export_dir'], 'source.jpeg').write_bytes(Path(row['path']).read_bytes())
                    return {'ok': True, 'results': [asset], 'album_counts': []}

                def audit(journal, rows, assets, helper):
                    request = p.membership_request(rows, assets)
                    result = {'ok': True, 'checked_at_utc': 'synthetic-check', 'albums': [{
                        'task_album': p.task_album(row), 'album_id': asset['album_id'],
                        'member_ids': [] if missing else [asset['asset_id']], 'count': 0 if missing else 1,
                        'expected_count': 1, 'missing': [asset['asset_id']] if missing else [], 'extra': []}]}
                    current, pending = p.classify_membership(result, request, assets)
                    proof = journal / 'membership-proof.json'
                    p.save(proof, result)
                    return current, pending, proof, result

                argv = ['photos_pipeline.py', str(manifest), '--journal', str(journal),
                        '--seed', str(root / 'no-seed.json'), '--apply', '--export-verify']
                with patch.object(sys, 'argv', argv), patch.object(p, 'native', native), \
                        patch.object(p, 'final_membership_audit', audit), redirect_stdout(io.StringIO()):
                    p.main()
                ledger = p.read(journal / 'verified-ledger.json')
                self.assertEqual(len(ledger['assets']), 0 if missing else 1)
                self.assertEqual(len(ledger['pending_recovery']), 1 if missing else 0)
                self.assertEqual(ledger['entire_collection_complete'], not missing)
                self.assertTrue(Path(ledger['final_membership_proof']).is_file())
                if not missing:
                    reference = Path(ledger['export_proof_references'][p.key(row)])
                    self.assertEqual(p.read(reference)['verified'][p.key(row)]['export_sha256'], row['import_sha256'])


if __name__ == '__main__':
    unittest.main()
