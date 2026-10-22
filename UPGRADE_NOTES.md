# TreeSTRM 升级说明（分支 `fix/runtime-bugs`）

面向执行升级部署的人 / Agent。请完整阅读「升级前必做」与「已知风险」两节后再操作。

## 一、升级前必做

1. **备份 `/opt/treestrm/config.json`**（以及 `/opt/treestrm/data/` 目录）。原因见「已知风险 1」。
2. 确认服务器上有 `requests`、`fastapi`、`pydantic` 等原有依赖（本次未新增依赖）。

## 二、本次变更

### 提交 a505d03：运行期 bug 修复

| # | 问题 | 修复 | 位置 |
|---|---|---|---|
| 1 | 仅补流信息分支未对 STRM 中 URL 编码路径做 `unquote`，中文/空格文件名补流全部失败 | 抽取 `map_to_cd2_path`，抽封面与补流共用 | `engine/cd2_extractor.py`、`app.py` |
| 2 | `app.py` 与 `core/events.py` 各有一份日志缓冲区，cleaner / emby_client 日志前端不可见 | 统一使用 `core.events`（含 7 天落盘），`app.py` 改为导入 | `core/events.py`、`app.py` |
| 3 | 治理任务 / 全量同步中途异常后 `current_running_job`、`is_syncing` 不复位，队列永久卡死 | `try/finally` 复位 | `app.py` `process_single_category_job`、`do_sync_task` |
| 4 | `write_nfo_file` 未做 XML 转义，AI 返回含 `&` `<` 时生成坏 NFO | studio/genre/tag 转义，CDATA 处理 `]]>` | `app.py` |
| 5 | 过期 STRM 清理只在 0 视频时熔断，目录树截断会误删大量 STRM | 待删 ≥ `ORPHAN_GUARD_MIN_COUNT`(500) 且占本地 > `ORPHAN_GUARD_MAX_RATIO`(30%) 时跳过清理并报警 | `app.py` |

> 注意：若确实在网盘大批量删除了文件，熔断会阻止本次清理，需临时调高上述两个常量。

### 提交 25a58f2：`/api/*` 访问口令鉴权

- 口令来源优先级：环境变量 `TREESTRM_ACCESS_TOKEN` > `config.json` 的 `access_token` > 首次启动自动生成并写回 `config.json`（同时打印到 stdout / journald）。
- 所有 `/api/*` 请求须带请求头 `X-Access-Token`，否则 401；首页 `/` 不受限。
- `/api/config` 响应不回显 `access_token`。
- 前端全局包装 `fetch` 自动携带口令，首次 401 弹窗输入并存入 localStorage；取消后本页不再弹窗，刷新可重新输入。口令须为 ASCII。
- 口令在启动时读取，**修改后需重启服务**。
- 口令为明文传输，公网访问时建议前置 HTTPS 反代。

## 三、已知风险（未修复）

1. **config.json 损坏会被默认配置覆盖**：`load_config`（`app.py` 与 `core/config.py` 各一份）在 JSON 解析失败时吞掉异常，落入默认配置分支并 `save_config` 覆盖原文件，cookie / rules / access_token 全部丢失。并发写入（多线程同时 `save_config`）产生半截文件时也会触发。
   建议修复方向：解析失败时抛错或返回只读默认值、不写盘；`save_config` 改为写临时文件后 `os.replace` 原子替换，并保留 `config.json.bak`。
2. **重复代码**：`app.py` 仍复制了 `core/`、`engine/`、`services/` 中的配置、115 接口、目录树解析、STRM 同步逻辑；运行时以 `app.py` 为准。`engine/reconciler.py` 中的 STRM 同步**未加**清理熔断，后续去重时应删除该副本或补上熔断。
3. `cover_threads` 配置在 `app.py` 中被强制改为 1，界面选项实际无效。
4. AI 失败账本 `ai_failure_batches.json` 靠前后长度差检测新失败，有竞争且文件无限增长。
5. `/api/tasks` 每次请求全量 `os.walk`，大库下较慢；`/api/status` 每次轮询读 `history.json`。
6. 前端日志区用 `innerHTML` 渲染日志原文，文件名含 HTML 时可被注入（现在 localStorage 里有口令，风险上升）。

## 四、升级后验证

1. 启动日志或 `config.json` 中取得口令，打开页面输入，确认各页数据正常加载。
2. 不带请求头访问 `curl http://<host>:8028/api/config` 应返回 401。
3. 跑一次全量同步，确认 STRM 新增 / 清理数量合理，无「安全熔断」误报。
4. 对含中文文件名的专区开启「补流」治理一次，确认日志出现「深度补流 成功」。
