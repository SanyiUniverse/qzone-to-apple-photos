"""Collect your QQ inventory and video links; never download media or import Photos."""
import argparse
import json
import os
import sys
import uuid
from pathlib import Path


def choose_detail(index, missing):
    """Use QQ's existing 9-previous + selected + 18-following response window."""
    groups = {}
    for key in index:
        groups.setdefault(key[0], []).append(key)
    positions = {key: n for group in groups.values() for n, key in enumerate(group)}
    return max(sorted(missing), key=lambda key: sum(candidate in missing for candidate in
        groups[key[0]][max(0, positions[key] - 9):positions[key] + 19]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True, help="Your QQ account number; never supply a password")
    parser.add_argument("--output", required=True, type=Path, help="Private data directory outside this source checkout")
    parser.add_argument("--refresh", action="store_true", help="Archive old API snapshots and explicitly refresh inventory and video links")
    parser.add_argument("--inventory-only", action="store_true", help="Collect inventory without requesting video detail pages")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if not args.owner.isdigit() or output.is_relative_to(Path(__file__).resolve().parent):
        parser.error("Use a numeric owner and a private output directory outside the source checkout")
    os.umask(0o077)
    os.environ["QZONE_OWNER"] = args.owner
    os.environ["QZONE_MIGRATION_OUTPUT"] = str(output)
    import migrate_session as migration
    import batch_download
    records = output / "迁移记录"
    marker = records / "source-account.json"
    if marker.exists() and json.loads(marker.read_text()).get("owner") != args.owner:
        raise RuntimeError("OUTPUT_BELONGS_TO_ANOTHER_ACCOUNT")
    migration.dump(marker, dict(owner=args.owner))
    if args.refresh and (records / "QQ原始响应").exists():
        archive = records / "旧QQ响应" / uuid.uuid4().hex
        archive.mkdir(parents=True)
        (records / "QQ原始响应").rename(archive / "QQ原始响应")
        if (records / "相册映射.json").exists():
            migration.dump(archive / "相册映射.json", json.loads((records / "相册映射.json").read_text()))
    session, cached, logged_in = migration.Session(args.owner), None, False
    try:
        if not args.refresh:
            try:
                cached = batch_download.load_saved()
            except (FileNotFoundError, KeyError, ValueError, RuntimeError):
                pass
        if cached is None:
            migration.say(login_required=True, instruction="Complete login in the official QQ browser window")
            session._login_and_get_cookies()
            logged_in = True
            session.inventory()
            cached = batch_download.load_saved()
        session.index, session.album_map = cached.index, cached.album_map
        video_keys = {key for key, (photo, _) in session.index.items() if photo.is_video}
        attempted = set()
        while not args.inventory_only:
            missing = video_keys - cached.video_urls.keys()
            available = missing - attempted
            if not available:
                break
            if not logged_in:
                migration.say(login_required=True, instruction="Complete login in the official QQ browser window")
                session._login_and_get_cookies()
                logged_in = True
            key = choose_detail(session.index, available)
            attempted.add(key)
            session.detail(*key)
            cached = batch_download.load_saved()
            migration.say(detail_requests=len(attempted), video_links=len(video_keys & cached.video_urls.keys()), video_total=len(video_keys))
        missing = video_keys - cached.video_urls.keys()
        migration.say(collection_complete=True, albums=len(session.album_map), items=len(session.index),
                      cached_video_links=len(video_keys & cached.video_urls.keys()), unavailable_video_links=len(missing), login_used=logged_in)
        if missing and not args.inventory_only:
            raise RuntimeError("SOME_VIDEO_LINKS_UNAVAILABLE")
    finally:
        session.cookies.clear()
        session.session.cookies.clear()
        session.qzone_g_tk = ""
        session.session.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps(dict(status="error", error=type(exc).__name__)), flush=True)
        sys.exit(1)
