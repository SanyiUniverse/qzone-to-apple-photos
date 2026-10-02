"""
QQ空间相册下载器 - 核心模块

复用固定上游的网页登录、相册列表、视频详情与原始媒体下载能力。
迁移协调、不可变备份和副本元数据处理由仓库内的适配模块负责。
"""

import json
import json_repair
import logging
import os
import random
import re
import shutil
import sys
from collections import namedtuple

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 请求超时
# ---------------------------------------------------------------------------

REQUEST_TIMEOUT = 30


# ---------------------------------------------------------------------------
# 命名元组
# ---------------------------------------------------------------------------

QzoneAlbum = namedtuple("QzoneAlbum", ["uid", "name", "count"])
QzonePhoto = namedtuple(
    "QzonePhoto",
    [
        "url",
        "name",
        "album_name",
        "is_video",
        "pic_key",
        "exif_data",   # dict，来自 photo_data["exif"]
        "shoottime",   # str，来自 rawshoottime，含时分秒
        "uploadtime",  # str，来自 uploadtime，含时分秒
        "cameratype",  # str，完整设备名，如 "Apple iPhone 15 Pro Max"
    ],
)

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def get_script_directory() -> str:
    """获取脚本文件所在的绝对路径。"""
    return os.path.dirname(os.path.realpath(__file__))


def sanitize_filename_component(name_component: str) -> str:
    """安全处理文件名组件，替换所有非法字符为下划线。"""
    if not isinstance(name_component, str):
        raise TypeError("输入必须是字符串类型")
    illegal_chars = r'[\/\\:*?"<>|\0]'
    return re.sub(illegal_chars, "_", name_component)


def download_photo_network_helper(
    request_cookies: dict | None, url: str, timeout: int
) -> requests.Response:
    """
    下载照片的辅助函数：优先携带 cookies 请求，失败时再回退到无 cookies 请求。

    Raises:
        requests.exceptions.RequestException: 当所有网络请求都失败时
    """
    try:
        if request_cookies:
            return requests.get(url, cookies=request_cookies, timeout=timeout)
        return requests.get(url, timeout=timeout)
    except requests.exceptions.RequestException as first_error:
        if request_cookies:
            logger.warning(f"[警告] 携带 cookies 下载失败，尝试无 cookies 重试: {first_error}")
            try:
                return requests.get(url, timeout=timeout)
            except requests.exceptions.RequestException as second_error:
                raise ConnectionError(
                    f"[网络错误] 尝试下载 {url} 时出错（cookies/无cookies均失败）: {second_error}"
                ) from first_error
        raise ConnectionError(
            f"[网络错误] 尝试下载 {url} 时出错: {first_error}"
        ) from first_error


# ---------------------------------------------------------------------------
# QzonePhotoManager
# ---------------------------------------------------------------------------


class QzonePhotoManager:
    """管理 QQ 空间相册和照片的获取与下载。"""

    ALBUM_LIST_URL_TEMPLATE = (
        "https://user.qzone.qq.com/proxy/domain/photo.qzone.qq.com/fcgi-bin/fcg_list_album_v3?"
        "g_tk={gtk}&t={t}&hostUin={dest_user}&uin={user}"
        "&appid=4&inCharset=utf-8&outCharset=utf-8&source=qzone&plat=qzone&format=jsonp"
        "&notice=0&filter=1&handset=4&pageNumModeSort=40&pageNumModeClass=15&needUserInfo=1"
        "&idcNum=4&callbackFun=shine0&callback=shine0_Callback"
    )

    ALBUM_LIST_URL_WITH_PAGE_TEMPLATE = (
        "https://user.qzone.qq.com/proxy/domain/photo.qzone.qq.com/fcgi-bin/fcg_list_album_v3?"
        "g_tk={gtk}&t={t}&hostUin={dest_user}&uin={user}"
        "&appid=4&inCharset=utf-8&outCharset=utf-8&source=qzone&plat=qzone&format=jsonp"
        "&notice=0&filter=1&handset=4&pageNumModeSort=40&pageNumModeClass=15&needUserInfo=1"
        "&idcNum=4&callbackFun=shine{fn}&mode=2&sortOrder=2"
        "&pageStart={pageStart}&pageNum={pageNum}&callback=shine{fn}_Callback"
    )

    PHOTO_LIST_URL_TEMPLATE = (
        "https://h5.qzone.qq.com/proxy/domain/photo.qzone.qq.com/fcgi-bin/"
        "cgi_list_photo?g_tk={gtk}&t={t}&mode=0&idcNum=4&hostUin={dest_user}"
        "&topicId={album_id}&noTopic=0&uin={user}&pageStart={pageStart}&pageNum={pageNum}"
        "&skipCmtCount=0&singleurl=1&batchId=&notice=0&appid=4&inCharset=utf-8&outCharset=utf-8"
        "&source=qzone&plat=qzone&outstyle=json&format=jsonp&json_esc=1&question=&answer="
        "&callbackFun=shine0&callback=shine0_Callback"
    )

    VIDEO_DETAIL_URL_TEMPLATE = (
        "https://user.qzone.qq.com/proxy/domain/photo.qzone.qq.com/fcgi-bin/"
        "cgi_floatview_photo_list_v2?g_tk={gtk}&t={t}&topicId={album_id}&picKey={pic_key}"
        "&shootTime=&cmtOrder=1&fupdate=1&plat=qzone&source=qzone&cmtNum=10&likeNum=5"
        "&inCharset=utf-8&outCharset=utf-8&callbackFun=viewer&offset=0&number=15"
        "&uin={user}&hostUin={dest_user}&appid=4&isFirst=1&sortOrder=1&showMode=1"
        "&need_private_comment=1&prevNum=9&postNum=18"
    )

    def __init__(self, user_qq: str):
        """
        初始化官方网页登录与相册 API 会话；user_qq 为登录用户的 QQ 号。
        """
        self.user_qq = str(user_qq)
        self.cookies: dict = {}
        self.session = requests.Session()
        self.qzone_g_tk = ""
        self.total_albums = 0

    def _emit_log(self, message: str) -> None:
        """记录日志；迁移入口关闭此 logger，私有证据单独保存。"""
        logger.info(message)


    def _resolve_chromedriver_path(self) -> str:
        """优先级：脚本目录 > 系统 PATH > webdriver_manager 自动下载。"""
        driver_name = "chromedriver.exe" if sys.platform == "win32" else "chromedriver"
        local_path = os.path.join(get_script_directory(), driver_name)
        if os.path.exists(local_path):
            return local_path
        system_driver = shutil.which(driver_name)
        if system_driver:
            return system_driver
        self._emit_log("未在脚本目录或系统 PATH 中找到 ChromeDriver，尝试自动下载匹配版本...")
        return ChromeDriverManager().install()


    def _login_and_get_cookies(self) -> None:
        """使用 Selenium 打开 QQ 空间，等待用户手动登录后抓取 cookie。"""
        self._emit_log("尝试启动 Chrome 进行登录...")
        options = webdriver.ChromeOptions()
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--lang=zh-CN")

        driver_path = None
        try:
            driver_path = self._resolve_chromedriver_path()
            service = ChromeService(executable_path=driver_path)
            driver = webdriver.Chrome(service=service, options=options)
        except Exception as e:
            self._emit_log(f"启动 ChromeDriver 失败。错误: {e}")
            self._emit_log(f"尝试使用的驱动路径: {driver_path}")
            self._emit_log(
                "如果问题仍然存在，请手动下载 ChromeDriver: "
                "https://googlechromelabs.github.io/chrome-for-testing"
            )
            logger.exception("启动 ChromeDriver 失败。")
            raise

        driver.get("https://user.qzone.qq.com")
        self._emit_log("请在浏览器窗口中登录 QQ 空间。脚本将在登录后继续...")

        LOGIN_TIMEOUT = 300
        try:
            wait = WebDriverWait(driver, LOGIN_TIMEOUT)
            logged_in = wait.until(
                EC.any_of(
                    EC.presence_of_element_located((By.ID, "QM_OwnerInfo_Icon")),
                    EC.presence_of_element_located((By.ID, "QZ_Toolbar_Container")),
                    EC.presence_of_element_located((By.ID, "QM_Mood_Poster_Container")),
                )
            )
            if not logged_in:
                raise TimeoutException("登录超时或无法确认登录状态")
        except TimeoutException:
            self._emit_log(f"错误: {LOGIN_TIMEOUT} 秒内未检测到成功登录")
            self._emit_log("建议：1) 确保网络正常 2) 可能需要手动处理验证码")
            logger.error(f"登录超时或无法确认登录状态 ({LOGIN_TIMEOUT}秒)。")
            driver.quit()
            raise
        except Exception as e:
            self._emit_log(f"登录过程中发生意外错误: {e}")
            logger.exception("登录过程中发生意外错误。")
            driver.quit()
            raise

        selenium_cookies = driver.get_cookies()
        if not selenium_cookies:
            self._emit_log("获取 cookie 失败。登录可能失败或 cookie 无法访问。")
            logger.error("获取 cookie 失败。")
            driver.quit()
            raise RuntimeError("获取 cookie 失败")

        self.cookies = {c["name"]: c["value"] for c in selenium_cookies}
        for name, value in self.cookies.items():
            self.session.cookies.set(name, value)

        p_skey = self.cookies.get("p_skey") or self.cookies.get("skey")
        if not p_skey:
            self._emit_log("错误: 在 cookie 中未找到 'p_skey' 或 'skey'。无法计算 g_tk。")
            logger.error("在 cookie 中未找到 'p_skey' 或 'skey'。")
            driver.quit()
            raise RuntimeError("无法计算 g_tk")

        self.qzone_g_tk = self._calculate_g_tk(p_skey)
        self._emit_log("成功获取 cookie 和 g_tk。")

        driver.quit()

    def _calculate_g_tk(self, p_skey: str) -> int:
        """根据 p_skey 计算 g_tk。"""
        hash_val = 5381
        for char in p_skey:
            hash_val += (hash_val << 5) + ord(char)
        return hash_val & 0x7FFFFFFF

    def _access_qzone_api(self, url: str, timeout_seconds: int | None = None) -> dict:
        """访问 QQ 空间 API 端点并解析 JSONP 响应。"""
        if timeout_seconds is None:
            timeout_seconds = REQUEST_TIMEOUT

        try:
            response = requests.get(url, cookies=self.cookies, timeout=timeout_seconds)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            self._emit_log(f"API 请求失败，URL: {url}: {e}")
            return {}

        text_content = response.text
        raw_response_hook = getattr(self, "raw_response_hook", None)
        if raw_response_hook:
            raw_response_hook(text_content)
        if text_content.startswith("shine0_Callback(") and text_content.endswith(");"):
            json_str = text_content[len("shine0_Callback("):-2]
        elif text_content.startswith("viewer_Callback(") and text_content.endswith(");"):
            json_str = text_content[len("viewer_Callback("):-2]
        elif text_content.startswith("_Callback(") and text_content.endswith(");"):
            json_str = text_content[len("_Callback("):-2]
        else:
            match = re.match(r"^\w+_Callback\((.*)\);?$", text_content, re.DOTALL)
            if match:
                json_str = match.group(1)
            else:
                json_str = text_content

        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解码失败，尝试修复: {e}")
            try:
                repaired = json_repair.repair_json(json_str, return_objects=True)
                if repaired:
                    logger.info("JSON 修复成功")
                    return repaired
            except Exception as repair_err:
                logger.error(f"JSON 修复也失败: {repair_err}")
            self._emit_log(f"JSON 解码失败，响应内容: {json_str[:200]}... 错误: {e}")
            return {}

    def get_video_download_url(
        self, dest_user_qq: str, album_id: str, pic_key: str
    ) -> str:
        """获取视频的真实下载 URL（MP4）。失败返回空字符串。"""
        url = self.VIDEO_DETAIL_URL_TEMPLATE.format(
            gtk=self.qzone_g_tk,
            t=random.random(),
            album_id=album_id,
            pic_key=pic_key,
            user=self.user_qq,
            dest_user=dest_user_qq,
        )

        data = self._access_qzone_api(url)

        if not data or not data.get("data"):
            self._emit_log(f"获取视频详情失败，pic_key: {pic_key}")
            return ""

        try:
            photos = data["data"].get("photos", [])
            if not photos:
                self._emit_log("视频详情响应中没有找到 photos 数据")
                return ""

            photo_data = None
            for photo in photos:
                if photo.get("picKey") == pic_key or photo.get("lloc") == pic_key:
                    photo_data = photo
                    break

            if not photo_data:
                return ""

            video_info = photo_data.get("video_info", {})
            if not video_info:
                self._emit_log(
                    f"照片数据中没有 video_info 字段，is_video={photo_data.get('is_video')}"
                )
                return ""

            download_url = video_info.get("download_url", "")
            if download_url:
                self._emit_log(f"成功获取视频下载 URL: {download_url[:100]}...")
                return download_url

            if video_info.get("video_url"):
                self._emit_log("警告: 仅找到 m3u8 格式的视频 URL，当前版本暂不支持下载")
                return ""

            self._emit_log("video_info 中没有可用的下载 URL")
            return ""

        except Exception as e:
            self._emit_log(f"解析视频详情时出错: {e}")
            logger.exception("解析视频详情时出错")
            return ""

    def get_albums_by_page(self, dest_user_qq: str) -> list[QzoneAlbum]:
        """分页获取目标用户的所有相册列表。"""
        self.total_albums = 0
        page_start = 0
        all_albums: list[QzoneAlbum] = []

        while True:
            albums = self.get_albums(dest_user_qq, page_start)
            if not albums:
                break
            all_albums.extend(albums)
            page_start += len(albums)
            # 已获取到全部相册（total_albums 首次从 API 中读取）
            if self.total_albums > 0 and page_start >= self.total_albums:
                break

        return all_albums

    def get_albums(
        self, dest_user_qq: str, pageStart: int = 0, pageNum: int = 32
    ) -> list[QzoneAlbum]:
        """获取给定用户指定分页的相册列表。"""
        albums: list[QzoneAlbum] = []
        if pageStart == 0:
            url = self.ALBUM_LIST_URL_TEMPLATE.format(
                gtk=self.qzone_g_tk,
                t=random.random(),
                dest_user=dest_user_qq,
                user=self.user_qq,
            )
        else:
            url = self.ALBUM_LIST_URL_WITH_PAGE_TEMPLATE.format(
                gtk=self.qzone_g_tk,
                t=random.random(),
                dest_user=dest_user_qq,
                user=self.user_qq,
                pageStart=pageStart,
                pageNum=pageNum,
                fn=0,
        )

        data = self._access_qzone_api(url)

        if not data or not data.get("data"):
            logger.warning(f"获取相册列表失败或没有数据：{data}")
            return albums

        album_data = data["data"]
        if self.total_albums == 0:
            self.total_albums = album_data.get("albumsInUser", 0)

        if "albumListModeSort" in album_data:
            album_list = album_data["albumListModeSort"]
        elif "albumListModeClass" in album_data:
            album_list = [
                item
                for d in album_data["albumListModeClass"]
                for item in d.get("albumList", [])
            ]
        elif "albumList" in album_data:
            album_list = album_data["albumList"]
        else:
            album_list = []

        if album_list:
            for album in album_list:
                albums.append(
                    QzoneAlbum(uid=album["id"], name=album["name"], count=album["total"])
                )
        elif "albumlist" in album_data:
            for album in album_data["albumlist"]:
                albums.append(
                    QzoneAlbum(
                        uid=album["albumid"],
                        name=album["name"],
                        count=album.get("total", album.get("picnum", 0)),
                    )
                )

        return albums

    def get_photos_from_album(
        self, dest_user_qq: str, album: QzoneAlbum
    ) -> list[QzonePhoto]:
        """从特定相册获取所有照片，支持分页。"""
        photos: list[QzonePhoto] = []
        page_start = 0
        page_num_to_fetch = 500

        while True:
            url = self.PHOTO_LIST_URL_TEMPLATE.format(
                gtk=self.qzone_g_tk,
                t=random.random(),
                dest_user=dest_user_qq,
                user=self.user_qq,
                album_id=album.uid,
                pageStart=page_start,
                pageNum=page_num_to_fetch,
            )

            data = self._access_qzone_api(url)

            if not data or not data.get("data"):
                if data and data.get("code", 0) != 0:
                    self._emit_log(
                        f"相册 '{album.name}' API 错误: code {data.get('code')}, "
                        f"message: {data.get('message')}, subcode: {data.get('subcode')}"
                    )
                break

            api_data = data["data"]
            total_in_album = api_data.get("totalInAlbum", 0)
            photos_in_page = api_data.get("totalInPage", 0)

            if total_in_album == 0:
                self._emit_log(f"相册 '{album.name}' (ID: {album.uid}) 为空或没有可访问的照片。")
                break

            photo_list_data = api_data.get("photoList")
            if not photo_list_data:
                if photos_in_page == 0 and page_start > 0:
                    self._emit_log(
                        f"在相册 '{album.name}' 中，页码起点 {page_start} 之后未找到更多照片。"
                    )
                elif photos_in_page == 0 and page_start == 0:
                    self._emit_log(f"在相册 '{album.name}' 的第一页未找到照片。")
                break

            for photo_data in photo_list_data:
                pic_url = (
                    photo_data.get("raw")
                    or photo_data.get("origin_url")
                    or photo_data.get("url")
                    or photo_data.get("custom_url")
                )
                if not pic_url and "lloc" in photo_data:
                    pic_url = photo_data["lloc"]
                if not pic_url and "sloc" in photo_data:
                    pic_url = photo_data["sloc"]

                if not pic_url:
                    continue

                pic_key = photo_data.get("lloc") or photo_data.get("sloc") or ""
                photos.append(
                    QzonePhoto(
                        url=pic_url,
                        name=photo_data.get("name", "untitled").strip(),
                        album_name=album.name,
                        is_video=bool(
                            photo_data.get("is_video", False)
                            or photo_data.get("phototype") == "video"
                        ),
                        pic_key=pic_key,
                        exif_data=photo_data.get("exif", {}),
                        shoottime=photo_data.get("rawshoottime", ""),
                        uploadtime=photo_data.get("uploadtime", ""),
                        cameratype=photo_data.get("cameratype", "").strip(),
                    )
                )

            if len(photos) >= total_in_album or photos_in_page == 0:
                break

            page_start += photos_in_page

        return photos
