"""Synthetic regression tests; no network, personal media, or Photos access."""
import base64
import copy
import datetime as dt
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrate_session as migration
import prepare_capture as capture


def record(*, original=None, rawshoot=None, upload='2024-06-01 10:00:00',
           name='synthetic.jpg', video=False, exif=None):
    tags = dict(exif or {})
    if original is not None:
        tags['originalTime'] = original
    return {'raw_fields': {'exif': tags, 'rawshoottime': rawshoot,
                          'uploadtime': upload, 'name': name},
            'original_name': name, 'is_video': video,
            'album': {'photos_album': '迁移QQ相册之“合成测试”', 'album_id': 'synthetic-album'},
            'pic_key': 'synthetic-media', 'sha256': 'synthetic-digest'}


def plan(item, meta=None, detail=None):
    return capture.plan_item(item, Path('/synthetic/sample'),
                             meta or {'File:FileType': 'JPEG'},
                             capture.fallback_classification(item), detail or {})


class CaptureRules(unittest.TestCase):
    def test_original_time_selected_instead_of_upload(self):
        result = plan(record(original='2018-02-03 04:05:06'))
        self.assertEqual(result['target_source'], 'qq.exif.originalTime')
        self.assertEqual(result['capture_wall_time'], '2018-02-03 04:05:06')
        self.assertEqual(result['write_tags']['ExifIFD:DateTimeOriginal'], '2018:02:03 04:05:06')
        self.assertNotIn('ExifIFD:CreateDate', result['write_tags'])
        self.assertNotIn('ExifIFD:OffsetTimeOriginal', result['write_tags'])

    def test_upload_or_upload_named_file_never_becomes_capture(self):
        result = plan(record(name='2024-06-01.jpg'))
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['target_epoch'])
        self.assertNotIn('ExifIFD:DateTimeOriginal', result['write_tags'])

    def test_full_raw_shoot_time_is_a_fallback(self):
        result = plan(record(rawshoot='2017-03-04 05:06:07'))
        self.assertEqual(result['target_source'], 'qq.rawshoottime')
        self.assertEqual(result['capture_wall_time'], '2017-03-04 05:06:07')

    def test_only_explicit_screenshot_name_is_inferred(self):
        result = plan(record(name='截图 2020-02-03 04-05-06.png'), {'File:FileType': 'PNG'})
        self.assertEqual(result['target_source'], 'filename.screenshot')
        self.assertEqual(result['capture_wall_time'], '2020-02-03 04:05:06')
        self.assertIsNone(plan(record(name='2020-02-03 04-05-06.jpg'))['target_epoch'])

    def test_raw_midnight_withholds_unknown_hours(self):
        result = plan(record(rawshoot='2016-01-02 00:00:00'))
        self.assertEqual(result['status'], 'date_only')
        self.assertEqual(result['date_only'], '2016-01-02')
        self.assertEqual(result['precision'], 'day')
        self.assertIsNone(result['target_epoch'])
        self.assertNotIn('ExifIFD:DateTimeOriginal', result['write_tags'])
        self.assertIsNone(capture.date('2016-01-02'))

    def test_explicit_original_midnight_is_not_discarded(self):
        result = plan(record(original='2016-01-02 00:00:00'))
        self.assertEqual(result['status'], 'capture_ready')
        self.assertEqual(result['capture_wall_time'], '2016-01-02 00:00:00')

    def test_embedded_capture_and_offset_are_preserved_on_conflict(self):
        meta = {'File:FileType': 'JPEG', 'ExifIFD:DateTimeOriginal': '2015:04:03 02:01:00',
                'ExifIFD:OffsetTimeOriginal': '-04:00', 'GPS:GPSLatitude': 1.25}
        result = plan(record(original='2018-02-03 04:05:06'), meta)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['target_source'], 'embedded.ExifIFD:DateTimeOriginal')
        self.assertEqual(result['target_iso'], '2015-04-03T02:01:00-04:00')
        self.assertNotIn('ExifIFD:DateTimeOriginal', result['write_tags'])
        self.assertNotIn('GPS:GPSLatitude', result['write_tags'])

    def test_invalid_qq_fnumber_is_skipped(self):
        for value in ('0', '-2.8', '1/0', 'not-a-number', '4294967295/1'):
            with self.subTest(value=value):
                item = record(original='2018-02-03 04:05:06', exif={'fnumber': value})
                result = plan(item)
                self.assertNotIn('ExifIFD:FNumber', result['write_tags'])
                self.assertTrue(any('Invalid QQ fnumber' in flag for flag in result['flags']))
                self.assertEqual(item['raw_fields']['exif']['fnumber'], value)

    def test_valid_qq_fnumber_fills_only_missing_tag(self):
        item = record(exif={'fnumber': '14/5'})
        self.assertEqual(plan(item)['write_tags']['ExifIFD:FNumber'], '14/5')
        self.assertNotIn('ExifIFD:FNumber', plan(item, {'File:FileType': 'JPEG',
                                                     'ExifIFD:FNumber': 4.0})['write_tags'])

    def test_gif_uses_sourced_library_date_without_file_tags(self):
        result = plan(record(original='2018-02-03 04:05:06'), {'File:FileType': 'GIF'})
        self.assertEqual(result['write_tags'], {})
        self.assertEqual(result['status'], 'capture_ready')
        self.assertEqual(result['capture_wall_time'], '2018-02-03 04:05:06')

    def test_unsupported_formats_are_withheld_even_with_capture_evidence(self):
        for kind in ('WEBP', 'BMP', 'AVIF', 'UNKNOWN', None):
            with self.subTest(kind=kind):
                result = plan(record(original='2018-02-03 04:05:06'), {'File:FileType': kind})
                self.assertEqual(result['status'], 'needs_review')
                self.assertEqual(result['write_tags'], {})
                self.assertTrue(any('Unsupported media type' in x for x in result['flags']))


class VideoRules(unittest.TestCase):
    @staticmethod
    def movie_dates(value):
        return {'File:FileType': 'MP4', 'QuickTime:CreateDate': value,
                'Track1:TrackCreateDate': value, 'Track1:MediaCreateDate': value}

    def test_upload_near_transcode_date_is_not_capture(self):
        item = record(original='2018-02-03 04:05:06+08:00', video=True)
        meta = {'File:FileType': 'MP4', 'QuickTime:CreateDate': '2024:06:01 02:02:00',
                'Track1:TrackCreateDate': '2024:06:01 02:02:00',
                'Track1:MediaCreateDate': '2024:06:01 02:02:01'}
        result = plan(item, meta)
        self.assertEqual(result['status'], 'capture_ready')
        self.assertIsNotNone(result['transcode_evidence'])
        self.assertEqual(result['capture_wall_time'], '2018-02-03 04:05:06')
        self.assertEqual(capture.date(result['write_tags']['QuickTime:CreateDate']),
                         dt.datetime(2018, 2, 3, 4, 5, 6, tzinfo=capture.TZ))

    def test_ambiguous_nonzero_video_date_is_not_overwritten(self):
        meta = {'File:FileType': 'MP4', 'QuickTime:CreateDate': '2020:01:01 02:00:00'}
        result = plan(record(original='2018-02-03 04:05:06+08:00', video=True), meta)
        self.assertEqual(result['status'], 'needs_review')
        self.assertIsNone(result['transcode_evidence'])
        self.assertNotIn('QuickTime:CreateDate', result['write_tags'])

    def test_zero_video_dates_can_be_filled_from_proved_time(self):
        meta = {'File:FileType': 'MP4', 'QuickTime:CreateDate': capture.ZERO,
                'Track1:TrackCreateDate': capture.ZERO, 'Track1:MediaCreateDate': capture.ZERO}
        result = plan(record(original='2018-02-03 04:05:06+08:00', video=True), meta)
        self.assertEqual(result['status'], 'capture_ready')
        self.assertTrue({'QuickTime:CreateDate', 'Keys:CreationDate', 'Track1:TrackCreateDate',
                         'Track1:MediaCreateDate'}.issubset(result['write_tags']))
        self.assertIsNone(capture.date(capture.ZERO))

    def test_video_without_offset_or_matching_epoch_is_withheld(self):
        result = plan(record(original='2018-02-03 04:05:06', video=True),
                      {'File:FileType': 'MP4', 'QuickTime:CreateDate': capture.ZERO})
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['write_tags'], {})

    def test_upload_processing_window_is_directional_and_bounded(self):
        item = record(original='2018-02-03 04:05:06+08:00', video=True)
        upload_utc = dt.datetime(2024, 6, 1, 2, 0, 0)
        for seconds, accepted in ((-1, False), (0, True), (600, True), (1800, True), (1801, False)):
            with self.subTest(seconds=seconds):
                value = capture.exif_date(upload_utc + dt.timedelta(seconds=seconds))
                result = plan(item, self.movie_dates(value))
                self.assertEqual(result['status'], 'capture_ready' if accepted else 'needs_review')
                self.assertEqual(bool(result['transcode_evidence']), accepted)
                self.assertEqual('QuickTime:CreateDate' in result['write_tags'], accepted)

    def test_upload_inference_requires_matching_track_dates(self):
        item = record(original='2018-02-03 04:05:06+08:00', video=True)
        meta = self.movie_dates('2024:06:01 02:02:00')
        for track_date, accepted in (('2024:06:01 02:02:02', True),
                                     ('2024:06:01 02:02:03', False)):
            with self.subTest(track_date=track_date):
                meta['Track1:MediaCreateDate'] = track_date
                self.assertEqual(bool(plan(item, meta)['transcode_evidence']), accepted)
        self.assertIsNone(plan(item, {'File:FileType': 'MP4',
                                    'QuickTime:CreateDate': '2024:06:01 02:02:00'})['transcode_evidence'])

    def test_naive_video_candidate_needs_exact_detail_epoch(self):
        item = record(rawshoot='2018-02-03 04:05:06', video=True)
        epoch = int(dt.datetime(2018, 2, 3, 4, 5, 6, tzinfo=capture.TZ).timestamp())
        meta = self.movie_dates('2024:06:01 02:30:00')
        for detail, accepted in (({}, False), ({'shootTime': epoch + 1}, False),
                                 ({'shootTime': epoch}, True)):
            with self.subTest(detail=detail):
                result = plan(item, meta, detail)
                self.assertEqual(result['status'], 'capture_ready' if accepted else 'needs_review')
                self.assertEqual('QuickTime:CreateDate' in result['write_tags'], accepted)
        result = plan(item, meta, {'shootTime': epoch})
        self.assertEqual(result['timezone_status'], 'QQ shootTime epoch confirms +08 display')

    def test_unrepaired_movie_date_must_match_exactly_not_within_one_second(self):
        item = record(rawshoot='2024-06-01 10:00:00', video=True)
        epoch = int(dt.datetime(2024, 6, 1, 10, 0, 0, tzinfo=capture.TZ).timestamp())
        for value, status in (('2024:06:01 02:00:00', 'capture_ready'),
                              ('2024:06:01 02:00:01', 'needs_review')):
            with self.subTest(value=value):
                result = plan(item, self.movie_dates(value), {'shootTime': epoch})
                self.assertEqual(result['status'], status)
                self.assertIsNone(result['transcode_evidence'])
                self.assertNotIn('QuickTime:CreateDate', result['write_tags'])
                if status == 'needs_review':
                    self.assertEqual(result['write_tags'], {})


class BytePreservation(unittest.TestCase):
    def test_media_magic_is_not_guessed_from_filename(self):
        cases = [(b'\xff\xd8\xff', ('JPEG', '.jpeg')),
                 (b'\x89PNG\r\n\x1a\n', ('PNG', '.png')),
                 (b'GIF89a', ('GIF', '.gif')),
                 (b'RIFF\0\0\0\0WEBP', ('WEBP', '.webp')),
                 (b'\0\0\0\x18ftypheic\0\0\0\0', ('HEIC', '.heic')),
                 (b'\0\0\0\x18ftypisom\0\0\0\0', ('MP4', '.mp4')),
                 (b'\0\0\0\x18ftypqt  \0\0\0\0', ('MOV', '.mov')),
                 (b'<html>login</html>', ('UNKNOWN', '.unknown'))]
        for header, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(migration.media_format(header), expected)

    def test_download_preserves_response_bytes_and_reuses_verified_original(self):
        # A generated single-pixel GIF, not a downloaded or personal fixture.
        payload = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
        item = record(name='synthetic.gif')
        item['raw_fields']['raw'] = 'https://media.example.invalid/synthetic.gif'
        photo = migration.core.QzonePhoto(item['raw_fields']['raw'], item['original_name'],
            '合成测试', False, item['pic_key'], {}, '', '', '')
        session = migration.Session.__new__(migration.Session)
        key = (item['album']['album_id'], item['pic_key'])
        session.index = {key: (photo, item['raw_fields'])}
        session.album_map = {key[0]: item['album']}
        response = Mock(content=payload, headers={'Content-Type': 'image/gif'})
        with tempfile.TemporaryDirectory() as temp, patch.object(migration, 'OUT', Path(temp).resolve()), \
                patch.object(migration.core, 'download_photo_network_helper', return_value=response) as network:
            request = [{'album_id': key[0], 'pic_key': key[1]}]
            first = session.download(request, quiet=True)[0]
            original = Path(temp) / first['path']
            self.assertEqual(original.read_bytes(), payload)
            self.assertEqual(first['sha256'], hashlib.sha256(payload).hexdigest())
            self.assertFalse(first['metadata_modified'])
            self.assertTrue(session.download(request, quiet=True)[0]['cached'])
            self.assertEqual(original.read_bytes(), payload)
            network.assert_called_once()
            response.close.assert_called_once()

    def test_changed_payload_and_semantic_metadata_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            original, target = Path(temp) / 'original', Path(temp) / 'copy'
            original.write_bytes(b'synthetic original')
            target.write_bytes(original.read_bytes())
            self.assertEqual(capture.validate(original, target, {}, {}, {}), capture.sha(original))
            target.write_bytes(b'synthetic changed content')
            with self.assertRaisesRegex(RuntimeError, 'Media payload verification failed'):
                capture.validate(original, target, {}, {}, {})
            before = {'File:ImageDataHash': 'synthetic-payload-hash', 'GPS:GPSLatitude': 1.25,
                      'ICC_Profile:ProfileDescription': 'synthetic profile'}
            after = copy.deepcopy(before)
            after['GPS:GPSLatitude'] = 9.0
            with self.assertRaisesRegex(RuntimeError, 'Existing semantic metadata changed'):
                capture.validate(original, target, before, after, {})
            self.assertEqual(original.read_bytes(), b'synthetic original')

    def test_quicktime_readback_must_equal_converted_utc_instant(self):
        with tempfile.TemporaryDirectory() as temp:
            original, target = Path(temp) / 'original', Path(temp) / 'copy'
            original.write_bytes(b'synthetic container before')
            target.write_bytes(b'synthetic container after metadata edit')
            before = {'File:ImageDataHash': 'synthetic-payload-hash'}
            after = {**before, 'QuickTime:CreateDate': '2018:02:02 20:05:06'}
            allowed = {'QuickTime:CreateDate': '2018:02:03 04:05:06+0800'}
            self.assertEqual(capture.validate(original, target, before, after, allowed), capture.sha(target))
            after['QuickTime:CreateDate'] = '2018:02:03 04:05:06'
            with self.assertRaisesRegex(RuntimeError, 'Written metadata did not verify'):
                capture.validate(original, target, before, after, allowed)


if __name__ == '__main__':
    unittest.main()
