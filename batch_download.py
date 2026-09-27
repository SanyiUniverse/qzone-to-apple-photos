"""Offline coordinator: reuse saved QQ inventory, cached video URLs and upstream download code."""
import argparse
import hashlib
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import migrate_session as migration


class OfflineSession(migration.Session):
    def _access_qzone_api(self, *args, **kwargs):
        raise RuntimeError("QQ_API_DISABLED_IN_OFFLINE_BATCH")

    def get_video_download_url(self, owner, album_id, pic_key):
        return self.video_urls.get((album_id, pic_key), "")


def load_saved():
    session = OfflineSession()
    records = migration.OUT / "迁移记录"
    session.album_map = json.loads((records / "相册映射.json").read_text())
    snapshots = records / "QQ原始响应"
    for file in sorted(snapshots.glob("*/列表/*.json"), key=lambda p: (p.stat().st_mtime_ns, str(p))):
        album_id = file.parent.parent.name
        for row in json.loads(file.read_text())["data"].get("photoList", []):
            url = next((row[k] for k in ("raw", "origin_url", "url", "custom_url", "lloc", "sloc") if row.get(k)), None)
            key = row.get("lloc") or row.get("sloc") or ""
            if not key or not url or album_id not in session.album_map:
                raise RuntimeError("INCOMPLETE_SAVED_INVENTORY")
            photo = migration.core.QzonePhoto(url, row.get("name", "untitled").strip(),
                session.album_map[album_id]["source_name"], bool(row.get("is_video") or row.get("phototype") == "video"),
                key, row.get("exif", {}), row.get("rawshoottime", ""), row.get("uploadtime", ""), row.get("cameratype", "").strip())
            session.index[(album_id, key)] = (photo, row)
    counts = Counter(a for a, _ in session.index)
    if any(counts[a] != int(info["source_count"]) for a, info in session.album_map.items()):
        raise RuntimeError("SAVED_INVENTORY_COUNT_MISMATCH")
    session.video_urls = {}
    for file in sorted(snapshots.glob("*/*详情/*.json"), key=lambda p: (p.stat().st_mtime_ns, str(p))):
        for row in json.loads(file.read_text()).get("data", {}).get("photos", []):
            key = (file.parent.parent.name, row.get("picKey"))
            url = (row.get("video_info") or {}).get("download_url")
            if key in session.index and url and isinstance(url, str):
                session.video_urls[key] = url
    return session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Explicitly download; otherwise only show coverage")
    parser.add_argument("--selection", type=Path, help="Explicit JSON list of album_id/pic_key items, such as private sample.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--photos-only", action="store_true")
    mode.add_argument("--videos-only", action="store_true")
    parser.add_argument("--missing-details", action="store_true", help="Emit one small live-session detail command; rerun after it finishes")
    args = parser.parse_args()
    session = load_saved()
    missing = [key for key, (photo, _) in session.index.items() if photo.is_video and key not in session.video_urls
               and not session.validated_record(*key, update=False)]
    if args.missing_details:
        if missing:
            remaining = set(missing)
            groups = {album: [key for key in session.index if key[0] == album] for album, _ in missing}
            key = max(missing, key=lambda key: sum(k in remaining for k in groups[key[0]][max(0, groups[key[0]].index(key)-9):groups[key[0]].index(key)+19]))
            command = json.dumps(dict(cmd="detail", album_id=key[0], pic_key=key[1]), ensure_ascii=False)
            if len(command.encode()) >= 900:
                raise RuntimeError("DETAIL_COMMAND_TOO_LONG")
            print(command, flush=True)
        return
    selected = [key for key, (photo, _) in session.index.items()
                if not (args.photos_only and photo.is_video) and not (args.videos_only and not photo.is_video)]
    if args.selection:
        requested = json.loads(args.selection.read_text())
        if not isinstance(requested, list) or not requested:
            raise ValueError("NONEMPTY_SELECTION_LIST_REQUIRED")
        keys = [(str(item["album_id"]), str(item["pic_key"])) for item in requested]
        if len(set(keys)) != len(keys) or any(key not in session.index for key in keys):
            raise ValueError("SELECTION_ITEM_UNKNOWN_OR_DUPLICATED")
        selected = [key for key in keys if key in selected]
    migration.say(inventory=len(session.index), selected=len(selected), cached_video_urls=len(session.video_urls), missing_video_details=len(missing))
    if not args.download:
        return
    if any(key in missing for key in selected):
        raise RuntimeError("MISSING_CACHED_VIDEO_DETAILS")
    results = []
    def run(key):
        try:
            result = session.download([dict(album_id=key[0], pic_key=key[1])], quiet=True)[0]
            return dict(album_id=key[0], pic_key=key[1], status=result["status"], cached=result["cached"], file_type=result["file_type"], bytes=result["bytes"])
        except Exception as exc:
            return dict(album_id=key[0], pic_key=key[1], status="failed", error=type(exc).__name__)
    def collect(result):
        results.append(result)
        if len(results) % 25 == 0 or len(results) == len(selected):
            migration.say(done=len(results), total=len(selected), statuses=dict(Counter(r["status"] for r in results)), cached=sum(r.get("cached", False) for r in results))
            migration.dump(migration.OUT / "迁移记录" / "批量下载结果.json", results)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run, key) for key in selected]
        for future in as_completed(futures):
            collect(future.result())
    if any(r["status"] != "verified_original" for r in results):
        raise RuntimeError("BATCH_HAS_FAILED_OR_UNIDENTIFIED_ITEMS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        migration.say(status="error", error=type(exc).__name__)
        sys.exit(1)
