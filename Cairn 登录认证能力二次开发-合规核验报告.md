# Cairn 登录认证能力二次开发 — 合规性核验报告

依据《Cairn 实际环境登录认证能力源码开发方案》45 节，逐条核验实现与文档的一致性。

> 核心原则核验：**Secret Plane 与 Reasoning Plane 分离** —— 凭据只存于 auth store（`state.json`），
> 图中只产生 `AuthSessionVerified` / `AuthSessionInvalid` Fact；密码/Cookie/JWT/Token/MFA Secret
> 一律不写入 Fact、Intent、Hint 或 Agent Prompt。

## 一、逐节合规核验表

| 文档节 | 要求 | 实现位置 | 状态 |
|---|---|---|---|
| §1 开发目标 | 不破坏 Blackboard/Fact-Intent 架构，人工辅助登录 | `auth/` 包独立新增，不动 server 协议 | ✅ |
| §2 架构归属 | 认证放入 Dispatcher Runtime Layer，非 LLM Driver | `dispatcher/runtime/*` + `dispatcher/tasks/common.py` | ✅ |
| §3 源码目录 | `cairn/src/cairn/auth/` 六模块 + tests 五文件 | 全部就位 | ✅ |
| §4 配置模型 | AuthVerifyConfig/AuthTargetConfig/AuthConfig + `DispatchConfig.auth` | `dispatcher/config.py` | ✅ |
| §5 dispatch.yaml | auth 段示例（target-user/target-admin） | `dispatch.example.yaml`（注释版） | ✅ |
| §6 存储设计 | `<store_root>/<project>/<ref>/{state.json,meta.json}`，meta 无凭据 | `auth/store.py` | ✅ |
| §7 AuthStore | 项目/目录/路径计算 + 路径穿越净化 | `AuthStore` + `_sanitize_component` | ✅ |
| §8 AuthManager | 打开浏览器→人工登录→验证→storage_state | `auth/manager.py` `capture_interactive` | ✅ |
| §9 AuthVerifier | 独立于 Capture，三层验证 page/selector/api | `auth/verifier.py` | ✅ |
| §10 不能只判 200 | HTTP+selector+api 组成证据 | `AuthVerifier._verify_context` | ✅ |
| §11 AuthGraph | 外部结果转 Intent→Fact，不直接写 SQLite | `auth/graph.py` | ✅ |
| §12 CairnClient | `create_intent` 增加 `worker` 参数 | `protocol/client.py` | ✅ |
| §13/§14 Fact 规范 | `AuthSessionVerified`/`AuthSessionInvalid` 固定格式，无凭据字段 | `graph.py` `_verified/_invalid_description` | ✅ |
| §15/§16/§17 CLI | `cairn auth login/verify`（+list/remove） | `cli.py` | ✅ |
| §18 容器挂载 | 只读 bind mount `ro` → `/run/cairn-auth` | `containers.py` `_auth_volumes` | ✅ |
| §19 Docker Socket | source 用 Docker Host 路径 | `containers.py` 直接拼接 `store_root` | ✅ |
| §20 compose | dispatcher 加 `/opt/cairn/auth:/opt/cairn/auth` | `docker-compose.yaml` | ✅ |
| §21/§22/§23 project_env | 统一注入 `CAIRN_PROJECT_ID`/`CAIRN_AUTH_DIR` | `backend.py` 协议 + 两 backend 实现 | ✅ |
| §24 环境注入 | `common.py` `run_worker_process` 合并 `project_env` | `tasks/common.py` | ✅ |
| §25 定位 session | `$CAIRN_AUTH_DIR/<auth_ref>/state.json` | explore.md 规则 + 环境变量 | ✅ |
| §26/§27 Playwright 依赖 | Worker Dockerfile + `pyproject.toml` 加 `playwright>=1.51,<2` | 两处均已加 | ✅ |
| §28 reason 门禁 | 无 AuthSessionVerified 不提 authenticated intent | `reason.md` 认证规则 | ✅ |
| §29 explore 规则 | 匹配 Fact→auth_ref→读 CAIRN_AUTH_DIR，禁 cat state.json | `explore.md` 认证规则 | ✅ |
| §30 Sanitizer | 检测 Authorization/Cookie/session/token 等，redact | `auth/sanitizer.py` | ✅ |
| §31~§34 生命周期 | 不自动 refresh，过期→AuthSessionInvalid→人工重登 | `cairn auth verify` 走 invalid 分支 | ✅ |
| §35 .gitignore | 追加 `.auth/ auth/ *.storage-state.json *.storage.json` | `.gitignore` | ✅ |
| §36 权限 | state 600 / dispatcher rw / worker ro / server 不挂载 | `store.py` chmod 600 + `ro` mount | ✅ |
| §37 容器生命周期 | 旧容器需重建（文档性要求） | 未改动，保留现有行为 | ✅ |
| §38~§42 后续阶段 | V2/V3 明确不做 | 未实现（符合 V1 范围） | ✅ |
| §39 测试方案 | Config/Store/Graph/Container/LocalBackend/Sanitizer 测试 | `tests/test_auth_*.py` 五文件 | ✅ |
| §40 V1 MVP 范围 | 仅实现 MVP 项，不做自动 CAPTCHA/MFA/OAuth 等 | 严格遵循 | ✅ |

## 二、测试结果

- 新增 auth 测试：**28 passed**（config 7 / store 6 / graph 3 / runtime 6 / sanitizer 6）。
- 核心非环境依赖回归测试：**71 passed**。
- 全量测试：121 passed + 5 failed（`test_local_execution.py` 因 Windows 缺 `sh`/`claude` 二进制，属预存环境问题，与本次改动无关）。

## 三、关键设计确认

1. **凭据零泄漏**：`test_auth_graph.py::test_verified_fact_never_contains_secret_markers` 断言 Fact 描述不含 `cookie=`/`token=`/`password=`/`Authorization=`/`Bearer `。
2. **路径穿越防护**：`test_auth_store.py::test_store_rejects_path_traversal` 覆盖 `../`、绝对路径、反斜杠、空串。
3. **只读挂载**：`test_auth_runtime.py::test_container_auth_volumes_are_read_only` 断言 `mode == "ro"`。
4. **双后端环境一致性**：Container 返回 `/run/cairn-auth`，Local 返回 `<store_root>/<project_id>`，Agent 只读 `$CAIRN_AUTH_DIR`。
