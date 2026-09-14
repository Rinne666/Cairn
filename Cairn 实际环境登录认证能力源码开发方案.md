# Cairn 实际环境登录认证能力源码开发方案

> 文档状态：**实现参考版（2026-09-06）**
>
> 本文前面的设计章节记录认证能力的目标架构和演进方案；以下“当前实现参考”以工作区源码为准，
> 用于实际部署、联调和代码维护。若历史设计描述与本节或源码不一致，以源码、测试和部署配置为准。

## 0. 当前实现参考

### 0.1 实现范围

当前已经实现的是一套**人工在环的浏览器登录自动化**：

```text
Reason
  -> auth intervention
  -> Server AuthRequest
  -> Desktop Auth Helper
  -> headed Chromium
  -> human login
  -> Playwright storage_state
  -> page / selector / API verification
  -> AuthSessionVerified / AuthSessionInvalid Fact
  -> next Reason cycle
```

支持的登录挑战由人工完成，包括用户名/密码、SSO、MFA、CAPTCHA 和硬件密钥。系统不尝试自动绕过这些挑战。
认证凭据只存在于浏览器上下文、认证存储和 Worker 运行时；Graph 中只记录认证结果和逻辑身份。

当前**不等于**无人值守账号登录，也不包含自动刷新 OAuth、自动处理 CAPTCHA/MFA、凭据保管库或浏览器池。

### 0.2 源码索引

| 能力 | 当前实现 | 说明 |
|---|---|---|
| 配置 | `cairn/src/cairn/dispatcher/config.py` | `AuthConfig`、目标、验证规则、干预开关、TTL、角色白名单 |
| 会话存储 | `cairn/src/cairn/auth/store.py` | 项目/目标隔离的 `state.json`、`meta.json`，路径净化和原子写入 |
| 交互式登录 | `cairn/src/cairn/auth/manager.py` | headed Chromium，人工完成登录，等待受保护页面可验证 |
| 会话验证 | `cairn/src/cairn/auth/verifier.py` | 页面状态、selector、认证 API 三层验证 |
| Graph 适配 | `cairn/src/cairn/auth/graph.py` | 通过普通 Intent conclude 产生认证 Fact，不直接写 SQLite |
| 凭据泄漏检测 | `cairn/src/cairn/auth/sanitizer.py` | 检测并 redact 常见 Cookie/Token/Header 字段；当前为工具模块 |
| AuthRequest API | `cairn/src/cairn/server/routers/auth_requests.py` | 创建、查询、原子 claim、状态转换和失败记录 |
| AuthRequest 服务 | `cairn/src/cairn/server/services.py` | 去重、认证状态解析、Claim/Request TTL 回收 |
| 桌面 Helper | `cairn/src/cairn/auth_helper/daemon.py` | 轮询 pending 请求、通知、自动启动登录进程 |
| 登录启动器 | `cairn/src/cairn/auth_helper/launcher.py` | 子进程执行 `cairn auth login --request ...` |
| CLI | `cairn/src/cairn/cli.py` | `auth login/verify/list/remove` 和 `auth-helper` |
| Worker 运行时 | `dispatcher/runtime/containers.py`、`local_backend.py` | 注入 `CAIRN_AUTH_DIR`，容器只读挂载 |
| Worker Prompt | `dispatcher/prompts/default/reason.md`、`explore.md` | 认证前置门禁、干预协议和 Secret 禁止规则 |

### 0.3 实际登录流程

1. Reason 根据 Graph 判断认证是否为当前探索的实际前置条件。
2. 没有有效 `AuthSessionVerified` 时，Reason 输出 `interventions` 中的 `type=auth` 项；不创建“让 Agent 自己登录”的普通 Intent。
3. Dispatcher 以 `(project_id, auth_ref)` 创建或复用一个 AuthRequest。若已有有效会话或活跃请求，Server 拒绝重复请求。
4. 桌面运行的 `cairn auth-helper` 轮询 `GET /auth-requests?status=pending`，通过原子 claim 抢占请求。
5. Helper 发送桌面通知，并在 `auto_launch` 开启时启动：

   ```text
   cairn auth login --config dispatch.yaml --project <project_id> --target <auth_ref> --request <request_id>
   ```

6. `AuthManager` 启动 headed Chromium，打开目标 `login_url`，等待操作员完成登录。状态从 `claimed` 进入 `waiting_user`。
7. 登录态候选出现后，使用受保护页面、认证 selector 和认证 API 进行验证。验证期间状态为 `verifying`。
8. 验证成功后保存：

   ```text
   <store_root>/<project_id>/<auth_ref>/state.json
   <store_root>/<project_id>/<auth_ref>/meta.json
   ```

   `meta.json` 只保存目标、角色、时间和验证结果，不保存 Cookie、Token 或密码。
9. `AuthGraphAdapter` 创建 `Verify authenticated session` Intent 并 conclude，Server 追加：

   ```text
   AuthSessionVerified
   target=<auth_ref>;
   role=<role>;
   scope=<base_url>;
   verification=protected_page+selector+api
   ```

10. 登录请求进入 `completed`，Graph 变化触发下一轮 Reason；认证分支自然继续，不需要单独的 `resume_scan()`。

### 0.4 状态机和异常路径

正常状态机：

```text
pending -> claimed -> waiting_user -> verifying -> completed
                                  \-> failed
```

服务端还支持 `cancelled` 和 `expired`。惰性回收发生在 AuthRequest 的 list/claim 请求入口：

- `claimed` 超过 `auth_claim_ttl`：释放回 `pending`，允许其他 Helper 重新 claim；
- 活跃请求超过 `auth_request_ttl`：标记为 `expired`，不再参与去重。

会话失效时，验证工具可以追加：

```text
AuthSessionInvalid
target=<auth_ref>;
role=<role>;
evidence=<non-secret evidence>
```

Reason 下一轮看到最新失效状态后，会重新产生认证干预。项目保持 `active`，匿名探索不因某条认证分支等待而停止。

### 0.5 配置最小示例

认证配置位于 Dispatcher 配置的 `auth` 段；实际 `dispatch.yaml` 被 `.gitignore` 忽略，含密钥的配置不得提交。

```yaml
auth:
  store_root: "/opt/cairn/auth"
  worker_mount_root: "/run/cairn-auth"
  login_timeout: 900
  verify_timeout: 30
  intervention:
    enabled: true
    request_ttl: 1800
    claim_ttl: 300
    allow_roles: [user, admin]
  targets:
    - name: "target-user"
      base_url: "https://target.example.com"
      login_url: "https://target.example.com/login"
      role: "user"
      indexed_db: true
      verify:
        url: "https://target.example.com/dashboard"
        expect_status: 200
        selector: "[data-testid='user-menu']"
        http_url: "https://target.example.com/api/me"
        http_expect_status: 200
```

`allow_roles` 使用配置中目标的权威 `role` 校验，不信任 LLM 输出的角色。`enabled: false` 时应视为关闭人工认证干预入口。

### 0.6 运维命令

```bash
# 启动 API Server
uv run --project cairn cairn serve

# 启动 Dispatcher
uv run --project cairn cairn dispatch --config dispatch.yaml

# 在操作员桌面启动自动提醒/弹窗 Helper
uv run --project cairn cairn auth-helper \
  --server http://localhost:8000 \
  --config dispatch.yaml \
  --auto-launch

# 手动登录并保存认证态
uv run --project cairn cairn auth login \
  --config dispatch.yaml --project proj_001 --target target-user

# 复验已有认证态并写入认证 Fact
uv run --project cairn cairn auth verify \
  --config dispatch.yaml --project proj_001 --target target-user

# 查看/删除项目认证 profile
uv run --project cairn cairn auth list --config dispatch.yaml --project proj_001
uv run --project cairn cairn auth remove --config dispatch.yaml --project proj_001 --target target-user
```

### 0.7 运行时认证态路径

容器模式要求 Dispatcher 和 Docker daemon 能看到同一个宿主机路径：

```text
Host / Dispatcher: /opt/cairn/auth/<project_id>/<auth_ref>/state.json
Worker container:  /run/cairn-auth/<auth_ref>/state.json
```

`ContainerManager` 将项目目录以 `ro` 方式挂载，并注入：

```text
CAIRN_PROJECT_ID=<project_id>
CAIRN_AUTH_DIR=/run/cairn-auth
```

Local 模式注入宿主机目录：

```text
CAIRN_AUTH_DIR=<store_root>/<project_id>
```

Worker 只应根据 `CAIRN_AUTH_DIR` 和 Graph 中的 `auth_ref` 定位会话；不得把 `state.json` 内容打印到 Prompt、日志或 Fact。

### 0.8 安全边界

Secret Plane 包括：密码、Cookie、JWT、Bearer Token、MFA Secret 和完整 storage state；Reasoning Plane 只包括：

```text
auth_ref
role
scope
verification result
non-secret failure evidence
```

认证 Fact 必须通过 `AuthGraphAdapter` 的固定格式产生。当前 `sanitizer.py` 已有检测和 redact 实现，但尚未自动接入所有通用 Fact conclude 路径；生产接入前应补上 Server/Dispatcher 的强制拦截，并覆盖泄漏拒绝测试。

### 0.9 当前验证证据

- 认证相关测试：`62 passed`。
- 全量测试：当前 Windows 环境下 `148 passed, 12 failed`。
- 失败集中在既有 LocalProcess/本地 CLI 路径（Windows 缺少 `sh`、`python3` 或 Worker CLI），不是认证专项测试失败。
- Playwright Python 包可导入，当前机器的 Chromium 可正常 headless 启动；尚未针对真实业务站点执行人工登录端到端测试。

### 0.10 当前已知缺口

以下问题属于当前源码的明确待办，不应在部署文档中标记为“已完成”：

1. `AuthTargetConfig.indexed_db` 已建模，但 `AuthManager` 调用 `context.storage_state()` 时尚未显式传入 `indexed_db=target.indexed_db`。
2. `auth/sanitizer.py` 尚未接入通用 Fact 写入链路；目前主要依赖 Prompt 规则和认证 Fact 的固定构造。
3. `AuthVerifier` 的网络异常仍可能直接抛出，CLI/helper 需要将其稳定转换为 `AuthSessionInvalid` 或 AuthRequest `failed`。
4. `AuthHelperDaemon` 的 `_active` 集合尚未根据登录子进程退出自动清理，可能阻塞后续请求；需要补进程回收和并发槽位测试。
5. `AuthInterventionConfig.enabled` 当前需要在 `reason.py` 创建 AuthRequest 前显式执行门禁。
6. 已存在的 Worker 容器不会自动刷新认证 volume；启用认证配置后需要重建旧容器，或实现 mount fingerprint/重建检测。
7. 当前没有真实目标站点的浏览器端到端测试，SSO、MFA、CAPTCHA、IndexedDB 和跨域登录仍需在授权环境验证。
8. 远程 `auth-helper --server` 依赖部署层 HTTPS/VPN 或其他认证保护，当前代码没有额外的远程身份认证机制。

### 0.11 维护规则

- 修改认证配置模型时，同时更新 `dispatch.example.yaml` 和配置测试。
- 修改数据库字段时，只在 `server/db.py` 增加带 `PRAGMA table_info` 检查的 additive migration。
- 修改 AuthRequest 状态转换、去重或 TTL 时，更新对应 Server/API/TTL 测试。
- 修改认证 Prompt 或输出协议时，同时保持 `mock` Prompt 和 Mock 端到端测试可用。
- 不把真实 `dispatch.yaml`、`state.json`、Cookie、JWT、API Key 或浏览器导出文件加入 Git。

## 1. 开发目标

在不破坏 Cairn 现有 Blackboard / Fact-Intent Graph 架构的前提下，为 Cairn 增加真实 Web 环境下的登录认证能力，使其能够处理：

- 普通用户名/密码登录；
- Cookie / Session 登录；
- JWT / localStorage 登录；
- IndexedDB 登录状态；
- SSO / OAuth / 企业 IdP；
- MFA；
- CAPTCHA；
- 多角色账号；
- Session 过期；
- 登录状态重新验证；
- 容器内认证态复用；
- Agent 认证态扫描。

第一版不负责自动绕过 MFA、验证码或复杂身份认证，而是采用：

```text
Human / Operator
        │
        ▼
Browser Login
        │
        ▼
Auth Broker
        │
        ▼
Storage State
        │
        ▼
Auth Verifier
        │
        ▼
AuthSessionVerified Fact
        │
        ▼
Cairn Reason / Explore
        │
        ▼
Authenticated Testing
```

核心原则：

```text
认证凭据属于执行环境
认证成功属于事实图
```

即：

```text
Password / Cookie / Token / StorageState
                │
                ▼
          Secret Storage

AuthSessionVerified / AuthSessionInvalid
                │
                ▼
           Cairn Graph
```

禁止将密码、Cookie、JWT、Bearer Token、MFA Secret 等敏感认证信息写入 Fact、Intent、Hint 或 Agent Prompt。

---

# 2. 与 Cairn 现有架构的关系

Cairn 当前整体由：

```text
Cairn Server
    │
    ▼
Dispatcher
    │
    ▼
Project Container / Local Backend
    │
    ▼
Claude Code / Codex / Pi
```

构成。

Dispatcher 是实际控制面，负责：

- 读取项目图；
- 调度 bootstrap / reason / explore；
- 创建项目容器；
- 启动 Worker；
- 管理 session；
- 管理 timeout；
- 将结果写回 Cairn Server。

Agent 本身不直接控制协议状态。

因此认证功能应主要放入：

```text
Dispatcher Runtime Layer
```

而不是放入 LLM Worker Driver。

最终结构：

```text
                       Cairn Server
                Facts / Intents / Hints
                         ▲
                         │
                  Auth Facts
                         │
               +---------+---------+
               |    Dispatcher     |
               |-------------------|
               | Scheduler         |
               | Auth Broker       |
               | Auth Verifier     |
               | Auth Materializer |
               +---------+---------+
                         │
                  Auth State Store
                         │
              /opt/cairn/auth/
                         │
                read-only mount
                         ▼
              Project Worker Container
              /run/cairn-auth/
                         │
                         ▼
          Claude / Codex / Pi / Playwright
```

---

# 3. 推荐源码目录

新增后建议形成：

```text
cairn/
└── src/cairn/
    ├── cli.py
    │
    ├── auth/
    │   ├── __init__.py
    │   ├── models.py
    │   ├── manager.py
    │   ├── verifier.py
    │   ├── graph.py
    │   ├── store.py
    │   └── sanitizer.py
    │
    ├── dispatcher/
    │   ├── config.py
    │   ├── protocol/
    │   │   └── client.py
    │   │
    │   ├── runtime/
    │   │   ├── backend.py
    │   │   ├── containers.py
    │   │   └── local_backend.py
    │   │
    │   ├── tasks/
    │   │   └── common.py
    │   │
    │   └── prompts/
    │       └── default/
    │           ├── bootstrap.md
    │           ├── reason.md
    │           └── explore.md
    │
    └── tests/
        ├── test_auth_config.py
        ├── test_auth_store.py
        ├── test_auth_graph.py
        ├── test_auth_runtime.py
        └── test_auth_sanitizer.py

container/
├── Dockerfile
└── AGENTS.md

dispatch.example.yaml
docker-compose.yaml
.gitignore
```

---

# 4. 第一层：认证配置模型

修改：

```text
cairn/src/cairn/dispatcher/config.py
```

当前 Dispatcher 配置已经由 Pydantic 管理，并区分 `runtime.execution=container/local`。

新增认证配置：

```python
class AuthVerifyConfig(BaseModel):
    url: str
    expect_status: int = 200

    selector: str | None = None

    http_url: str | None = None
    http_expect_status: int = 200


class AuthTargetConfig(BaseModel):
    name: str

    base_url: str
    login_url: str

    role: str

    indexed_db: bool = True

    verify: AuthVerifyConfig


class AuthConfig(BaseModel):
    store_root: str

    worker_mount_root: str = "/run/cairn-auth"

    login_timeout: int = Field(
        default=600,
        gt=0,
    )

    verify_timeout: int = Field(
        default=30,
        gt=0,
    )

    targets: list[AuthTargetConfig] = Field(
        default_factory=list
    )
```

修改：

```python
class DispatchConfig(BaseModel):
    ...

    auth: AuthConfig | None = None
```

---

# 5. dispatch.yaml 设计

推荐：

```yaml
server: "http://cairn-server:8000"

auth:
  store_root: "/opt/cairn/auth"

  worker_mount_root: "/run/cairn-auth"

  login_timeout: 900
  verify_timeout: 30

  targets:

    - name: "target-user"

      base_url: "https://target.example.com"

      login_url: "https://target.example.com/login"

      role: "user"

      indexed_db: true

      verify:

        url: "https://target.example.com/dashboard"

        expect_status: 200

        selector: "[data-testid='user-menu']"

        http_url: "https://target.example.com/api/me"

        http_expect_status: 200


    - name: "target-admin"

      base_url: "https://target.example.com"

      login_url: "https://target.example.com/login"

      role: "admin"

      verify:

        url: "https://target.example.com/admin"

        selector: "#admin-panel"
```

这里：

```text
target-user
```

是认证 profile 的逻辑标识。

后续 Agent 只知道：

```text
auth_ref=target-user
```

而不知道：

```text
Cookie
Password
JWT
Token
```

---

# 6. 认证状态存储设计

宿主机：

```text
/opt/cairn/auth/
├── proj_001/
│   ├── target-user/
│   │   ├── state.json
│   │   └── meta.json
│   │
│   └── target-admin/
│       ├── state.json
│       └── meta.json
│
└── proj_002/
```

`state.json`：

```text
Playwright Storage State
```

可能包含：

```text
cookies
localStorage
IndexedDB
```

`meta.json`：

```json
{
  "target": "target-user",
  "role": "user",
  "base_url": "https://target.example.com",
  "created_at": "...",
  "verified_at": "...",
  "verification": {
    "page": true,
    "selector": true,
    "api": true
  }
}
```

meta 中仍然不保存 Cookie 和 Token。

---

# 7. AuthStore

新增：

```text
cairn/src/cairn/auth/store.py
```

职责：

```text
项目认证目录创建
state.json 路径计算
meta.json 管理
权限检查
profile 查找
```

核心接口：

```python
class AuthStore:

    def __init__(self, root: Path):
        self.root = root

    def project_dir(
        self,
        project_id: str,
    ) -> Path:
        ...

    def profile_dir(
        self,
        project_id: str,
        auth_ref: str,
    ) -> Path:
        ...

    def state_file(
        self,
        project_id: str,
        auth_ref: str,
    ) -> Path:
        ...

    def meta_file(
        self,
        project_id: str,
        auth_ref: str,
    ) -> Path:
        ...
```

需要对：

```text
project_id
auth_ref
```

进行路径净化，避免：

```text
../../
/
\
```

等路径穿越。

---

# 8. AuthManager

新增：

```text
cairn/src/cairn/auth/manager.py
```

职责：

```text
打开浏览器
执行人工辅助登录
等待登录完成
验证目标页面
保存 StorageState
```

建议 API：

```python
class AuthManager:

    def capture_interactive(
        self,
        project_id: str,
        target: AuthTargetConfig,
    ) -> AuthCaptureResult:
        ...
```

核心流程：

```text
launch headed chromium
        │
        ▼
goto login_url
        │
        ▼
Human login
        │
        ├── Password
        ├── SSO
        ├── MFA
        ├── CAPTCHA
        └── Hardware Key
        │
        ▼
verify protected page
        │
        ▼
storage_state()
        │
        ▼
state.json
```

这里的一个重要设计是：

```text
人完成身份验证
Agent 消费已经验证过的会话
```

而不是：

```text
Agent 尝试自动处理所有登录挑战
```

---

# 9. AuthVerifier

新增：

```text
cairn/src/cairn/auth/verifier.py
```

必须独立于 Capture。

原因是：

```text
成功创建 state.json
```

并不能证明：

```text
state.json 仍然有效
```

Verifier 至少做三层验证。

第一层：

```text
访问 protected URL
```

检查：

```text
HTTP 200
```

第二层：

```text
authenticated selector
```

例如：

```text
Logout
User Menu
Admin Panel
Username
```

第三层：

```text
Authenticated API
```

例如：

```text
GET /api/me
GET /api/profile
```

期望：

```text
200
```

最终：

```python
class AuthVerificationResult:
    valid: bool

    page_ok: bool
    selector_ok: bool
    api_ok: bool

    reason: str | None
```

---

# 10. 为什么不能只判断 200

有些应用：

```text
/dashboard
```

未登录时也可能：

```text
HTTP 200
```

但页面实际是：

```text
Login Page
```

所以：

```text
status == 200
```

不是充分条件。

推荐：

```text
HTTP Status
     +
Authenticated Selector
     +
Authenticated API
```

组成认证证据。

---

# 11. AuthGraph Adapter

新增：

```text
cairn/src/cairn/auth/graph.py
```

职责：

```text
把外部认证结果转换成 Cairn Intent → Fact
```

不要直接操作 SQLite。

当前 Cairn Server 是在 Intent conclude 时产生 Fact。

因此：

```text
origin
  │
  │ Verify authenticated session
  ▼
AuthSessionVerified
```

例如：

```text
Intent:

Verify authenticated session:
target=target-user
role=user
```

结论：

```text
AuthSessionVerified
target=target-user
role=user
auth_ref=target-user
verification=protected_page+selector+api
```

---

# 12. 修改 CairnClient

修改：

```text
cairn/src/cairn/dispatcher/protocol/client.py
```

当前 `create_intent()` 固定：

```python
worker=None
```



改成：

```python
def create_intent(
    self,
    project_id: str,
    from_ids: list[str],
    description: str,
    creator: str,
    worker: str | None = None,
) -> ApiResult:

    return self._request_json(
        "POST",
        f"/projects/{project_id}/intents",
        json={
            "from": from_ids,
            "description": description,
            "creator": creator,
            "worker": worker,
        },
    )
```

Auth Broker 使用：

```text
creator=operator.auth
worker=operator.auth
```

Server 当前协议本身允许：

```text
worker=null

或者

worker == creator
```



因此不需要修改 Server Protocol。

---

# 13. AuthSessionVerified Fact 规范

建议固定格式：

```text
AuthSessionVerified
target=<auth_ref>;
role=<role>;
scope=<base_url>;
verification=<methods>
```

例如：

```text
AuthSessionVerified
target=target-user;
role=user;
scope=https://target.example.com;
verification=protected_page+selector+api
```

不要写：

```text
cookie=...
token=...
password=...
Authorization=...
```

---

# 14. AuthSessionInvalid Fact

认证状态失效时：

```text
AuthSessionInvalid
target=target-user;
role=user;
evidence=protected endpoint redirected to login
```

常见触发：

```text
401
403 authentication failure
302 → /login
Session expired message
authenticated selector missing
/api/me becomes unauthorized
```

Cairn 本身就采用“追加 Fact 表达状态变化”的模型，因此认证状态过期不需要修改历史 Fact。

---

# 15. CLI 设计

当前：

```text
cairn serve
cairn dispatch
```



扩展：

```text
cairn auth
```

最终：

```text
cairn auth login
cairn auth verify
cairn auth list
cairn auth remove
```

第一版至少完成：

```text
login
verify
```

---

# 16. `cairn auth login`

推荐：

```bash
uv run --project cairn cairn auth login \
  --config dispatch.yaml \
  --project proj_001 \
  --target target-user
```

运行：

```text
读取 AuthTargetConfig
        │
        ▼
启动 Chromium headed
        │
        ▼
Operator 登录
        │
        ▼
AuthVerifier
        │
        ▼
storage_state()
        │
        ▼
state.json
        │
        ▼
重新验证 state.json
        │
        ▼
创建 AuthVerify Intent
        │
        ▼
Conclude
        │
        ▼
AuthSessionVerified Fact
```

---

# 17. `cairn auth verify`

```bash
cairn auth verify \
  --config dispatch.yaml \
  --project proj_001 \
  --target target-user
```

执行：

```text
load existing state.json
        │
        ▼
new browser context
        │
        ▼
protected page
        │
        ▼
selector
        │
        ▼
API verify
```

成功：

```text
AuthSessionVerified
```

失败：

```text
AuthSessionInvalid
```

---

# 18. 容器认证状态挂载

这是整个实现中最重要的运行时修改。

当前 `ContainerManager` 创建 Worker 容器时只传：

```text
image
network_mode
cap_add
```

没有认证 volume。

修改：

```text
cairn/src/cairn/dispatcher/runtime/containers.py
```

让：

```python
ContainerManager(
    container_config,
    auth_config,
)
```

支持：

```text
project_id
       ↓
/opt/cairn/auth/<project_id>
       ↓
Docker bind
       ↓
/run/cairn-auth
```

必须使用：

```text
read-only
```

即：

```python
volumes={
    host_auth_dir: {
        "bind": "/run/cairn-auth",
        "mode": "ro",
    }
}
```

---

# 19. Docker Socket 特殊问题

当前 Cairn Dispatcher 自身运行于 Docker，并挂载：

```text
/var/run/docker.sock
```



这意味着：

```text
Dispatcher
   ↓
Docker SDK
   ↓
HOST Docker daemon
```

Worker 的 bind mount source 必须是：

```text
Docker Host 路径
```

不能只是 Dispatcher 内部路径。

推荐统一：

```text
Host:
    /opt/cairn/auth

Dispatcher:
    /opt/cairn/auth

Worker:
    /run/cairn-auth
```

---

# 20. docker-compose 修改

增加：

```yaml
cairn-dispatcher:

  volumes:

    - /var/run/docker.sock:/var/run/docker.sock

    - ./dispatch.yaml:/cairn/dispatch.yaml

    - /opt/cairn/auth:/opt/cairn/auth
```

Dispatcher：

```text
读写 auth store
```

Worker：

```text
只读 auth store
```

---

# 21. Runtime Backend 扩展

建议给：

```text
ExecutionBackend
```

增加：

```python
def project_env(
    self,
    project_id: str,
) -> dict[str, str]:
    ...
```

统一处理：

```text
CAIRN_PROJECT_ID
CAIRN_AUTH_DIR
```

---

# 22. ContainerBackend 环境变量

返回：

```text
CAIRN_PROJECT_ID=proj_001
CAIRN_AUTH_DIR=/run/cairn-auth
```

---

# 23. LocalBackend 环境变量

Cairn LocalBackend 当前 Worker 会直接在宿主机运行，并继承宿主机环境。

因此：

```text
CAIRN_PROJECT_ID=proj_001

CAIRN_AUTH_DIR=/opt/cairn/auth/proj_001
```

Agent 不需要知道当前是：

```text
Docker
```

还是：

```text
Local
```

它永远只读取：

```text
$CAIRN_AUTH_DIR
```

---

# 24. Worker 环境变量注入

最佳修改位置：

```text
cairn/src/cairn/dispatcher/tasks/common.py
```

因为：

```text
bootstrap
reason
explore
```

最终全部经过：

```text
run_worker_process()
```



修改：

```python
env = {
    **worker.env,
    **backend.project_env(project_id),
}
```

再：

```python
build_exec_process(
    container_name,
    env,
    argv,
)
```

这是比修改：

```text
claudecode.py
codex.py
pi.py
```

更合理的做法。

认证能力属于：

```text
Runtime
```

而不是：

```text
LLM Driver
```

---

# 25. Worker 如何定位 Session

统一：

```text
$CAIRN_AUTH_DIR/<auth_ref>/state.json
```

例如 Fact：

```text
AuthSessionVerified
target=target-user;
role=user;
auth_ref=target-user
```

Agent 使用：

```text
$CAIRN_AUTH_DIR/target-user/state.json
```

Container：

```text
/run/cairn-auth/target-user/state.json
```

Local：

```text
/opt/cairn/auth/proj_001/target-user/state.json
```

---

# 26. Worker Dockerfile 修改

当前 Cairn Worker 已经包含 Playwright CLI 和 Chromium。

推荐增加 Python Playwright：

```dockerfile
RUN pip3 install \
    'playwright>=1.51,<2' \
    --break-system-packages
```

必要时：

```dockerfile
RUN python3 -m playwright install chromium
```

这样 Worker 可以：

```text
playwright-cli
Python Playwright
```

双路使用。

---

# 27. Cairn Python 依赖

当前：

```text
cairn/pyproject.toml
```

只有 FastAPI、Click、Docker、Requests 等主要依赖，没有 Python Playwright。

增加：

```toml
"playwright>=1.51,<2",
```

---

# 28. Prompt 认证规则

## reason.md

`reason` 必须知道：

```text
认证是前置状态
```

规则：

```text
没有 AuthSessionVerified
        ↓
不能产生 authenticated scan Intent
```

应该首先提出：

```text
Acquire / Verify Authentication
```

如果：

```text
AuthSessionInvalid
```

比：

```text
AuthSessionVerified
```

更新，则重新认证。

---

# 29. explore.md

规定：

```text
Authenticated Intent
        │
        ▼
寻找匹配 AuthSessionVerified
        │
        ▼
获得 auth_ref
        │
        ▼
读取 CAIRN_AUTH_DIR
        │
        ▼
使用 state.json
```

禁止：

```text
cat state.json
print Cookie
print Authorization
把 Token 写入最终 JSON
```

当前 `explore` 本来就只要求输出最新确认的增量事实，因此非常适合这一模式。

---

# 30. Auth Sanitizer

新增：

```text
cairn/src/cairn/auth/sanitizer.py
```

用于检测 Worker 最终返回结果中是否误包含敏感认证信息。

最低限度检测：

```text
Authorization:
Bearer eyJ...
Cookie:
Set-Cookie:
session=
access_token=
refresh_token=
```

如果发现：

```text
secret leak
```

不要直接把内容写 Fact。

可以：

```text
redact
```

后再提交。

例如：

```text
Authorization: Bearer <redacted>
```

更推荐：

```text
完全删除这一字段
```

---

# 31. Graph 认证状态模型

推荐状态链：

```text
origin
   │
   ▼
AuthRequired
   │
   ▼
AuthAcquire
   │
   ▼
AuthSessionVerified
   │
   ├────────────┐
   ▼            ▼
Crawl         API Map
   │            │
   └─────┬──────┘
         ▼
   Vulnerability Testing
         │
         ▼
 AuthSessionInvalid
         │
         ▼
     AuthRefresh
         │
         ▼
 AuthSessionVerified
```

---

# 32. 多角色支持

目录：

```text
proj_001/
├── anonymous
├── user-a
├── user-b
└── admin
```

Facts：

```text
AuthSessionVerified role=user auth_ref=user-a

AuthSessionVerified role=user auth_ref=user-b

AuthSessionVerified role=admin auth_ref=admin
```

于是 Cairn 可以进一步探索：

```text
user-a vs user-b
```

适用于：

```text
IDOR
BOLA
Horizontal Authorization
```

以及：

```text
user vs admin
```

适用于：

```text
Vertical Privilege Escalation
RBAC
Broken Access Control
```

---

# 33. Session 过期机制

不要给 Session 设置“永久有效”。

每次重要认证态任务前至少进行一次轻量验证：

```text
/api/me
```

或者：

```text
protected URL
```

如果失败：

```text
AuthSessionInvalid
```

而不是继续扫描。

---

# 34. 第一版不建议自动 refresh

V1：

```text
Session expired
       ↓
AuthSessionInvalid
       ↓
Operator
       ↓
cairn auth login
       ↓
AuthSessionVerified
```

V2 再考虑：

```text
refresh token
silent login
OAuth token refresh
```

否则认证逻辑会迅速变成独立身份系统。

---

# 35. `.gitignore`

当前仓库已经忽略：

```text
datas/
dispatch.yaml
.playwright-cli/
```



建议追加：

```gitignore
.auth/
auth/
*.storage-state.json
*.storage.json
```

实际推荐将 auth store 放在仓库之外：

```text
/opt/cairn/auth
```

因此 Git 本身不会看到它。

---

# 36. 权限设计

宿主机：

```text
/opt/cairn/auth
700
```

state：

```text
600
```

Dispatcher：

```text
rw
```

Worker：

```text
ro
```

Server：

```text
完全不挂载
```

这样：

```text
Cairn Server
```

甚至不知道认证 secret 在哪里。

这是更合理的安全边界。

---

# 37. 容器生命周期问题

当前 Cairn 会复用已存在的：

```text
cairn-dispatch-<project>
```

容器。

如果升级前已经创建过项目容器，它不会自动拥有新的 auth mount。

因此 V1 安装后必须删除旧容器：

```bash
docker ps -a \
  --filter name=cairn-dispatch- \
  -q \
  | xargs -r docker rm -f
```

---

# 38. 后续改进：Mount Fingerprint

V2 可以给 Worker Container 设置 Label：

```text
cairn.auth.mount.version=1
```

启动时检查：

```text
Image
Mount
Network
Capabilities
Auth Config
```

任何基础配置变化：

```text
自动重建容器
```

避免旧容器配置漂移。

---

# 39. 测试方案

## Config Test

验证：

```text
AuthConfig parse
空 target
重复 target
非法 timeout
非法目录
```

---

## AuthStore Test

验证：

```text
project directory
profile directory
state path
path traversal rejection
```

---

## AuthGraph Test

验证：

```text
AuthVerify Intent
        ↓
Conclude
        ↓
AuthSessionVerified
```

不能：

```text
直接写 Fact
```

---

## Container Test

验证：

```text
HOST auth directory
        ↓
Container mount
        ↓
/run/cairn-auth
```

以及：

```text
mode == ro
```

---

## LocalBackend Test

验证：

```text
CAIRN_AUTH_DIR
```

正确指向：

```text
<store_root>/<project_id>
```

---

## Secret Sanitizer Test

输入：

```text
Authorization: Bearer eyJ...
```

输出不能保留 Token。

---

# 40. 第一版 MVP 范围

V1 只实现：

```text
AuthConfig

AuthStore

AuthManager

AuthVerifier

cairn auth login

cairn auth verify

AuthSessionVerified Fact

AuthSessionInvalid Fact

CAIRN_AUTH_DIR

Worker read-only auth mount

reason authentication gate

explore authentication rule
```

不要第一版就实现：

```text
自动账号密码管理
自动 CAPTCHA
自动 MFA
OAuth Refresh
Credential Vault
Browser Pool
Remote Browser
多人审批
```

---

# 41. 第二阶段

V2 可以增加：

```text
Auth Session TTL
Automatic lightweight verification
Session Refresh
Multi-role Matrix
Auth Health Status
Auth UI
```

Graph 可以显示：

```text
user     VERIFIED
admin    EXPIRED
tenant-b MISSING
```

---

# 42. 第三阶段

V3 再引入：

```text
Auth Broker Service
```

独立进程：

```text
Cairn Dispatcher
       │
       ▼
Auth Broker API
       │
       ├── Browser Pool
       ├── Credential Vault
       ├── Session Store
       ├── Human Approval
       └── Session Refresh
```

适合企业规模运行。

---

# 43. 推荐开发顺序

建议按以下顺序开发：

```text
Phase 1
Config + Storage

        ↓

Phase 2
Playwright Login + Verify

        ↓

Phase 3
CLI

        ↓

Phase 4
Auth → Cairn Graph

        ↓

Phase 5
Container Mount

        ↓

Phase 6
CAIRN_AUTH_DIR

        ↓

Phase 7
Prompt Auth Gate

        ↓

Phase 8
Session Invalid Handling

        ↓

Phase 9
Secret Sanitizer

        ↓

Phase 10
Tests
```

不要先从 Prompt 开始。

真正的核心是：

```text
认证状态生命周期
+
容器生命周期
+
Fact 生命周期
```

---

# 44. 最终实际工作流

完整工作流：

```text
Create Project
     │
     ▼
Anonymous Exploration
     │
     ▼
Login Required
     │
     ▼
Operator executes:
cairn auth login
     │
     ▼
Headed Browser
     │
     ▼
Password / SSO / MFA
     │
     ▼
Protected Page
     │
     ▼
Storage State
     │
     ▼
Auth Verification
     │
     ▼
AuthVerify Intent
     │
     ▼
AuthSessionVerified Fact
     │
     ▼
Reason
     │
     ▼
Authenticated Intents
     │
     ├── Authenticated Crawl
     ├── API Enumeration
     ├── Authorization Testing
     ├── Business Logic
     └── Upload / Account / Admin
             │
             ▼
       Session Failure?
         │         │
        No        Yes
         │         │
         ▼         ▼
      Continue   AuthSessionInvalid
                    │
                    ▼
                Re-authenticate
```

---

# 45. 最终源码边界

最终职责应该非常明确：

```text
Cairn Server
    =
只维护 Graph

Dispatcher
    =
认证状态控制
任务调度
运行时注入

Auth Broker
    =
创建和验证真实登录态

Worker
    =
消费认证态
执行安全测试

LLM
    =
理解 AuthSessionVerified
规划 authenticated exploration
```

不要变成：

```text
LLM
 ↓
自己管理密码
 ↓
自己维护 Cookie
 ↓
自己决定 Session 是否可信
```

---

# 46. 最终设计原则

整个实现可以浓缩成：

```text
              Secret Plane
                  │
          Storage State / Token
                  │
        ┌─────────┴─────────┐
        │                   │
 Auth Broker           Worker Runtime
        │                   │
        └─────────┬─────────┘
                  │
             Verification
                  │
                  ▼
          AuthSessionVerified
                  │
                  ▼
             Cairn Graph
                  │
                  ▼
         Agent State Search
```

也就是说：

**Secret Plane 与 Reasoning Plane 分离。**

Cairn 的黑板只知道：

```text
“user 角色当前存在一个经过验证的登录能力”
```

而不知道：

```text
“这个 Cookie 的实际内容是什么”
```

这是在 Cairn 现有源码和 Worker 容器体系上实现真实环境登录认证最合理、最容易维护、也最符合其 Fact/Intent 设计哲学的实现方式。
