# SPDX-License-Identifier: GPL-3.0-only
"""Exact-whitelist cleanup adapter; importing this module performs no action.

Login/listing: existing dslwind core (MIT, pinned in qzone-photo-downloader/UPSTREAM.txt).
Delete wire format: existing QzonePhoto cgi_delpic_multi_v2, pinned commit
038e1beb38a230246dde3824f8f0ada3bf4a98ea, src/main/api/qzone/photo.js.
No downloader, credential persistence, automatic retries, or permanent-delete API.
"""
import datetime as dt
import hashlib
import json
import re
import time
import types
import uuid
from pathlib import Path

import migrate_session as migration
from photos_pipeline import (check_copy, key, native, native_rows, prove_filename_aliases,
                             read, require, save, verified_assets)
from media_utils import OUT, RECORDS, sha

PRIVATE = RECORDS / 'QQ清理核验'
API_JOURNAL = PRIVATE / 'API批次'
WHITELIST = PRIVATE / '精确白名单.json'
ITEM_LOG = PRIVATE / '逐项清理记录.json'
HELPER = Path(__file__).with_name('photos_batch.js')
DELETE_URL = 'https://user.qzone.qq.com/proxy/domain/photo.qzone.qq.com/cgi-bin/common/cgi_delpic_multi_v2'


def configure(plan_path):
    """Select a private frozen plan; importing this module performs no action."""
    global PRIVATE, API_JOURNAL, WHITELIST, ITEM_LOG
    plan_path = Path(plan_path).expanduser().resolve()
    require(plan_path.is_relative_to(RECORDS.resolve()), 'PRIVATE_PLAN_REQUIRED')
    WHITELIST, PRIVATE = plan_path, plan_path.parent
    API_JOURNAL, ITEM_LOG = PRIVATE / 'API批次', PRIVATE / '逐项清理记录.json'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest_ids(album_id, ids):
    return hashlib.sha256('\n'.join(sorted(album_id + '|' + i for i in ids)).encode()).hexdigest()


def context():
    m = read(WHITELIST)
    require(Path(m['data_root']).resolve() == OUT.resolve() and
            Path(m['records_directory']).resolve() == RECORDS.resolve(), 'OUTPUT_CONTEXT_CHANGED')
    require(sha(Path(m['prepared_manifest'])) == m['prepared_manifest_sha256'], 'PREPARED_MANIFEST_CHANGED')
    require(str(read(RECORDS / 'source-account.json').get('owner')) == m['owner'], 'SOURCE_ACCOUNT_CHANGED')
    allowed = {v['source_key']: v for v in m['allowed']}
    keep = {v['source_key']: v for v in m['keep']}
    removed = read(ITEM_LOG) if ITEM_LOG.exists() else {}
    require(len(allowed) == len(m['allowed']) and len(keep) == len(m['keep']) and
            all(k == key(v) for k, v in {**allowed, **keep}.items()), 'INVALID_PLAN_SOURCE_KEYS')
    require(set(allowed).isdisjoint(keep), 'WHITELIST_OVERLAP')
    require(set(removed) <= set(allowed) and all(v.get('observed_qq_absent') is True for v in removed.values()),
            'INVALID_REMOVAL_LEDGER')
    require(sha(Path(m['photos_ledger'])) == m['photos_ledger_sha256'], 'PHOTOS_LEDGER_CHANGED')
    baseline = {}
    for p in RECORDS.glob('*/*-download.json'):
        v = read(p)
        baseline[str(v['album']['album_id']) + '\n' + v['pic_key']] = v['raw_fields']
    require(set(baseline) == set(allowed) | set(keep), 'SOURCE_INVENTORY_MISMATCH')
    return m, allowed, keep, removed, baseline


def safe_fields(raw):
    """Metadata comparison only; URLs, tokens and signed media parameters are excluded."""
    return {k: raw.get(k) for k in ('lloc', 'name', 'is_video', 'phototype', 'width', 'height',
        'rawshoottime', 'shoottime', 'uploadtime', 'modifytime')} | {
        'originalTime': (raw.get('exif') or {}).get('originalTime')}


def access(self, url, timeout_seconds=None):
    # Bypass Session's historical raw-response writer; reuse upstream parser.
    self.raw_response_hook = None
    result = migration.core.QzonePhotoManager._access_qzone_api(self, url, timeout_seconds)
    require(isinstance(result, dict) and result.get('code') == 0 and isinstance(result.get('data'), dict),
            'QQ_READ_API_FAILED')
    data = result['data']
    self._cleanup_rows.extend(data.get('photoList') or [])
    albums = data.get('albumListModeSort') or data.get('albumList') or data.get('albumlist') or []
    if not albums and isinstance(data.get('albumListModeClass'), list):
        albums = [a for group in data['albumListModeClass'] for a in group.get('albumList', [])]
    for a in albums:
        self._cleanup_album_raw[str(a.get('id') or a.get('albumid'))] = a
    return result


def snapshot(session, directory, ctx, only_album=None, permit_missing=None):
    m, allowed, keep, removed, baseline = ctx
    expected = (set(allowed) | set(keep)) - set(removed)
    permitted = set(permit_missing or [])
    require(permitted <= set(allowed), 'INVALID_RECONCILIATION_SCOPE')
    session._cleanup_rows, session._cleanup_album_raw = [], {}
    session._access_qzone_api = types.MethodType(access, session)
    albums = session.get_albums_by_page(session.user_qq)
    expected_albums = {v['album_id']: v for v in m['albums']}
    require(len(albums) == len(expected_albums) == session.total_albums and
            {str(a.uid) for a in albums} == set(expected_albums), 'QQ_ALBUM_SET_CHANGED')
    result = {'checked_utc': now(), 'albums': [], 'items': {}, 'read_scope': only_album or 'all'}
    for album in albums:
        aid = str(album.uid)
        wanted = {k for k in expected if k.split('\n', 1)[0] == aid}
        can_miss = wanted & permitted
        require(album.name == expected_albums[aid]['source_name'], 'QQ_ALBUM_NAME_CHANGED')
        require(len(wanted) - len(can_miss) <= int(album.count) <= len(wanted), 'QQ_ALBUM_COUNT_CHANGED')
        raw_album = session._cleanup_album_raw[aid]
        require(isinstance(raw_album.get('priv'), int), 'QQ_ALBUM_PRIVACY_MISSING')
        result['albums'].append({'album_id': aid, 'name': album.name,
            'count': int(album.count), 'priv': raw_album['priv']})
        if only_album is not None and only_album != aid:
            continue
        session._cleanup_rows = []
        photos = session.get_photos_from_album(session.user_qq, album)
        rows = {str(v.get('lloc') or ''): v for v in session._cleanup_rows}
        require(len(photos) == len(rows) == int(album.count), 'QQ_ITEM_COUNT_INCOMPLETE')
        actual = {aid + '\n' + p.pic_key for p in photos}
        require(len(actual) == len(photos) and actual <= wanted and wanted - actual <= can_miss,
                'QQ_SOURCE_IDS_CHANGED')
        for photo in photos:
            source_key = aid + '\n' + photo.pic_key
            raw = rows[photo.pic_key]
            require(safe_fields(raw) == safe_fields(baseline[source_key]), 'QQ_SOURCE_METADATA_CHANGED')
            result['items'][source_key] = {'album_id': aid, 'pic_key': photo.pic_key,
                'metadata': safe_fields(raw), 'photo_data': {
                    'id': raw.get('lloc') or raw.get('picKey') or raw.get('id'),
                    'picrefer': raw.get('picrefer') or '',
                    'imageType': raw.get('uploadtype') or raw.get('type') or 1}}
    if only_album is None:
        require(set(result['items']) <= expected and expected - set(result['items']) <= permitted,
                'QQ_FULL_INVENTORY_CHANGED')
    save(directory / 'inventory.json', result)
    return result


def parse_delete_response(body):
    # Existing QzonePhoto response forms, parsed as data; never evaluated as JS.
    value = body.strip()
    match = re.search(r'frameElement\.callback\s*\(\s*({[\s\S]*?})\s*\)\s*;?', value)
    if not match:
        match = re.fullmatch(r'[A-Za-z_$][\w$]*\s*\(\s*({[\s\S]*})\s*\)\s*;?', value)
    if match:
        value = match.group(1)
    try:
        data = json.loads(value)
    except ValueError:
        data = migration.core.json_repair.repair_json(value, return_objects=True)
    require(isinstance(data, dict), 'QQ_DELETE_RESPONSE_UNPARSED')
    if isinstance(data.get('raw'), list) and data['raw'] and isinstance(data['raw'][0], dict):
        data = data['raw'][0]
    code = data.get('code')
    if not isinstance(code, bool) and isinstance(code, (int, str)) and re.fullmatch(r'-?\d+', str(code)):
        data['code'] = int(code)
    return data


def post_existing_delete(session, album, photo_data):
    # Port of QzonePhoto cgi_delpic_multi_v2 wire fields. Auth stays in memory.
    stamp = int(time.time() * 1000)
    codes = ['|'.join(map(str, [p['id'], p.get('picrefer') or '', stamp, '', '',
        p['id'], p.get('imageType') or 1, 0])) for p in photo_data]
    data = {'qzreferrer': 'https://user.qzone.qq.com/' + session.user_qq,
        'albumid': album['album_id'], 'nvip': '1', 'bgid': 'undefined', 'tpid': 'undefined',
        'priv': str(album['priv']), 'albumname': album['name'], 'codelist': '_'.join(codes),
        'ismultiup': '0' * len(photo_data), 'resetcover': '1', 'newcover': '',
        'uin': session.user_qq, 'hostUin': session.user_qq, 'plat': 'qzone', 'source': 'qzone',
        'inCharset': 'utf-8', 'outCharset': 'utf-8'}
    # requests has no status/transport retry configured here. Never repeat a POST.
    with session.session.post(DELETE_URL, params={'g_tk': session.qzone_g_tk},
            data=data, timeout=45, allow_redirects=False,
            headers={'Content-Type': 'application/x-www-form-urlencoded'}) as response:
        require(response.status_code == 200, 'QQ_DELETE_HTTP_UNCERTAIN')
        parsed = parse_delete_response(response.text)
        return {'code': parsed.get('code'), 'succ_count': len((parsed.get('data') or {}).get('succ') or []),
            'response_sha256': hashlib.sha256(response.content).hexdigest()}


def unfinished():
    return [p.parent for p in API_JOURNAL.glob('*/intent.json') if not (p.parent / 'reconciled.json').exists()]


def persistent_blocked():
    state = PRIVATE / 'last-error.json'
    return state.exists() and read(state).get('delete_blocked_until_explicit_reconcile') is True


def reconcile(session, directory):
    intent = read(directory / 'intent.json')
    ctx = context()
    selected = set(intent['source_keys'])
    after = snapshot(session, directory / ('after-' + uuid.uuid4().hex[:8]), ctx,
                     only_album=intent['album_id'], permit_missing=selected)
    before = read(directory / 'before/inventory.json')
    missing = set(before['items']) - set(after['items'])
    require(missing <= selected and set(after['items']) <= set(before['items']), 'UNEXPECTED_CLOUD_CHANGE')
    removed = ctx[3]
    for source_key in missing:
        a = ctx[1][source_key]
        removed[source_key] = {'album_id': a['album_id'], 'pic_key': a['pic_key'],
            'photos_asset_id': a['photos_asset_id'], 'original_sha256': a['original_sha256'],
            'import_sha256': a['import_sha256'], 'operation_id': directory.name,
            'status': 'fresh_api_absence_verified', 'observed_qq_absent': True,
            'final_fresh_inventory_check_pending': True, 'checked_utc': now()}
    save(ITEM_LOG, removed)
    result = {'checked_utc': now(), 'album_id': intent['album_id'], 'selected_count': len(selected),
        'verified_absent_count': len(missing), 'still_present_count': len(selected - missing),
        'verified_absent_source_keys': sorted(missing), 'still_present_source_keys': sorted(selected - missing),
        'all_selected_absent': missing == selected,
        'cumulative_removed': len(removed), 'remaining_whitelist': len(ctx[1]) - len(removed)}
    save(directory / 'reconciled.json', result)
    return result


def _handle(session, command):
    require(str(session.cookies.get('uin') or session.cookies.get('p_uin') or '').lstrip('o0') ==
            session.user_qq.lstrip('0'), 'WRONG_SIGNED_IN_ACCOUNT')
    require(str(read(WHITELIST)['owner']).lstrip('0') == session.user_qq.lstrip('0'), 'PLAN_BELONGS_TO_ANOTHER_ACCOUNT')
    action = command.get('cmd')
    if action == 'inventory':
        require(not unfinished(), 'RECONCILE_PENDING_BATCH_FIRST')
        ctx = context()
        directory = PRIVATE / '实时清单' / (dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        value = snapshot(session, directory, ctx)
        return {'status': 'inventory_verified', 'items': len(value['items']), 'albums': value['albums'],
                'remaining_whitelist': len(ctx[1]) - len(ctx[3]), 'keep': len(ctx[2]),
                'remaining_equals_protected': set(value['items']) == set(ctx[2]),
                'inventory_proof': str(directory / 'inventory.json'), 'no_cloud_change': True}
    if action == 'reconcile':
        pending = unfinished()
        require(len(pending) <= 1, 'MULTIPLE_PENDING_BATCHES_REQUIRE_REVIEW')
        if pending:
            result = reconcile(session, pending[0])
        else:
            ctx = context()
            directory = PRIVATE / '实时清单' / ('reconcile-' + uuid.uuid4().hex[:8])
            value = snapshot(session, directory, ctx)
            result = {'items': len(value['items']), 'remaining_whitelist': len(ctx[1]) - len(ctx[3]),
                      'no_pending_post': True}
        session._cleanup_blocked = False
        save(PRIVATE / 'last-error.json', {'checked_utc': now(), 'status': 'reconciled_read_only',
            'delete_blocked_until_explicit_reconcile': False})
        return {'status': 'reconciled_read_only', **{k: v for k, v in result.items() if not k.endswith('_keys')}}
    require(action == 'delete_batch', 'UNKNOWN_COMMAND')
    require(getattr(session, '_cleanup_authorized', False) is True, 'DELETE_NOT_ENABLED')
    require(getattr(session, '_cleanup_recycle_confirmed', False) is True, 'CURRENT_RECYCLE_SEMANTICS_NOT_CONFIRMED')
    require(not getattr(session, '_cleanup_blocked', False) and not persistent_blocked() and not unfinished(),
            'RECONCILE_OR_REVIEW_BEFORE_DELETE')
    require(command.get('confirm') == 'move_verified_items_to_qq_recycle_bin', 'EXPLICIT_DELETE_COMMAND_REQUIRED')
    aid, ids = command.get('album_id'), command.get('pic_keys')
    require(isinstance(aid, str) and isinstance(ids, list) and 1 <= len(ids) <= 50 and
            all(isinstance(i, str) and i for i in ids) and len(ids) == len(set(ids)), 'EXPLICIT_1_TO_50_IDS_REQUIRED')
    require(command.get('expected_sha256') == digest_ids(aid, ids), 'SELECTION_DIGEST_MISMATCH')
    ctx = context()
    selected = {aid + '\n' + i for i in ids}
    require(selected <= set(ctx[1]) - set(ctx[3]) and selected.isdisjoint(ctx[2]), 'SOURCE_NOT_CURRENT_WHITELIST')
    directory = API_JOURNAL / (dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    before = snapshot(session, directory / 'before', ctx, only_album=aid)
    require(selected <= set(before['items']), 'SOURCE_NOT_CURRENT_QQ_ALBUM')
    all_rows = read(Path(ctx[0]['prepared_manifest']))
    prepared = [r for r in all_rows if key(r) in selected]
    assets = read(Path(ctx[0]['photos_ledger']))['assets']
    require(len(prepared) == len(selected), 'PREPARATION_COUNT_MISMATCH')
    for row in prepared:
        approved, asset = ctx[1][key(row)], assets[key(row)]
        require(row['import_sha256'] == approved['import_sha256'] == asset['import_sha256'],
                'IMPORT_PROOF_BINDING_CHANGED')
        require(asset['asset_id'] == approved['photos_asset_id'], 'PHOTOS_ASSET_BINDING_CHANGED')
        check_copy(row)
        require(sha(Path(row['original_path'])) == ctx[1][key(row)]['original_sha256'], 'ORIGINAL_BACKUP_CHANGED')
    observed = native(HELPER, directory / 'photos-preflight', 'verify', native_rows(prepared, assets, 'verify'))
    aliases = prove_filename_aliases(HELPER, directory, observed, prepared, assets, all_rows)
    checked = verified_assets(observed, prepared, assets, aliases)
    for row in prepared:
        source_key = key(row)
        require(checked[source_key]['album_id'] == assets[source_key]['album_id'] ==
                ctx[1][source_key]['photos_album_id'], 'PHOTOS_ALBUM_BINDING_CHANGED')
    album = next(a for a in before['albums'] if a['album_id'] == aid)
    save(directory / 'intent.json', {'written_utc': now(), 'album_id': aid, 'source_keys': sorted(selected),
        'selection_sha256': command['expected_sha256'], 'whitelist_sha256': sha(WHITELIST),
        'user_authorized_delete': True,
        'current_account_recycle_semantics_confirmed': True,
        'recovery_semantics': 'Operator declared current-account recycle-bin semantics before this session',
        'do_not_retry_post': True})
    try:
        response = post_existing_delete(session, album, [before['items'][aid + '\n' + i]['photo_data'] for i in ids])
        save(directory / 'response-summary.json', response)
    except Exception as exc:
        session._cleanup_blocked = True
        save(directory / 'uncertain-response.json', {'error_class': type(exc).__name__, 'checked_utc': now(),
            'requires_read_only_reconciliation': True, 'post_not_retried': True})
        raise
    result = reconcile(session, directory)
    require(response.get('code') == 0 and result['all_selected_absent'], 'PARTIAL_OR_REJECTED_DELETE_REVIEW_REQUIRED')
    return {'status': 'delete_batch_verified', **{k: v for k, v in result.items() if not k.endswith('_keys')}}


def handle(session, command):
    try:
        require(isinstance(command, dict), 'COMMAND_MUST_BE_OBJECT')
        return _handle(session, command)
    except Exception as exc:
        if isinstance(command, dict) and command.get('cmd') == 'delete_batch':
            session._cleanup_blocked = True
        session._cleanup_blocked = bool(getattr(session, '_cleanup_blocked', False) or persistent_blocked())
        reason = str(exc)
        save(PRIVATE / 'last-error.json', {'checked_utc': now(), 'error_class': type(exc).__name__,
            'reason_code': reason if re.fullmatch('[A-Z_]+', reason) else 'DETAILS_OMITTED_TO_AVOID_AUTH_LOGGING',
            'delete_blocked_until_explicit_reconcile': bool(getattr(session, '_cleanup_blocked', False))})
        raise
