"""Prepare capture-date copies with ExifTool. Default is a read-only media plan.

Use ExifTool inspection/hash helpers and the audited dslwind field mapping.
No downloads or Photos operations. Explicit --apply is required to create copies.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from media_utils import OUT, RECORDS, ROOT, TZ, metadata, sha

VERSION = 1
ZERO = '0000:00:00 00:00:00'
IMAGE_TYPES = {'JPEG', 'PNG', 'HEIC', 'HEIF', 'TIFF'}
VIDEO_TYPES = {'MP4', 'MOV', 'M4V'}
MAPPING = {'make': 'IFD0:Make', 'model': 'IFD0:Model', 'lensModel': 'ExifIFD:LensModel',
    'exposureTime': 'ExifIFD:ExposureTime', 'fnumber': 'ExifIFD:FNumber',
    'iso': 'ExifIFD:ISO', 'focalLength': 'ExifIFD:FocalLength',
    'exposureCompensation': 'ExifIFD:ExposureCompensation', 'flash': 'ExifIFD:Flash',
    'exposureMode': 'ExifIFD:ExposureMode', 'exposureProgram': 'ExifIFD:ExposureProgram',
    'meteringMode': 'ExifIFD:MeteringMode'}
CAPTURE_TAGS = ('ExifIFD:DateTimeOriginal', 'XMP-exif:DateTimeOriginal',
                'XMP-photoshop:DateCreated')


def date(value):
    """Accept a complete real date, preserving any source offset/subseconds."""
    value = str(value or '').strip()
    if not re.match(r'^\d{4}[:-]\d\d[:-]\d\d[ T]\d\d:\d\d:\d\d', value):
        return None
    value = re.sub(r'^(\d{4}):(\d\d):(\d\d)', r'\1-\2-\3', value)
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def wall(value):
    value = date(value) if not isinstance(value, dt.datetime) else value
    return value.replace(tzinfo=None) if value else None


def exif_date(value):
    return value.strftime('%Y:%m:%d %H:%M:%S')


def fallback_classification(record):
    """Same conservative rules without depending on this task's private audit."""
    raw = record['raw_fields']
    original = date((raw.get('exif') or {}).get('originalTime'))
    shoot = date(raw.get('rawshoottime'))
    candidate = original or (shoot if shoot and shoot.time() != dt.time() else None)
    source = 'qq.exif.originalTime' if original else 'qq.rawshoottime' if candidate else None
    name = record.get('original_name') or raw.get('name') or ''
    match = re.search(r'截图\s*(\d{4}-\d\d-\d\d)[ _T]+(\d\d)[-:](\d\d)[-:](\d\d)', name)
    if not candidate and match:
        candidate = date(f'{match[1]} {match[2]}:{match[3]}:{match[4]}')
        if candidate:
            source = 'filename.screenshot'
    uploaded = date(raw.get('uploadtime'))
    anomaly = candidate and uploaded and wall(candidate) > wall(uploaded)
    return {'classification': 'capture_candidate' if candidate else 'date_only' if shoot else 'unknown',
        'fullTimestamp': candidate.isoformat() if candidate else None,
        'dateOnly': (candidate or shoot).date().isoformat() if candidate or shoot else None,
        'precision': 'second' if candidate else 'day' if shoot else 'unknown', 'source': source,
        'clockAnomalies': ['capture follows upload'] if anomaly else [],
        'sourceValues': {k: raw.get(k) for k in ('rawshoottime', 'shoottime', 'uploadtime')} |
                        {'originalTime': (raw.get('exif') or {}).get('originalTime'), 'originalName': name}}


def load_details():
    details = {}
    for path in (RECORDS / 'QQ原始响应').glob('*/*/*.json'):
        if path.parent.name == '列表':
            continue
        for item in json.loads(path.read_text()).get('data', {}).get('photos', []):
            details[(item.get('albumId') or item.get('topicId') or path.parents[1].name,
                     item.get('picKey') or item.get('lloc'))] = item
    return details


def qt_transcode_evidence(meta, raw, candidate):
    """QQ upload-near UTC movie date + matching track dates + distinct capture.

    This is an explicit inference, retained in each record, not blanket authority
    to replace every nonzero video creation date.
    """
    created = date(meta.get('QuickTime:CreateDate'))
    uploaded = date(raw.get('uploadtime'))
    tracks = {k: v for k, v in meta.items()
              if re.fullmatch(r'Track\d+:(Track|Media)CreateDate', k) and date(v)}
    if not created or not uploaded or not candidate or not tracks:
        return None
    utc = created.replace(tzinfo=dt.timezone.utc) if created.tzinfo is None else created
    delta = (utc.astimezone(TZ) - uploaded.replace(tzinfo=TZ)).total_seconds()
    same_tracks = all(abs((wall(v) - wall(created)).total_seconds()) <= 2
                      for v in tracks.values())
    separation = (utc.astimezone(TZ).replace(tzinfo=None) - wall(candidate)).total_seconds()
    if 0 <= delta <= 1800 and same_tracks and separation > 1:
        return {'rule': 'movie UTC date 0..1800s after QQ upload; all present '
                        'track/media creation dates agree within 2s; movie time is >1s after capture',
                'upload_delta_seconds': delta, 'capture_separation_seconds': separation,
                'old_fields': {'QuickTime:CreateDate': meta['QuickTime:CreateDate'], **tracks},
                'confidence': 'upload-related processing inference corroborated by local samples'}
    return None


def choose_capture(record, meta, classification):
    raw = record['raw_fields']
    qq_exif = date((raw.get('exif') or {}).get('originalTime'))
    raw_shoot = date(raw.get('rawshoottime'))
    candidate = qq_exif or (raw_shoot if raw_shoot and raw_shoot.time() != dt.time() else None)
    source = 'qq.exif.originalTime' if qq_exif else 'qq.rawshoottime' if candidate else None
    # Only the independently audited explicit screenshot pattern is allowed.
    if not candidate and classification.get('source') == 'filename.screenshot':
        candidate = date(classification.get('fullTimestamp'))
        source = 'filename.screenshot'
    flags, conflicts = [], []
    if qq_exif and raw_shoot and wall(qq_exif) != wall(raw_shoot):
        conflicts.append('QQ originalTime differs from rawshoottime')
    embedded = [(tag, date(meta.get(tag))) for tag in CAPTURE_TAGS if date(meta.get(tag))]
    if record.get('is_video'):
        embedded += [(tag, date(meta.get(tag))) for tag in
                     ('Keys:CreationDate', 'UserData:DateTimeOriginal', 'QuickTime:DateTimeOriginal')
                     if date(meta.get(tag))]
    if embedded:
        tag, chosen = embedded[0]
        if tag == 'ExifIFD:DateTimeOriginal' and chosen.tzinfo is None:
            offset = str(meta.get('ExifIFD:OffsetTimeOriginal') or '')
            if re.fullmatch(r'[+-]\d\d:\d\d', offset):
                chosen = date(chosen.isoformat() + offset) or chosen
        if candidate and wall(chosen) != wall(candidate):
            conflicts.append('Existing embedded capture differs from QQ/name candidate; preserved')
        if any(wall(other) != wall(chosen) for _, other in embedded[1:]):
            conflicts.append('Existing embedded capture fields disagree; preserved')
        candidate, source = chosen, 'embedded.' + tag
    if classification.get('clockAnomalies'):
        flags.append('QQ capture/upload clock anomaly; source capture retained')
    return candidate, source, flags, conflicts


def plan_item(record, original, meta, classification, detail):
    candidate, source, flags, conflicts = choose_capture(record, meta, classification)
    kind, raw = meta.get('File:FileType'), record['raw_fields']
    target = OUT / '导入副本' / record['album']['photos_album'] / original.name
    tags, evidence = {}, None
    if kind in IMAGE_TYPES:
        for qq, tag in MAPPING.items():
            raw_value = (raw.get('exif') or {}).get(qq)
            value = str(raw_value if raw_value is not None else '').strip()
            if qq == 'fnumber' and value:
                from fractions import Fraction
                try:
                    plausible = 0 < float(Fraction(value)) <= 1024
                except (ValueError, ZeroDivisionError):
                    plausible = False
                if not plausible:
                    flags.append('Invalid QQ fnumber retained in source record, not written: ' + value)
                    continue
            if value and tag not in meta:
                tags[tag] = value
        if candidate and not conflicts and 'ExifIFD:DateTimeOriginal' not in meta:
            tags['ExifIFD:DateTimeOriginal'] = exif_date(candidate)
    epoch = detail.get('shootTime') or classification.get('qqDetailShootTimeEpoch')
    epoch_agrees = bool(candidate and isinstance(epoch, (int, float)) and epoch > 0
                       and wall(dt.datetime.fromtimestamp(epoch, TZ)) == wall(candidate))
    if kind in VIDEO_TYPES and candidate and not conflicts:
        # Offsets belong to the QQ representation, not an invented camera timezone.
        zoned = candidate if candidate.tzinfo else candidate.replace(tzinfo=TZ) if epoch_agrees else None
        evidence = qt_transcode_evidence(meta, raw, candidate)
        current = date(meta.get('QuickTime:CreateDate'))
        if current and not evidence:
            current_utc = current.replace(tzinfo=dt.timezone.utc) if not current.tzinfo else current
            comparable = zoned and (current_utc - zoned).total_seconds() == 0
            if not comparable:
                conflicts.append('Nonzero QuickTime CreateDate has ambiguous provenance; preserved')
        if not zoned:
            flags.append('Video capture offset unproved: no matching QQ shootTime epoch; no date write')
        elif not conflicts:
            value = exif_date(zoned) + zoned.strftime('%z')
            if 'Keys:CreationDate' not in meta:
                tags['Keys:CreationDate'] = value
            if meta.get('QuickTime:CreateDate') in (None, ZERO) or evidence:
                tags['QuickTime:CreateDate'] = value
            for tag, old in meta.items():
                if re.fullmatch(r'Track\d+:(Track|Media)CreateDate', tag):
                    if old == ZERO or (evidence and tag in evidence['old_fields']):
                        tags[tag] = value
    if conflicts:
        # Preserve every date under dispute; only independent missing camera fields may be added.
        tags = {k: v for k, v in tags.items() if not k.endswith(('Date', 'DateTimeOriginal'))}
    if kind == 'GIF':
        tags = {}
        flags.append('GIF bytes unchanged; sourced capture date assigned through native Photos only')
    status = 'needs_review' if conflicts else 'capture_ready' if candidate else classification.get('classification', 'unknown')
    if kind not in IMAGE_TYPES | VIDEO_TYPES | {'GIF'}:
        status = 'needs_review'
        flags.append('Unsupported media type; no date repair or Photos import: ' + str(kind))
    if kind in VIDEO_TYPES and candidate and not epoch_agrees and not candidate.tzinfo:
        status = 'needs_review'
    # target_epoch is solely a Photos display representation for source wall time.
    represented = candidate if candidate and candidate.tzinfo else candidate.replace(tzinfo=TZ) if candidate else None
    return {'schema_version': VERSION, 'path': str(target), 'original_path': str(original),
        'album': record['album']['photos_album'], 'album_id': record['album']['album_id'],
        'pic_key': record['pic_key'], 'original_sha256': record['sha256'], 'file_type': kind,
        'status': status, 'target_source': source,
        'capture_wall_time': wall(candidate).isoformat(' ') if candidate else None,
        'target_iso': represented.isoformat() if represented else None,
        'target_epoch': int(represented.timestamp()) if represented else None,
        'date_only': classification.get('dateOnly') if not candidate else None,
        'precision': 'second' if candidate else classification.get('precision', 'unknown'),
        'timezone_status': ('embedded offset preserved' if candidate and candidate.tzinfo else
            'QQ shootTime epoch confirms +08 display' if epoch_agrees else
            'source wall clock; +08 Photos display convention only, no camera offset tag invented'),
        'qq_source_values': classification.get('sourceValues', {}), 'flags': flags,
        'conflicts': conflicts, 'transcode_evidence': evidence, 'write_tags': tags,
        'repaired_fields': list(tags),
        'original_embedded_dates': {k: v for k, v in meta.items() if 'Date' in k or 'TimeOriginal' in k}}


def validate(original, target, before, after, allowed, extra_preserved_exceptions=()):
    if sha(original) == sha(target):
        payload_ok = True
    else:
        payload_ok = bool(before.get('File:ImageDataHash') and
                          before['File:ImageDataHash'] == after.get('File:ImageDataHash'))
    if not payload_ok:
        raise RuntimeError('Media payload verification failed: ' + str(target))
    for key, value in before.items():
        group, _, name = key.partition(':')
        if group in ('System', 'File', 'Composite', 'ExifTool') or key == 'SourceFile':
            continue
        if key in allowed or key in extra_preserved_exceptions or name.endswith(('Offset', 'Offsets', 'ByteCount', 'ByteCounts')):
            continue
        # Adding an mdta Keys directory changes the last metadata handler exposed
        # by -G1 JSON; the audio/video Track handlers are still checked verbatim.
        if key == 'QuickTime:HandlerType' and 'Keys:CreationDate' in allowed and \
                value == 'mdir' and after.get(key) == 'mdta':
            continue
        if after.get(key) != value:
            raise RuntimeError('Existing semantic metadata changed: ' + key)
    for key, wanted in allowed.items():
        got = after.get(key)
        if key.startswith(('QuickTime:', 'Track')) and key.endswith('CreateDate'):
            expected = date(wanted).astimezone(dt.timezone.utc).replace(tzinfo=None)
            okay = wall(got) == expected
        elif date(wanted):
            okay = (date(got) == date(wanted) if date(wanted).tzinfo else wall(got) == wall(wanted))
        else:
            # ExifTool rational values are read numerically under -n.
            try:
                from fractions import Fraction
                okay = abs(float(got) - float(Fraction(str(wanted)))) < 1e-6
            except (TypeError, ValueError, ZeroDivisionError):
                okay = str(got) == str(wanted)
        if not okay:
            raise RuntimeError(f'Written metadata did not verify: {key}: {got!r} != {wanted!r}')
    return sha(target)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.pending')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)


def checkpoint_path(item):
    item_id = Path(item['path']).stem.rsplit('__', 1)[-1]
    return RECORDS / item['album_id'] / (item_id + '-capture-prepared.json')


def apply_batch(items, before, known):
    """One ExifTool write/read per batch; never overwrite an unverified copy."""
    temps, pending, existing_paths = [], [], []
    repair_file = None
    try:
        for item in items:
            target = Path(item['path'])
            checkpoint = checkpoint_path(item)
            existing = json.loads(checkpoint.read_text()) if checkpoint.exists() else known.get(target)
            if target.exists():
                if not existing or existing.get('original_sha256') != item['original_sha256'] or \
                        sha(target) != existing.get('import_sha256'):
                    raise RuntimeError('Existing copy lacks a verified checkpoint; not overwritten: ' + str(target))
                existing_paths.append(target)
                pending.append((item, target, existing))
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                handle, name = tempfile.mkstemp(prefix='.capture-', suffix=target.suffix, dir=target.parent)
                os.close(handle)
                temp = Path(name)
                temps.append(temp)
                shutil.copyfile(Path(item['original_path']), temp)
                pending.append((item, temp, None))
        repairs = [{'SourceFile': str(path), **item['write_tags']}
                   for item, path, existing in pending if not existing and item['write_tags']]
        run_result = None
        if repairs:
            handle, name = tempfile.mkstemp(prefix='.capture-repairs-', suffix='.json', dir=RECORDS)
            os.close(handle)
            repair_file = Path(name)
            repair_file.write_text(json.dumps(repairs, ensure_ascii=False))
            command = ['exiftool', '-n', '-P', '-overwrite_original', '-api', 'QuickTimeUTC=1',
                       '-json=' + str(repair_file)] + [r['SourceFile'] for r in repairs]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            run_result = {'stdout': result.stdout, 'stderr': result.stderr}
        after_all = metadata(existing_paths + temps)
        # Validate every item before publishing any new copy from this batch.
        for item, path, existing in pending:
            original = Path(item['original_path'])
            after = after_all[path.resolve()]
            extra_exceptions = []
            for tag in (existing or {}).get('repaired_fields', []):
                old = before[original].get(tag)
                if tag in after and (old in (None, ZERO) or
                    (item['transcode_evidence'] and tag in item['transcode_evidence']['old_fields'])):
                    extra_exceptions.append(tag)
            digest = validate(original, path, before[original], after, item['write_tags'], extra_exceptions)
            if sha(original) != item['original_sha256']:
                raise RuntimeError('Original changed during preparation')
            item.update(import_sha256=digest, media_payload_unchanged=True,
                        reused_verified_copy=bool(existing))
            if before[original].get('QuickTime:HandlerType') != after.get('QuickTime:HandlerType'):
                item['metadata_directory_handler_change'] = {
                    'old': before[original].get('QuickTime:HandlerType'),
                    'new': after.get('QuickTime:HandlerType'),
                    'reason': 'Keys metadata directory; media track handlers preserved'}
            if not existing and item['write_tags']:
                item['exiftool_batch_result'] = run_result
        for item, path, existing in pending:
            if not existing:
                path.replace(Path(item['path']))
            Path(item['original_path']).chmod(0o444)
            atomic_json(checkpoint_path(item), item)
        return items
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)
        if repair_file:
            repair_file.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Create/repair new copies after plan review')
    parser.add_argument('--classification', type=Path, default=RECORDS / '拍摄时间分类.json')
    parser.add_argument('--reuse-manifest', type=Path, default=RECORDS / '拍摄时间导入清单.json')
    args = parser.parse_args()
    classes = ({(x['albumId'], x['picKey']): x for x in json.loads(args.classification.read_text())['items']}
               if args.classification.exists() else {})
    details = load_details()
    downloads = [json.loads(p.read_text()) for p in sorted(RECORDS.glob('*/*-download.json'))]
    originals = [(OUT / r['path']).resolve() for r in downloads]
    cache_path = RECORDS / '原件元数据全量缓存.json'
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    before = {}
    missing = []
    hashes = {p: r['sha256'] for p, r in zip(originals, downloads)}
    for original in originals:
        saved = cache.get(str(original), {})
        if saved.get('sha256') == hashes[original]:
            before[original] = saved['metadata']
        else:
            missing.append(original)
    for start in range(0, len(missing), 100):
        observed = metadata(missing[start:start + 100])
        before.update(observed)
        for path, values in observed.items():
            cache[str(path)] = {'sha256': hashes[path], 'metadata': values}
    atomic_json(cache_path, cache)
    plans = []
    for record, original in zip(downloads, originals):
        if sha(original) != record['sha256']:
            raise RuntimeError('Untouched original checksum mismatch: ' + str(original))
        key = (record['album']['album_id'], record['pic_key'])
        classification = classes.get(key) or fallback_classification(record)
        plans.append(plan_item(record, original, before[original], classification, details.get(key, {})))
    atomic_json(RECORDS / '拍摄时间准备计划.json', plans)
    if not args.apply:
        print(json.dumps({'planned': len(plans), 'media_modified': False,
            'review': sum(p['status'] == 'needs_review' for p in plans)}, ensure_ascii=False))
        return
    known = {}
    for manifest in (RECORDS / '导入清单.json', args.reuse_manifest):
        if manifest.exists():
            known.update({Path(p['path']).resolve(): p for p in json.loads(manifest.read_text())})
    completed = []
    for start in range(0, len(plans), 100):
        completed.extend(apply_batch(plans[start:start + 100], before, known))
        print(json.dumps({'prepared_checkpointed': len(completed), 'total': len(plans)}), file=sys.stderr, flush=True)
    atomic_json(RECORDS / '全量拍摄时间导入清单.json', completed)
    atomic_json(RECORDS / '全量拍摄时间待确认.json', [p for p in completed if p['status'] != 'capture_ready'])
    print(json.dumps({'prepared': len(completed), 'all_payload_checks_passed': True,
        'pending': sum(p['status'] != 'capture_ready' for p in completed)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
