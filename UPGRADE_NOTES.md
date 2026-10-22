# TreeSTRM 升级说明（分支 `fix/runtime-bugs`）

面向执行升级部署的人 / Agent。请完整阅读「升级前必做」与「行为变化」两节后再操作。

## 一、升级前必做

1. **备份 `/opt/treestrm/config.json` 和 `/opt/treestrm/data/` 目录。** 新版本会自动维护 `config.json.bak`，但第一次升级前它还不存在。
2. 确认服务器上有 `requests`、`fastapi`、`pydantic`、`beautifulsoup4` 等原有依赖（本次未新增依赖）。
3. 用 `cd /opt/treestrm && python3 -c "import app"` 之类方式在启动前做一次导入自检（会生成访问口令，见下文）。

## 二、行为变化（升级后会注意到的）

| 变化 | 说明 |
|---|---|
| **页面需要访问口令** | 首次打开弹窗输入。口令来源：环境变量 `TREESTRM_ACCESS_TOKEN` > `config.json` 的 `access_token` > 首次启动自动生成并写回 `config.json`（同时打印到 stdout / journald）。修改口令需重启服务。 |
| **config.json 损坏时拒绝启动** | 以前会静默用默认配置覆盖（丢 cookie / rules）。现在：主文件损坏 → 自动读取 `config.json.bak`；备份也不可用 → 抛 `ConfigLoadError`，服务启动失败并提示手动修复。 |
| **大批量删除 STRM 会被熔断** | 单次待删 ≥ 500 且占本地 > 30% 时跳过清理并在日志报 `🛑 安全熔断`。确需大批量清理时临时调高 `engine/reconciler.py` 中 `ORPHAN_GUARD_MIN_COUNT` / `ORPHAN_GUARD_MAX_RATIO`。 |
| **抽帧线程数设置项变灰** | 一直被代码强制为单线程（115 流控红线），现在 UI 明确标注「暂不生效」。 |
| **日志真正只保留 7 天** | 以前清理函数从未被调用，`data/logs/` 会无限增长；升级后首条日志即清理 7 天前文件。 |

## 三、本次变更明细

### a505d03 运行期 bug 修复
1. 补流分支路径未 `unquote`，中文/空格文件名补流全部失败 → 抽取 `map_to_cd2_path` 共用（`engine/cd2_extractor.py`）。
2. 两份日志缓冲区，cleaner / emby_client 日志前端不可见 → 统一 `core.events`。
3. 治理任务 / 全量同步异常后状态不复位，队列永久卡死 → `try/finally`。
4. `write_nfo_file` 未做 XML 转义 → 转义 studio/genre/tag，CDATA 处理 `]]>`。
5. 过期 STRM 清理比例熔断。

### 25a58f2 `/api/*` 访问口令鉴权
- 中间件校验请求头 `X-Access-Token`（`hmac.compare_digest`），首页 `/` 不受限；`/api/config` 不回显口令。
- 前端全局包装 `fetch` 自动携带口令，401 弹窗；取消后本页不再弹窗。口令须为 ASCII。

### 本轮：遗留问题清理
1. **config.json 安全读写**（`core/config.py`）：解析失败不再覆盖；`save_config` 写临时文件 + `fsync` + `os.replace` 原子替换，替换前把**有效的**主文件备份为 `.bak`；写入加线程锁。
2. **去除重复代码**：`app.py` 不再自带副本，统一导入：
   - 配置、路径常量、Pydantic 模型 → `core/config.py`
   - 日志、Telegram、流控延时 → `core/events.py`
   - 115 会话与目录树导出 → `services/cloud115.py`（采用原 `app.py` 带排队重试 + CID 搜索兜底的版本）
   - 目录树解析与 STRM 生成 → `engine/reconciler.py`（含熔断；进度改为逐条回调，`app.py` 用回调更新 `sync_progress`）
   - 有差异处一律以原 `app.py` 运行版本为准，`app.py` 从约 1740 行降到约 1370 行（Measure-Object 非空行计数）。
3. **AI 失败检测**：原实现读失败账本前后长度差，账本封顶 5000 条后差值恒为 0，失败条目不再打「AI待补」标记 → `call_ai_item_auditor` 通过 `failed_paths` 参数直接回传失败路径。
4. **cover_threads**：删除只有 1 个 worker 的线程池，改为显式串行循环；UI 输入框禁用并说明原因。
5. **性能**：`/api/tasks` 改读专区底账缓存（缺失时盘点一次）；`/api/status` 的历史记录改为内存缓存，不再每 2.5 秒读盘；`append_history` 原子写入。
6. **前端转义**：新增 `esc()` / `jsArg()`，日志、专区名、规则名等插入 `innerHTML` 前转义；`onclick` 参数改为 JSON 字面量。修复了专区名含单引号（如 `Girls' Day`）时按钮失效，以及文件名含 HTML 时可注入页面的问题。

## 四、仍未处理（发现但不在本轮范围）

1. **保存设置会覆盖部分配置**：`static/index.html` 的 `saveGlobalConfig` 固定提交 `auto_sync_enabled: false`、`auto_sync_interval_mins: 30`、`ai_governance.batch_size: 100`。若在 `config.json` 手动开启了自动同步，在页面点一次「保存全局配置」就会被关掉。
2. **TPDB token 输入框不生效**：前端提交的 `tpdb_token` 不在 `ConfigModel` 中，被静默丢弃；刮削实际使用 `scraper_service.py` 中的常量 `TPDB_TOKEN`。
3. **配置并发丢失更新**：各线程 `load_config → 修改 → save_config` 无整体加锁，同时修改不同字段时后写者会覆盖前者（文件本身不会再损坏）。
4. **未被引用的代码**：`router.py`、`engine/nfo_builder.py`、`core/state_db.py`、`app.py` 中的 `get_category_cid_map` / `fetch_folder_pickcodes` 均无调用方。
5. 代码中仍硬编码服务器 IP `23.19.231.70`、成人目录 CID、Telegram chat_id。

## 五、升级后验证

1. 启动日志或 `config.json` 中取得口令，打开页面输入，确认三个页签数据正常加载。
2. `curl http://<host>:8028/api/config` 不带请求头应返回 401。
3. `/opt/treestrm/config.json.bak` 在首次保存配置后出现。
4. 跑一次全量同步：进度条正常推进，STRM 新增 / 清理数量合理，无「安全熔断」误报。
5. 对含中文文件名的专区开启「补流」治理一次，确认日志出现「深度补流 成功」。
6. 对专区执行一次「清失效」，确认清理日志出现在页面实时日志中。
