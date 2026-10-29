# TreeSTRM 升级说明

面向执行升级部署的人 / Agent。请完整阅读「升级前必做」与「行为变化」两节后再操作。

## 一、升级前必做

1. **备份 `/opt/treestrm/config.json` 和 `/opt/treestrm/data/` 目录。** 新版本会自动维护 `config.json.bak`，但第一次升级前它还不存在。
2. 确认服务器上有 `requests`、`fastapi`、`pydantic`、`beautifulsoup4` 等原有依赖（本次未新增依赖）。
3. 部署方式为整体覆盖代码目录时，**不要覆盖 `config.json` 与 `data/`**。本次删除了 `router.py`、`core/state_db.py`，服务器上残留的旧文件无害，可一并删除。
4. 用 `cd /opt/treestrm && python3 -c "import app"` 之类方式在启动前做一次导入自检（会生成访问口令，见下文）。

## 二、行为变化（升级后会注意到的）

| 变化 | 说明 |
|---|---|
| **页面需要访问口令** | 首次打开弹窗输入。口令来源：环境变量 `TREESTRM_ACCESS_TOKEN` > `config.json` 的 `access_token` > 首次启动自动生成并写回 `config.json`（同时打印到 stdout / journald）。修改口令需重启服务。 |
| **config.json 损坏时拒绝启动** | 以前会静默用默认配置覆盖（丢 cookie / rules）。现在：主文件损坏 → 自动读取 `config.json.bak`；备份也不可用 → 抛 `ConfigLoadError`，服务启动失败并提示手动修复。 |
| **大批量删除 STRM 会被熔断** | 单次待删 ≥ 500 且占本地 > 30% 时跳过清理并在日志报 `🛑 安全熔断`。确需大批量清理时临时调高 `engine/reconciler.py` 中 `ORPHAN_GUARD_MIN_COUNT` / `ORPHAN_GUARD_MAX_RATIO`。 |
| **日志真正只保留 7 天** | 以前清理函数从未被调用，`data/logs/` 会无限增长；升级后首条日志即清理 7 天前文件。 |
| **前端完全重写** | 见第三节「UI 重写」。无外部 CDN 依赖，内网 / 断网可用。 |
| **TPDB / Telegram 改为页面配置** | 新增 `config.json` 字段 `tpdb_base`、`tpdb_token`、`telegram_bot_token`、`telegram_chat_id`，在「系统设置」页填写。`tpdb_token` 留空时继续使用 `scraper/sources/tpdb.py` 内置 Token，刮削行为不变。旧代码的 Telegram Bot Token 为空字符串，从未真正推送过；如需启用，在设置页填 Bot Token 与 Chat ID（旧代码中的 Chat ID 为 `5662349315`）。 |
| **保存设置不再改动任务与加锁列表** | `POST /api/config` 只更新全局设置，`rules` / `protected_categories` 由各自接口维护；未提交的可选字段保持原值；自动同步间隔最小 5 分钟。 |

## 三、变更明细

### a505d03 运行期 bug 修复
1. 补流分支路径未 `unquote`，中文/空格文件名补流全部失败 → 抽取 `map_to_cd2_path` 共用（`engine/cd2_extractor.py`）。
2. 两份日志缓冲区，cleaner / emby_client 日志前端不可见 → 统一 `core.events`。
3. 治理任务 / 全量同步异常后状态不复位，队列永久卡死 → `try/finally`。
4. `write_nfo_file` 未做 XML 转义 → 转义 studio/genre/tag，CDATA 处理 `]]>`。
5. 过期 STRM 清理比例熔断。

### 25a58f2 `/api/*` 访问口令鉴权
- 中间件校验请求头 `X-Access-Token`（`hmac.compare_digest`），首页 `/` 不受限；`/api/config` 不回显口令。

### 5b103b5 去重与遗留问题
1. **config.json 安全读写**（`core/config.py`）：解析失败不再覆盖；`save_config` 写临时文件 + `fsync` + `os.replace` 原子替换，替换前把**有效的**主文件备份为 `.bak`。
2. **去除重复代码**：`app.py` 统一从 `core/config.py`、`core/events.py`、`services/cloud115.py`、`engine/reconciler.py` 导入，有差异处以原 `app.py` 运行版本为准。
3. **AI 失败检测**：原实现读失败账本前后长度差，账本封顶 5000 条后差值恒为 0 → `call_ai_item_auditor` 通过 `failed_paths` 直接回传失败路径。
4. **cover_threads**：删除只有 1 个 worker 的线程池，改为显式串行循环（115 流控红线）。
5. **性能**：`/api/tasks` 读专区底账缓存；`/api/status` 历史记录走内存缓存；`append_history` 原子写入。

### 本轮：全部遗留问题 + UI 重写
1. **配置丢失更新**：新增 `modify_config(mutator)`，读-改-写整体持锁。所有写配置处（保存设置、增删任务、加锁/解锁、同步结果回写、访问口令生成）都改用它；同步任务结束时只回写本轮涉及规则的统计字段，不再用开始时读到的旧配置整体覆盖。
2. **保存设置覆盖配置**：旧前端固定提交 `auto_sync_enabled: false` 等值的问题随 UI 重写消失；后端合并 `ai_governance`（保留 `fallback_model` 等页面不展示的键）。
3. **TPDB Token 输入框不生效**：`ConfigModel` 增加 `tpdb_base` / `tpdb_token`，`scraper_service.py` 运行时读取配置，空值回退内置常量。
4. **死代码**：删除 `router.py`、`core/state_db.py`、`app.py` 中的 `get_category_cid_map` / `fetch_folder_pickcodes`；`engine/nfo_builder.py` 改为承载 `write_nfo_file`（从 `app.py` 迁入）。
5. **硬编码**：服务器 IP、成人/电影目录 CID、输出目录、TPDB 地址集中为 `core/config.py` 中的 `DEFAULT_*` 常量，仅在 `config.json` 缺字段时使用；Telegram 改为配置项。
6. **任务卡片统计错误**：`/api/rules` 以前对所有任务都套用「成人」专区底账，所有卡片数字相同；现在只有 `<default_output>/成人` 使用底账缓存，其他任务按各自目录实际统计，并返回 `governable` 标记。
7. **UI 重写**（`static/index.html`，单文件、零外部依赖）：
   - 结构：侧栏导航「概览 / 专区治理 / 运行日志 / 系统设置」，顶栏全局状态、流控档位、全量对账；同步或治理进行时所有页面顶部显示进度条。
   - 概览：真实的视频 / 封面 / NFO 覆盖率汇总；每个对账任务一张卡片，可单独对账、删除（二次确认）、跳转专区治理；新建任务弹窗（CID 校验）；最近同步记录表。
   - 专区治理：搜索、筛选（全部 / 待补齐 / 进行中 / 已加锁，带计数）、排序（名称 / 视频数 / 覆盖率低优先）、多选批量加入治理队列、排队中可取消；所有危险操作走确认弹窗，结果用 toast 提示。
   - 运行日志：只在有新日志时重绘；用户向上翻看时不强制滚动到底部；可只看警告与错误。
   - 系统设置：分组卡片，密钥字段可显示/隐藏，有修改时出现底部保存栏，离开页面前提示未保存修改。
   - 安全：所有动态内容经 `esc()` 转义，不再使用内联 `onclick` 拼接参数（事件委托 + `data-*`）。

### 部署后修复：115 目录树导出超时
- 提交导出任务读取超时 20 → 60 秒，超时/连接错误计入重试（10 次）；下载直链与目录树文件下载失败自动重试 3 次。
- 提交成功后每 30 秒查询一次生成状态，最长等待 30 分钟（原为每 2 秒查询、最长 4 分钟，十万级文件目录树来不及生成）。可调 `services/cloud115.py` 中 `POLL_INTERVAL` / `POLL_MAX_WAIT`。

### 抽封面 / 补流「目标路径不存在」误判
- `engine/cd2_extractor.py` 新增 `resolve_cd2_path`：首次 `exists` 未命中时列一次父目录（促使 CD2 FUSE 刷新目录缓存），并按原名 / Unicode NFC 规范化 / 忽略大小写在列表里找实际文件名；重试间隔 1.5s、3s（原为 1s×2）。
- 仍找不到时日志会区分：「父目录存在但列表中没有该文件」（115 已删除/改名，STRM 过期）与「父目录不存在，最深可访问目录为 …」（`cd2_mount_path` / STRM 前缀映射不符，或目录已移动）。
- 「仅补流」分支以前不做存在性检查，直接 ffprobe，失败只报「探针重试中」；现在同样先走 `resolve_cd2_path`。
- 部署后若仍大量报错，按日志中的「最深可访问目录」判断是映射问题还是文件确实不在。

### 官方刮削独立为 `scraper/` 模块
旧 `scraper_service.py` 已删除，由仓库内独立包 `scraper/` 取代（`app.py` 只负责挂载路由与回调）。

**结构**：`identify`（文件名 → 类型 + 番号）→ `sources/`（各站点解析器，按类型的优先级链）→ `merge`（置信度打分 + 多源字段合并）→ `output`（海报裁剪、NFO）→ `store`（sqlite 记录每条结果）→ `runner`（任务队列、断点续跑）→ `api`。

**修掉的旧问题**
1. JavDB 直接取搜索结果第一条，番号不对也写入 → 逐条核对番号。
2. FC2 官方页标题里混有隐藏的反爬字符 → 按可见文本提取；「找不到商品」页按专用标记识别。
3. 一本道番号 `010120_001` 被 `_\d+$` 规则截断 → 识别规则重写；且 `010120-001`（加勒比）与 `010120_001`（一本道）不再互相串号。
4. TPDB 站点名模糊匹配，Blacked Raw 被并入 Blacked → 站点名精确匹配，按日期 + 站点查询，日期不符的候选置信度封顶 0.5。
5. JavBus 横版封套直接当海报 → 默认裁右侧竖版海报（可关）。
6. MGS 素人番号带数字前缀（`259LUXU-1500`）各站搜不到 → 自动尝试去前缀别名。
7. 删除未使用的 `STATE_FILE`、AI 代码等死代码。

**新能力**
- **多源合并 + 置信度**：每部最多合并 `max_sources` 个站点（默认 3）；低于 `min_score`（默认 0.6）不落盘，进入「待确认」。
- **更全的输出**：NFO 包含标题/原标题、简介、演员（带头像）、类型、系列、片商、发行日期、评分、uniqueid 等，并带 `<scraper>TreeSTRM-Scraper</scraper>` 标记；保留已有 NFO 中的 `fileinfo` / 时长；新增 `-fanart.jpg`、`-thumb.jpg`；可选 AI 翻译标题/简介（复用「AI 治理」接口配置）。
- **覆盖策略**：`missing` 只补缺失文件（默认）；`replace_ai` 覆盖 AI 生成的 NFO，保留其他刮削器 NFO；`all` 全部覆盖（前端二次确认）。
- **失败处理与手动修正**：结果页按 待确认 / 未找到 / 出错 / 已匹配 / 已忽略 分类；可手动搜索候选（显示图片与置信度）、按番号重刮、忽略；手动结果会记住，后续重跑沿用。失败条目 `retry_failed_days` 天内不自动重试。
- **稳定性**：每个域名限速（`request_interval`）；连续失败 3 次熔断 10 分钟，再失败翻倍，最长 2 小时；页面磁盘缓存 `cache_days` 天；任务队列存 sqlite，服务重启后自动续跑；所有写文件均为原子写。
- **前端**：侧栏新增「官方刮削」页：统计、任务进度与历史、结果表（搜索 / 分页）、站点健康与启用开关、刮削设置。专区页「官方刮削」按钮改为弹出任务选项。

**部署相关**
- 新数据：`/opt/treestrm/data/scraper/scraper.db`（sqlite，WAL）与 `/opt/treestrm/data/scraper/cache/`（页面缓存，可在页面「清空页面缓存」）。首次启动自动创建。
- 新配置：`config.json` 的 `scraper` 段（页面「刮削设置」写入，缺省时用默认值）：`sources`、`min_score`、`overwrite`、`write_fanart`、`crop_poster`、`title_with_number`、`translate`、`request_interval`、`cache_days`、`retry_failed_days`、`max_sources`。
- 依赖：`requests`、`beautifulsoup4`（原有）；`Pillow` **可选**，用于裁剪海报，没有时回退 `ffmpeg`，再没有就直接用原图。
- 新接口（均需 `X-Access-Token`）：`/api/scraper/jobs`、`status`、`cancel`、`items`、`item`、`search`、`apply`、`retry`、`items/ignore`、`settings`、`cache/clear`、`health/reset`。
- 兼容：`POST /api/categories/scrape_official` 保留，变为「加入刮削队列」的别名，立即返回。`/api/status` 新增 `scraper` 字段。
- 清理失效（`engine/cleaner.py`）现在也删除孤立的 `-fanart.jpg`，返回 `deleted_fanarts`。

## 四、已知限制

1. 专区治理（`/api/categories`）只覆盖 `<default_output>/成人` 下的子目录；其他对账任务只做 STRM 对账。
2. `scraper/sources/tpdb.py` 中保留了内置 TPDB Token 作为回退值（所有者已知悉）。
3. 刮削站点中 MGStage / Prestige / FALENO 仅日本 IP 可访问；fc2ppvdb（526）、fc2club（401）、AVSOX（SSL 错误）、Getchu 在开发环境未能实测，解析器按页面结构编写，**部署后需在服务器上实测**。连续失败会自动熔断，不影响其他站点。
4. 一本道系（1Pondo / 10musume / Pacopacomama）与 HEYZO 官方只提供横版图，海报为横版。

## 五、升级后验证

1. 启动日志或 `config.json` 中取得口令，打开页面输入，确认四个页面数据正常加载、浏览器控制台无报错。
2. `curl http://<host>:8028/api/config` 不带请求头应返回 401。
3. 在设置页修改任意一项并保存：`config.json` 中 `rules`、`protected_categories`、`access_token` 保持不变，`config.json.bak` 出现。
4. 概览页各任务卡片的视频数不再相同（非「成人」任务按自身目录统计）。
5. 跑一次全量对账：进度条正常推进，STRM 新增 / 清理数量合理，无「安全熔断」误报；结束后任务卡片的「上次对账」时间更新。
6. 对含中文文件名的专区「查漏补缺」一次（勾选「同时补充音视频流信息」），确认日志出现「[深度补流] … 成功补全流信息」。
7. 对专区执行一次「清理失效」，确认 toast 显示删除数量，清理日志出现在运行日志页。
8. 官方刮削：先用小专区、「只补缺失」、最多 20 部跑一次；在「官方刮削」页确认任务完成、已匹配条目的 NFO / 海报写入、Emby 刷新；查看「数据源」表中各站点请求 / 失败数，对第四节第 3 条列出的未实测站点重点核对，持续失败的可在页面关闭。
