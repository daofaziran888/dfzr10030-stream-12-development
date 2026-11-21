# 管道工坊 · 实时数仓与流式管道平台

面向实时数据接入、流式转换、变更捕获、窗口聚合、状态管理、断流恢复与多维数据集下发的实时数仓与流式管道开发平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 源端绑定 | `source_binding` | 数据源端 | 源端编号、源端类型、连接地址 |
| 变更捕获 | `cdc_task` | CDC任务 | 任务编号、源数据库、监控表 |
| 流式转换 | `stream_transform` | 转换算子 | 算子编号、输入流、输出流 |
| 窗口聚合 | `window_agg` | 时间窗口 | 窗口编号、聚合指标、窗口类型 |
| 状态管理 | `state_store` | 算子状态 | 状态编号、关联算子、存储后端 |
| 规则引擎 | `rule_engine` | 业务规则 | 规则编号、规则条件、触发动作 |
| 水位管理 | `watermark_mgr` | 事件水位 | 水位编号、关联流、水位类型 |
| 数据落盘 | `sink_writer` | 输出槽位 | 槽位编号、输出类型、目标地址 |
| 背压控制 | `backpressure` | 背压策略 | 策略编号、关联算子、队列上限 |
| 断流恢复 | `checkpoint` | 检查点 | 检查点编号、关联流水线、位点时间 |
| 流式质检 | `data_quality` | 质检规则 | 规则编号、关联流、检查项 |
| Schema演进 | `schema_evolve` | 结构变更 | 变更编号、关联流、变更类型 |
| 管道编排 | `pipeline_orch` | 管道DAG | 管道编号、管道名称、节点列表 |
| 去重管理 | `dedup_mgr` | 去重策略 | 策略编号、关联流、去重键 |
| 指标计算 | `metric_calc` | 实时指标 | 指标编号、指标名称、计算公式 |
| 事件路由 | `event_router` | 路由规则 | 规则编号、事件类型、匹配条件 |
| 历史回放 | `replay_batch` | 回放任务 | 任务编号、数据源、起始时间 |
| 分组分区 | `grp_partition` | 分组策略 | 策略编号、关联流、分区键 |

## 结构变更期的兼容转换

金额列从 `金额` 改名为 `实付金额` 之后，上游订单流里会同时出现纯旧列、纯新列、
两列一致、两列冲突四种记录。兼容转换读取 `schema_evolve` 的结构变更记录，
生成一份**转换兼容数据批次**，让旧报表（读旧列）和新报表（读新列）都能找到
可实际读入的字段。

- 规则实现：`backend/app/services/compatibility.py`
- 接口：`backend/app/routers/compatibility.py`、`backend/app/routers/schema_evolve.py`
- 页面：`frontend/src/views/schema_evolve/index.vue` 的“结构变更期兼容转换”面板
- 样本：`backend/app/fixtures.py` 的 `MIGRATION_ORDER_RECORDS`（订单号 ORD-101 ~ ORD-110）
- 预置改名变更：`SCHE-0002`（`金额` → `实付金额`，生效版本 `v2`）

处理口径：

| 输入形态 | 兼容结果 | 聚合 |
| --- | --- | --- |
| 纯旧列 | 旧版视图取原值，新版视图按同一金额推导 | 计入 |
| 纯新列 | 新版视图取原值，旧版视图按同一金额推导 | 计入 |
| 两列一致 | 两版视图都取原值 | 计入 |
| 两列冲突 | 按原行标识写入待核对区，两版视图都不产出 | 跳过 |

- 旧版视图保留历史订单的原列取值和字段版本（默认 `v1`），只对纯新列行做标注为“推导”的补值。
- 校验结果回写到结构核对清单（`/api/schema_evolve/checks`，并落到结构变更记录的
  `核对结果` / `兼容批次` / `待核对行数` 字段）。
- 同一批次号重复执行按原行标识幂等：已确认的金额不会被写第二次，迁移中断后可以
  用 `batch_no` 续做。
- 批次记录 `字段版本依赖` 和 `数据库写入范围`（关联流、行键、写入字段、行数、位点范围、
  每行写入次数）；`/api/compatibility/batches/{id}/rollback-trace` 可按原行标识追溯到原列。
- `effective_boundary` 声明新旧字段的生效边界；旧流水线仍在补算时，边界两侧形态
  异常的记录会列进 `生效边界.冲突明细`。

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/schema_evolve/compatibility/candidates` | 可做兼容转换的字段改名变更 |
| POST | `/api/schema_evolve/compatibility/run` | 生成/续做兼容批次，回写核对清单 |
| GET | `/api/schema_evolve/checks` | 结构核对清单 |
| GET | `/api/schema_evolve/reviews` | 待核对区（两列冲突行） |
| GET | `/api/compatibility/batches` | 兼容批次摘要 |
| GET | `/api/compatibility/batches/{id}` | 批次明细（逐行两版视图、统计、依赖、写入范围） |
| GET | `/api/compatibility/batches/{id}/rollback-trace` | 回滚追溯 |
| POST | `/api/pipeline/stream-compat` | 从流式转换管道触发兼容批次生成 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
