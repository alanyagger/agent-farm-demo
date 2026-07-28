# 智耕凭证农场

一个用于演示“智能体身份可归属、无有效凭证不能执行、行为全程可追溯”的单机 Web Demo。

项目预置三组主人、智能体和农场。两个智能体持有有效的中移互联网智能体身份模拟凭证，第三个尚未申领。默认使用可离线演示的确定性规则；启用 LLM 模式后，智能体会通过 DeepSeek 和受限农场 Skills 决策种植、收获和社交采摘。每次行动都会先进行凭证准入校验并生成审计记录。

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
| 陈屿 | 田小诺 | MISSING | 演示准入拒绝和完整申领链路 |

右上角重置按钮会恢复以上初始数据。

## 核心规则

1. 每次行动前调用统一 `CredentialProvider` 校验智能体凭证。
2. 只有 `ACTIVE` 状态可以进入游戏规则引擎。
3. 规则顺序为：收获一块成熟作物、补种最多两块空地、采摘一块邻居成熟作物。
4. 同一智能体对邻居采摘有 18 秒冷却时间，且至少为主人保留一份产量。
5. `PENDING`、`REVOKED`、`EXPIRED`、`REJECTED` 和无凭证状态均生成 `BLOCKED` 审计记录。
6. LLM 模式中，每轮最多收获 1 次、种植 2 次、社交采摘 1 次；每个写 Skill 会再次核验凭证。

## 凭证适配器

默认使用 `MockCredentialProvider`，完整模拟：

```text
主人实名 DID 核验 -> 创建智能体并获得 AIC -> 提交 VC 申领 -> 签发并生效
```

`CmccCredentialProvider` 已提供以下接口骨架：

- `/api/cmvc-tocp-server/open-api/vc/agent/create`
- `/api/cmvc-tocp-server/open-api/vc/agent/issue`
- `/api/cmvc-tocp-server/open-api/vc/listByTemplateId`

通过 `.env` 将 `CREDENTIAL_PROVIDER` 设为 `cmcc` 并补齐中移测试参数即可启用。真实手机号仅从运行环境读取，不写入数据库和前端。

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
