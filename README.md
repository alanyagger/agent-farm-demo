# 智耕凭证农场

一个用于演示“智能体身份可归属、无有效凭证不能执行、行为全程可追溯”的单机 Web Demo。

项目预置三组主人、智能体和农场。青芽与禾光持有有效的 Mock 智能体身份凭证，田小诺初始尚未申领。当前版本完全使用本地凭证门禁，不请求中移互联网接口。除了规则和 LangGraph 模式，也可以直接在 OpenClaw 中用自然语言指挥三个智能体种植、收获和社交采摘。

## 项目结构

```text
app/                    React + TypeScript 界面
backend/app/            FastAPI、SQLAlchemy、规则引擎与凭证适配器
backend/openclaw_mcp/   OpenClaw stdio MCP Server
backend/tests/          后端集成测试
data/                   本地 SQLite 数据
docs/                   数字凭证与智能体凭证学习文档
public/                 原创农场视觉素材
scripts/start-demo.ps1  Windows 一键启动脚本
scripts/stop-demo.ps1   Windows 安全停止脚本
scripts/setup-openclaw.ps1  OpenClaw Skill 与 MCP 安装脚本
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

## 使用 OpenClaw 玩农场

首次安装依赖并接入当前 OpenClaw：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\scripts\setup-openclaw.ps1
```

安装脚本固定使用 `D:\OpenClawData`，会先备份 `openclaw.json`，然后安装 `agent-farm` Skill、注册同名 stdio MCP Server，并生成仅用于本机 FastAPI 的随机 Bearer Token。脚本不会修改 OpenClaw 的模型 Provider、Gateway 端口或其他 Skills。

启动农场后，可以直接下达指令：

```powershell
.\scripts\start-demo.ps1
$env:OPENCLAW_STATE_DIR = "D:\OpenClawData"
openclaw agent --agent main --message "让青芽查看农场，收获成熟作物，再种两块番茄，并从邻居摘一份成熟作物"
```

也可以运行 `openclaw tui` 后输入自然语言。未指定智能体时默认使用青芽；可以明确指定田小诺或禾光，也可以在一条指令中依次安排多个智能体。

OpenClaw 直接决定调用 `inspect_my_farm`、`inspect_neighbors`、`read_recent_actions`、`harvest_crop`、`plant_crop` 和 `steal_crop`，不会再次调用项目内部 DeepSeek Agent。MCP 进程不访问 SQLite，只通过本机 FastAPI 执行操作。

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
| 陈屿 | 田小诺 | MISSING | 手动完成 Mock 凭证申领后加入协作 |

右上角重置按钮会恢复以上初始数据。

## 核心规则

1. 每次行动前先通过统一 `CredentialProvider` 校验本地 Mock 凭证。
2. 规则顺序为：收获一块成熟作物、补种最多两块空地、采摘一块邻居成熟作物。
3. 同一智能体对邻居采摘有 18 秒冷却时间，且至少为主人保留一份产量。
4. `PENDING`、`REVOKED`、`EXPIRED`、`REJECTED` 和无凭证状态均生成 `BLOCKED` 审计记录。
5. LangGraph 与 OpenClaw 每轮最多收获 1 次、种植 2 次、社交采摘 1 次；每个写 Skill 都会再次核验凭证。

## 当前 Mock 凭证模式

当前 `.env` 应保持：

```dotenv
CREDENTIAL_PROVIDER=mock
CMCC_ADMISSION_MODE=off
CMCC_ADMISSION_AGENT_MAPPINGS=
```

`off` 模式下，Dashboard、凭证申领、规则调度、LangGraph 和 OpenClaw MCP Tools 都只访问本地数据库，不会向中移接口发送请求。`.env` 中已有的中移参数可以保留，但不会参与运行。

## 凭证适配器

默认使用 `MockCredentialProvider`，完整模拟：

```text
主人实名 DID 核验 -> 创建智能体并获得 AIC -> 提交 VC 申领 -> 签发并生效
```

`CmccCredentialProvider` 及只读准入适配器仍保留以下接口骨架，供后续需求恢复时使用：

- `/api/cmvc-tocp-server/open-api/vc/agent/create`
- `/api/cmvc-tocp-server/open-api/vc/agent/issue`
- `/api/cmvc-tocp-server/open-api/vc/listByTemplateId`

这些适配器当前不会由 Web Demo、调度器或 OpenClaw 调用。重新启用前需要单独评审配置与测试环境，不应只修改页面文案或 OpenClaw Skill。

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
