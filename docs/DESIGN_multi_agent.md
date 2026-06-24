# 多 Agent CAx 平台 — 设计方案

> 分支：`feature/multi-agent-cax`（基于已合入 FreeCAD 能力的 `main`）
> 状态：设计阶段，待评审后实施
> 决策基线：① 已合并 FreeCAD 分支 ② 轻量级用户管理（用户名标识） ③ SQLite 持久化 ④ 先出设计方案

---

## 1. 目标与范围

把现有"单一 CAD Agent"升级为**多 Agent 协作平台**，覆盖 `CAD → MESH → CAE 仿真` 全流程：

- **编排 Agent（Orchestrator）**：理解用户自然语言，把任务分解为工作流（DAG），按序调度各专业 Agent，并在阶段间传递产物（如 STEP → 网格 → 计算结果）。
- **CAD Agent**：沿用现有 `cad_agent` 全部能力（FreeCAD 主引擎 + trimesh 兜底 + STEP 导出）。
- **Mesh Agent**（本期占位）：未来调用网格软件（如 Gmsh）。
- **CAE Agent**（本期占位）：未来调用求解器（如 CalculiX / Elmer）。

附加能力：
- **用户管理**：轻量级，用户名即身份，无密码；数据按用户隔离。
- **历史记录管理**：项目 / 工作流 / 产物 / 脚本均持久化到 SQLite。
- **前端三 Tab**：① 编排工作流可视化 ② 3D 图形区（沿用现状） ③ 脚本日志（LLM 写给 CAx 软件的脚本）。

---

## 2. 总体架构

```
┌─────────────────────────────────────────────────────────────┐
│                         前端 (SPA)                            │
│  Header: 用户选择 · LLM设置 · 项目历史                        │
│  ┌───────────────┬──────────────┬──────────────┐            │
│  │ Tab1 编排工作流│ Tab2 3D视图  │ Tab3 脚本日志│  + 聊天面板 │
│  └───────────────┴──────────────┴──────────────┘  + 推理面板 │
└───────────────────────────┬─────────────────────────────────┘
                            │  WebSocket (事件流) + REST
┌───────────────────────────┴─────────────────────────────────┐
│                      FastAPI 后端                             │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  WorkflowService（编排执行 + 事件流 + 持久化）          │ │
│  │     │                                                    │ │
│  │     ▼                                                    │ │
│  │  OrchestratorAgent  ──plan──▶  [工作流 DAG]              │ │
│  │     │ execute                                            │ │
│  │     ├──▶ CADAgent  (FreeCAD/trimesh)  ──▶ STEP/STL      │ │
│  │     ├──▶ MeshAgent (占位)             ──▶ mesh          │ │
│  │     └──▶ CAEAgent  (占位)             ──▶ result        │ │
│  └────────────────────────────────────────────────────────┘ │
│  ┌──────────────┐  ┌──────────────────────────────────────┐ │
│  │ SQLite (db/) │  │ 文件存储 output/{user}/{project}/...  │ │
│  └──────────────┘  └──────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

### 关于 AgentScope 2.0.2 的限制（重要）

经核实，**AgentScope 2.0.2 没有内置 `pipeline` / `msghub` 等多 Agent 编排原语**，`Agent` 仅提供 `reply` / `reply_stream`。因此编排能力需要我们在**代码层自行实现**。这反而契合"工作流可视化"需求——我们可以显式产出计划再逐节点执行，而不是把编排藏在 LLM 的隐式循环里。

采用 **Plan-then-Execute（先规划后执行）** 模式：

1. **Plan 阶段**：OrchestratorAgent 调用 LLM，把用户请求分解为结构化工作流（JSON：节点列表 + 依赖 + 目标 Agent + 子指令）。→ 前端 Tab1 立即渲染整张工作流图。
2. **Execute 阶段**：WorkflowService 按拓扑顺序逐节点执行，把节点指令交给对应专业 Agent 的 `reply_stream`，实时回传子事件并更新节点状态。支持**逐节点中断 / 重置**（默认自动顺序执行）。
3. **Aggregate 阶段**：汇总结果、落库、通知前端。失败节点可重规划或重试。

### 关于 FreeCAD 运行方式（已从子进程迁移到进程内）

最初通过 `freecad.cmd` 子进程调用 FreeCAD（snap 的 Python 3.12 与 venv 的 3.14 不兼容，无法直接 import）。**现已迁移到 conda-forge 的 FreeCAD**：后端跑在 conda 环境 `cax`（Python 3.11 + freecad），`freecad_bridge` 直接 `import FreeCAD` **进程内调用**，每个项目维护一个常驻内存的 `FreeCAD.Document`（变更后写回 .FCStd 持久化）。

- 收益：去掉每次操作的进程启动与重复磁盘读写开销。
- 并发：FreeCAD 非线程安全，所有调用经单一专用线程 + `asyncio.Lock` 串行化。
- 启动：`./start.sh`（使用 `~/miniforge3/envs/cax`）；FreeCAD 不是 pip 依赖，见 `requirements.txt` 顶部说明。

---

## 3. 后端目录重构

现有 `backend/agent/`（单 Agent）扩展为 `backend/agents/`（多 Agent）+ 新增 `db/`、`services/`：

```
backend/
  main.py                      # FastAPI 路由 + WebSocket（瘦身，逻辑下沉到 services）
  db/
    __init__.py
    database.py                # SQLite 连接、初始化、schema 建表
    repository.py              # CRUD：users / projects / messages / runs / nodes / artifacts / scripts
  agents/
    __init__.py
    base.py                    # SpecialistAgent 基类（统一封装 build/reply_stream/能力声明）
    registry.py                # Agent 注册表：name -> 工厂函数 + 能力描述
    orchestrator.py            # OrchestratorAgent：规划器（输出 DAG）
    llm_factory.py             # build_model()（从 cad_agent.py 抽出，多 Agent 共用）
    cad/
      __init__.py
      agent.py                 # 由现 cad_agent.py 迁移，去掉 build_model
      tools/
        cad_engine.py          # 现有，原样迁移
        cad_tools.py           # 现有，原样迁移
        freecad_bridge.py      # 现有，+ 脚本捕获钩子（供 Tab3）
    mesh/
      __init__.py
      agent.py                 # 占位：注册但返回 "not implemented"
    cae/
      __init__.py
      agent.py                 # 占位：注册但返回 "not implemented"
  services/
    __init__.py
    workflow_service.py        # 编排执行 + 事件序列化 + 持久化
    session_service.py         # 项目/会话生命周期、场景注册表
  config/
    llm_config.yaml            # 现有
```

> 迁移策略：CAD 相关代码**原样平移**到 `agents/cad/`，仅把 `build_model` 抽到 `agents/llm_factory.py`，保证现有 CAD 能力零回归。

---

## 4. Agent 抽象

### 4.1 SpecialistAgent 基类（`agents/base.py`）

统一每个专业 Agent 的接口，供编排器调用：

```python
class SpecialistAgent:
    name: str                 # "cad" / "mesh" / "cae"
    display_name: str         # "CAD 设计" / "网格剖分" / "CAE 仿真"
    capabilities: str         # 给编排器看的能力描述（用于规划时选 Agent）
    input_kinds: list[str]    # 接受的产物类型，如 ["text"], ["step"]
    output_kinds: list[str]   # 产出的产物类型，如 ["step","stl"], ["mesh"]

    def __init__(self, llm_config, workspace: Path): ...
    async def run(self, instruction: str, context: TaskContext): 
        # 异步生成器，yield AgentScope 事件 + 自定义事件（脚本、产物）
        ...
```

`TaskContext` 携带上游产物路径、项目工作区、场景对象等，实现阶段间数据传递。

### 4.2 Agent 注册表（`agents/registry.py`）

```python
AGENT_REGISTRY = {
    "cad":  CADSpecialist,
    "mesh": MeshSpecialist,   # 占位
    "cae":  CAESpecialist,    # 占位
}
```

编排器规划时读取每个 Agent 的 `capabilities` 描述，决定调用哪些、按什么顺序。新增 Agent 只需实现基类并注册一行。

### 4.3 OrchestratorAgent（`agents/orchestrator.py`）

- 系统提示词：说明可用的专业 Agent 及其能力，要求输出**严格 JSON 工作流计划**。
- `plan(user_request, scene_state) -> Workflow`：返回节点列表。每节点：`{id, agent, title, instruction, depends_on:[...]}`。
- 规划失败 / 单纯设计类请求时，可退化为单节点（直接走 CAD），保证简单请求不被过度编排。

**工作流数据结构：**

```python
@dataclass
class WorkflowNode:
    id: str
    agent: str            # "cad" / "mesh" / "cae"
    title: str            # "创建底座" 
    instruction: str      # 交给该 Agent 的子指令
    depends_on: list[str]
    status: str = "pending"   # pending|running|success|failed|skipped

@dataclass
class Workflow:
    run_id: str
    user_request: str
    nodes: list[WorkflowNode]
```

---

## 5. 数据模型（SQLite）

`db/database.py` 初始化以下表（轻量用户管理，无密码）：

```sql
users         (id INTEGER PK, username TEXT UNIQUE, created_at TEXT)
projects      (id INTEGER PK, user_id FK, name TEXT, created_at, updated_at)
messages      (id INTEGER PK, project_id FK, role TEXT, content TEXT, created_at)
workflow_runs (id INTEGER PK, project_id FK, user_request TEXT, status TEXT, created_at)
workflow_nodes(id INTEGER PK, run_id FK, agent TEXT, title TEXT, instruction TEXT,
               status TEXT, sequence INT, summary TEXT, started_at, finished_at)
artifacts     (id INTEGER PK, project_id FK, run_id FK, node_id FK,
               kind TEXT,        -- stl|step|obj|mesh|result
               filename TEXT, path TEXT, created_at)
scripts       (id INTEGER PK, project_id FK, run_id FK, node_id FK,
               agent TEXT, software TEXT,   -- freecad|gmsh|calculix
               language TEXT,               -- python|geo|inp
               filename TEXT, content TEXT, created_at)
```

- **项目（project）** = 现在的"会话"，归属某用户，聚合其消息、工作流、产物、脚本。
- 文件物理存储：`backend/output/{user_id}/{project_id}/...`（替代现 `output/{session_id}/`）。
- `repository.py` 提供薄 CRUD 封装；用 Python 内置 `sqlite3`，无需额外依赖。并发以单写连接 + WAL 模式处理。

---

## 6. 用户管理（轻量级）

- 无注册/密码。前端首次进入要求输入用户名，存 `localStorage`；后续请求带 `user` 标识。
- 后端 `get_or_create_user(username)`，所有数据按 `user_id` 隔离。
- REST：
  - `POST /api/users/login` `{username}` → `{user_id}`（实为 get-or-create）
  - `GET  /api/users/{user_id}/projects` → 项目列表
  - `POST /api/users/{user_id}/projects` → 新建项目
  - `GET  /api/projects/{pid}/messages` → 历史消息
  - `GET  /api/projects/{pid}/runs` → 工作流历史
  - `GET  /api/projects/{pid}/scripts` → 脚本列表

---

## 7. WebSocket 事件协议（扩展）

连接：`/ws/chat/{project_id}`（project_id 取代旧 session_id）。

**新增工作流/脚本事件**（在现有 `agent_start/text_delta/tool_*/model_ready/error` 之上）：

| 事件 | 载荷 | 用途 |
|------|------|------|
| `workflow_plan` | `{run_id, nodes:[{id,agent,title,instruction,depends_on,status}]}` | Tab1 渲染整张工作流 |
| `workflow_node_start` | `{run_id, node_id, agent}` | 节点变"运行中" |
| `workflow_node_done` | `{run_id, node_id, status, summary, artifacts:[...]}` | 节点变"完成/失败" |
| `workflow_done` | `{run_id, status}` | 整体收尾 |
| `script_generated` | `{node_id, agent, software, language, filename, content}` | Tab3 追加脚本 |
| `model_ready` | （现状）`{filename, url}` | Tab2 加载模型 |

**关键改造**：现有 `text_delta` / `tool_*` / `thinking_*` 事件统一**附加 `node_id` + `agent` 字段**，让推理面板能标注"这是哪个 Agent、哪个工作流节点产生的"。

---

## 8. 前端三 Tab 设计

把现 `#viewer-panel`（单一图形区）改为**带 Tab 切换的容器**，聊天面板与推理面板保持不变。

```
┌─ 图形工作区 ──────────────────────────────────────┐
│ [ 编排工作流 ] [ 3D 视图 ] [ 脚本日志 ]   ← Tab 栏 │
│ ┌───────────────────────────────────────────────┐ │
│ │  当前 Tab 内容                                  │ │
│ └───────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────┘
```

### Tab1 — 编排工作流（`workflow.js`，新）

- 收到 `workflow_plan` 渲染**纵向流程图 / 步骤器**：每节点一张卡片
  - Agent 徽章（颜色区分 CAD/MESH/CAE）+ 标题 + 状态图标（⏳待执行 / 🔄运行中 / ✅完成 / ❌失败）
  - 节点间连接线表示依赖
  - 可展开查看该节点的子指令与执行摘要
- `workflow_node_start/done` 实时更新卡片状态与摘要
- 第一版用纵向 stepper（实现简单、够直观）；后续可升级为可拖拽 DAG 图

### Tab2 — 3D 视图（沿用 `viewer3d.js`）

- 把现有 `#viewer-canvas-wrap` + 工具栏（STL/STEP 下载、历史）整体移入此 Tab。
- 功能与当前完全一致，`model_ready` 时若当前不在该 Tab，可在 Tab 标题加红点提示"有新模型"。

### Tab3 — 脚本日志（`scripts.js`，新）

- 收到 `script_generated` 追加一条：软件标签（FreeCAD/Gmsh/CalculiX）+ 文件名 + 语言
- 等宽字体展示脚本内容（可选轻量语法高亮），提供「复制」「下载」按钮
- 进入页面时通过 `GET /api/projects/{pid}/scripts` 拉取历史脚本

### 新增 / 改动的前端文件

```
frontend/js/
  tabs.js        # 新：Tab 切换控制
  workflow.js    # 新：工作流渲染
  scripts.js     # 新：脚本日志渲染
  user.js        # 新：用户登录/项目历史
  main.js        # 改：接入 tabs，分发新事件，project_id 化
  viewer3d.js    # 不变（移入 Tab2 容器）
  chat.js / agent_trace.js  # 微调：事件附带 node_id/agent 标注
frontend/css/style.css       # 改：Tab 栏、工作流卡片、脚本面板样式
frontend/index.html          # 改：三 Tab 结构 + 用户/项目 UI
```

---

## 9. 脚本捕获（Tab3 数据来源）

CAD Agent 当前在 `freecad_bridge.run_freecad_script()` 内把 Python 脚本写入临时文件再交给 `freecad.cmd`。改造：在执行前通过回调把 `(software="freecad", language="python", content=script)` 上报给 WorkflowService → 落库 + 推 `script_generated` 事件。Mesh/CAE Agent 未来同理（Gmsh `.geo`、CalculiX `.inp`）。

---

## 10. 兼容性与回归保护

- CAD 能力代码原样平移，行为不变；编排器对"纯设计类请求"退化为单 CAD 节点，使用体验与现在一致。
- 旧 `output/{session_id}` 结构变为 `output/{user_id}/{project_id}`，属新增不影响历史（无线上数据需迁移）。
- SQLite 文件首次启动自动建表，`.gitignore` 忽略 `*.db`。

---

## 11. 分阶段实施路线

| 阶段 | 内容 | 产出 | 状态 |
|------|------|------|------|
| **P0** | SQLite 层（`db/`）+ 轻量用户管理 | 建表、repository、用户/项目 REST | ✅ 完成 |
| **P1** | Agent 重构：`agents/` 包、`base`、`registry`、`llm_factory`；CAD 平移 | CAD 能力零回归 | ✅ 完成 |
| **P2** | OrchestratorAgent（规划）+ WorkflowService（执行/事件/落库） | 后端编排闭环 | ✅ 完成 |
| **P3** | 前端三 Tab 重构 + `tabs.js`/`workflow.js`/`scripts.js` + 用户/历史 UI | 完整界面 | ✅ 完成 |
| **P4** | 脚本捕获钩子（FreeCAD）→ Tab3 数据 | 脚本日志可用 | ✅ 完成 |
| **P5** | Mesh / CAE Agent 占位（注册 + 优雅未实现）+ 端到端联调 | 框架可扩展 | ✅ 完成 |

> 本期已交付"框架可用 + CAD 全流程跑通 + Mesh/CAE 占位"。已端到端验证：
> "建模 + 网格 + 仿真" 请求被编排为 CAD→MESH→CAE 三节点并全部执行成功；
> CAD 节点真实调用 FreeCAD 并捕获脚本，Mesh/CAE 节点为占位说明。
> Mesh/CAE 的真实软件接入（建议 Gmsh + CalculiX）留待后续。

---

## 12. 待确认的后续细节（实施前可再定）

1. Mesh/CAE 的目标软件链路（建议开源：Gmsh + CalculiX），现仅占位。
2. Tab1 工作流首版用纵向 stepper 还是直接上 DAG 图（建议先 stepper）。
3. 是否需要"项目重命名/删除"等管理操作（建议 P0 先做最简，列表 + 新建 + 切换）。
```
