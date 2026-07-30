# 智耕凭证农场

一个用于演示“智能体身份可归属、无有效凭证不能执行、行为全程可追溯”的单机 Web Demo。

项目预置三组主人、智能体和农场。两个智能体持有有效的中移互联网智能体身份模拟凭证，第三个尚未申领。青芽额外映射到中移外部身份 `agent1`，每轮行动前通过只读接口验证联调准入。默认使用可离线演示的确定性规则；启用 LLM 模式后，智能体会通过 DeepSeek 和受限农场 Skills 决策种植、收获和社交采摘。

## 项目结构

```text
app/                    React + TypeScript 界面
backend/app/            FastAPI、SQLAlchemy、规则引擎与凭证适配器
backend/tests/          后端集成测试
data/                   本地 SQLite 数据
docs/                   数字凭证与智能体凭证学习文档
public/                 原创农场视觉素材
scripts/start-demo.ps1  Windows 一键启动脚本
scripts/stop-demo.ps1   Windows 安全停止脚本
```

## 本地启动

首次运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm install
```

启动前后端：

```powershell
.\scripts\start-demo.ps1
```

访问：

- 农场 Demo：`http://localhost:3000`
- FastAPI 文档：`http://127.0.0.1:8010/docs`
- 健康检查：`http://127.0.0.1:8010/health`

## 启用真实 DeepSeek Agent

默认 `AGENT_RUNTIME_MODE=rules`，因此没有 API Key 时也能稳定演示凭证准入和农场流程。要让“运行一次”和自动调度实际调用 DeepSeek：

```powershell
Copy-Item .env.example .env
```

在 `.env` 中填写：

```dotenv
AGENT_RUNTIME_MODE=llm
MODEL_PROVIDER=deepseek
DEEPSEEK_API_KEY=你的服务端DeepSeek密钥
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

`deepseek-v4-flash` 适合高频、边界明确的农场工具调用；可把 `DEEPSEEK_MODEL` 改为 `deepseek-v4-pro`。密钥只能保存在后端 `.env`，不要填写 `NEXT_PUBLIC_*` 变量，也不要提交 `.env`。

LLM 模式使用 LangGraph 编排：

```text
凭证门禁 -> 脱敏农场状态 -> DeepSeek 工具调用 -> 受限 Skill 执行 -> 行为审计
```

模型只可调用 `inspect_my_farm`、`inspect_neighbors`、`read_recent_actions`、`harvest_crop`、`plant_crop`、`steal_crop`。种植、收获和采摘均会在服务端再次核验凭证、归属、库存、冷却和单轮额度；模型不能访问网络、文件、Shell 或数据库。

DeepSeek 超时、鉴权失败、工具参数错误或超过最大调用轮次时，系统会生成 `AGENT_ERROR` 审计并停止当前轮次，不会静默改用规则引擎。界面的“行为记录”会标记每条行为来自“模型 Skill”还是“规则引擎”。

## 停止项目

在项目根目录运行：

```powershell
.\scripts\stop-demo.ps1
```

停止脚本会读取 `.logs/demo-processes.json`，校验进程启动时间、可执行文件和项目命令，然后停止前后端及其子进程。进程清单缺失或失效时，脚本会检查 `3000`、`8010` 端口，但只停止能够确认属于当前项目的 Vinext 和 Uvicorn 进程。

停止操作会保留 SQLite 数据和日志。关闭浏览器页面不会停止前后端服务；重复执行停止脚本是安全的，项目已经停止时会直接返回成功提示。

手动检查端口：

```powershell
netstat -ano | Select-String ':3000|:8010'
```

如果目标端口被无法确认身份的其他程序占用，停止脚本只会输出警告，不会结束该进程。此时可根据输出的 PID 检查具体程序：

```powershell
Get-CimInstance Win32_Process |
    Where-Object ProcessId -eq <PID> |
    Select-Object ProcessId, ParentProcessId, ExecutablePath, CommandLine
```

## 演示账号

| 主人 | 智能体 | 初始凭证状态 | 用途 |
| --- | --- | --- | --- |
| 林晓 | 青芽 | ACTIVE | 自动种植、收获、邻居采摘 |
| 周晴 | 禾光 | ACTIVE | 自动种植、收获、邻居采摘 |
| 陈屿 | 田小诺 | MISSING | 手动申领后通过中移接口加入协作 |

右上角重置按钮会恢复以上初始数据。

## 核心规则

1. 每次行动前先通过统一 `CredentialProvider` 校验本地凭证。
2. 三个智能体均映射到中移身份，行动前必须通过只读中移准入校验。
3. 规则顺序为：收获一块成熟作物、补种最多两块空地、采摘一块邻居成熟作物。
4. 同一智能体对邻居采摘有 18 秒冷却时间，且至少为主人保留一份产量。
5. `PENDING`、`REVOKED`、`EXPIRED`、`REJECTED` 和无凭证状态均生成 `BLOCKED` 审计记录。
6. LLM 模式中，每轮最多收获 1 次、种植 2 次、社交采摘 1 次；每个写 Skill 会再次核验凭证。

## 中移联调演示准入

当前演示不依赖现场创建或签发测试凭证。保持 `CREDENTIAL_PROVIDER=mock`，配置以下只读准入参数：

```dotenv
CMCC_BASE_URL=https://vctest.cmccsign.com
CMCC_APP_ID=测试环境appId
CMCC_APP_KEY=测试环境appKey
CMCC_CLIENT_SECRET=测试环境clientSecret
CMCC_AGENT_TEMPLATE_ID=341kv2bl96zpb925h1qqk2q687zz2zpv
CMCC_DEMO_PHONE=测试手机号

CMCC_ADMISSION_MODE=demo
CMCC_ADMISSION_AGENT_MAPPINGS=agent-sprout:agent1,agent-nova:agent2,agent-orbit:agent3
CMCC_ADMISSION_TIMEOUT_SECONDS=3
CMCC_ADMISSION_CACHE_SECONDS=10
```

`demo` 模式会真实调用 `/api/cmvc-tocp-server/open-api/vc/listByTemplateId`：

- 青芽使用外部身份 `agent1`，田小诺使用外部身份 `agent2`，禾光使用外部身份 `agent3`，三者分别查询和缓存。
- 田小诺初始未申领；页面手动申领后会立即执行中移准入校验，通过后自动加入农场协作。
- 青芽和禾光在启动及重置后默认暂停，可手动运行一次或开启自动运行。
- 查询到对应智能体且凭证状态为 `2`：以 `REAL_ACTIVE` 真实准入。
- 接口成功但没有对应智能体记录：以 `DEMO_CONNECTED` 联调演示准入。
- HTTP、网络、签名或业务错误：以 `DENIED` 拒绝进入农场。

可将 `CMCC_ADMISSION_MODE` 改为 `strict`，此时必须查询到真实有效记录；设为 `off` 则恢复完全本地门禁。GET `/api/agents/{agentId}/admission` 只读取内存缓存，不访问中移；POST `/api/agents/{agentId}/admission/verify` 会强制重新查询。普通 Dashboard 加载和主人切换不会等待外部接口。

## 凭证适配器

默认使用 `MockCredentialProvider`，完整模拟：

```text
主人实名 DID 核验 -> 创建智能体并获得 AIC -> 提交 VC 申领 -> 签发并生效
```

`CmccCredentialProvider` 已提供以下接口骨架：

- `/api/cmvc-tocp-server/open-api/vc/agent/create`
- `/api/cmvc-tocp-server/open-api/vc/agent/issue`
- `/api/cmvc-tocp-server/open-api/vc/listByTemplateId`

`CmccCredentialProvider` 用于后续完整创建、签发联调，不是当前演示准入的必需条件。真实手机号仅从运行环境读取，不写入数据库和前端。

在启用真实 Provider 前，先执行只读查询诊断。该命令只查询测试环境中已有的智能体凭证，不创建、签发或修改数据库，输出中的手机号和凭证记录 ID 均会脱敏：

```powershell
.\.venv\Scripts\python.exe -m backend.tools.cmcc_credential_probe `
  --base-url https://vctest.cmccsign.com `
  --template-id 341kv2bl96zpb925h1qqk2q687zz2zpv `
  --agent-name agent1
```

工具默认拒绝生产域名，避免误将联调请求发送到生产环境。

需要继续完整联调时，才使用一次性创建与签发工具。该工具只允许测试域名，并在 `.logs` 保存不含手机号和密钥的阶段回执，防止网络中断后误创建重复凭证：

```powershell
.\.venv\Scripts\python.exe -m backend.tools.cmcc_agent_issue `
  --base-url https://vctest.cmccsign.com `
  --template-id 341kv2bl96zpb925h1qqk2q687zz2zpv `
  --agent-name agent1 `
  --claw-id agent1 `
  --confirm-create-and-issue
```

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q
npm run typecheck
npm run build
```

详细流程见：

- [数字凭证操作流程](docs/01-数字凭证操作流程.md)
- [智能体身份凭证流程](docs/02-智能体身份凭证流程.md)
- [接口适配与风险清单](docs/03-接口适配与风险清单.md)
- [五分钟演示讲稿](docs/04-五分钟演示讲稿.md)
- [Skill 驱动真实 Agent 说明](docs/05-Skill驱动真实Agent说明.md)
