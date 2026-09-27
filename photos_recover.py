"""Reconcile one unfinished Photos batch without importing or editing assets.

Default only reads the durable journal and prints a plan. --apply may read native
Photos IDs/dates/membership and export originals to prove their SHA-256. It never
calls import, set_dates, delete or restore. Missing/partial returned IDs require
manual reconciliation; do not delete the unfinished batch or rerun its import.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import uuid

import photos_pipeline as p


def import_result(directory):
    """Use recorded return events, including a crash after the native return."""
    candidates = []
    for name in ('import-result.json', 'import-stdout.json'):
        path = directory / name
        if path.exists():
            try:
                value = p.read(path)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(value, dict) and isinstance(value.get('imports'), list):
                candidates.append(value['imports'])
    path = directory / 'returned-import-groups.json'
    if path.exists():
        candidates.append(p.read(path))
    path = directory / 'import-stderr.log'
    if path.exists():
        groups = []
        for line in path.read_text().splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get('event') == 'import_returned':
                groups.append(value['group'])
        if groups:
            candidates.append(groups)
    p.require(candidates, 'No durable returned IDs. Keep the journal; manually locate the imported assets by original export SHA, date and album. Do not reimport.')
    def identities(groups):
        return sorted((g.get('task_album'), item.get('asset_id'), item.get('filename'))
                      for g in groups for item in g.get('returned', []))
    # Partial snapshots from before another album finished are allowed only if
    # every recorded return is identical to the larger durable snapshot.
    groups = max(candidates, key=lambda value: len(identities(value)))
    largest = identities(groups)
    p.require(all(set(identities(value)) <= set(largest) for value in candidates),
              'Conflicting durable import returns; manual reconciliation required')
    return {'operation': 'import', 'imports': groups}


def recovery_plan(directory, known_ledger=None):
    p.require(not (directory / 'completed.json').exists(), 'Batch is already completed; do not overwrite its proof')
    pending = p.read(directory / 'pending.json')
    rows = pending.get('prepared_rows', [])
    p.require(1 <= len(rows) <= 50 and len({p.key(row) for row in rows}) == len(rows), 'Invalid prepared batch')
    p.require(pending.get('source_keys') == [p.key(row) for row in rows] and
              all(row.get('status') == 'capture_ready' for row in rows), 'Pending batch/source status differs')
    operation = pending.get('operation')
    imported, assets = None, {}
    if operation == 'import':
        imported = import_result(directory)
        returned = [item for group in imported['imports'] for item in group.get('returned', [])]
        p.require(len(returned) == len(rows) and len({item.get('asset_id') for item in returned}) == len(rows)
                  and all(item.get('asset_id') and item.get('filename') for item in returned),
                  'Incomplete/nonunique returned IDs: automatic recovery is blocked. Keep all journals; manually map every missing source to its Photos ID using original export SHA, date and album before an independently reviewed recovery. Never reimport the entire batch.')
    elif operation == 'verify':
        assets.update(pending.get('known_assets', {}))
        if known_ledger:
            for source_key, value in p.read(known_ledger).get('assets', {}).items():
                if source_key in assets:
                    p.require(assets[source_key]['asset_id'] == value['asset_id'], 'Known ledger contradicts pending asset ID')
                else:
                    assets[source_key] = value
        for row in rows:
            value = assets.get(p.key(row), {})
            p.require(value.get('asset_id') and value.get('import_sha256') == row['import_sha256'],
                      'Verify batch lacks matching known IDs; supply its prior --known-ledger, never guess')
        assets = {p.key(row): assets[p.key(row)] for row in rows}
        p.require(len({value['asset_id'] for value in assets.values()}) == len(rows), 'Duplicate known Photos IDs')
    else:
        p.require(False, 'Only unfinished import/verify batches can be reconciled')
    return rows, assets, imported


def recover(directory, helper, *, apply=False, known_ledger=None):
    directory = directory.resolve()
    rows, assets, imported = recovery_plan(directory, known_ledger)
    summary = {'batch': str(directory), 'source_count': len(rows), 'apply': apply,
               'photos_mutations': False, 'will_import': False,
               'operations': ['verify IDs/date/album', 'export and hash originals', 'write completed proof']}
    if not apply:
        return summary
    for row in rows:
        p.check_copy(row)
    recovery = directory / ('recovery-' + uuid.uuid4().hex)
    recovery.mkdir(parents=True, exist_ok=False)
    p.save(recovery / 'plan.json', summary)
    if imported is not None:
        assets = p.import_ids(imported, rows, helper, recovery)
    p.save(recovery / 'returned-asset-ids.json', assets)
    verified = p.native(helper, recovery, 'verify', p.native_rows(rows, assets, 'verify'))
    aliases = p.prove_filename_aliases(helper, recovery, verified, rows, assets, rows)
    checked = p.verified_assets(verified, rows, assets, aliases)
    p.export_verify(helper, recovery, rows, checked)
    # Fresh read after potentially slow exports catches an intervening library
    # change. A new alias still requires another actual exported-byte proof.
    final_dir = recovery / 'final-native-verification'
    final = p.native(helper, final_dir, 'verify', p.native_rows(rows, checked, 'verify'))
    aliases = p.prove_filename_aliases(helper, final_dir, final, rows, checked, rows)
    checked = p.verified_assets(final, rows, checked, aliases)
    reference = recovery / 'export-proof.json'
    for source_key, value in checked.items():
        p.resolve_export_reference(reference, source_key, value)
    p.require(not (directory / 'completed.json').exists(), 'Another recovery completed this batch; do not overwrite')
    p.save(directory / 'completed.json', {'assets': checked, 'album_counts': final.get('album_counts', []),
        'export_verified': True, 'export_proof_references': {p.key(row): str(reference) for row in rows},
        'recovered_without_import_or_edit': True, 'recovery_directory': str(recovery),
        'completed_utc': dt.datetime.now(dt.timezone.utc).isoformat()})
    return {**summary, 'completed': True, 'recovery_directory': str(recovery)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('batch', type=Path, help='One unfinished batch-* directory')
    parser.add_argument('--helper', type=Path, default=Path(__file__).with_name('photos_batch.js'))
    parser.add_argument('--known-ledger', type=Path, help='Prior verified ledger for legacy verify batches missing known_assets')
    parser.add_argument('--apply', action='store_true', help='Read/export existing IDs and write proof; never import or edit Photos')
    args = parser.parse_args()
    result = recover(args.batch, args.helper.resolve(), apply=args.apply, known_ledger=args.known_ledger)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
