# 第三方来源与许可

本项目整体按 [GNU GPL v3](LICENSE) 分发，原创适配代码和文档版权为 Copyright (c) 2026 qzone-to-apple-photos contributors。QQ 清理适配包含 QzonePhoto 的 GPLv3 派生实现；不能将它标为 MIT。下述 dslwind 文件继续保留原始 MIT 版权和许可；外部工具保留各自许可证。

## dslwind/qzone-photo-downloader

- 上游：[dslwind/qzone-photo-downloader](https://github.com/dslwind/qzone-photo-downloader)
- 审计及适配基线：[`b008737cd439424e7a1abaaf938c38c24ebc219a`](https://github.com/dslwind/qzone-photo-downloader/tree/b008737cd439424e7a1abaaf938c38c24ebc219a)
- 上游许可证：[MIT](https://github.com/dslwind/qzone-photo-downloader/blob/b008737cd439424e7a1abaaf938c38c24ebc219a/LICENSE)
- 复用范围：`QzonePhotoManager` 的网页登录、相册／列表／详情 API，`QzonePhoto` 数据结构与 `download_photo_network_helper` 下载函数，以及 QQ 日期字段解析思路。原项目的完整 GUI 没有作为运行依赖。
- 适配：移除浏览器 `--no-sandbox`、未使用的自动检测伪装和含凭据调试分支；添加仅记录响应正文的私有取证钩子；视频详情严格匹配同一项目，不回退为邻居视频或封面。删除未使用的配置读取、GUI 回调、Cookie 注入／探测、原位 EXIF 写入和另一套批量下载流程；最小会话／批量协调代码复用保留的函数，保存未经修改原件，再交由 ExifTool 修复副本。
- 分发的上游文件保留在 `qzone-photo-downloader/`，保留原始许可证和提交来源。下列原始许可文本同样适用于包含其代码的适配部分；不能把适配后的文件称为完全未经修改的上游文件。

```text
MIT License

Copyright (c) 2025 dslwind

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 11273/QzonePhoto

- 上游：[11273/QzonePhoto](https://github.com/11273/QzonePhoto)，作者及贡献者保留上游版权。
- 固定来源：v2.7.0，提交 `038e1beb38a230246dde3824f8f0ada3bf4a98ea`。
- 许可：[上游 GPL v3 原文](https://github.com/11273/QzonePhoto/blob/038e1beb38a230246dde3824f8f0ada3bf4a98ea/LICENSE)，本仓库根目录保留同一许可文本。
- 派生范围：`cleanup_adapter.py` 的 `cgi_delpic_multi_v2` 请求地址、字段及选择数据编码来自 [`src/main/api/qzone/photo.js`](https://github.com/11273/QzonePhoto/blob/038e1beb38a230246dde3824f8f0ada3bf4a98ea/src/main/api/qzone/photo.js)，选择身份映射参考其相册界面。
- 本地修改：增加经 Photos 原片哈希验证的精确白名单、逐批当前成员和日期核验、原件/副本哈希绑定、调用前持久化意图、单次提交、异常后只读对账和保留项目保护。登录仍复用 dslwind，不分发 QzonePhoto 的完整应用或 Electron 运行时。
- 研究范围另包括原图/日期写入路径，并对照 main 提交 `dfb7d4d24af5544a7d17394f7237fba6962936fd`。审计结论仅适用于记录的版本。

## 外部工具

- [ExifTool](https://exiftool.org/)：外部元数据检查和修复工具，由 Phil Harvey 等维护；本仓库不重新许可或打包它。安装时遵守其发布包附带的许可。
- Apple Photos、AppleScript/JXA 和 macOS：使用系统原生接口与官方文档；不是本项目分发的软件组件。

运行依赖由 [requirements.txt](requirements.txt) 固定：Selenium、Requests、webdriver-manager 和 json-repair。ExifTool 单独安装，用于副本元数据检查和修复；Photos 和 `osascript` 由 macOS 提供。

QQ 来源清理默认关闭，仅在用户明确授权和当前账号回收语义核对后启用。已验证的本机流程包含图片及视频，但不保证平台未来版本或其他账号的接口与恢复行为相同。本项目与腾讯或 Apple 无官方隶属关系。

新增或分发第三方代码时须同步更新本文件及相应版权说明。所有示例与测试必须使用合成数据，不包含真实账号、来源 ID、照片或 API 响应。
