"""Journaled native Photos import. Default prints a plan; --apply performs it.

Only capture_ready rows are eligible. An unfinished native batch always stops a
resume for reconciliation: a successful Photos import cannot be rolled back by
rewinding this journal. No Photos database is read or modified directly.
"""
import argparse
from collections import Counter
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from media_utils import RECORDS, sha

os.umask(0o077)


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.pending')
    with temp.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def key(row):
    return row['album_id'] + '\n' + row['pic_key']


def task_album(row):
    name = row['album']
    return name if name.startswith('迁移QQ相册之“') else '迁移QQ相册之“' + name + '”'


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def check_copy(row):
    require(row.get('media_payload_unchanged') is True, 'Missing verified payload flag: ' + key(row))
    require(Path(row['path']).is_file() and sha(Path(row['path'])) == row.get('import_sha256'),
            'Import copy changed or missing: ' + row['path'])
    require(row.get('target_iso') and row.get('target_epoch') is not None, 'Missing exact capture date')
    require(dt.datetime.fromisoformat(row['target_iso']).timestamp() == row['target_epoch'],
            'Capture epoch and ISO representation disagree')


def video_date_mismatches(rows):
    """Block known Photos container-date precedence conflicts before import."""
    videos = [row for row in rows if row.get('file_type') in ('MP4', 'MOV', 'M4V', 'AVI')]
    if not videos:
        return []
    result = subprocess.run(['exiftool', '-json', '-G1', '-n', '-api', 'QuickTimeUTC=0',
        '-QuickTime:CreateDate', *[row['path'] for row in videos]],
        check=True, capture_output=True, text=True)
    metadata = {Path(item['SourceFile']).resolve(): item for item in json.loads(result.stdout)}
    issues = []
    for row in videos:
        value = metadata[Path(row['path']).resolve()].get('QuickTime:CreateDate')
        if not value or str(value).startswith('0000:'):
            continue
        # Integer QuickTime creation times are UTC. This read explicitly avoids
        # ExifTool's local-time conversion; no camera timezone is invented.
        try:
            stamp = dt.datetime.fromisoformat(value[:10].replace(':', '-') + value[10:])
            epoch = stamp.replace(tzinfo=dt.timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()
        except (TypeError, ValueError):
            epoch = None
        if epoch != row['target_epoch']:
            issues.append({'source_key': key(row), 'path': row['path'],
                'target_epoch': row['target_epoch'], 'quicktime_create_date': value,
                'quicktime_epoch': epoch, 'reason': 'Nonzero QuickTime creation time differs from exact capture target'})
    return issues


def native(helper, directory, op, rows, extra=None):
    request = directory / (op + '-request.json')
    require(not request.exists(), 'Native operation already journaled; reconcile before retry: ' + str(request))
    cfg = {'operation': op, 'items': rows, **(extra or {})}
    save(request, cfg)  # Durable before Photos can receive any side effect.
    with (directory / (op + '-stdout.json')).open('w') as stdout, \
            (directory / (op + '-stderr.log')).open('w') as stderr:
        proc = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', str(helper), str(request)],
                              stdout=stdout, stderr=stderr, timeout=900)
    result = read(directory / (op + '-stdout.json'))
    save(directory / (op + '-result.json'), result)
    if op == 'import':
        save(directory / 'returned-import-groups.json', result.get('imports', []))
    returned = [item for group in result.get('imports', []) for item in group.get('returned', [])]
    complete_import_ids = (op == 'import' and len(returned) == len(rows) and
                           len({item.get('asset_id') for item in returned}) == len(rows) and
                           all(item.get('asset_id') for item in returned))
    # A prior helper may fail only while mapping aliased filenames after all
    # assets were imported. Its complete IDs permit read/export reconciliation,
    # never another import attempt. Incomplete IDs remain a hard stop.
    require(proc.returncode == 0 and (result.get('ok') is True or complete_import_ids),
            f'Photos {op} incomplete; inspect journal {directory}; do not reimport blindly')
    return result


def native_rows(rows, assets, operation):
    result = []
    for row in rows:
        item = {'source_id': row['pic_key'], 'source_album_id': row['album_id'],
                'path': row['path'], 'album': row['album']}
        if operation != 'import':
            item['asset_id'] = assets[key(row)]['asset_id']
        # Photos may share an original resource between byte-identical imports,
        # changing its filename. Verification/export uses IDs plus byte proof.
        if operation == 'set_dates':
            item['expected_filename'] = assets[key(row)].get('filename', Path(row['path']).name)
        if operation == 'verify':
            item['expected_date'] = row['target_iso']
        if operation == 'set_dates':
            item['date'] = row['target_iso']
        result.append(item)
    return result


def import_ids(result, rows, helper=None, directory=None):
    expected = {(task_album(row), Path(row['path']).name): row for row in rows}
    require(len(expected) == len(rows), 'Duplicate filenames in one destination album')
    returned = [{**media, 'task_album': group['task_album']}
                for group in result.get('imports', []) for media in group.get('returned', [])]
    require(len(returned) == len(rows) and len({v['asset_id'] for v in returned}) == len(rows),
            'Import returned incomplete/nonunique IDs; reconcile journal')
    counts = Counter((v['task_album'], v['filename']) for v in returned)
    assets, unmapped = {}, []
    for media in returned:
        identity = (media['task_album'], media['filename'])
        if identity not in expected or counts[identity] != 1:
            unmapped.append(media)
            continue
        row = expected[identity]
        assets[key(row)] = {'asset_id': media['asset_id'], 'filename': media['filename'],
                            'import_sha256': row['import_sha256']}
    if unmapped:
        require(helper and directory, 'Filename aliases require exported-byte reconciliation')
        proof_dir = directory / 'import-mapping-proof'
        requests = []
        for media in unmapped:
            out = proof_dir / 'exports' / media['asset_id'].split('/')[0]
            out.mkdir(parents=True, exist_ok=False)
            requests.append({'asset_id': media['asset_id'], 'album': media['task_album'],
                             'source_id': 'unmapped:' + media['asset_id'], 'export_dir': str(out)})
        exported = native(helper, proof_dir, 'export', requests)
        observed = {v['asset_id']: v for v in exported['results']}
        proofs = {}
        for media, request in sorted(zip(unmapped, requests), key=lambda pair: pair[0]['asset_id']):
            value = observed[media['asset_id']]
            files = [p for p in Path(request['export_dir']).iterdir() if p.is_file()]
            require(len(files) == 1 and value.get('in_album') is True, 'Unmapped asset export/membership failed')
            digest = sha(files[0])
            candidates = [row for row in rows if key(row) not in assets and
                task_album(row) == media['task_album'] and row['import_sha256'] == digest and
                row['target_epoch'] == value['epoch_seconds']]
            if not candidates:
                pool = [row for row in rows if key(row) not in assets and
                    task_album(row) == media['task_album'] and row['import_sha256'] == digest]
                # GIF capture time lives only in Photos. Byte-equivalent sources
                # may be assigned before that authorized adjustment only when
                # all remaining candidates have the same sourced target time.
                if pool and len({row['target_epoch'] for row in pool}) == 1 and all(
                    row['file_type'] == 'GIF' or row.get('native_date_override_verified') is True for row in pool):
                    candidates = pool
            require(candidates, 'Exported asset has no matching prepared hash/date/album; reconcile journal')
            candidates.sort(key=key)
            row = candidates[0]
            proof = {'actual_filename': value['filename'], 'source_filename': Path(row['path']).name,
                     'export_sha256': digest, 'epoch_seconds': value['epoch_seconds'],
                     'target_epoch': row['target_epoch'],
                     'native_date_pending': value['epoch_seconds'] != row['target_epoch'],
                     'export_path': str(files[0]), 'equivalent_source_keys': [key(v) for v in candidates],
                     'proof': 'Native original export matches prepared SHA-256 and album; '
                              'equivalent source timestamps assigned deterministically; '
                              'final native date verification remains required'}
            assets[key(row)] = {'asset_id': media['asset_id'], 'filename': value['filename'],
                'import_sha256': digest, 'filename_alias_evidence': proof, 'import_mapping_evidence': proof}
            proofs[media['asset_id']] = proof
        save(proof_dir / 'evidence.json', proofs)
    require(len(assets) == len(rows), 'Import returned incomplete IDs; reconcile journal')
    require(len({item['asset_id'] for item in assets.values()}) == len(rows),
            'Photos returned one asset for multiple sources; reconcile before assigning dates')
    return assets


def verified_assets(result, rows, assets, alias_evidence=None):
    observed = {entry['asset_id']: entry for entry in result.get('results', [])}
    require(len(observed) == len(rows), 'Native verification result count differs')
    checked = {}
    for row in rows:
        asset = assets[key(row)]
        value = observed.get(asset['asset_id'], {})
        require(value.get('in_album') is True and value.get('task_album') == task_album(row),
                'Asset not in intended migration album: ' + key(row))
        proof = (alias_evidence or {}).get(asset['asset_id'])
        name_matches = value.get('filename') == Path(row['path']).name
        if not name_matches:
            require(proof and proof.get('actual_filename') == value.get('filename') and
                    proof.get('export_sha256') == row['import_sha256'] and
                    proof.get('epoch_seconds') == row['target_epoch'],
                    'Filename alias lacks original-byte proof: ' + key(row))
        require(value.get('epoch_seconds') == row['target_epoch'],
                'Filename/date mismatch; journal retained: ' + key(row))
        checked[key(row)] = {'qq_album_id': row['album_id'], 'pic_key': row['pic_key'],
                            'source_filename': Path(row['path']).name,
                            'import_sha256': row['import_sha256'], **value}
        if proof:
            checked[key(row)]['filename_alias_evidence'] = proof
        if asset.get('import_mapping_evidence'):
            checked[key(row)]['import_mapping_evidence'] = asset['import_mapping_evidence']
    return checked


def prove_filename_aliases(helper, directory, result, rows, assets, all_rows):
    """A filename change is accepted only after export SHA-256 equality.

    Known identical-copy names narrow the explanation; an unknown resource name
    still requires direct byte proof, never an unchecked name exception.
    """
    observed = {value['asset_id']: value for value in result.get('results', [])}
    names = {}
    for row in all_rows:
        names.setdefault((row['import_sha256'], row.get('target_epoch')), set()).add(Path(row['path']).name)
    proofs, needs = {}, set()
    for row in rows:
        asset = assets[key(row)]
        value = observed[asset['asset_id']]
        if value['filename'] == Path(row['path']).name:
            continue
        prior = asset.get('filename_alias_evidence', {})
        if (prior.get('actual_filename') == value['filename'] and
            prior.get('export_sha256') == row['import_sha256'] and
            prior.get('epoch_seconds') == row['target_epoch']):
            proofs[asset['asset_id']] = prior
        else:
            needs.add((row['import_sha256'], row['target_epoch']))
    # Include matching peers in the batch to prove the shared-byte equivalence.
    export_rows = [row for row in rows if (row['import_sha256'], row['target_epoch']) in needs]
    if not export_rows:
        return proofs
    proof_dir = directory / 'filename-alias-proof'
    requests = native_rows(export_rows, assets, 'export')
    for item in requests:
        out = proof_dir / 'exports' / item['asset_id'].split('/')[0]
        out.mkdir(parents=True, exist_ok=False)
        item['export_dir'] = str(out)
    exported = native(helper, proof_dir, 'export', requests)
    exported_by_id = {value['asset_id']: value for value in exported['results']}
    for row, request in zip(export_rows, requests):
        value = exported_by_id[request['asset_id']]
        files = [p for p in Path(request['export_dir']).iterdir() if p.is_file()]
        require(len(files) == 1, 'Alias export did not return exactly one original')
        digest = sha(files[0])
        require(digest == row['import_sha256'] and value['epoch_seconds'] == row['target_epoch'] and
                value['in_album'] is True, 'Alias original bytes/date/membership do not match')
        proofs[request['asset_id']] = {'actual_filename': value['filename'],
            'source_filename': Path(row['path']).name, 'export_sha256': digest,
            'epoch_seconds': value['epoch_seconds'], 'export_path': str(files[0]),
            'alias_in_prepared_same_hash_and_date': value['filename'] in
                names.get((digest, row['target_epoch']), set()),
            'proof': 'Native Photos original export SHA-256 equals verified prepared copy'}
    save(proof_dir / 'evidence.json', proofs)
    return proofs


def export_verify(helper, directory, rows, assets):
    pending, proofs, result = rows, {}, None
    for attempt in range(2):
        attempt_dir = directory if attempt == 0 else directory / 'export-retry-missing'
        requests = native_rows(pending, assets, 'export')
        for request in requests:
            out = attempt_dir / 'exports' / request['asset_id'].split('/')[0]
            out.mkdir(parents=True, exist_ok=False)
            request['export_dir'] = str(out)
        observed = native(helper, attempt_dir, 'export', requests)
        if result is None:
            result = observed
        by_id = {item['asset_id']: item for item in observed['results']}
        # Photos can return without creating a file. Wait only for filesystem
        # completion; a success response alone is never original-byte proof.
        deadline = time.monotonic() + 30
        while True:
            files = [[path for path in Path(q['export_dir']).iterdir() if path.is_file()] for q in requests]
            ready = all(len(paths) == 1 and paths[0].stat().st_size == Path(row['path']).stat().st_size
                        for row, paths in zip(pending, files))
            if ready or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        missing, problems = [], []
        for row, request, paths in zip(pending, requests, files):
            value = by_id[request['asset_id']]
            if not paths:
                missing.append(row)
                continue
            digest = sha(paths[0]) if len(paths) == 1 else None
            if (digest != row['import_sha256'] or value.get('in_album') is not True or
                    value.get('epoch_seconds') != row['target_epoch']):
                problems.append({'source_key': key(row), 'asset_id': request['asset_id'],
                    'files': [str(path) for path in paths], 'expected_sha256': row['import_sha256'],
                    'actual_sha256': digest, 'epoch_seconds': value.get('epoch_seconds'),
                    'target_epoch': row['target_epoch'], 'in_album': value.get('in_album')})
            else:
                proofs[key(row)] = {'asset_id': request['asset_id'], 'export_path': str(paths[0]),
                    'export_sha256': digest, 'epoch_seconds': value['epoch_seconds'], 'in_album': True}
        save(directory / 'export-proof.json', {'verified': proofs,
            'missing_source_keys': [key(row) for row in missing], 'problems': problems,
            'attempts': attempt + 1, 'all_passed': not missing and not problems})
        require(not problems, 'Exported originals failed file-count, SHA, date or membership proof; inspect export-proof.json')
        if not missing:
            return result
        # Only a missing export may be retried, once, to fresh directories.
        # This is read-only for Photos and never retries an import or date edit.
        pending = missing
    require(False, 'Photos returned without exported files after bounded wait and one retry; inspect export-proof.json')
    return result


def sample_gate(prepared, eligible, proved, sample=False):
    if sample:
        require(5 <= len(eligible) <= 15, '--sample requires 5..15 eligible source items')
        proved = eligible
    elif len(eligible) <= 15:
        return
    else:
        require(len(proved) >= 5, 'Full import requires a matching passed sample or exported, completed journal proof')
    kind = lambda row: 'video' if row.get('file_type') in ('MP4', 'MOV', 'M4V', 'AVI') else 'photo'
    required = {kind(row) for row in prepared}
    require(required <= {kind(row) for row in proved},
            'Sample must cover both photos and videos when the prepared manifest contains both')


def export_proof_record(source_key, value, reference):
    """Snapshot the identity covered by a completed, full-file export proof."""
    return {'source_key': source_key, 'reference': str(reference) if isinstance(reference, (str, Path)) else None,
        **{name: value.get(name) for name in ('asset_id', 'import_sha256', 'epoch_seconds',
            'album_id', 'task_album', 'filename', 'in_album', 'qq_album_id', 'pic_key')}}


def reusable_export_proof(row, current, proof):
    """Reuse bytes proof only alongside fresh exact native and copy checks."""
    return bool(proof and proof.get('reference') and Path(proof['reference']).is_file() and
        proof.get('source_key') == key(row) and
        proof.get('qq_album_id') == row['album_id'] and proof.get('pic_key') == row['pic_key'] and
        proof.get('import_sha256') == row['import_sha256'] == current.get('import_sha256') and
        proof.get('epoch_seconds') == row['target_epoch'] == current.get('epoch_seconds') and
        proof.get('task_album') == task_album(row) == current.get('task_album') and
        proof.get('in_album') is True and current.get('in_album') is True and
        all(proof.get(name) and proof.get(name) == current.get(name)
            for name in ('asset_id', 'album_id', 'filename')))


def resolve_export_reference(reference, source_key, asset):
    """Follow journal references to actual successful byte evidence, not flags."""
    reference = Path(reference).resolve()
    visited = set()
    while reference.name == 'completed.json':
        require(reference not in visited, 'Export proof reference cycle')
        visited.add(reference)
        value = read(reference)
        old = value.get('assets', {}).get(source_key, {})
        require(value.get('export_verified') is True and
                old.get('asset_id') == asset['asset_id'] and
                old.get('import_sha256') == asset['import_sha256'], 'Completed export identity differs')
        next_reference = value.get('export_proof_references', {}).get(source_key)
        if next_reference:
            reference = Path(next_reference).resolve()
        elif value.get('export_proof_directory'):
            reference = Path(value['export_proof_directory']).resolve() / 'combined-export-proof.json'
        else:
            reference = reference.parent / 'export-proof.json'
    evidence = read(reference)
    proof = evidence.get('verified', {}).get(source_key, {})
    require(evidence.get('all_passed') is True and not evidence.get('problems') and
            not evidence.get('missing_source_keys') and proof.get('asset_id') == asset['asset_id'] and
            proof.get('export_sha256') == asset['import_sha256'] and
            proof.get('epoch_seconds') == asset['epoch_seconds'] and proof.get('in_album') is True,
            'Actual export proof does not match current source/hash/date')
    return reference


def membership_request(rows, assets):
    groups = {}
    for row in rows:
        value = assets[key(row)]
        name = task_album(row)
        group = groups.setdefault(name, {'task_album': name, 'album_id': value['album_id'], 'expected_ids': []})
        require(group['album_id'] == value['album_id'], 'One task name maps to multiple Photos album IDs')
        group['expected_ids'].append(value['asset_id'])
    require(len({item['asset_id'] for item in assets.values()}) == len(assets), 'Duplicate tracked Photos IDs')
    return {'albums': list(groups.values())}


def classify_membership(result, request, assets):
    """Missing IDs are protected; extra IDs or replaced albums block the ledger."""
    expected = {g['task_album']: g for g in request['albums']}
    observed = result.get('albums', [])
    require(result.get('ok') is True and len(observed) == len(expected) and
            {g.get('task_album') for g in observed} == set(expected), 'Incomplete album membership response')
    missing = set()
    for group in observed:
        prior = expected[group['task_album']]
        ids = group.get('member_ids', [])
        require(group.get('album_id') == prior['album_id'], 'Photos album identity changed')
        require(len(ids) == len(set(ids)) and group.get('count') == len(ids) and
                group.get('expected_count') == len(prior['expected_ids']), 'Malformed album members')
        absent = set(prior['expected_ids']) - set(ids)
        extra = set(ids) - set(prior['expected_ids'])
        require(set(group.get('missing', [])) == absent and set(group.get('extra', [])) == extra,
                'Album membership difference does not match returned IDs')
        require(not extra, 'Unexpected task-album assets; review exact member IDs before finalizing')
        missing.update(absent)
    pending = [{'source_key': source_key, 'asset_id': value['asset_id'],
                'task_album': value['task_album'], 'album_id': value['album_id'],
                'reason': 'missing_from_current_task_album', 'requires_user_decision': True}
               for source_key, value in assets.items() if value['asset_id'] in missing]
    return {k: v for k, v in assets.items() if v['asset_id'] not in missing}, pending


def final_membership_audit(journal, rows, assets, helper):
    directory = journal / ('final-membership-' + uuid.uuid4().hex)
    request = membership_request(rows, assets)
    save(directory / 'request.json', request)
    with (directory / 'stdout.json').open('w') as stdout, (directory / 'stderr.log').open('w') as stderr:
        proc = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', str(helper),
                               str(directory / 'request.json')], stdout=stdout, stderr=stderr, timeout=180)
    result = read(directory / 'stdout.json')
    proof_path = directory / 'membership-result.json'
    save(proof_path, result)
    require(proc.returncode == 0, 'Final membership native read failed')
    current, pending = classify_membership(result, request, assets)
    return current, pending, proof_path, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', nargs='?', type=Path, default=RECORDS / '全量拍摄时间导入清单.json')
    parser.add_argument('--seed', type=Path, default=RECORDS / 'Photos样本最终验证.json')
    parser.add_argument('--journal', type=Path, default=RECORDS / 'Photos批量导入')
    parser.add_argument('--helper', type=Path, default=Path(__file__).with_name('photos_batch.js'))
    parser.add_argument('--album-helper', type=Path, default=Path(__file__).with_name('photos_album_audit.js'))
    parser.add_argument('--batch-size', type=int, default=50)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--sample', action='store_true', help='Verify/export 5..15 eligible items and write a passed seed')
    parser.add_argument('--export-verify', action='store_true', help='Also export and hash every eligible asset')
    args = parser.parse_args()
    args.export_verify = args.export_verify or args.sample
    require(1 <= args.batch_size <= 50, 'Batch size must be 1..50')
    args.journal, args.helper = args.journal.resolve(), args.helper.resolve()
    args.album_helper = args.album_helper.resolve()
    rows = read(args.manifest)
    require(len({key(row) for row in rows}) == len(rows), 'Duplicate QQ album/item identities')
    eligible = [row for row in rows if row.get('status') == 'capture_ready']
    pending = [row for row in rows if row.get('status') != 'capture_ready']
    by_source = {(task_album(row), row['pic_key']): row for row in eligible}
    assets, completed_dirs, proved, excluded, export_proofs = {}, [], {}, {}, {}
    previous_ledger = args.journal / 'verified-ledger.json'
    recovery_pending = read(previous_ledger).get('pending_recovery', []) if previous_ledger.exists() else []
    protected = {value['source_key'] for value in recovery_pending}
    sample_proofs = {}
    sample_proof_path = None
    if args.seed.exists():
        seed = read(args.seed)
        sample_proof_path = args.journal / 'sample-export-proofs' / (sha(args.seed) + '-' + sha(args.manifest) + '.json')
        require(seed.get('passed') is True, 'Seed sample has not passed')
        require(isinstance(seed.get('items'), list), 'Malformed sample items')
        require(5 <= len(seed['items']) <= 15, 'Passed sample seed must contain 5..15 source items')
        seed_asset_ids = set()
        for item in seed['items']:
            require(isinstance(item, dict) and item.get('task_album') and item.get('source_id'), 'Malformed sample identity')
            row = by_source.get((item['task_album'], item['source_id']))
            if row:
                require(item.get('capture_time_matches') is True and item.get('in_album') is True and
                        item.get('export_matches_import') is True and
                        item.get('epoch_seconds') == row['target_epoch'] and
                        item.get('export_sha256') == row['import_sha256'] and
                        isinstance(item.get('asset_id'), str) and item['asset_id'], 'Seed does not match verified copy/date')
                require(key(row) not in proved and item['asset_id'] not in seed_asset_ids, 'Duplicate sample source or asset ID')
                seed_asset_ids.add(item['asset_id'])
                assets[key(row)] = {'import_sha256': row['import_sha256'], **item}
                proved[key(row)] = row
                sample_proofs[key(row)] = {'asset_id': item['asset_id'],
                    'export_sha256': item['export_sha256'], 'epoch_seconds': item['epoch_seconds'],
                    'in_album': True, 'source_sample_proof': str(args.seed.resolve())}
                export_proofs[key(row)] = export_proof_record(key(row),
                    {**assets[key(row)], 'qq_album_id': row['album_id'], 'pic_key': row['pic_key']},
                    sample_proof_path)
    for directory in sorted(args.journal.glob('batch-*')):
        require((directory / 'completed.json').exists(), 'Unfinished batch; reconcile before any new import: ' + str(directory))
        completed_dirs.append(directory)
        completion = read(directory / 'completed.json')
        assets.update(completion['assets'])
        for item in completion.get('excluded_items', []):
            if item.get('resolved') is not True:
                excluded[item['source_key']] = item
        if completion.get('export_verified') is True:
            for row in eligible:
                item = completion['assets'].get(key(row), {})
                if (item.get('import_sha256') == row['import_sha256'] and item.get('in_album') is True and
                    item.get('epoch_seconds') == row['target_epoch'] and item.get('asset_id')):
                    proved[key(row)] = row
                    reference = completion.get('export_proof_references', {}).get(key(row), directory / 'completed.json')
                    reference = resolve_export_reference(reference, key(row), item)
                    export_proofs[key(row)] = export_proof_record(key(row), item, reference)
    for source_key in protected:
        require(source_key in assets, 'Pending recovery lacks original recorded asset; manual reconciliation required')
        proved.pop(source_key, None)
    sample_gate(rows, eligible, list(proved.values()), args.sample)
    require(not any(key(row) in excluded for row in eligible),
            'Previously excluded source is capture_ready; reconcile its exclusion before another import')
    for row in eligible:
        if key(row) in assets:
            require(assets[key(row)]['import_sha256'] == row['import_sha256'], 'Previously imported copy changed')
    todo = [row for row in eligible if key(row) not in assets and key(row) not in protected]
    summary = {'prepared_total': len(rows), 'eligible': len(eligible), 'already_recorded': len(eligible) - len(todo),
               'to_import': len(todo), 'not_imported_pending': len(pending),
               'pending_statuses': dict(Counter(row.get('status') for row in pending)),
               'sample_mode': args.sample, 'matching_previously_proved': len(proved),
               'protected_pending_recovery': len(recovery_pending),
               'unresolved_cleanup_count': sum(item.get('status') == 'awaiting_removal' for item in excluded.values())}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if not args.apply:
        return
    if sample_proofs:
        sample_evidence = {'all_passed': True, 'verified': sample_proofs,
            'missing_source_keys': [], 'problems': [], 'normalized_from': str(args.seed.resolve())}
        if sample_proof_path.exists():
            require(read(sample_proof_path) == sample_evidence, 'Prior normalized sample proof differs')
        else:
            save(sample_proof_path, sample_evidence)
    video_issues = video_date_mismatches(todo)
    save(args.journal / 'video-date-preflight.json', {'checked_videos': sum(
        row.get('file_type') in ('MP4', 'MOV', 'M4V', 'AVI') for row in todo), 'issues': video_issues})
    require(not video_issues, 'Video date preflight failed before import; inspect video-date-preflight.json and repair or hold the source items')
    save(args.journal / 'not-imported-pending.json', pending)
    # Re-verify recorded sample/checkpoint IDs natively before adding more media.
    existing = [row for row in eligible if key(row) in assets and key(row) not in protected]
    batches = [('verify', existing[i:i + args.batch_size]) for i in range(0, len(existing), args.batch_size)]
    batches += [('import', todo[i:i + args.batch_size]) for i in range(0, len(todo), args.batch_size)]
    album_counts = {}
    for offset, (op, batch) in enumerate(batches, len(completed_dirs) + 1):
        for row in batch:
            check_copy(row)
        directory = args.journal / f'batch-{offset:05d}'
        directory.mkdir(parents=True, exist_ok=False)
        save(directory / 'pending.json', {'operation': op, 'source_keys': [key(row) for row in batch],
             'prepared_rows': batch, 'known_assets': {key(row): assets[key(row)] for row in batch if key(row) in assets},
             'do_not_blindly_retry': True})
        if op == 'import':
            imported = native(args.helper, directory, 'import', native_rows(batch, {}, 'import'),
                              {'skip_check_duplicates': True, 'defer_import_mapping': True})
            returned = import_ids(imported, batch, args.helper, directory)
            save(directory / 'returned-asset-ids.json', returned)
            assets.update(returned)
            # Only formats explicitly approved by this migration may use native date repair.
            adjustments = [row for row in batch if row['file_type'] == 'GIF' or
                           row.get('native_date_override_verified') is True]
            if adjustments:
                native(args.helper, directory, 'set_dates', native_rows(adjustments, assets, 'set_dates'))
        verified = native(args.helper, directory, 'verify', native_rows(batch, assets, 'verify'))
        alias_evidence = prove_filename_aliases(args.helper, directory, verified, batch, assets, rows)
        checked = verified_assets(verified, batch, assets, alias_evidence)
        export_references, reused, freshly_exported = {}, {}, []
        if args.export_verify:
            if not args.sample:
                reused = {key(row): export_proofs[key(row)] for row in batch
                    if reusable_export_proof(row, checked[key(row)], export_proofs.get(key(row)))}
            freshly_exported = [row for row in batch if key(row) not in reused]
            if freshly_exported:
                export_verify(args.helper, directory, freshly_exported, assets)
            export_references = {key(row): reused[key(row)]['reference'] if key(row) in reused
                else str(directory / 'export-proof.json') for row in batch}
            save(directory / 'export-verification.json', {'all_passed': True,
                'reused_proofs': reused, 'fresh_export_source_keys': [key(row) for row in freshly_exported],
                'current_native_verification': str(directory / 'verify-result.json'),
                'current_copy_sha256_verified': True})
            for row in batch:
                export_proofs[key(row)] = export_proof_record(key(row), checked[key(row)], export_references[key(row)])
        assets.update(checked)
        for count in verified.get('album_counts', []):
            album_counts[count['task_album']] = count
        save(directory / 'completed.json', {'assets': checked, 'album_counts': verified.get('album_counts', []),
             'export_verified': args.export_verify, 'export_proof_references': export_references,
             'completed_utc': dt.datetime.now(dt.timezone.utc).isoformat()})
        print(json.dumps({'batch_complete': offset, 'assets_recorded': len(assets), 'batch_size': len(batch),
              'reused_export_proofs': len(reused), 'fresh_exports': len(freshly_exported)}), flush=True)
    tracked = {key(row): assets[key(row)] for row in eligible}
    tracked, missing_now, membership_path, membership = final_membership_audit(
        args.journal, eligible, tracked, args.album_helper)
    # A previously missing asset that reappears still needs an explicit user
    # decision and fresh reconciliation; membership alone cannot reinstate it.
    recovery_pending = list({value['source_key']: value for value in
                             recovery_pending + missing_now}.values())
    tracked = {source_key: value for source_key, value in tracked.items()
               if source_key not in {item['source_key'] for item in recovery_pending}}
    save(args.journal / 'pending-recovery.json', recovery_pending)
    summary.update(verified_assets=len(tracked), albums=[{'qq_album_id': album_id,
        'source_items_verified': sum(row['album_id'] == album_id and key(row) in tracked for row in eligible),
        'photos_album_ids': sorted({tracked[key(row)]['album_id'] for row in eligible
                                   if row['album_id'] == album_id and key(row) in tracked})}
        for album_id in sorted({row['album_id'] for row in eligible})],
        native_album_counts=membership['albums'], pending_recovery=len(recovery_pending))
    save(args.journal / 'verified-ledger.json', {'summary': summary, 'assets': tracked,
        'excluded_items': list(excluded.values()), 'pending_recovery': recovery_pending,
        'final_membership_proof': str(membership_path),
        'export_proof_references': {source_key: export_proofs[source_key]['reference']
                                    for source_key in tracked if source_key in export_proofs},
        'entire_collection_complete': not pending and not recovery_pending and
                                      set(tracked) <= set(export_proofs)})
    save(args.journal / 'not-imported-pending.json', pending)
    if args.sample:
        require(not recovery_pending, 'Sample asset missing at final audit; sample has not passed')
        # Reached only after every eligible asset passed date/membership and
        # original-export hash verification in this run, including reused IDs.
        items = [{**tracked[key(row)], 'source_id': row['pic_key'],
                  'capture_time_matches': True, 'export_matches_import': True,
                  'export_sha256': row['import_sha256']} for row in eligible]
        save(args.seed, {'passed': True, 'items': items, 'sample_count': len(items),
            'not_imported_pending': len(pending), 'pending_statuses': summary['pending_statuses'],
            'entire_collection_complete': False, 'manifest_sha256': sha(args.manifest),
            'verified_utc': dt.datetime.now(dt.timezone.utc).isoformat()})
        summary['sample_passed'] = True
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
