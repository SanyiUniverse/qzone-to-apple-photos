"""Select a small, diverse explicit test batch from saved QQ metadata, without network access."""
import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


def timestamp(value):
    for pattern in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value).strip(), pattern)
        except ValueError:
            pass
    return None


def select(session, count, known_formats):
    candidates = []
    for key, (photo, raw) in session.index.items():
        date = timestamp((raw.get("exif") or {}).get("originalTime")) or timestamp(raw.get("rawshoottime"))
        extension = Path(urlsplit(str(photo.url)).path).suffix.lower() or Path(photo.name).suffix.lower()
        kind = known_formats.get(key) or ({".png": "PNG", ".gif": "GIF", ".heic": "HEIC", ".webp": "WEBP"}.get(extension))
        if not kind:
            kind = "video" if photo.is_video else {"2": "GIF_hint", "3": "PNG_hint"}.get(str(raw.get("phototype")), "photo_unspecified")
        features = {f"year:{date.year if date else 'unknown'}", f"media:{'video' if photo.is_video else 'photo'}", f"format:{kind}"}
        if raw.get("raw") or raw.get("origin_url"):
            features.add("original_url_available")
        candidates.append(dict(key=key, date=date, features=features, kind=kind, video=photo.is_video))
    candidates.sort(key=lambda item: (item["date"] or datetime.max, item["key"]))
    chosen, used = [], set()
    def add(item):
        if item and item not in chosen and len(chosen) < count:
            chosen.append(item)
            used.update(item["features"])
    dated = [item for item in candidates if item["date"]]
    if dated:
        add(dated[0])
        add(dated[-1])
    for wanted in ("GIF", "PNG"):
        matching = [item for item in candidates if item["kind"].startswith(wanted)]
        if matching:
            add(max(matching, key=lambda item: "original_url_available" in item["features"]))
    videos = [item for item in candidates if item["video"]]
    if videos:
        add(videos[0])
        if count >= 10:
            add(videos[-1])
    while len(chosen) < min(count, len(candidates)):
        remaining = [item for item in candidates if item not in chosen]
        def score(item):
            return sum(5 if feature.startswith("year:") else 3 if feature.startswith("format:") else 1
                       for feature in item["features"] - used) + (0.25 if "original_url_available" in item["features"] else 0)
        add(max(remaining, key=score))
    return [dict(album_id=item["key"][0], pic_key=item["key"][1], reason=sorted(item["features"])) for item in chosen]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="Existing private collection directory")
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if args.count < 1 or output.is_relative_to(Path(__file__).resolve().parent):
        parser.error("Use a positive count and a private output directory outside the source checkout")
    os.umask(0o077)
    os.environ["QZONE_MIGRATION_OUTPUT"] = str(output)
    import batch_download
    import migrate_session as migration
    session = batch_download.load_saved()
    formats = {}
    for file in (output / "迁移记录").glob("*/*-download.json"):
        record = json.loads(file.read_text())
        if record.get("file_type") and record["file_type"] != "UNKNOWN":
            formats[(record["album"]["album_id"], record["pic_key"])] = record["file_type"]
    selection = select(session, args.count, formats)
    migration.dump(output / "sample.json", selection)
    migration.say(sample_items=len(selection), features=dict(Counter(feature for item in selection for feature in item["reason"])),
                  note="API format hints must be verified after download; upload dates are not capture evidence")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps(dict(status="error", error=type(exc).__name__)), flush=True)
        raise SystemExit(1)
