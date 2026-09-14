# Cairn 登录认证自动提醒与自动弹窗机制开发文档

## 1. 开发目标

在 Cairn 现有 `Fact → Intent → Fact` 状态图与 Dispatcher 调度体系上增加一套 Human Intervention 机制，用于解决：

- Agent 扫描过程中发现业务必须登录；
- 自动识别“需要人工登录”这一状态；
- 自动提醒用户；
- 自动弹出登录浏览器；
- 用户完成登录、SSO、MFA、CAPTCHA；
- 自动保存登录态；
- 自动验证登录态；
- 自动写入 `AuthSessionVerified` Fact；
- Cairn 自动继续后续 authenticated scan；
- Session 失效后自动再次提醒登录；
- 多 Worker、多 Dispatcher 情况下避免重复弹窗。

目标不是让 Agent 自动绕过认证，而是：

```text
Agent 判断需要认证
        ↓
系统生成认证介入请求
        ↓
用户桌面收到通知
        ↓
自动打开登录浏览器
        ↓
用户完成人工认证
        ↓
系统验证并保存 Session
        ↓
Cairn 自动恢复扫描
```

---

# 2. 核心设计原则

不要采用：

```text
Agent 输出“需要登录”
        ↓
grep "登录"
        ↓
subprocess(cairn auth login)
```

正式实现应采用：

```text
Reason
  ↓
Typed Intervention
  ↓
Auth Request
  ↓
Desktop Helper
  ↓
Auth Broker
  ↓
AuthSessionVerified
```

核心原则：

```text
自动任务 → Intent

人工任务 → Intervention
```

因此 Cairn 状态空间从：

```text
Fact
 ↓
Intent
 ↓
Worker
 ↓
Fact
```

扩展为：

```text
               ┌→ Intent → Agent ─────┐
Fact → Reason ─┤                       ├→ Fact
               └→ Intervention → Human┘
```

---

# 3. 最终系统架构

```text
                   Cairn Server
              Facts / Intents / Hints
                     +
               Auth Requests
                     ▲
                     │
                 Dispatcher
                     │
                Reason Agent
                     │
          ┌──────────┴──────────┐
          │                     │
      Normal Intent       Auth Intervention
          │                     │
          ▼                     ▼
    Explore Worker        Auth Request Queue
                                │
                                ▼
                        Desktop Auth Helper
                                │
                     Notification / Popup
                                │
                                ▼
                         cairn auth login
                                │
                                ▼
                       Headed Chromium
                                │
                 Password / SSO / MFA / CAPTCHA
                                │
                                ▼
                          Auth Verifier
                                │
                                ▼
                        Storage State
                                │
                                ▼
                   AuthSessionVerified Fact
                                │
                                ▼
                         Reason 再次运行
                                │
                                ▼
                    Authenticated Intents
```

---

# 4. 源码目录规划

建议新增：

```text
cairn/src/cairn/
├── auth/
│   ├── __init__.py
│   ├── models.py
│   ├── store.py
│   ├── manager.py
│   ├── verifier.py
│   ├── graph.py
│   ├── sanitizer.py
│   │
│   └── intervention/
│       ├── __init__.py
│       ├── models.py
│       ├── service.py
│       └── detector.py
│
├── auth_helper/
│   ├── __init__.py
│   ├── daemon.py
│   ├── client.py
│   ├── desktop.py
│   └── launcher.py
│
├── dispatcher/
│   ├── config.py
│   ├── contracts.py
│   │
│   ├── protocol/
│   │   └── client.py
│   │
│   ├── tasks/
│   │   ├── reason.py
│   │   └── common.py
│   │
│   └── prompts/default/
│       ├── reason.md
│       └── explore.md
│
├── server/
│   ├── db.py
│   ├── models.py
│   │
│   └── routers/
│       └── auth_requests.py
│
└── cli.py
```

测试：

```text
cairn/tests/
├── test_auth_requests.py
├── test_auth_helper.py
├── test_auth_intervention.py
├── test_auth_graph.py
├── test_auth_store.py
└── test_auth_dedup.py
```

---

# 5. Reason 输出协议扩展

目前 Reason 主要产生：

```text
complete
intents
```

需要增加：

```text
interventions
```

推荐输出格式：

```json
{
  "accepted": true,
  "data": {
    "intents": [
      {
        "from": ["f005"],
        "description": "继续枚举公开 API"
      }
    ],
    "interventions": [
      {
        "type": "auth",
        "from": ["f008"],
        "target": "target-user",
        "role": "user",
        "login_url": "https://target.example.com/login",
        "reason": "订单和账户业务需要用户认证后才能继续"
      }
    ]
  }
}
```

这样允许：

```text
公开业务继续扫描
+
登录业务等待人工认证
```

同时进行。

---

# 6. Intervention 数据结构

新增：

```python
class InterventionRequest(BaseModel):
    type: Literal["auth"]

    from_: list[str]

    target: str

    role: str

    login_url: str | None = None

    reason: str
```

后续可以扩展：

```text
auth
mfa
captcha
approval
vpn
hardware_key
account_provision
```

因此建议统一抽象为：

```text
HumanIntervention
```

而不是只做 LoginRequest。

---

# 7. 修改 contracts.py

扩展 Reason Payload 校验。

支持：

```python
{
    "intents": [...],
    "interventions": [...]
}
```

推荐逻辑：

```python
def validate_intervention(
    item: dict,
) -> None:

    if item.get("type") != "auth":
        raise ValueError(
            "unsupported intervention type"
        )

    required = {
        "from",
        "target",
        "role",
        "reason",
    }

    missing = required - set(item)

    if missing:
        raise ValueError(
            f"missing intervention fields: {missing}"
        )
```

Reason 返回结构可归一化成：

```python
{
    "complete": None,
    "intents": [...],
    "interventions": [...]
}
```

不要再让 validator 只能返回单一：

```text
kind=intents
```

建议逐渐改成统一 ReasonResult。

---

# 8. 推荐 ReasonResult

新增：

```python
@dataclass
class ReasonResult:
    complete: dict | None

    intents: list[dict]

    interventions: list[dict]
```

这样 Reason 一次可以：

```text
创建普通任务
+
创建人工介入请求
```

而不是互斥。

---

# 9. reason.md Prompt 修改

增加：

```text
当继续测试必须依赖认证，而 Graph 中不存在有效
AuthSessionVerified Fact 时，不要创建一个要求 Agent
自己登录的普通 Intent。

应产生 authentication intervention。

只有当认证是实际扫描前置条件时才创建。

不要输出：

password
cookie
token
authorization header
MFA secret
```

Agent 应该输出：

```json
{
  "type": "auth",
  "target": "target-user",
  "role": "user",
  "reason": "该业务需要用户认证"
}
```

---

# 10. Auth Request 数据库

新增表：

```sql
CREATE TABLE auth_requests (
    id TEXT PRIMARY KEY,

    project_id TEXT NOT NULL,

    source_fact_ids TEXT NOT NULL,

    auth_ref TEXT NOT NULL,

    role TEXT NOT NULL,

    login_url TEXT,

    reason TEXT NOT NULL,

    status TEXT NOT NULL,

    claimed_by TEXT,

    created_at TEXT NOT NULL,

    claimed_at TEXT,

    completed_at TEXT,

    failure_reason TEXT
);
```

推荐 ID：

```text
auth_001
auth_002
auth_003
```

---

# 11. Auth Request 状态机

定义：

```text
pending
   │
   ▼
claimed
   │
   ▼
waiting_user
   │
   ▼
verifying
   │
   ├───────────────┐
   ▼               ▼
completed         failed
```

补充：

```text
cancelled
expired
```

完整状态：

```text
pending
claimed
waiting_user
verifying
completed
failed
cancelled
expired
```

---

# 12. Server Model

新增：

```python
class AuthRequest(BaseModel):

    id: str

    project_id: str

    source_fact_ids: list[str]

    auth_ref: str

    role: str

    login_url: str | None

    reason: str

    status: Literal[
        "pending",
        "claimed",
        "waiting_user",
        "verifying",
        "completed",
        "failed",
        "cancelled",
        "expired",
    ]

    claimed_by: str | None

    created_at: datetime

    claimed_at: datetime | None

    completed_at: datetime | None

    failure_reason: str | None
```

---

# 13. Server API

新增 Router：

```text
server/routers/auth_requests.py
```

接口：

```text
POST
/projects/{project_id}/auth-requests
```

创建。

---

```text
GET
/auth-requests?status=pending
```

查询待处理认证。

---

```text
POST
/auth-requests/{id}/claim
```

Claim。

---

```text
POST
/auth-requests/{id}/waiting
```

进入等待用户。

---

```text
POST
/auth-requests/{id}/verifying
```

开始验证。

---

```text
POST
/auth-requests/{id}/complete
```

完成。

---

```text
POST
/auth-requests/{id}/fail
```

失败。

---

# 14. Claim 必须原子化

避免：

```text
Helper A
Helper B
```

同时弹浏览器。

数据库应执行类似：

```sql
UPDATE auth_requests
SET
    status = 'claimed',
    claimed_by = ?,
    claimed_at = ?
WHERE
    id = ?
AND
    status = 'pending';
```

如果：

```text
rowcount == 1
```

成功。

如果：

```text
rowcount == 0
```

返回：

```text
409 Already claimed
```

---

# 15. Auth Request 去重

必须防止 Reason 每一轮都重复创建。

创建前查询：

```text
project_id
+
auth_ref
+
active status
```

例如：

```sql
SELECT id
FROM auth_requests
WHERE
    project_id = ?
AND
    auth_ref = ?
AND
    status IN (
        'pending',
        'claimed',
        'waiting_user',
        'verifying'
    );
```

如果存在：

```text
直接返回已有 request
```

不要新建。

逻辑唯一键：

```text
(project_id, auth_ref, active-request)
```

---

# 16. Session 已存在时不弹窗

创建 AuthRequest 前，还必须检查：

```text
当前 Graph 是否存在有效 AuthSessionVerified
```

如果：

```text
AuthSessionVerified target=target-user
```

并且之后没有：

```text
AuthSessionInvalid target=target-user
```

则：

```text
不创建 AuthRequest
```

因此需要 AuthStateResolver：

```python
class AuthStateResolver:

    def resolve(
        self,
        project,
        auth_ref,
    ) -> AuthState:
        ...
```

输出：

```text
missing
valid
invalid
```

---

# 17. reason.py 修改

Reason 解析完成后：

```python
result = validate_reason_payload(...)
```

处理：

```python
for intent in result.intents:
    client.create_intent(...)
```

然后：

```python
for intervention in result.interventions:

    if intervention["type"] == "auth":

        client.create_auth_request(
            project_id=project.project.id,
            source_fact_ids=intervention["from"],
            auth_ref=intervention["target"],
            role=intervention["role"],
            login_url=intervention.get(
                "login_url"
            ),
            reason=intervention["reason"],
        )
```

这样 Agent 的 Human Intervention 不会进入普通 Explore 调度。

---

# 18. 为什么不能创建普通 Intent

错误：

```text
Intent:
获取 user 登录态
```

Dispatcher 会认为这是：

```text
普通 Explore Task
```

然后把它发给 Claude/Codex/Pi。

但人工登录不是 Agent Task。

因此：

```text
自动执行工作 → Intent

需要人参与 → Intervention
```

必须分离。

---

# 19. Desktop Auth Helper

新增：

```text
cairn auth-helper
```

这是运行在：

```text
真实用户桌面
```

上的小型后台进程。

推荐：

```bash
cairn auth-helper \
  --server http://localhost:8000 \
  --config dispatch.yaml
```

如果 Cairn 在远程服务器：

```bash
cairn auth-helper \
  --server https://cairn.example.com \
  --config local-auth.yaml
```

---

# 20. Desktop Helper 工作循环

```python
while running:

    requests = client.list_pending_auth_requests()

    for request in requests:

        if client.claim(
            request.id,
            helper_id,
        ):

            notify(request)

            launch_login(request)

    sleep(poll_interval)
```

V1：

```text
poll 2 秒
```

即可。

V2 可以换：

```text
SSE
WebSocket
```

降低延迟。

---

# 21. helper_id

推荐：

```text
hostname + username
```

例如：

```text
rinne-windows
```

或者：

```text
desktop-rinne-01
```

用于：

```text
claim ownership
日志
排错
```

---

# 22. 自动提醒

新增：

```text
auth_helper/desktop.py
```

接口：

```python
class DesktopNotifier:

    def notify_auth_required(
        self,
        request,
    ) -> None:
        ...
```

通知内容：

```text
Cairn requires authentication

Project:
proj_001

Target:
target-user

Role:
user

Reason:
订单和账户业务需要认证后继续测试
```

---

# 23. 自动弹窗

如果配置：

```yaml
auto_launch: true
```

Helper Claim 成功后直接执行：

```text
cairn auth login
```

例如：

```bash
cairn auth login \
  --config dispatch.yaml \
  --project proj_001 \
  --target target-user \
  --request auth_007
```

实际上：

```text
Headed Chromium
```

本身就是主要弹窗。

系统通知用于提醒。

---

# 24. 为什么浏览器必须运行在宿主机

不要：

```text
Dispatcher Docker
    ↓
launch Chromium GUI
```

推荐：

```text
Desktop Auth Helper
    ↓
Local Playwright
    ↓
Local Chromium
```

原因：

```text
Docker GUI 映射复杂
远程服务器没有桌面
MFA/硬件 Key 需要真实用户环境
SSO 可能依赖系统浏览器
代理/VPN/证书可能只在宿主机存在
```

因此：

```text
Dispatcher
```

只负责产生：

```text
AuthRequest
```

而：

```text
Desktop Helper
```

负责：

```text
GUI
```

---

# 25. auth login CLI

增加：

```text
--request
```

例如：

```bash
cairn auth login \
  --config dispatch.yaml \
  --project proj_001 \
  --target target-user \
  --request auth_007
```

开始时：

```text
auth_007
claimed
   ↓
waiting_user
```

---

# 26. 登录流程

```text
Auth Helper
   │
   ▼
launch cairn auth login
   │
   ▼
Chromium headed
   │
   ▼
login_url
   │
   ▼
用户人工操作
   │
   ├── Username
   ├── Password
   ├── SSO
   ├── MFA
   ├── CAPTCHA
   └── Hardware Key
   │
   ▼
进入业务页面
```

用户完成后：

```text
AuthManager
```

保存：

```text
storage_state
```

---

# 27. Auth State 存储

推荐：

```text
/opt/cairn/auth/
└── proj_001/
    └── target-user/
        ├── state.json
        └── meta.json
```

其中：

```text
state.json
```

保存：

```text
Cookies
localStorage
IndexedDB
```

meta：

```json
{
  "project": "proj_001",
  "target": "target-user",
  "role": "user",
  "created_at": "...",
  "verified_at": "..."
}
```

禁止把 raw secrets 写 meta。

---

# 28. Auth Verifier

登录完成后必须重新验证。

不要：

```text
浏览器到了 dashboard
=
认证成功
```

应执行：

```text
Protected Page
+
Authenticated Selector
+
Authenticated API
```

例如：

```text
GET /dashboard → 200

[data-testid=user-menu] 存在

GET /api/me → 200
```

全部通过：

```text
valid
```

---

# 29. AuthRequest 验证状态

开始验证：

```text
waiting_user
   ↓
verifying
```

如果成功：

```text
completed
```

失败：

```text
failed
```

failure_reason 可以保存：

```text
protected endpoint redirected to login
authenticated selector missing
/api/me returned 401
```

不要保存敏感响应内容。

---

# 30. AuthSessionVerified Fact

验证成功后写：

```text
AuthSessionVerified
target=target-user;
role=user;
auth_ref=target-user;
verification=protected_page+selector+api
```

不要写：

```text
password
cookie
session id
JWT
refresh token
authorization
```

---

# 31. 完成 AuthRequest

成功：

```text
POST /auth-requests/auth_007/complete
```

同时：

```text
AuthSessionVerified
```

进入 Graph。

完整：

```text
auth_007
pending
 ↓
claimed
 ↓
waiting_user
 ↓
verifying
 ↓
completed
```

---

# 32. Cairn 如何自动恢复扫描

不需要实现：

```python
resume_scan()
```

只需要：

```text
AuthSessionVerified
```

成为新的 Fact。

Graph 发生变化：

```text
Graph changed
    ↓
Dispatcher 下一轮 Reason
    ↓
Reason 看到 Session 已有效
    ↓
生成 authenticated intents
```

例如：

```text
Authenticated Crawl

API Enumeration

IDOR Testing

Business Logic

RBAC Testing
```

因此恢复机制完全复用 Cairn 本身的：

```text
Graph → Reason → Intent
```

机制。

---

# 33. 不要暂停整个 Project

错误设计：

```text
project.status = waiting_auth
```

因为可能同时存在：

```text
公开 API
JavaScript
静态文件
匿名接口
```

这些不依赖登录。

正确设计：

```text
Project = active
```

只有认证分支：

```text
AuthRequest = pending
```

结构：

```text
                 Project
                   │
       ┌───────────┴───────────┐
       │                       │
Anonymous Work            Auth Branch
       │                       │
Continue                Waiting User
```

---

# 34. 如果认证是全站硬前置

如果目标所有功能都要求登录：

Reason 没有：

```text
normal intent
```

只有：

```text
auth intervention
```

此时项目自然没有可执行任务。

表现就是：

```text
逻辑上等待登录
```

依然不需要新增 Project Status。

---

# 35. Session 过期处理

Explore 发现：

```text
401
302 /login
missing authenticated selector
/api/me unauthorized
```

生成：

```text
AuthSessionInvalid
target=target-user;
role=user;
evidence=protected endpoint redirected to login
```

Reason 下一轮看到：

```text
AuthSessionInvalid
+
当前业务仍要求 user auth
```

产生新的：

```text
Auth Intervention
```

然后自动：

```text
桌面通知
+
浏览器重新弹出
```

因此：

```text
首次登录
Session Expired
重新登录
```

都走同一套机制。

---

# 36. Auth 状态解析原则

不要仅判断是否存在：

```text
AuthSessionVerified
```

而要根据 Graph 时间顺序解析最新状态：

```text
AuthSessionVerified
        ↓
AuthSessionInvalid
```

最终：

```text
invalid
```

如果：

```text
AuthSessionInvalid
        ↓
AuthSessionVerified
```

最终：

```text
valid
```

因此认证状态也是 Temporal Fact。

---

# 37. 多角色认证

支持：

```text
target-user-a
target-user-b
target-admin
```

Graph：

```text
AuthSessionVerified
role=user
auth_ref=user-a

AuthSessionVerified
role=user
auth_ref=user-b

AuthSessionVerified
role=admin
auth_ref=admin
```

可以自动驱动：

```text
user-a vs user-b
→ Horizontal Access Control

user vs admin
→ Vertical Privilege Escalation
```

---

# 38. Desktop Helper 配置

推荐：

```yaml
auth_helper:

  server: "http://localhost:8000"

  helper_name: "rinne-desktop"

  poll_interval: 2

  notification: true

  auto_launch: true

  max_parallel_logins: 1
```

建议：

```text
max_parallel_logins = 1
```

防止多个登录浏览器同时弹出。

---

# 39. Dispatcher Auth 配置

```yaml
auth:

  store_root: "/opt/cairn/auth"

  worker_mount_root: "/run/cairn-auth"

  intervention:

    enabled: true

    request_ttl: 1800

    allow_roles:
      - user
      - admin
```

---

# 40. Docker 环境

推荐：

```text
Host
├── Auth Helper
├── Auth Store
│   └── /opt/cairn/auth
│
└── Docker
    ├── Cairn Server
    ├── Dispatcher
    └── Worker
```

Dispatcher 访问：

```text
/opt/cairn/auth
```

Worker：

```text
/run/cairn-auth
```

只读挂载。

---

# 41. 远程 Cairn 场景

如果：

```text
Cairn Server
```

运行在云服务器：

```text
Cloud
├── Server
├── Dispatcher
└── Workers

       │
       │ HTTPS / VPN
       ▼

Desktop PC
└── cairn auth-helper
```

Auth Helper 轮询远程：

```text
/auth-requests
```

发现：

```text
pending
```

就在本地弹 Chromium。

这比远程容器 GUI 稳定得多。

---

# 42. Secret 边界

严格保持：

```text
Secret Plane
```

和：

```text
Reasoning Plane
```

分离。

Secret Plane：

```text
Password
Cookie
JWT
Storage State
MFA Session
```

只存在：

```text
Auth Store
Browser
Runtime
```

Reasoning Plane：

```text
AuthSessionVerified
AuthSessionInvalid
role
scope
auth_ref
```

进入：

```text
Cairn Graph
```

---

# 43. Worker 认证态使用

Worker 只读取：

```text
CAIRN_AUTH_DIR
```

例如：

```text
/run/cairn-auth
```

然后：

```text
/run/cairn-auth/target-user/state.json
```

不要把：

```text
state.json
```

内容送入 Prompt。

Agent 只知道：

```text
auth_ref=target-user
```

---

# 44. Secret Sanitizer

建议继续实现：

```text
auth/sanitizer.py
```

检测：

```text
Authorization:
Bearer
Cookie:
Set-Cookie:
access_token
refresh_token
session=
```

防止 Agent 输出被直接写入 Fact。

发现后：

```text
拒绝
或
redact
```

---

# 45. 日志规范

推荐日志：

```text
auth request created

auth request claimed

waiting for operator

browser launched

authentication verifying

authentication verified

authentication failed

auth request completed
```

不要记录：

```text
password
cookie
JWT
Authorization Header
StorageState content
```

---

# 46. 故障处理

## Helper 不在线

AuthRequest 保持：

```text
pending
```

其他无需认证任务继续。

---

## 用户关闭浏览器

状态：

```text
failed
```

failure：

```text
operator cancelled authentication
```

Reason 后续可以重新创建请求。

---

## Helper 崩溃

如果：

```text
claimed
```

超过 TTL：

```text
claimed
   ↓
expired
   ↓
pending
```

允许其他 Helper 重新 claim。

---

# 47. Claim TTL

建议：

```text
claim_timeout = 5 min
```

如果：

```text
claimed_at
```

超过阈值，且没有：

```text
waiting_user / verifying
```

允许释放。

如果已经：

```text
waiting_user
```

则可以：

```text
login_timeout = 15 min
```

---

# 48. V0 快速原型

如果不想立刻改数据库，可以先做：

```text
AUTH_REQUIRED marker
```

例如：

```text
AUTH_REQUIRED
target=target-user
role=user
reason=account functionality requires authentication
```

Reason 创建 Intent 前拦截：

```python
if description.startswith(
    "AUTH_REQUIRED"
):
    create_auth_request_file(...)
    continue
```

写：

```text
/opt/cairn/interventions/auth_001.json
```

Desktop Helper 轮询目录。

但这只是：

```text
Prototype
```

不建议作为最终架构。

---

# 49. 正式 V1 开发顺序

推荐：

```text
Phase 1
Reason Prompt 支持 intervention

        ↓

Phase 2
contracts.py 支持 intervention

        ↓

Phase 3
AuthRequest Model + DB

        ↓

Phase 4
AuthRequest API

        ↓

Phase 5
CairnClient

        ↓

Phase 6
reason.py 创建 AuthRequest

        ↓

Phase 7
Dedup + Claim

        ↓

Phase 8
cairn auth-helper

        ↓

Phase 9
Desktop notification

        ↓

Phase 10
auto-launch auth login

        ↓

Phase 11
Playwright Auth Broker

        ↓

Phase 12
Auth Verifier

        ↓

Phase 13
AuthSessionVerified Fact

        ↓

Phase 14
AuthSessionInvalid

        ↓

Phase 15
自动重新提醒
```

---

# 50. V1 最小交付范围

第一版必须完成：

```text
Typed Auth Intervention

AuthRequest DB

AuthRequest API

Reason → AuthRequest

AuthRequest Dedup

Atomic Claim

Desktop Auth Helper

Notification

Auto Launch

cairn auth login

Storage State

Auth Verify

AuthSessionVerified

AuthSessionInvalid

Automatic Resume
```

暂时不用：

```text
WebSocket
Browser Pool
Credential Vault
Automatic MFA
CAPTCHA automation
OAuth auto refresh
复杂 UI
```

---

# 51. V2

第二阶段增加：

```text
SSE / WebSocket push

Auth Request Web UI

Session Health Check

Automatic lightweight revalidation

Multi-role session dashboard

Session TTL

Helper heartbeat

Multiple Desktop Helpers
```

---

# 52. V3

最终可以形成通用：

```text
Human Intervention Framework
```

支持：

```text
authentication
MFA
CAPTCHA
VPN
hardware key
manual approval
test account creation
destructive action approval
```

统一模型：

```text
Reason
   │
   ├── Intent
   │      ↓
   │    Agent
   │
   └── Intervention
          ↓
        Human

Agent / Human
      ↓
     Fact
```

---

# 53. 最终完整流程

```text
Agent 扫描
   │
   ▼
发现订单页面要求登录
   │
   ▼
Reason
   │
   ▼
Auth Intervention
   │
   ▼
auth_007 pending
   │
   ▼
Desktop Helper
   │
   ▼
claim
   │
   ▼
Windows Notification
   │
   ▼
自动启动 cairn auth login
   │
   ▼
Chromium
   │
   ▼
用户登录
   │
   ├── Password
   ├── SSO
   ├── MFA
   └── CAPTCHA
   │
   ▼
Storage State
   │
   ▼
Auth Verifier
   │
   ▼
AuthSessionVerified
   │
   ▼
auth_007 completed
   │
   ▼
Graph changed
   │
   ▼
Reason
   │
   ▼
Authenticated Crawl
   │
   ├── API
   ├── IDOR
   ├── RBAC
   └── Business Logic
```

---

# 54. 最终架构结论

建议最终不要实现：

```text
“监听 Agent 文本，发现登录两个字就弹窗”
```

而应该实现：

```text
Reason
  ↓
Typed Human Intervention
  ↓
Auth Request Queue
  ↓
Desktop Helper
  ↓
Auth Broker
  ↓
Auth Verification
  ↓
Verified Fact
  ↓
Reason Resume
```

这样不仅解决：

```text
扫描到需要登录的业务自动提醒
自动弹窗
自动登录态采集
自动继续扫描
```

还为 Cairn 后续的人机协作能力提供了一个统一的基础设施。

最终可以把 Cairn 的核心状态搜索模型扩展为：

```text
               Automated Branch
              ┌─────────────────┐
              │ Intent → Agent  │
              └────────┬────────┘
                       │
Fact → Reason ─────────┼────────→ Fact
                       │
              ┌────────┴─────────────┐
              │ Intervention → Human │
              └──────────────────────┘
```

这会比单独做一个“登录弹窗功能”更适合作为 Cairn 的长期架构。