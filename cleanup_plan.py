# SPDX-License-Identifier: GPL-3.0-only
"""Build an offline, exact-source QQ cleanup plan from completed Photos proofs.

Reuses the pinned QzonePhoto deletion record mapping; this module performs no
network request or Photos operation. A commands file is data, never executed here.
"""
import argparse
import datetime as dt
import hashlib
import json
from collections import Counter
from pathlib import Path

from photos_pipeline import key, read, require, save, task_album
from media_utils import OUT, sha


def build_plan(data_root, prepared_path, ledger_path, target):
    data_root = Path(data_root).expanduser().resolve()
    records = data_root / '迁移记录'
    prepared_path, ledger_path, target = [Path(p).expanduser().resolve()
                                          for p in (prepared_path, ledger_path, target)]
    require(not data_root.is_relative_to(Path(__file__).resolve().parent), 'PRIVATE_OUTPUT_REQUIRED')
    require(all(p.is_relative_to(records) for p in (prepared_path, ledger_path, target)),
            'PLAN_INPUTS_MUST_BE_IN_PRIVATE_RECORDS')
    require(not target.exists(), 'PLAN_ALREADY_EXISTS_USE_COMMANDS_OR_REVIEW')
    require(not (target.parent / '逐项清理记录.json').exists() and
            not list((target.parent / 'API批次').glob('*/intent.json')), 'CLEANUP_JOURNAL_ALREADY_EXISTS')
    owner = str(read(records / 'source-account.json').get('owner', ''))
    require(owner.isdigit() and int(owner) > 0, 'SOURCE_ACCOUNT_MARKER_REQUIRED')
    rows = read(prepared_path)
    by_key = {key(row): row for row in rows}
    require(len(rows) == len(by_key), 'Duplicate prepared source key')
    ledger = read(ledger_path)
    ledger_digest = sha(ledger_path)
    assets = ledger['assets']
    require(len(assets) == ledger['summary']['verified_assets'], 'Ledger count mismatch')
    require(len({a['asset_id'] for a in assets.values()}) == len(assets), 'Duplicate Photos ID')
    forbidden = {v['source_key'] for v in ledger.get('excluded_items', []) if not v.get('resolved')}
    forbidden.update(v['source_key'] for v in ledger.get('pending_recovery', []))
    require(not (set(assets) & forbidden), 'Excluded source appears in active ledger')
    membership_path = Path(ledger['final_membership_proof'])
    require(membership_path.resolve().is_relative_to(records), 'MEMBERSHIP_PROOF_OUTSIDE_PRIVATE_RECORDS')
    membership = read(membership_path)
    native_albums = {v['task_album']: v for v in membership['albums']}
    require(membership.get('ok') is True and len(native_albums) == len(membership['albums']),
            'INVALID_NATIVE_ALBUM_PROOF')
    require(all(len(a['member_ids']) == len(set(a['member_ids'])) == a['count']
                for a in native_albums.values()), 'INVALID_NATIVE_MEMBER_COUNT')
    require(not any(a.get('extra') for a in native_albums.values()), 'Unexpected task-album assets')
    missing_ids = {i for a in native_albums.values() for i in a.get('missing', [])}
    require(not (missing_ids & {a['asset_id'] for a in assets.values()}), 'Missing asset in ledger')

    downloads = {}
    for p in sorted(records.glob('*/*-download.json')):
        v = read(p)
        source_key = str(v['album']['album_id']) + '\n' + v['pic_key']
        require(source_key not in downloads, 'Duplicate download record')
        downloads[source_key] = (v, p)
    require(set(downloads) == set(by_key), 'Download/preparation inventory mismatch')

    # Resolve proof references to the actual saved export hash evidence, rather
    # than treating a completed flag alone as sufficient proof.
    proof_cache = {}
    def cached(p):
        if str(p) not in proof_cache:
            proof_cache[str(p)] = read(p)
        return proof_cache[str(p)]

    def export_proof(source_key, current):
        p = Path(ledger['export_proof_references'][source_key]).resolve()
        require(p.is_relative_to(records.resolve()), 'Proof outside migration records')
        if p.name == 'completed.json':
            c = cached(p)
            old = c['assets'][source_key]
            require(c.get('export_verified') is True and
                    old['asset_id'] == current['asset_id'] and
                    old['import_sha256'] == current['import_sha256'], 'Completed proof mismatch')
            if c.get('export_proof_directory'):
                p = Path(c['export_proof_directory']) / 'combined-export-proof.json'
            else:
                p = p.parent / 'export-proof.json'
        require(p.resolve().is_relative_to(records), 'EXPORT_PROOF_OUTSIDE_PRIVATE_RECORDS')
        evidence = cached(p)
        require(evidence.get('all_passed') is True and not evidence.get('problems'), 'Incomplete export proof')
        value = evidence['verified'][source_key]
        require(value['asset_id'] == current['asset_id'] and
                value['export_sha256'] == current['import_sha256'] and
                value['epoch_seconds'] == current['epoch_seconds'] and
                value['in_album'] is True, 'Export identity/hash/date mismatch')
        return p, value

    allowed, keep = [], []
    for source_key, row in by_key.items():
        download, download_path = downloads[source_key]
        raw = download['raw_fields']
        qq_id = str(raw.get('lloc') or raw.get('picKey') or raw.get('id') or '')
        require(qq_id == row['pic_key'], 'QQ delete identifier does not match verified source key')
        base = {'source_key': source_key, 'album_id': row['album_id'], 'pic_key': row['pic_key'],
                'qq_album_name': download['album']['source_name'],
                'photos_album': task_album(row), 'original_name': download['original_name'],
                'file_type': row['file_type'], 'is_video': bool(download['is_video'])}
        if source_key not in assets:
            keep.append({**base, 'status': row['status'],
                         'reason': 'photos_recovery_pending' if source_key in forbidden and
                                   row['status'] == 'capture_ready' else row['status']})
            continue
        require(row['status'] == 'capture_ready' and row.get('media_payload_unchanged') is True,
                'Ineligible preparation row in ledger')
        current = assets[source_key]
        native_album = native_albums[task_album(row)]
        require(current['qq_album_id'] == row['album_id'] and current['pic_key'] == row['pic_key'] and
                current['task_album'] == task_album(row) and current['album_id'] == native_album['album_id'] and
                current['asset_id'] in native_album['member_ids'] and
                current['in_album'] is True and
                current['epoch_seconds'] == row['target_epoch'] and
                current['import_sha256'] == row['import_sha256'], 'Current Photos proof mismatch')
        original = Path(row['original_path']).resolve()
        copy = Path(row['path']).resolve()
        require(original.is_relative_to((data_root / '原始备份').resolve()) and
                copy.is_relative_to((data_root / '导入副本').resolve()), 'Media outside private backup/copy roots')
        require(original == (data_root / download['path']).resolve() and
                original.stat().st_size == download['bytes'] and
                sha(original) == download['sha256'] == row['original_sha256'], 'Original backup changed')
        require(original.stat().st_mode & 0o222 == 0, 'Untouched backup must remain read only')
        require(sha(copy) == row['import_sha256'], 'Prepared copy changed')
        proof_path, proof = export_proof(source_key, current)
        # Match QzonePhoto's existing mapping. This is data only, not a request.
        photo_data = {'id': qq_id, 'picrefer': raw.get('picrefer') or '',
                      'imageType': raw.get('uploadtype') or raw.get('type') or 1}
        allowed.append({**base, 'qzonephoto_photo_data': photo_data,
            'photos_asset_id': current['asset_id'], 'photos_album_id': current['album_id'],
            'capture_epoch': row['target_epoch'], 'capture_iso': row['target_iso'],
            'original_path': str(original), 'original_bytes': download['bytes'],
            'original_sha256': row['original_sha256'], 'import_path': str(copy),
            'import_sha256': row['import_sha256'], 'export_sha256': proof['export_sha256'],
            'download_record': str(download_path.resolve()), 'export_proof': str(proof_path),
            'native_membership_proof': str(membership_path)})
    require(len(allowed) == len(assets) and len(allowed) + len(keep) == len(rows), 'Accounting incomplete')
    require(sha(ledger_path) == ledger_digest, 'Photos ledger changed while building; rerun')
    counts = Counter(v['album_id'] for v in allowed)
    kept_counts = Counter(v['album_id'] for v in keep)
    albums = read(records / '相册映射.json')
    require({row['album_id'] for row in rows} <= set(albums), 'SOURCE_ALBUM_MAPPING_INCOMPLETE')
    album_summary = [{**a, 'whitelist_count': counts[i], 'keep_count': kept_counts[i],
        'whitelist_photos': sum(v['album_id'] == i and not v['is_video'] for v in allowed),
        'whitelist_videos': sum(v['album_id'] == i and v['is_video'] for v in allowed)}
        for i, a in albums.items()]
    result = {'schema_version': 1, 'built_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'purpose': 'Private exact-source cleanup whitelist; this file performs no cloud action',
        'owner': owner, 'data_root': str(data_root), 'records_directory': str(records),
        'prepared_manifest': str(prepared_path),
        'photos_verified_candidates': len(allowed), 'keep_on_qq': len(keep),
        'source_total': len(rows), 'qq_deletion_executed': False,
        'cloud_execution_ready': False,
        'before_execution_required': ['Fresh QQ inventory matches explicit source IDs',
            'Confirm current QQ recovery semantics or action-time approval for irreversible deletion',
            'Recheck Photos membership if library changed after this ledger'],
        'prepared_manifest_sha256': sha(prepared_path), 'photos_ledger': str(ledger_path),
        'photos_ledger_sha256': ledger_digest, 'membership_checked_utc': membership['checked_at_utc'],
        'counts_by_media': dict(Counter('video' if v['is_video'] else 'photo' for v in allowed)),
        'albums': album_summary, 'allowed': allowed, 'keep': keep}
    save(target, result)
    save(target.parent / '必须保留项目.json', keep)
    save(target.parent / '摘要.json', {k: v for k, v in result.items() if k not in ('allowed', 'keep')})
    by_album = [{**a,
        'delete_ids': [v['pic_key'] for v in allowed if v['album_id'] == a['album_id']],
        'keep_ids': [v['pic_key'] for v in keep if v['album_id'] == a['album_id']]}
        for a in album_summary]
    save(target.parent / '逐册精确ID.json', by_album)
    save(target.parent / '保留ID列表.json', {a['album_id']: a['keep_ids'] for a in by_album if a['keep_ids']})
    def digest(album_id, ids):
        value = '\n'.join(sorted(album_id + '|' + i for i in ids))
        return hashlib.sha256(value.encode('utf-8')).hexdigest()
    save(target.parent / '逐册集合哈希.json', [{
        'album_id': a['album_id'], 'album': a['photos_album'],
        'delete_count': len(a['delete_ids']), 'keep_count': len(a['keep_ids']),
        'all_count': len(a['delete_ids']) + len(a['keep_ids']),
        'delete_sha256': digest(a['album_id'], a['delete_ids']),
        'keep_sha256': digest(a['album_id'], a['keep_ids']),
        'all_sha256': digest(a['album_id'], a['delete_ids'] + a['keep_ids'])}
        for a in by_album])
    return result


def digest_ids(album_id, ids):
    return hashlib.sha256('\n'.join(sorted(album_id + '|' + value for value in ids)).encode()).hexdigest()


def planned_commands(plan, removed, batch_size=25):
    """Yield explicit bounded commands; never submits or retries them."""
    require(isinstance(batch_size, int) and 1 <= batch_size <= 50, 'BATCH_SIZE_MUST_BE_1_TO_50')
    allowed = {item['source_key']: item for item in plan['allowed']}
    kept = {item['source_key'] for item in plan['keep']}
    require(len(allowed) == len(plan['allowed']) and set(allowed).isdisjoint(kept), 'INVALID_PLAN_SCOPE')
    require(set(removed) <= set(allowed) and all(item.get('observed_qq_absent') is True
            for item in removed.values()), 'INVALID_REMOVAL_LEDGER')
    albums = {}
    for source_key, item in allowed.items():
        require(source_key == key(item), 'SOURCE_KEY_MISMATCH')
        if source_key not in removed:
            albums.setdefault(item['album_id'], []).append(item['pic_key'])
    for aid, ids in sorted(albums.items()):
        ids.sort()
        for start in range(0, len(ids), batch_size):
            batch = ids[start:start + batch_size]
            yield {'cmd': 'delete_batch', 'album_id': aid, 'pic_keys': batch,
                   'expected_sha256': digest_ids(aid, batch),
                   'confirm': 'move_verified_items_to_qq_recycle_bin'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUT, help='Existing private migration directory')
    parser.add_argument('--manifest', type=Path, help='Prepared media manifest within private records')
    parser.add_argument('--ledger', type=Path, help='Final Photos verified-ledger.json within private records')
    parser.add_argument('--plan', type=Path, help='Frozen cleanup plan; default: private records/QQ清理核验/精确白名单.json')
    parser.add_argument('--commands', type=Path, help='Write JSONL commands from an existing plan; does not execute them')
    parser.add_argument('--batch-size', type=int, default=25, help='Explicit command size, maximum 50')
    args = parser.parse_args()
    data_root = args.output.expanduser().resolve()
    records = data_root / '迁移记录'
    target = (args.plan or records / 'QQ清理核验/精确白名单.json').expanduser().resolve()
    require(target.is_relative_to(records) and not data_root.is_relative_to(Path(__file__).resolve().parent),
            'PRIVATE_PLAN_REQUIRED')
    if args.commands:
        commands_path = args.commands.expanduser().resolve()
        require(commands_path.is_relative_to(records) and commands_path != target, 'PRIVATE_COMMANDS_PATH_REQUIRED')
        require(not commands_path.exists(), 'COMMANDS_FILE_ALREADY_EXISTS')
        pending = [p for p in (target.parent / 'API批次').glob('*/intent.json')
                   if not (p.parent / 'reconciled.json').exists()]
        require(not pending, 'RECONCILE_PENDING_BATCH_FIRST')
        removed_path = target.parent / '逐项清理记录.json'
        removed = read(removed_path) if removed_path.exists() else {}
        commands = list(planned_commands(read(target), removed, args.batch_size))
        commands_path.parent.mkdir(parents=True, exist_ok=True)
        with commands_path.open('x') as stream:
            for command in commands:
                stream.write(json.dumps(command, ensure_ascii=False) + '\n')
        print(json.dumps({'commands_written': len(commands), 'cloud_changes': False,
                          'path': str(commands_path)}, ensure_ascii=False))
    else:
        plan = build_plan(data_root, args.manifest or records / '全量拍摄时间导入清单.json',
                          args.ledger or records / 'Photos批量导入/verified-ledger.json', target)
        print(json.dumps({'whitelist': len(plan['allowed']), 'keep': len(plan['keep']),
                          'cloud_changes': False, 'path': str(target)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
