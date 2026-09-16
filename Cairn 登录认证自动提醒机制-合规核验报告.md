# Cairn 登录认证「自动提醒 + 自动弹窗」机制 — 开发合规核验报告

对照《Cairn 登录认证自动提醒与自动弹窗机制开发文档》（48 节），逐条核验实现合规性。

## 一、核心架构（§1–§4）

| 文档要求 | 实现 | 状态 |
|---------|------|------|
| Human Intervention 机制 | `auth_helper` 包 + `auth_requests` 路由 + `ReasonResult.interventions` | ✅ |
| 状态空间 `Fact → Reason → {Intent→Agent / Intervention→Human} → Fact` | `reason.py` 同时处理 intents 与 interventions，二者互不阻塞 | ✅ |
| 目录规划 `auth/intervention/`、`auth_helper/`、`routers/auth_requests.py` | `auth_helper/`（daemon/client/desktop/launcher）、`routers/auth_requests.py` 均已实现 | ✅ |

## 二、Reason 协议与数据结构（§5–§9）

| 文档要求 | 实现 | 状态 |
|---------|------|------|
| Reason 输出新增 `interventions` | `validate_reason_payload` 支持 `interventions`，返回归一化 `ReasonResult` | ✅ |
| `InterventionRequest` 结构（type/from/target/role/login_url/reason） | `validate_intervention()` 校验必填 `from/target/role/reason`，`login_url` 可选，仅支持 `type="auth"` | ✅ |
| `ReasonResult`（complete/intents/interventions） | `contracts.py` 新增 `@dataclass ReasonResult`，含 `rejected` 与 `is_noop` | ✅ |
| reason.md 加 intervention 规则、禁泄 secret | `reason.md` 新增 "Human Intervention Rules" + 输出示例 | ✅ |
| intents + interventions 可同时产出 | 测试 `test_reason_creates_intents_and_interventions_together` 覆盖 | ✅ |

## 三、Auth Request 数据与状态机（§10–§16）

| 文档要求 | 实现 | 状态 |
|---------|------|------|
| `auth_requests` 表（id/project_id/source_fact_ids/auth_ref/role/login_url/reason/status/claimed_by/时间戳/failure_reason） | `db.py` 新增同名表，字段一一对应 | ✅ |
| ID 形如 `auth_001` | `next_auth_request_id` 用 counters 生成 `auth_{:03d}` | ✅ |
| 状态机 pending→claimed→waiting_user→verifying→completed/failed（+cancelled/expired） | `models.AuthRequestStatus` 含全部 8 态；路由状态转换校验前置状态 | ✅ |
| claim 原子化（`WHERE status='pending'`，rowcount==1） | `claim_auth_request_atomic` 精确实现，rowcount==0 返回 409 | ✅ |
| 去重逻辑键 `(project_id, auth_ref, active-status)` | `find_active_auth_request` 查询 pending/claimed/waiting_user/verifying，命中直接返回已有 | ✅ |
| session 已有效时不创建（AuthStateResolver） | `build_auth_state_resolver` 解析时序状态，`valid` 时返回 409 | ✅ |

## 四、服务层与 API（§12–§13）

| 文档要求 | 实现 | 状态 |
|---------|------|------|
| `AuthRequest` model | `server/models.py` 新增，`status` 用 Literal 8 态 | ✅ |
| POST `/projects/{project_id}/auth-requests` | ✅ |
| GET `/auth-requests?status=` | ✅ |
| POST `/auth-requests/{id}/claim` / `waiting` / `verifying` / `complete` / `fail` | 全部实现 | ✅ |

## 五、AuthStateResolver（§16、§36）

| 文档要求 | 实现 | 状态 |
|---------|------|------|
| 输出 missing/valid/invalid | `AuthStateResolver.resolve` 返回三态 | ✅ |
| 时序解析（Verified→Invalid=invalid，Invalid→Verified=valid） | 按 `facts` 的 rowid 顺序遍历，后写入覆盖前状态 | ✅ |

## 六、Desktop Auth Helper（§19–§24、§38）

| 文档要求 | 实现 | 状态 |
|---------|------|------|
| `cairn auth-helper` 后台进程 | `cli.py` 新增 `auth-helper` 命令 | ✅ |
| 按项目作用域轮询 pending，通过事件 claim，再 notify + launch | `AuthHelperDaemon.run_once` 完整实现 | ✅ |
| helper_id = hostname+username | `default_helper_id()` | ✅ |
| 桌面通知 | `DesktopNotifier`（Win PowerShell / macOS osascript / Linux notify-send） | ✅ |
| 自动弹窗（auto_launch） | `launcher.launch` 子进程调 `cairn auth login`（headed Chromium 即主弹窗） | ✅ |
| max_parallel_logins 防多弹窗 | daemon 每 tick 受 `max_parallel_logins` 上限约束 | ✅ |
| 浏览器在宿主机（非 Docker GUI） | helper 独立桌面进程，dispatcher 只产生 AuthRequest | ✅ |

## 七、CLI 与登录流程（§25–§31）

| 文档要求 | 实现 | 状态 |
|---------|------|------|
| `cairn auth login --request auth_007` | `login` 新增 `--request` 参数 | ✅ |
| 登录前转 waiting_user → verifying → complete/fail | `login` 内串接状态机（capture 前 waiting，保存后 verifying，成功 complete，失败 fail） | ✅ |
| 登录后重新验证（三层） | 复用 `AuthVerifier.verify_storage_state` | ✅ |
| 成功后写 AuthSessionVerified（不含 secret） | 复用 `AuthGraphAdapter.verified` | ✅ |

## 八、安全边界（§42–§45）

| 文档要求 | 实现 | 状态 |
|---------|------|------|
| Secret Plane / Reasoning Plane 分离 | auth_request 只存逻辑字段，不存凭据；Fact 只写 AuthSessionVerified/Invalid | ✅ |
| Worker 只读 `CAIRN_AUTH_DIR` | 上一阶段已实现，本轮未改 | ✅ |
| sanitizer 检测 Authorization/Bearer/Cookie 等 | 沿用 `auth/sanitizer.py` | ✅ |
| 日志不记 password/cookie/JWT | 各模块日志仅记 request id/auth_ref/role/reason | ✅ |

## 九、测试与验证

| 项 | 结果 |
|----|------|
| 新增测试文件 | `test_auth_requests.py`(13)、`test_auth_helper.py`(8)、`test_auth_intervention.py`(3)、`test_auth_dedup.py`(5) |
| 新增用例数 | 23 个，全部通过 |
| 适配的既有测试 | `test_contracts_and_drivers.py` 两个用例改为 ReasonResult 断言 |
| 全量测试 | 144 passed + 5 failed（5 failed 为 `test_local_execution.py` 预存 Windows 环境缺 `sh`/`claude` 二进制，与本次无关） |
| 端到端状态机 | 手工验证 pending→claimed→waiting_user→verifying→completed 全部 200/201 |

## 十、与文档的差异说明

> **更新（2026-09-05）**：以下第 1、2 项已完成落地，详见「十一、V2 补齐记录」。

1. ✅ **§47 Claim TTL / §46 Helper 崩溃过期回收**（已落地）：`expire_stale_claims` 将超时 `claimed` 请求原子回收到 `pending`，`expire_stale_requests` 将超时活跃请求标记为 `expired`，在 `list`/`claim` 路由入口惰性触发。TTL 值入 server `settings` 表（`auth_claim_ttl`/`auth_request_ttl`），支持旧库迁移。
2. ✅ **§39 `allow_roles` 强制拦截**（已落地）：在 dispatcher 侧 `reason.py` 创建 auth request 前，以配置中 target 的**权威 role**（非 LLM 输出）校验 `allow_roles` 白名单，未知 target 一并跳过。
3. **§41 远程 Cairn 场景**：`auth-helper --server` 已支持指向远程 URL，功能具备，未做 HTTPS 认证加固（依赖部署层 TLS/VPN）。

第 3 项仍为部署层待办；第 1、2 项已完成。

## 十一、V2 补齐记录（2026-09-05）

补齐此前标注的两项「已建模未落地」：

**1. Claim TTL 回收与请求过期**

- `config.py`：`AuthInterventionConfig` 新增 `claim_ttl: int = 300`。
- `server/db.py`：`settings` 表新增 `auth_claim_ttl`（默认 300）、`auth_request_ttl`（默认 1800），含 `_ensure_settings_columns` 旧库 ALTER 迁移。
- `server/models.py`：`Settings` 新增两字段。
- `server/services.py`：新增 `get_auth_claim_ttl` / `get_auth_request_ttl` / `expire_stale_claims` / `expire_stale_requests`（修正了 julianday 运算优先级）。
- `server/routers/auth_requests.py`：`_reap_expired` 在 `list` / `claim` 入口惰性回收。
- `server/routers/settings.py`：`/settings` 读写两字段。

**2. allow_roles 强制拦截**

- `dispatcher/tasks/reason.py`：创建 auth request 前，以 `config.auth.target(auth_ref).role` 为权威 role 校验 `allow_roles`，未知 target 跳过；被策略跳过的 intervention 计入「已处理」，避免误报 `failed` 触发调度重试。

**测试**：新增 `test_auth_ttl.py`（7 例）、扩展 `test_auth_intervention.py`（+4 例），全量 155 passed + 5 failed（5 个仍为 Windows 预存环境问题）。
