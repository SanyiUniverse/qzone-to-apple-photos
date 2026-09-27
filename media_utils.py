"""Local-only metadata and checksum helpers."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
from migrate_session import OUT
ROOT = OUT
RECORDS = OUT / '迁移记录'
TZ = dt.timezone(dt.timedelta(hours=8))

def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def metadata(paths):
    if not paths:
        return {}
    result = subprocess.run(['exiftool', '-json', '-G1', '-a', '-n', '-api',
        'ImageHashType=SHA256', '-all', '-ImageDataHash', *map(str, paths)],
        check=True, capture_output=True, text=True)
    return {Path(v['SourceFile']).resolve(): v for v in json.loads(result.stdout)}
