# QQ 相册迁移到 Apple Photos

在 Apple Silicon Mac 上，将自己的 QQ 相册照片和视频迁移到 Apple Photos，保留未经修改的下载原件、有证据的拍摄时间和源相册归属。复用固定版本的 QQ 下载核心、ExifTool 和 Photos 原生接口；不重编码，也不以上传时间或下载日期补造拍摄时间。

此前已在 Photos 11.0 完成真实整库迁移及逐项核验，先通过 12 项 JPEG、PNG、GIF、MP4 样本，再处理有可靠时间的项目。缺少证据的项目暂缓并保留 QQ 来源。换系统、图库、账号或规则后，仍须重跑自己的样本；QQ 已丢失或压缩的信息无法恢复。

## 准备

- Apple Silicon Mac、Apple Photos、Google Chrome、Python 3.11+。
- [ExifTool](https://exiftool.org/) 在命令搜索路径中；已有 Homebrew 时可用 `brew install exiftool`。
- 足够容纳原件、导入副本、Photos 图库及原片导出核验的空间，另备独立备份位置。
- 确认 Photos 当前目标图库；样本与全量使用同一图库、账号和私有目录。

从源码根目录执行以下准备。账号主人在官方 QQ 页面自行登录，不向脚本提供密码；Cookie 仅在会话内存使用。源码与私有资料分开保存：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

umask 077
export QQ_OWNER="你的QQ号码"
export QZONE_MIGRATION_OUTPUT="$HOME/Pictures/QQ-Photos-Migration"
mkdir -p "$QZONE_MIGRATION_OUTPUT"
exiftool -ver
```

所有阶段复用这个私有目录。当前 QQ 日期规则以 `+08:00` 展示约定为基础，保留已有内嵌偏移；旅行或其他时区素材需先核对。各阶段之间审阅结果，不把全流程串成无人检查的一条命令。

## 先跑样本

### 1. 清点并下载 12 项

```sh
python collect.py --owner "$QQ_OWNER" --output "$QZONE_MIGRATION_OUTPUT"
python sample_selection.py --output "$QZONE_MIGRATION_OUTPUT" --count 12
python batch_download.py --selection "$QZONE_MIGRATION_OUTPUT/sample.json"
python batch_download.py --selection "$QZONE_MIGRATION_OUTPUT/sample.json" --download
```

收集器保存清单和视频详情链接；完整缓存可复用时不重复登录。确需刷新才加 `--refresh`，旧响应会归档；开始导入后不要未经核对刷新相册映射。

检查 `sample.json` 是否覆盖不同年份、现有格式、照片和视频、日期冲突与午夜附近项目。格式最终按文件内容判定。下载器不带 `--download` 只显示范围；实际下载保留原字节、SHA-256 与来源身份，原件设为只读，已验证原件复用。视频必须匹配同一项目，封面不计成功。

### 2. 审阅时间计划并生成副本

```sh
python prepare_capture.py
python prepare_capture.py --apply
```

第一条只检查并生成计划，不改媒体。审阅 `迁移记录/拍摄时间准备计划.json` 的来源、精度、时区、冲突和拟改字段，再执行 `--apply`。后者只修复副本，核对媒体内容哈希与非目标元数据。

只有 `capture_ready` 项允许导入；未知、只有日期或需要确认的项目暂缓。样本阶段只下载样本，不提前下载整库后拿整份清单作样本导入。准备清单虽然名为“全量”，实际只包含当前已下载项目。

### 3. 导入并验证

```sh
python photos_pipeline.py --sample \
  --journal "$QZONE_MIGRATION_OUTPUT/迁移记录/Photos样本导入"
python photos_pipeline.py --sample --apply \
  --journal "$QZONE_MIGRATION_OUTPUT/迁移记录/Photos样本导入"
```

默认只显示计划，`--apply` 才操作 Photos；首次可能需系统自动化权限。样本要求 5–15 个可入库项目，清单含照片和视频时必须同时覆盖。每次样本运行都重新导出未修改原片核验 SHA，不复用旧导出证明。

在 Photos 核对信息窗日期、跨年排序、方向、GIF 动画及视频画面和声音。脚本仅在日期精确到秒、相册与哈希均通过后生成 `迁移记录/Photos样本最终验证.json`；不要手改通过标记或绕过样本门槛。

## 样本通过后全量

确认样本结果和范围后，分阶段执行：

```sh
python batch_download.py --download
python prepare_capture.py
python prepare_capture.py --apply
python photos_pipeline.py --export-verify
python photos_pipeline.py --apply --export-verify
```

保持同一目录、来源映射、日期规则和图库。每批最多 50 项；先读回已记录资产，再处理剩余项。每个迁移项都需要完整原片导出 SHA 证明，样本不能代替全量核验。续跑仅在来源、资产 ID、哈希、日期、相册及文件名与现有证明精确一致时复用证明，见[复用规则](docs/workflow.md#完整导出证明的复用)。

完成条件是每个来源 ID 都归入已验证、明确暂缓或失败；已迁移项日期、相册、导出 SHA 均通过，下载原件仍可读且哈希未变。下载完成或总进度达到 100% 不代表迁移完成。

## 恢复、暂缓和可选清理

发生超时或数量不符时保留日志，先核对指定相册与返回的资产 ID，不盲目重导。按稳定来源 ID 保留同名相册和同内容项目的归属；Photos 返回顺序不能作映射依据。当前流水线绕过 Photos 的重复确认，依靠持久化来源记录防止本任务重导，不清理图库中原有的重复资产。详见[中断恢复](docs/workflow.md#发生中断后)。

缺少可靠时间的原件和原因继续保留；需要独立暂缓副本时，准备清单生成后可运行：

```sh
python hold_pending.py \
  --manifest "$QZONE_MIGRATION_OUTPUT/迁移记录/全量拍摄时间导入清单.json" \
  --destination "$HOME/Desktop/QQ-Photos-Pending"
```

该命令立即复制并核验 SHA，不访问 QQ 或 Photos；重复运行复用相同副本，不覆盖不同内容。

**默认不删除 QQ 内容。** 来源清理须另行明确授权，只处理当前 Photos 及完整导出证明再次核验通过的精确白名单，不含暂缓项、不整相册删除、不清空回收站。命令与异常对账见[可选来源清理](docs/workflow.md#可选来源清理)。

## 维护、文档与许可

- [需求与验收](docs/requirements.md)：范围及完成标准。
- [元数据规则](docs/metadata.md)：日期证据、格式边界与实测限制。
- [记录与恢复](docs/workflow.md)：私有资料、去重、证明复用及可选收尾。
- [第三方来源](THIRD_PARTY.md)：固定上游版本、适配范围和许可。
- [AGENTS.md](AGENTS.md)：维护和本地执行约束，助手接手先读。

在已安装依赖的环境中运行离线合成测试：

```sh
python -m unittest discover -s tests
```

测试不访问账号或 Photos，也不代替新环境的真实样本验证。公开仓库、issue 和示例不得包含真实媒体、QQ 号码、Cookie、签名 URL、来源 ID、Photos 资产 ID 或私有路径。

整体采用 [GNU GPL v3](LICENSE)；保留 dslwind 核心的原始 MIT 版权及许可，QzonePhoto 清理适配遵守 GPLv3，外部工具保留各自许可。本项目与腾讯或 Apple 无官方隶属关系。
