# 网关工坊 · API接口采集与数据网关平台

面向第三方API接入注册、接口鉴权编排、限流熔断、响应解析、schema映射、结果缓存、重放恢复与交付订阅的API接口采集与数据网关平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
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
| 接口提供方 | `api_provider` | API提供方 | 提供方编号、提供方名称、认证方式 |
| 鉴权配置 | `auth_profile` | 鉴权方案 | 方案编号、提供方、鉴权类型 |
| 接口定义 | `endpoint_def` | 接口端点 | 端点编号、提供方、请求路径 |
| 限流熔断 | `rate_limit` | 限流策略 | 策略编号、目标端点、QPS上限 |
| 请求批次 | `request_batch` | 调用批次 | 批次编号、关联端点、参数模板 |
| 响应解析 | `response_parse` | 解析规则 | 规则编号、目标端点、JSON路径 |
| Schema差异 | `schema_diff` | 差异记录 | 记录编号、关联端点、变更字段 |
| 结果缓存 | `data_cache` | 缓存策略 | 策略编号、目标端点、缓存TTL |
| 重放队列 | `retry_queue` | 重放任务 | 任务编号、失败端点、失败原因 |
| 数据投递 | `data_delivery` | 投递管道 | 管道编号、数据来源、目标地址 |
| 订阅管理 | `subscriber` | 订阅方 | 订阅编号、订阅方、数据主题 |
| 接口监控 | `api_monitor` | 监控指标 | 指标编号、目标端点、监控类型 |
| Webhook路由 | `webhook_router` | 回调规则 | 规则编号、触发事件、目标URL |
| 传输加密 | `data_encrypt` | 加密配置 | 配置编号、数据范围、加密算法 |
| 调用日志 | `call_log` | 调用记录 | 记录编号、接口端点、请求参数 |
| 数据版本 | `data_version` | 数据快照 | 版本编号、数据来源、行数统计 |
| 管道审计 | `pipeline_audit` | 审计记录 | 记录编号、管道名称、操作人 |
| 治理报告 | `govern_report` | 治理报告 | 报告编号、评估范围、合规指标 |
| 增量追踪 | `incr_tracker` | 增量标记 | 标记编号、目标端点、增量字段 |
| 抽样校验 | `sample_check` | 抽样计划 | 计划编号、目标端点、抽样比例 |
| 用量计费 | `cost_account` | 用量账单 | 账单编号、提供方、计费周期 |
| 任务调度 | `task_schedule` | 调度计划 | 计划编号、关联端点、Cron表达式 |

## 采集续跑（响应解析缺字段续跑投递）

`backend/app/services/resume_delivery.py` + `backend/app/routers/resume.py` 承载
「响应解析异常 → 缺字段续跑 → 投递待办 → 接收确认 → 补齐重定位」全链路，
独立于通用 CRUD 资源路由，固定前缀 `/api/resume`（先于 `/api/{module}` 挂载）。

核心规则：

- 显式 `null` 与路径缺失严格区分：映射 `allow_null` 时显式 null 原样保留并视为已核验；
  JSON 路径找不到的行记入解析问题清单（留存原响应行与未通过字段），核验前不得按零额交付。
- 续跑明细随可交付数据写入投递待办；阻断行不进待办。
- 字段补录只把新就绪行放入「下一次投递」待办，不隐式立即发出；原始响应行永不被改写，
  补录记录带原请求编号 + 原响应行号定位坐标，全部状态变化写入只增不改的事件日志。
- 启动/重试投递只发未确认行，已确认/已拒收行任何情况下不重复投递；确认回执幂等（重复确认只回原回执）。
- 接收方可拒收在途行并记录原因；拒收行不再自动投递、不能再确认，接收方视图与运维视图均可追溯。
- 接收确认结果同步回解析问题清单；全部行核验并到达终态（已确认/已拒收）后请求自动置为「投递完成」。

| 端点 | 作用 |
| --- | --- |
| `POST /api/resume/intake` | 解析一次采集响应，产续跑明细/投递待办/问题清单 |
| `POST /api/resume/requests/{rn}/start` | 启动投递，实际发出待投递且未确认的行 |
| `POST /api/resume/requests/{rn}/interrupt` | 标记投递中断 |
| `POST /api/resume/requests/{rn}/retry` | 中断重试，重发在途/待投递行，排除已确认/已拒收行 |
| `POST /api/resume/requests/{rn}/rows/{row}/acknowledge` | 接收方确认（幂等，回执唯一） |
| `POST /api/resume/requests/{rn}/rows/{row}/reject` | 接收方拒收（必填原因），拒收行不再自动投递 |
| `POST /api/resume/requests/{rn}/rows/{row}/supplement` | 按原请求+原响应行补交字段，核验后进下一次投递待办 |
| `GET /api/resume/requests/{rn}/receiver-results` | 接收方视图：确认/未确认/拒收行、null 字段与回执 |
| `GET /api/resume/requests/{rn}/ops-view` | 运维视图：异常行、缺字段、补录记录、重试次数、最终状态与事件轨迹 |
| `GET /api/resume/requests/{rn}/parse-issues` | 解析问题清单：原响应行、缺字段、补齐历史 |
| `POST /api/resume/requests/{rn}/reset` | 清除单请求链路数据（测试/重跑隔离） |

端到端验证脚本（A 缺 amount / B amount=null / C amount=8 的完整时序）：

```bash
# 先启动后端，再跑真实 HTTP 链路；也可加 --in-process 直接驱动领域服务
python3 -I verification/verify_resume_delivery.py
```

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
