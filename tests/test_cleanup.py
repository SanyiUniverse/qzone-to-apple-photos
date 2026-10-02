# SPDX-License-Identifier: GPL-3.0-only
"""Synthetic cleanup checks. Never opens Photos, logs in, or contacts QQ."""
import contextlib
import datetime as dt
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cleanup_adapter as adapter
import cleanup_plan as planner
import migrate_session as migration
import requests


class Fixture:
    def __init__(self, root):
        self.root, self.records = root, root / '迁移记录'
        self.prepared = self.records / 'prepared.json'
        self.ledger = self.records / 'Photos批量导入/verified-ledger.json'
        self.target = self.records / 'QQ清理核验/精确白名单.json'
        self.album_id, self.album = 'synthetic-album', '迁移QQ相册之“合成相册”'
        self.rows, self.raw = [], {}
        epoch = int(dt.datetime(2020, 1, 2, tzinfo=dt.timezone.utc).timestamp())
        for pic, status in [('verified', 'capture_ready'), ('held', 'capture_missing'),
                            ('missing-asset', 'capture_ready')]:
            original = root / '原始备份' / pic / 'example.jpeg'
            copied = root / '导入副本' / pic / 'example.jpeg'
            original.parent.mkdir(parents=True)
            copied.parent.mkdir(parents=True)
            original.write_bytes(('synthetic-original-' + pic).encode())
            copied.write_bytes(('synthetic-import-' + pic).encode())
            original.chmod(0o444)
            row = dict(album_id=self.album_id, pic_key=pic, album=self.album, status=status,
                file_type='JPEG', path=str(copied), original_path=str(original),
                original_sha256=planner.sha(original), import_sha256=planner.sha(copied),
                target_epoch=epoch, target_iso='2020-01-02T00:00:00+00:00',
                media_payload_unchanged=True)
            self.rows.append(row)
            raw = dict(lloc=pic, name='example', is_video=False, width=10, height=20,
                       exif={'originalTime': '2020-01-02 00:00:00'}, uploadtime=epoch)
            self.raw[pic] = raw
            planner.save(self.records / 'downloads' / (pic + '-download.json'),
                dict(album=dict(album_id=self.album_id, source_name='合成相册'), pic_key=pic,
                     raw_fields=raw, original_name='example.jpeg', is_video=False,
                     path=str(original.relative_to(root)), bytes=original.stat().st_size,
                     sha256=row['original_sha256']))
        self.source_key = planner.key(self.rows[0])
        asset = dict(asset_id='synthetic-photos-asset', qq_album_id=self.album_id,
            pic_key='verified', task_album=self.album, album_id='synthetic-photos-album',
            in_album=True, epoch_seconds=epoch, import_sha256=self.rows[0]['import_sha256'],
            filename='example.jpeg')
        self.asset = asset
        membership = self.records / 'membership.json'
        proof = self.records / 'export-proof.json'
        planner.save(membership, dict(ok=True, checked_at_utc='2020-01-03T00:00:00Z', albums=[dict(
            task_album=self.album, album_id=asset['album_id'], count=1, expected_count=2,
            missing=['synthetic-missing-asset'], extra=[], member_ids=[asset['asset_id']])]))
        planner.save(proof, dict(all_passed=True, problems=[], verified={self.source_key: dict(
            asset_id=asset['asset_id'], export_sha256=asset['import_sha256'], epoch_seconds=epoch,
            in_album=True)}))
        planner.save(self.ledger, dict(summary={'verified_assets': 1}, assets={self.source_key: asset},
            excluded_items=[], pending_recovery=[{'source_key': planner.key(self.rows[2]),
                                                  'asset_id': 'synthetic-missing-asset'}],
            final_membership_proof=str(membership), export_proof_references={self.source_key: str(proof)}))
        planner.save(self.prepared, self.rows)
        planner.save(self.records / 'source-account.json', {'owner': '12345'})
        planner.save(self.records / '相册映射.json', {self.album_id: dict(album_id=self.album_id,
            source_name='合成相册', source_count=3, photos_album=self.album)})

    def build(self):
        return planner.build_plan(self.root, self.prepared, self.ledger, self.target)

    @contextlib.contextmanager
    def configured(self):
        with patch.multiple(adapter, OUT=self.root, RECORDS=self.records, PRIVATE=self.target.parent,
                WHITELIST=self.target, API_JOURNAL=self.target.parent / 'API批次',
                ITEM_LOG=self.target.parent / '逐项清理记录.json'):
            yield

    def session(self):
        fixture = self
        class FakeQQ:
            user_qq = '12345'
            cookies = {'uin': 'o00012345'}
            total_albums = 1
            _cleanup_authorized = True
            _cleanup_recycle_confirmed = True
            def __init__(self):
                self.current = dict(fixture.raw)
            def get_albums_by_page(self, owner):
                self._cleanup_album_raw = {fixture.album_id: {'priv': 3}}
                return [SimpleNamespace(uid=fixture.album_id, name='合成相册', count=len(self.current))]
            def get_photos_from_album(self, owner, album):
                self._cleanup_rows = list(self.current.values())
                return [SimpleNamespace(pic_key=pic) for pic in self.current]
        return FakeQQ()

    def command(self):
        return next(planner.planned_commands(planner.read(self.target), {}))


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(Path(self.temp.name))

    def test_plan_protects_held_and_missing_assets_and_never_rebuilds(self):
        plan = self.f.build()
        self.assertEqual([v['pic_key'] for v in plan['allowed']], ['verified'])
        self.assertEqual({v['pic_key'] for v in plan['keep']}, {'held', 'missing-asset'})
        self.assertFalse(plan['qq_deletion_executed'])
        with self.assertRaisesRegex(RuntimeError, 'PLAN_ALREADY_EXISTS'):
            self.f.build()

    def test_builder_rejects_export_hash_mismatch(self):
        p = self.f.records / 'export-proof.json'
        value = planner.read(p)
        value['verified'][self.f.source_key]['export_sha256'] = 'changed'
        planner.save(p, value)
        with self.assertRaisesRegex(RuntimeError, 'Export identity/hash/date mismatch'):
            self.f.build()

    def test_builder_rejects_missing_asset_in_active_ledger(self):
        p = self.f.records / 'membership.json'
        value = planner.read(p)
        value['albums'][0]['missing'].append(self.f.asset['asset_id'])
        planner.save(p, value)
        with self.assertRaisesRegex(RuntimeError, 'Missing asset in ledger'):
            self.f.build()

    def test_planner_skips_confirmed_removed_and_bounds_batches(self):
        plan = self.f.build()
        self.assertEqual(list(planner.planned_commands(plan, {self.f.source_key: {'observed_qq_absent': True}})), [])
        for size in (0, 51):
            with self.assertRaisesRegex(RuntimeError, 'BATCH_SIZE'):
                list(planner.planned_commands(plan, {}, size))

    def test_readonly_and_recycle_flags_are_independent_guards(self):
        self.f.build()
        with self.f.configured(), patch.object(adapter, 'post_existing_delete') as post:
            for attr, reason in [('_cleanup_authorized', 'DELETE_NOT_ENABLED'),
                                 ('_cleanup_recycle_confirmed', 'CURRENT_RECYCLE_SEMANTICS_NOT_CONFIRMED')]:
                session = self.f.session()
                setattr(session, attr, False)
                with self.assertRaisesRegex(RuntimeError, reason):
                    adapter.handle(session, self.f.command())
                self.assertTrue(session._cleanup_blocked)
            post.assert_not_called()

    def test_scope_digest_and_protected_ids_fail_before_post(self):
        self.f.build()
        with self.f.configured(), patch.object(adapter, 'post_existing_delete') as post:
            for mode in ('held', 'digest', 'duplicate', 'confirm'):
                command = self.f.command()
                if mode == 'held':
                    command['pic_keys'] = ['held']
                    command['expected_sha256'] = adapter.digest_ids(self.f.album_id, ['held'])
                elif mode == 'digest':
                    command['expected_sha256'] = 'wrong'
                elif mode == 'duplicate':
                    command['pic_keys'] *= 2
                else:
                    command['confirm'] = ''
                with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                    adapter.handle(self.f.session(), command)
            post.assert_not_called()

    def test_fresh_inventory_change_blocks_before_photos_and_post(self):
        self.f.build()
        with self.f.configured(), patch.object(adapter, 'native') as native, \
                patch.object(adapter, 'post_existing_delete') as post:
            session = self.f.session()
            session.current['added'] = {'lloc': 'added'}
            with self.assertRaisesRegex(RuntimeError, 'QQ_ALBUM_COUNT_CHANGED'):
                adapter.handle(session, self.f.command())
            native.assert_not_called()
            post.assert_not_called()

    def test_context_binds_frozen_preparation_and_ledger(self):
        self.f.build()
        with self.f.configured():
            adapter.context()
            self.f.prepared.write_text('[]')
            with self.assertRaisesRegex(RuntimeError, 'PREPARED_MANIFEST_CHANGED'):
                adapter.context()

    def test_binding_mismatch_blocks_native_and_post(self):
        self.f.build()
        plan = planner.read(self.f.target)
        plan['allowed'][0]['import_sha256'] = 'self-consistent-but-unapproved-digest'
        planner.save(self.f.target, plan)
        with self.f.configured(), patch.object(adapter, 'native') as native, \
                patch.object(adapter, 'post_existing_delete') as post:
            with self.assertRaisesRegex(RuntimeError, 'IMPORT_PROOF_BINDING_CHANGED'):
                adapter.handle(self.f.session(), self.f.command())
            native.assert_not_called()
            post.assert_not_called()

    def test_timeout_is_journaled_once_then_readonly_reconciliation(self):
        self.f.build()
        observed = {'ok': True, 'results': [self.f.asset]}
        session = self.f.session()
        with self.f.configured(), patch.object(adapter, 'native', return_value=observed), \
                patch.object(adapter, 'post_existing_delete', side_effect=TimeoutError) as post:
            with self.assertRaises(TimeoutError):
                adapter.handle(session, self.f.command())
            self.assertEqual(post.call_count, 1)
            self.assertEqual(len(adapter.unfinished()), 1)
            with self.assertRaisesRegex(RuntimeError, 'RECONCILE_OR_REVIEW'):
                adapter.handle(session, self.f.command())
            result = adapter.handle(session, {'cmd': 'reconcile'})
            self.assertEqual(result['still_present_count'], 1)
            self.assertEqual(result['verified_absent_count'], 0)
            self.assertEqual(post.call_count, 1)
            self.assertFalse(session._cleanup_blocked)

    def test_same_name_replaced_native_album_blocks_post(self):
        self.f.build()
        observed = {'results': [{**self.f.asset, 'album_id': 'replaced-same-name-album'}]}
        with self.f.configured(), patch.object(adapter, 'native', return_value=observed), \
                patch.object(adapter, 'post_existing_delete') as post:
            with self.assertRaisesRegex(RuntimeError, 'PHOTOS_ALBUM_BINDING_CHANGED'):
                adapter.handle(self.f.session(), self.f.command())
            post.assert_not_called()

    def test_review_lock_survives_new_session_until_explicit_reconcile(self):
        self.f.build()
        with self.f.configured(), patch.object(adapter, 'post_existing_delete') as post:
            planner.save(adapter.PRIVATE / 'last-error.json',
                         {'delete_blocked_until_explicit_reconcile': True})
            session = self.f.session()
            with self.assertRaisesRegex(RuntimeError, 'RECONCILE_OR_REVIEW'):
                adapter.handle(session, self.f.command())
            post.assert_not_called()
            adapter.handle(session, {'cmd': 'reconcile'})
            self.assertFalse(adapter.persistent_blocked())

    def test_failed_read_after_restart_does_not_clear_review_lock(self):
        self.f.build()
        with self.f.configured():
            planner.save(adapter.PRIVATE / 'last-error.json',
                         {'delete_blocked_until_explicit_reconcile': True})
            session = self.f.session()
            session.current['unexpected'] = {'lloc': 'unexpected'}
            with self.assertRaisesRegex(RuntimeError, 'QQ_ALBUM_COUNT_CHANGED'):
                adapter.handle(session, {'cmd': 'inventory'})
            self.assertTrue(adapter.persistent_blocked())

    def test_reconciliation_uses_actual_absence_preserves_protected_items(self):
        self.f.build()
        session = self.f.session()
        def uncertain_post(*args):
            del session.current['verified']
            raise TimeoutError()
        with self.f.configured(), patch.object(adapter, 'native', return_value={'results': [self.f.asset]}), \
                patch.object(adapter, 'post_existing_delete', side_effect=uncertain_post) as post:
            with self.assertRaises(TimeoutError):
                adapter.handle(session, self.f.command())
            result = adapter.handle(session, {'cmd': 'reconcile'})
            self.assertEqual(result['verified_absent_count'], 1)
            self.assertEqual(result['remaining_whitelist'], 0)
            self.assertEqual(set(session.current), {'held', 'missing-asset'})
            self.assertEqual(set(planner.read(adapter.ITEM_LOG)), {self.f.source_key})
            self.assertEqual(post.call_count, 1)

    def test_delete_wire_fields_are_form_encoded_without_retries(self):
        response = Mock(status_code=200, text='frameElement.callback({code:0,data:{succ:[]}});', content=b'synthetic')
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        http = Mock()
        http.post.return_value = response
        session = SimpleNamespace(user_qq='12345', qzone_g_tk='synthetic-token', session=http)
        with patch.object(adapter.time, 'time', return_value=1000):
            value = adapter.post_existing_delete(session, {'album_id': 'album', 'name': '合成 & 相册', 'priv': 3},
                [{'id': 'demo+key==', 'picrefer': 'ref&value', 'imageType': 1}])
        self.assertEqual(value['code'], 0)
        self.assertEqual(http.post.call_count, 1)
        kwargs = http.post.call_args.kwargs
        request = requests.Request('POST', 'https://example.invalid', data=kwargs['data']).prepare()
        decoded = parse_qs(request.body)
        self.assertEqual(decoded['albumname'], ['合成 & 相册'])
        self.assertEqual(decoded['codelist'], ['demo+key==|ref&value|1000000|||demo+key==|1|0'])
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], 45)

    def test_wrong_login_owner_clears_authentication(self):
        session = migration.Session('12345')
        self.addCleanup(session.session.close)
        session.cookies = {'uin': 'o0099999', 'p_skey': 'synthetic-only'}
        session.session.cookies.set('uin', 'o0099999')
        session.qzone_g_tk = 'synthetic-only'
        with patch.object(migration.core.QzonePhotoManager, '_login_and_get_cookies'):
            with self.assertRaisesRegex(ValueError, 'WRONG_SIGNED_IN_ACCOUNT'):
                session._login_and_get_cookies()
        self.assertFalse(session.cookies)
        self.assertFalse(session.session.cookies)
        self.assertEqual(session.qzone_g_tk, '')


if __name__ == '__main__':
    unittest.main()
