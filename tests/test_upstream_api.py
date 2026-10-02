"""Retained downloader behavior with synthetic responses; never opens a browser."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qzone-photo-downloader'))
import core


class RetainedAPI(unittest.TestCase):
    def setUp(self):
        self.manager = core.QzonePhotoManager('synthetic-owner')
        self.addCleanup(self.manager.session.close)

    def test_jsonp_and_repair_preserve_raw_response_evidence(self):
        expected = {'code': 0, 'data': {'photos': []}}
        bodies = [json.dumps(expected), 'viewer_Callback(' + json.dumps(expected) + ');',
                  'shine7_Callback(' + json.dumps(expected) + ');',
                  "viewer_Callback({code: 0, data: {photos: []}});"]
        for body in bodies:
            response = Mock(text=body)
            self.manager.raw_response_hook = Mock()
            with self.subTest(body=body), patch.object(core.requests, 'get', return_value=response) as request:
                result = self.manager._access_qzone_api('https://api.example.invalid/synthetic')
            self.assertEqual(result, expected)
            self.manager.raw_response_hook.assert_called_once_with(body)
            request.assert_called_once_with('https://api.example.invalid/synthetic',
                                           cookies={}, timeout=30)

    def test_video_url_never_falls_back_to_neighbor_or_stream(self):
        neighbor = {'picKey': 'neighbor', 'video_info': {'download_url': 'https://media.example.invalid/neighbor.mp4'}}
        target = {'lloc': 'target', 'video_info': {'download_url': 'https://media.example.invalid/target.mp4'}}
        with patch.object(self.manager, '_access_qzone_api', return_value={'data': {'photos': [neighbor, target]}}):
            self.assertEqual(self.manager.get_video_download_url('synthetic-owner', 'album', 'target'),
                             target['video_info']['download_url'])
        for photos in ([neighbor], [{**target, 'video_info': {'video_url': 'https://media.example.invalid/stream.m3u8'}}]):
            with self.subTest(photos=photos), patch.object(self.manager, '_access_qzone_api',
                                                         return_value={'data': {'photos': photos}}):
                self.assertEqual(self.manager.get_video_download_url('synthetic-owner', 'album', 'target'), '')

    def test_album_paging_retains_same_name_source_identities(self):
        pages = [{'data': {'albumsInUser': 2, 'albumListModeSort': [{'id': 'first', 'name': 'same', 'total': 1}]}},
                 {'data': {'albumlist': [{'albumid': 'second', 'name': 'same', 'picnum': 2}]}}]
        with patch.object(self.manager, '_access_qzone_api', side_effect=pages) as api:
            albums = self.manager.get_albums_by_page('synthetic-owner')
        self.assertEqual([(a.uid, a.name, a.count) for a in albums], [('first', 'same', 1), ('second', 'same', 2)])
        self.assertEqual(api.call_count, 2)
        self.assertIn('pageStart=1', api.call_args.args[0])

    def test_photo_listing_keeps_original_url_and_date_evidence(self):
        photo = {'lloc': 'photo', 'raw': 'https://media.example.invalid/original',
                 'url': 'https://media.example.invalid/preview', 'name': ' synthetic.png ',
                 'exif': {'originalTime': '2020:01:02 03:04:05'},
                 'rawshoottime': '2020-01-02 03:04:05', 'uploadtime': '2024-05-06 07:08:09'}
        video = {'lloc': 'video', 'origin_url': 'https://media.example.invalid/cover', 'phototype': 'video'}
        data = {'data': {'totalInAlbum': 2, 'totalInPage': 2, 'photoList': [photo, video]}}
        with patch.object(self.manager, '_access_qzone_api', return_value=data):
            result = self.manager.get_photos_from_album('synthetic-owner', core.QzoneAlbum('album', 'same', 2))
        self.assertEqual(result[0].url, photo['raw'])
        self.assertEqual(result[0].pic_key, 'photo')
        self.assertEqual(result[0].exif_data, photo['exif'])
        self.assertEqual(result[0].shoottime, photo['rawshoottime'])
        self.assertEqual(result[0].uploadtime, photo['uploadtime'])
        self.assertTrue(result[1].is_video)


if __name__ == '__main__':
    unittest.main()
