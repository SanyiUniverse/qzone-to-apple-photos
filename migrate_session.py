"""One-time Qzone migration session; upstream MIT code is retained beside this file."""
import hashlib
import json
import logging
import os
import random
import sys
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path

os.umask(0o077)
sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "qzone-photo-downloader"))
import core

OWNER = os.environ.get("QZONE_OWNER", "")
OUT = Path(os.environ.get("QZONE_MIGRATION_OUTPUT", HERE.parent / "qzone-migration-private")).resolve()
if OUT.is_relative_to(HERE):
    raise ValueError("QZONE_MIGRATION_OUTPUT must be outside the public source checkout")
core.logger.disabled = True
logging.getLogger("urllib3").setLevel(logging.CRITICAL)


def safe(value):
    return core.sanitize_filename_component(str(value)).strip(" .")[:60] or "未命名"


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def media_format(header):
    for magic, kind, ext in ((b"\xff\xd8\xff", "JPEG", ".jpeg"), (b"\x89PNG\r\n\x1a\n", "PNG", ".png"),
        (b"GIF87a", "GIF", ".gif"), (b"GIF89a", "GIF", ".gif"), (b"II*\0", "TIFF", ".tif"),
        (b"MM\0*", "TIFF", ".tif"), (b"BM", "BMP", ".bmp")):
        if header.startswith(magic):
            return kind, ext
    if header[:4] == b"RIFF":
        return {b"WEBP": ("WEBP", ".webp"), b"AVI ": ("AVI", ".avi")}.get(header[8:12], ("UNKNOWN", ".unknown"))
    if header[4:8] == b"ftyp":
        brands = header[8:64]
        if b"avif" in brands or b"avis" in brands:
            return "AVIF", ".avif"
        if any(brand in brands for brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1")):
            return "HEIC", ".heic"
        if header[8:12] == b"qt  ":
            return "MOV", ".mov"
        return ("3GP", ".3gp") if header[8:11] == b"3gp" else ("MP4", ".mp4")
    return "UNKNOWN", ".unknown"


def say(**data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


class Session(core.QzonePhotoManager):
    def __init__(self, owner=None):
        super().__init__(owner or OWNER)
        self.context = OUT / "迁移记录" / "QQ原始响应" / "相册列表"
        self.serial, self.rows, self.index, self.album_map = 0, [], {}, {}
        self.run_id = uuid.uuid4().hex[:12]

    def _login_and_get_cookies(self):
        super()._login_and_get_cookies()
        signed_in = str(self.cookies.get('uin') or self.cookies.get('p_uin') or '').lstrip('o0')
        if not self.user_qq.isdigit() or not signed_in or signed_in != self.user_qq.lstrip('0'):
            self.cookies.clear()
            self.session.cookies.clear()
            self.qzone_g_tk = ''
            raise ValueError('WRONG_SIGNED_IN_ACCOUNT')

    def _access_qzone_api(self, url, timeout_seconds=None):
        self.serial += 1
        stem = self.context / f"{self.run_id}-{self.serial:06d}"
        stem.parent.mkdir(parents=True, exist_ok=True)
        self.raw_response_hook = lambda body: stem.with_suffix(".jsonp").write_text(body, encoding="utf-8")
        result = super()._access_qzone_api(url, timeout_seconds)
        dump(stem.with_suffix(".json"), result)
        if not isinstance(result, dict) or result.get("code", 0) != 0 or not isinstance(result.get("data"), dict):
            raise RuntimeError("QQ_API_ERROR")
        self.rows.extend(result["data"].get("photoList") or [])
        return result

    def inventory(self):
        if not self.user_qq:
            raise ValueError("QZONE_OWNER_REQUIRED")
        self.index, self.album_map = {}, {}
        self.context = OUT / "迁移记录" / "QQ原始响应" / "相册列表"
        albums = self.get_albums_by_page(self.user_qq)
        if len({a.uid for a in albums}) != len(albums):
            raise RuntimeError("DUPLICATE_ALBUM_IDS")
        if self.total_albums and len(albums) != self.total_albums:
            raise RuntimeError("INCOMPLETE_ALBUM_LIST")
        names = Counter()
        for number, album in enumerate(albums, 1):
            album_id = str(album.uid)
            names[album.name] += 1
            suffix = f"（{names[album.name]}）" if names[album.name] > 1 else ""
            self.album_map[album_id] = dict(source_name=album.name, source_count=album.count,
                photos_album=f"迁移QQ相册之“{album.name}”{suffix}", album_id=album_id)
            self.context = OUT / "迁移记录" / "QQ原始响应" / safe(album_id) / "列表"
            self.rows = []
            photos = self.get_photos_from_album(self.user_qq, album)
            raw = {str(r.get("lloc") or r.get("sloc") or ""): r for r in self.rows}
            for photo in photos:
                key = (album_id, photo.pic_key)
                if not photo.pic_key or key in self.index or photo.pic_key not in raw:
                    raise RuntimeError("MISSING_OR_DUPLICATE_PHOTO_KEY")
                self.index[key] = (photo, raw[photo.pic_key])
            dump(OUT / "迁移记录" / "相册映射.json", self.album_map)
            say(album_number=number, count=len(photos), expected=album.count,
                videos=sum(p.is_video for p in photos), complete=len(photos) == int(album.count))
            if len(photos) != int(album.count):
                raise RuntimeError("INCOMPLETE_PHOTO_LIST")
        say(inventory_complete=True, albums=len(albums), items=len(self.index))

    def detail(self, album_id, pic_key):
        if (album_id, pic_key) not in self.index:
            raise ValueError("ITEM_NOT_IN_INVENTORY")
        self.context = OUT / "迁移记录" / "QQ原始响应" / safe(album_id) / "详情"
        url = self.VIDEO_DETAIL_URL_TEMPLATE.format(gtk=self.qzone_g_tk, t=random.random(),
            album_id=album_id, pic_key=pic_key, user=self.user_qq, dest_user=self.user_qq)
        data = self._access_qzone_api(url)
        matches = [p for p in data["data"].get("photos", [])
                   if p.get("picKey") == pic_key or p.get("lloc") == pic_key]
        if len(matches) != 1:
            raise RuntimeError("DETAIL_KEY_NOT_UNIQUE")
        dump(OUT / "迁移记录" / safe(album_id) / f"{hashlib.sha256(pic_key.encode()).hexdigest()[:16]}-detail.json", matches[0])
        say(detail_saved=True)

    def validated_record(self, album_id, pic_key, update=True):
        short_id = hashlib.sha256(pic_key.encode()).hexdigest()[:12]
        record_path = OUT / "迁移记录" / safe(album_id) / f"{short_id}-download.json"
        if not record_path.exists():
            return None
        record = json.loads(record_path.read_text())
        path = (OUT / record["path"]).resolve()
        if not path.is_relative_to(OUT / "原始备份") or record["pic_key"] != pic_key or str(record["album"]["album_id"]) != album_id:
            raise RuntimeError("BACKUP_RECORD_IDENTITY_MISMATCH")
        if not path.is_file() or path.stat().st_size != record["bytes"] or file_hash(path) != record["sha256"]:
            return None
        with path.open("rb") as handle:
            kind, extension = media_format(handle.read(4096))
        record.update(file_type=kind, file_extension=extension,
                      status="verified_original" if kind != "UNKNOWN" else "needs_format_identification")
        if update:
            path.chmod(0o444)
            dump(record_path, record)
        return record

    def download(self, items, quiet=False):
        if not isinstance(items, list) or not items:
            raise ValueError("EXPLICIT_ITEM_LIST_REQUIRED")
        selected = [(str(i["album_id"]), str(i["pic_key"])) for i in items]
        if len(set(selected)) != len(selected) or any(key not in self.index for key in selected):
            raise ValueError("ITEM_NOT_IN_INVENTORY_OR_DUPLICATED")
        records = []
        for number, (album_id, pic_key) in enumerate(selected, 1):
            cached = self.validated_record(album_id, pic_key)
            if cached:
                records.append(dict(cached, cached=True))
                if not quiet:
                    say(download_number=number, cached=True)
                continue
            photo, raw = self.index[(album_id, pic_key)]
            field = next((k for k in ("raw", "origin_url", "url", "custom_url", "lloc", "sloc") if raw.get(k)), None)
            url = photo.url
            if photo.is_video:
                self.context = OUT / "迁移记录" / "QQ原始响应" / safe(album_id) / "视频详情"
                url = self.get_video_download_url(self.user_qq, album_id, pic_key)
                field = "video_info.download_url"
                if not url:
                    raise RuntimeError("VIDEO_DOWNLOAD_UNAVAILABLE_NO_COVER_FALLBACK")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                raise RuntimeError("MEDIA_URL_MISSING")
            for attempt in range(3):
                response = None
                try:
                    response = core.download_photo_network_helper(None, url.replace("\\", ""), 30)
                    response.raise_for_status()
                    body = response.content
                    if not body or any(token in response.headers.get("Content-Type", "").lower() for token in ("text/", "json", "xml")) or body.lstrip()[:1] == b"<":
                        raise RuntimeError("NON_MEDIA_RESPONSE")
                    break
                except Exception as exc:
                    if attempt == 2:
                        raise RuntimeError("MEDIA_DOWNLOAD_FAILED") from None
                    time.sleep(2 ** attempt)
                finally:
                    if response is not None:
                        response.close()
            kind, extension = media_format(body[:4096])
            if photo.is_video and kind not in ("MP4", "MOV", "3GP", "AVI", "UNKNOWN"):
                raise RuntimeError("VIDEO_RESPONSE_IS_NOT_VIDEO")
            digest = hashlib.sha256(body).hexdigest()
            short_id = hashlib.sha256(pic_key.encode()).hexdigest()[:12]
            name = safe(photo.name)
            if Path(name).suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".mp4", ".mov"):
                name = Path(name).stem
            path = OUT / "原始备份" / safe(album_id) / f"{name}__{short_id}{extension}"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and file_hash(path) != digest:
                path = path.with_name(f"{path.stem}__{digest[:12]}{path.suffix}")
            if not path.exists():
                with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    if file_hash(temporary) != digest:
                        raise RuntimeError("ORIGINAL_VERIFICATION_FAILED")
                    temporary.chmod(0o444)
                    os.link(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            if file_hash(path) != digest:
                raise RuntimeError("ORIGINAL_VERIFICATION_FAILED")
            path.chmod(0o444)
            record = dict(album=self.album_map[album_id], pic_key=pic_key, original_name=photo.name,
                      path=str(path.relative_to(OUT)), bytes=len(body), sha256=digest, raw_fields=raw,
                      selected_url_field=field, is_video=photo.is_video, metadata_modified=False,
                      file_type=kind, file_extension=extension,
                      status="verified_original" if kind != "UNKNOWN" else "needs_format_identification",
                      quality="QQ available URL; original quality requires sample verification")
            dump(OUT / "迁移记录" / safe(album_id) / f"{short_id}-download.json", record)
            records.append(dict(record, cached=False))
            if not quiet:
                say(download_number=number, bytes=len(body), file_type=kind)
        if not quiet:
            say(download_complete=True, items=len(selected))
        return records


def main():
    session = Session()
    if not session.user_qq:
        raise ValueError("QZONE_OWNER_REQUIRED")
    session._login_and_get_cookies()
    print("READY", flush=True)
    for line in sys.stdin:
        try:
            command = json.loads(line)
            action = command["cmd"]
            if action == "quit":
                break
            if action == "inventory":
                session.inventory()
            elif action == "detail":
                session.detail(str(command["album_id"]), str(command["pic_key"]))
            elif action == "download":
                session.download(command["items"])
            else:
                raise ValueError("UNKNOWN_COMMAND")
        except Exception as exc:
            say(status="error", error=type(exc).__name__)
    session.cookies.clear()
    session.session.cookies.clear()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        say(status="error", error=type(exc).__name__)
        sys.exit(1)
