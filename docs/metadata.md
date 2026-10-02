# 日期、元数据与画质验证

本文记录字段语义、测试方法和有限样本的观察。迁移目标是现有证据支持的拍摄／创建时间，QQ 展示的上传时间仅作记录。不要把单个工具的实现优先级当成字段含义或所有 Apple Photos 版本的保证。

## 先保留证据，再决定时间

每项至少记录以下内容，保存在私有清单中：

| 来源 | 应保留的证据 |
| --- | --- |
| QQ 页面 | 可见日期文字、显示精度、页面位置、可证明的显示时区 |
| QQ API | `exif.originalTime`、`rawshoottime`、`shoottime`、`uploadtime`、`modifytime` 的原始值与类型；不存在的字段保留缺失 |
| 下载文件 | 全部分组的日期、EXIF/XMP/QuickTime、GPS、相机和色彩/方向元数据 |
| 获取过程 | 实际下载规格、原始名称、检测出的格式、原件 SHA-256、下载版本和来源类别 |
| 决策 | 采用的日期、对应字段、时区依据、已知精度、冲突及未修复原因 |

选择三个 QQ 候选字段不相等的样本进行比较；如果所有值相同，无法由该样本推断页面究竟采用哪个字段。秒级 Unix 时间需要先确认单位与显示时区，再与页面对照；不要对所有字符串统一追加当前 Mac 的时区。

在一组样本中，QQ 页面展示日期与上传时间对应，而媒体拍摄日期可以早得多。因此 `uploadtime` 以及上传语义的 `modifytime` 不作为本流程的拍摄时间回退。

## 拍摄时间与图库排序

有效的内嵌拍摄信息应优先保留。缺失时，可以根据 QQ 仍暴露且经过核对的拍摄字段补充；不同来源冲突时保留证据并暂停自动覆盖。不能因为 `originalTime` 的名字含有 original 就认定它一定是真实拍摄时间。

转发、编辑、扫描或截图素材，可能只有编辑时间、数字化时间或转发者保留的时间。此时只能描述为“现有证据支持的拍摄相关时间”。明确截图名称中的创建日期可以作为低置信推断并记录 `filename_inferred` 等来源标记；普通文件名、上传日期名称、下载日及地理位置不用于猜测拍摄时间。

目标日期优先来自可信的内嵌拍摄信息；缺失时依次核对 QQ `exif.originalTime`、完整 `rawshoottime`／详情 `shootTime`，最后才考虑明确截图名称中的创建时间并标记推断。冲突项不自动覆盖。没有完整且合理来源的项目暂缓入库。内嵌字段优先也不意味着无条件相信视频容器的创建时间：它可能是后续处理生成的时间。

| 来源 | 当前自动流程如何使用 |
| --- | --- |
| `ExifIFD:DateTimeOriginal`、明确的 XMP 拍摄字段 | 有效值优先保留；不同内嵌字段或 QQ 候选不一致则暂缓，不覆盖 |
| QQ `exif.originalTime` | 缺少有效内嵌拍摄信息时的优先候选；与 `rawshoottime` 冲突时暂缓 |
| QQ `rawshoottime` | 完整非零时间可作候选；无其他证明的 `00:00:00` 视为可能的日期占位，不宣称恢复时分秒 |
| QQ `shoottime` | 保留原值用于交叉检查，可能只有日期或展示文本；当前不单独作为精确时间的自动回退 |
| 详情 `shootTime` | 正数 Unix 秒与字符串候选对照，确认 QQ 的显示约定和瞬间；当前用于视频时区交叉证据，不用不一致的 epoch 静默替换候选 |
| 明确的 `截图 YYYY-MM-DD HH-MM-SS` 名称 | 上述候选缺失时允许带来源的推断；普通相机名称、纯日期或一般日期时间文件名不自动解析 |
| `uploadtime`、上传语义 `modifytime`、下载文件时间 | 只用于来源、冲突及处理时间分析，永不回退为拍摄时间 |

来自明确 `originalTime` 或有效内嵌字段的午夜可以是真实午夜，不能一概丢弃；仅由 `rawshoottime` 提供的午夜则采取保守暂缓。QQ 字段和视频详情可能共享同一个服务端来源，二者相同是表示一致的证据，不是独立证明“这就是真实拍摄瞬间”。

当前实现对无偏移的完整图片候选使用 `+08:00` 作为 Photos 显示约定，并在记录里注明这是源墙上时间的表示，不向图片发明 `OffsetTimeOriginal`。已有拍摄偏移保留。没有内嵌偏移的视频必须有匹配的详情 epoch 才允许写创建日期。其他地区、夏令时或旅行素材须扩展并重新验证规则，不能将当前约定推广为相机的历史时区。

对于 GIF 等需要直接设置图库日期的格式，只写入选定的拍摄／创建时间。这项图库修改与原文件字段不同：导出“未修改的原片”不会自动把图库日期写入文件，旁车清单必须保存映射。

仅有日期或分钟的来源不能被描述为已恢复确切秒数；不得填入虚构的亚秒、相机或 GPS。需要完整时间才能执行的步骤应保留待处理状态，并说明精度限制。

## 图片

| 字段 | 含义与处理 |
| --- | --- |
| `ExifIFD:DateTimeOriginal` | 原始图像拍摄时间；保留有效值，缺失/已证实错误才有针对性修复 |
| `ExifIFD:CreateDate` | EXIF 规范中的 DateTimeDigitized；不必与原始拍摄时间相同 |
| `IFD0:ModifyDate` | 图像修改时间；不要为了排序把所有修改日期写成拍摄日期 |
| `OffsetTimeOriginal` | DateTimeOriginal 的偏移量，仅在来源明确时写入 |
| `OffsetTimeDigitized` | CreateDate 的偏移量 |
| XMP 中的对应字段 | 核查是否冲突；只有来源和应用需要明确时才同步 |

字段含义参见 [ExifTool EXIF 文档](https://exiftool.org/TagNames/EXIF.html)。避免 `-AllDates` 一次覆盖语义不同的日期，也不要删除全部 EXIF 后重建。保留 MakerNotes、相机/镜头、曝光、GPS、Orientation、ICC 等非目标数据。

当前适配优先补缺失的 `ExifIFD:DateTimeOriginal`，没有为了凑齐字段而把 `CreateDate`、`ModifyDate` 全部设成同一时间；它们的语义未被证明相同。QQ 仍暴露的相机、镜头和曝光等字段只在文件缺失对应标签时补充；无效值保留在来源记录而不强写，例如零值或无法解析的光圈。现有 GPS 保留，QQ 未提供的 GPS 不推断。

PNG 可包含 EXIF、XMP 和 `PNG:CreationTime`。该 PNG 日期是文本字段；不同应用读取支持不同，应通过实际导入核验。ExifTool 调整文本块位置以改善部分 Apple/Adobe 软件读取，不需要把 PNG 转成 JPEG。[PNG 文档](https://exiftool.org/TagNames/PNG.html)

HEIC 虽使用 ISO-BMFF 容器，图片日期仍需检查其 EXIF；不能不加区分套用全部视频规则。未在目标系统和样本中验证的格式，保持待验证。

## 视频

MOV/MP4 的整数型 `QuickTime:CreateDate`、`TrackCreateDate`、`MediaCreateDate` 按规范以 UTC 存储。但历史设备可能写入当地时间，因此 ExifTool 默认不会一概自动转换。启用 `-api QuickTimeUTC=1` 时才按 UTC 解释相应整数日期。

`Keys:CreationDate`、`UserData:DateTimeOriginal` 是字符串日期，应携带来源明确的时区。缺失时区可能导致 Apple 软件解释异常；“当前 Mac 位于某个时区”不是历史拍摄时区的证据。[ExifTool QuickTime 文档](https://exiftool.org/TagNames/QuickTime.html)

本机样本实测：一个存在非零 QuickTime 创建时间的 MP4，在新写入 Keys 日期后导入，Photos 仍采用 QuickTime 创建时间。因此不能依赖“Keys 一定优先”的假设。

处理原则：

1. 保存未经修改的下载原件及所有时间字段。
2. 判断容器日期是否是上传附近的处理时间。必须有 API、原始元数据和样本对照支持，不能仅凭时间较晚就覆盖；仅靠时间和封装器名称不能断言处理发生在 QQ 服务端。
3. 有证据时在导入副本修复 `QuickTime:CreateDate`，并按需要同步 Keys 日期，确保它们代表同一个瞬间。明确日期偏移量，按 UTC 规则写入整数值。
4. 不无差别覆写全部 Track/Media 修改日期。保留仍有意义的结束时间、编辑时间和其他字段。
5. 重新导入样本后读回 Photos 日期，并核验实际信息窗。视频、音频和色彩/方向等属性都要验证。

如果视频拍摄元数据本身有效却导入识别异常，应先定位字段和时区原因。确有必要采用图库日期修正时，仍只使用有来源的拍摄／创建时间；不能将图库修正描述成已修好视频内嵌时间。

### 当前上传附近处理时间的推断边界

当前规则只有同时满足以下条件，才允许把非零容器创建时间视为可修复的处理时间候选：已有完整拍摄候选且无日期冲突；偏移来自内嵌信息或与 QQ 详情 epoch 一致；将 QuickTime 整数日期按 UTC 表示后，它在 QQ 上传时间之后 0–1800 秒；现有 Track/Media 创建日期均与容器创建时间相差不超过 2 秒；容器时间比拍摄候选晚超过 1 秒。

1800 秒是根据本次实际样本形成的明确策略窗口，不是 QQ 官方对转码时长的保证。记录会保存旧字段、与上传／拍摄候选的差值和推断原因。平台封装标记或所有轨道时间相同，单独都不足以证明真实拍摄时间；也不能据此声称还原了上传前被编辑的视频。拍摄候选晚于上传的时钟异常要继续显示，不能因日期写入成功隐藏。

若非零 QuickTime 时间更早、远离上传、来源矛盾或偏移无法证明，则保留字段、标记 `needs_review`。不可用绝对时间差或“总选最早日期”放宽规则。修复时只补零值或经上述推断确认的创建字段，不改全部修改日期。规则变更必须重新跑受影响样本。

目标日期验证精确到秒，没有“差 1 秒也通过”的容差：不符合处理时间推断且与候选相差 1 秒的非零 QuickTime 日期仍属歧义，暂缓处理。新导入前，流水线会重新读取实际副本的视频创建字段作预检；预检失败就停止新导入，不能仅依赖旧清单中的 `capture_ready` 状态。

## GIF

GIF 保持原编码与动画内容，不为写 EXIF 转成其他格式。没有本流程已验证的通用内嵌拍摄日期方案时，使用 Photos 图库日期，并在旁车/清单中保存 QQ 字段、选用日期、来源和精度。重新导出 GIF 原片不保证其他应用自动采用这一日期；迁移清单不能丢弃。

## 无重编码验证

保真验证分为三层：

1. **备份未改。** 下载原件的整文件 SHA-256 在处理前后相同。
2. **修复未改媒体内容。** 原件与修复副本的 `ImageDataHash` 相同，非日期语义元数据、图像尺寸、色彩/方向及视频音频属性无非预期变化。
3. **Photos 保留导入文件。** 样本及全量 `--export-verify` 要求每项通过“导出未修改的原片”与实际导入副本比较整文件 SHA-256。普通导出/拖出可能产生渲染版本，不能用于这一证明。少量样本不能代替其他项目的导出证明；全量续跑可在重新核验当前身份、SHA、日期和相册后复用严格匹配的既有证明，样本模式每次重新导出。详见[复用条件](workflow.md#完整导出证明的复用)。

ExifTool 的 `ImageDataHash` 支持 JPEG、PNG、HEIC 等图片及 MOV/MP4；视频哈希包含音视频数据，但不覆盖全部缩略图/预览图。无哈希输出不能当作通过。GIF 等不在该支持范围的格式，若未修改文件内容，可采用整文件哈希及实际动画查看验证。[ExifTool 哈希文档](https://exiftool.org/TagNames/Extra.html)、[Apple 导出指南](https://support.apple.com/guide/photos/export-photos-videos-slideshows-and-memories-pht6e157c5f/mac)

以下为通用只读检查示例，文件名为占位值：

```sh
exiftool -a -G0:1:4 -s -j -n -u -ee -api QuickTimeUTC=0 -all SAMPLE.jpg SAMPLE.mp4
exiftool -a -G0:1:4 -s -api QuickTimeUTC=1 -time:all SAMPLE.jpg SAMPLE.mp4
exiftool -G1 -s -api ImageHashType=SHA256 -ImageDataHash ORIGINAL.jpg IMPORT_COPY.jpg
exiftool -a -G1 -s -validate -warning -error SAMPLE.jpg SAMPLE.mp4
```

ExifTool 元数据写入可能改变文件大小、内部偏移或无用填充，但不重新编码图像。因此整个文件哈希变化不自动代表画质变化，应与媒体内容哈希和标签差异一起判断。[ExifTool FAQ](https://exiftool.org/faq.html#Q13)

这些验证只能证明本地处理和 Photos 导入没有降低已下载媒体的质量。如果没有当初上传的原片，不能据此证明 QQ 没有早年压缩或转码。

## Photos 本机观察与批处理

12 项有完整时间来源的样本覆盖了 JPEG、PNG、GIF、MP4。修正后，12 项 Photos 日期均精确到秒匹配选定来源，相册成员关系正确，导出的未修改原片哈希与导入副本一致。样本最初暴露了缺少可识别日期时落在下载当天、部分视频优先采用 QuickTime 容器日期等问题；修改图库日期与修改文件元数据是两个不同操作。只有日期而无确切时分秒的另一个候选被暂缓处理。

当前流水线每批最多 50 项，按相册批量导入，并根据唯一文件名或原片字节证明匹配返回 ID。Photos 返回顺序与输入顺序不同，不能按数组位置盲配。导入前后均持久化操作记录；超时后先核对指定相册及已返回 ID，不能认为整批失败后直接重导。

在一次已知旧测试项目移入“最近删除”后的重导中，Photos 仍出现重复确认。当前流水线显式设置 `skip_check_duplicates: true`，按 QQ 稳定项目 ID 和持久化记录排除本任务已导入项。这不会去重用户图库中的既有资产；既有内容可能被再次导入。恢复测试项目时不清空“最近删除”；遇到超时仍须先核对，不能切换选项盲重试。

全量批处理还观察到：Photos 可能为内容相同的导入件沿用另一资源的文件名。匹配必须用资产 ID、明确相册、目标日期及导出 SHA 的证明，不能只依赖文件名，也不能无条件接受名字不符。等价来源、实际文件名和字节证明保留在私有日志中，详见[恢复说明](workflow.md#两类重复不能混为一谈)。

本机 JXA 还观察到：

- `Photos.exists(mediaItem)` 会报类型转换错误。
- `album.mediaItems.byId(id)` 可能返回不属于该相册的对象，不能据此核验成员关系。
- `album.mediaItems.whose({id: id}).id()` 可对明确相册做成员核查；批处理用一次 `album.mediaItems.id()` 获取指定任务相册的成员集合，避免逐项重复查询。不会枚举整个图库。
- `mediaItem.properties()` 可一次读回所需字段；优化后已对同一组 12 项核验，结果与原逐字段读取完全一致。
- `date` 返回的瞬间可按 Mac 本地时区格式化，但不等于 Photos 每个资产的信息窗显示时区。涉及其他时区的样本仍需界面核对。

这些行为来自当前环境，不是 Apple 文档对所有版本的承诺。Apple 公开了信息窗、日期调整和未修改原片导出功能，但没有为本流程提供完整且固定的元数据导入优先级。[Apple 信息窗](https://support.apple.com/guide/photos/see-photo-and-video-information-phta8b25fa42/mac)、[日期调整](https://support.apple.com/guide/photos/add-titles-captions-and-more-phta4e5a733f/mac)

## 上游实现的边界

### QzonePhoto

审计 v2.7.0 时，设置读取使用 `settings[key] || null`，导致 `false` 变为缺省值，调用方随后恢复默认开启。因此“关闭保留动态信息”不能直接证明下载件没有被改写。[固定版本相关代码](https://github.com/11273/QzonePhoto/blob/v2.7.0/src/main/services/main/download.js#L264-L337)

另一个被审计的 main 提交即使关闭描述信息，也会进入 JPEG 日期写入路径；它与同号发布包行为并不完全一致。[固定提交的写入路径](https://github.com/11273/QzonePhoto/blob/dfb7d4d24af5544a7d17394f7237fba6962936fd/src/main/utils/media-metadata-writer.mjs#L220-L239)

因此本流程没有直接采用未经修正的上游写入路径来保证不可变备份。以上说明限定于被审计版本；后续版本是否修复应重新检查。

### dslwind/qzone-photo-downloader

固定提交的 JPEG 日期逻辑优先 `exif.originalTime`，其次传入的拍摄字段（API 实际取 `rawshoottime`），再取 `uploadtime`。这是上游实现，不是 QQ 页面的语义证明。非 JPEG 只设文件时间，视频没有完整 QuickTime 日期修复；原位写 EXIF 也不满足原件备份要求。[固定提交](https://github.com/dslwind/qzone-photo-downloader/tree/b008737cd439424e7a1abaaf938c38c24ebc219a)

适配保留其读取/下载能力，已删除未使用的原位元数据写入与另一套下载流程；原始响应和证据单独保存，再对副本执行最小修复。上述日期回退是固定上游的审计记录，不是当前迁移路径。仅日期字符串不能无说明补成午夜；视频下载失败也不能以封面计入成功。
