# 采购公告采集 API

传入**采购公告列表页 URL** 与**项目编号**，自动抓取该页指向的同站点原公告与更正公告，
从详情页提取发布时间、投标截止时间与取消采购信息，按发布时间合并后返回：
当前是否可投标、有效截止时间、项目状态以及全部来源链接。

## 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 只采集列表页指向的**同站点**详情链接，外站链接、登录/导航链接自动忽略 | `app/parser.py::extract_detail_links` |
| 按**发布时间**从早到晚合并原公告与更正公告，响应中的 `notices`/`sources` 保持同一顺序 | `app/merger.py::merge_notices` |
| 更正公告中的**明确改期表述**（标签字段或正文的“由 X 调整为 Y / 原时间…现延期至 Y / 新截止时间为 Y”）只取**现行时间 Y** 覆盖旧期限，句首的原时间一律忽略，不再取第一个日期 | `app/parser.py::_analyse_deadline_text` |
| 更正公告**未提及截止时间**，或仅重述旧日期并声明“未调整/不变/仍按原……执行”时，不产生新期限，沿用此前已生效的截止时间 | `app/parser.py` + `app/merger.py` |
| 同一公告给出**相互矛盾的现行截止时间**（如结构化字段与正文改期句不一致）→ 状态 `pending` 待核实，`effective_deadline` 强制为 `null`，候选时间随 `deadline_candidates` 返回，**不按页面先后猜测** | `app/parser.py` + `app/merger.py` |
| 公告明确**取消采购** → 状态 `closed`、不可投标，不再返回有效截止时间（旧时间仅在公告明细中保留） | `app/merger.py` |
| **任一关联详情页**抓取/解析失败（404、超时、结构异常等）→ 状态 `pending` 待核实，`effective_deadline` 强制为 `null`，**绝不把旧时间当作最终期限** | `app/merger.py` + `app/collector.py` |
| 发布时间缺失、缺少原公告、全部公告均无截止时间等信息链不完整情况，同样标记 `pending` | `app/merger.py` |

### 项目状态（`status`）

- `open`：可投标，`effective_deadline` 为合并后的有效截止时间；
- `closed`：采购已取消，`can_bid=false`，无有效截止时间；
- `pending`：待核实，存在关联页面失败或信息不完整，`effective_deadline=null`；
- `not_found`：列表页未发现与该项目编号相关的公告。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 入口、路由、浏览器演示页
│   ├── collector.py   # 采集编排：列表发现 → 并发抓取 → 归属判定 → 合并
│   ├── parser.py      # HTML 解析：同站链接发现、三层字段提取、取消识别
│   ├── merger.py      # 业务合并规则（时间继承、延期覆盖、取消、待核实）
│   └── models.py      # 请求/响应 Pydantic 模型
├── sample-site/       # 本地示例公告站点（nginx 静态托管）
│   ├── list.html      # 公告列表页（含外站/登录干扰链接）
│   └── detail/        # 5 个项目的原公告/更正/取消/矛盾期限详情页
├── nginx/default.conf # 示例站点 nginx 配置（缺失页面返回真实 404）
├── tests/             # 解析/合并单测 + 本地静态服务器端到端测试（39 个用例）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 公告采集 API | http://localhost:8000 | Swagger 文档：http://localhost:8000/docs |
| 浏览器演示页 | http://localhost:8000/ | 填表即可发起采集 |
| 示例公告站点 | http://localhost:8080/list.html | nginx 托管的静态采购公告 |

停止：`docker compose down`。

## API 用法

### POST（推荐）

```bash
curl -X POST http://localhost:8000/api/v1/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url":"http://localhost:8080/list.html","project_code":"GSB-2026-001"}'
```

### GET（便于浏览器快速验证）

```bash
curl "http://localhost:8000/api/v1/collect?list_url=http://localhost:8080/list.html&project_code=GSB-2026-001"
```

响应关键字段：

```json
{
  "project_code": "GSB-2026-001",
  "status": "open",
  "can_bid": true,
  "effective_deadline": "2026-11-15T14:00:00",
  "cancelled": false,
  "notices": [
    {
      "kind": "original",
      "published_at": "...",
      "deadline": "...",
      "deadline_conflict": false,
      "deadline_candidates": []
    }
  ],
  "sources": [ { "role": "list", "url": "..." }, { "role": "correction", "url": "..." } ],
  "warnings": []
}
```

- `notices` 按发布时间从早到晚排序，每条公告带 `kind`、`deadline`（该公告声明的**现行**
  截止时间；未改期重述为 `null`）与来源 URL；
- 同一公告截止时间自相矛盾时，该条 `deadline_conflict=true`、`deadline=null`，
  `deadline_candidates` 列出页面上的全部候选时间，整体状态置为 `pending`。

## 示例站点内置的五个演示项目

| 项目编号 | 场景 | 预期结果 |
| --- | --- | --- |
| `GSB-2026-001` | 原公告 → 澄清更正（**未提截止时间，沿用原时间**）→ 延期更正（**新时间覆盖**） | `open`，有效截止时间 `2026-11-15 14:00` |
| `GSB-2026-002` | 原公告 → 取消公告（预算收回） | `closed`，不可投标，无有效截止时间 |
| `GSB-2026-003` | 原公告 → 更正页**已下线（404）** | `pending` 待核实，`effective_deadline=null`，来源中列出失败链接 |
| `GSB-2026-004` | 原公告（旧期限 `11-12 09:00`）→ 未改期更正（重述旧日期并声明**未调整**）→ 延期更正（标签字段写明“**由 2026-11-12 09:00 调整为 2026-11-18 09:00**”） | `open`，有效截止时间 `2026-11-18 09:00`（只取现行时间，旧时间不回流） |
| `GSB-2026-005` | 原公告 → 更正公告中结构化字段（`11-20 09:30`）与正文改期句（`11-22 10:00`）**两个现行时间相互矛盾** | `pending` 待核实，`effective_deadline=null`，候选时间随公告明细返回 |

另外列表页还包含：外站链接、登录导航链接、其他项目公告（`GSB-2026-099`）、
无关项目的坏链接（`GSB-2025-888`），用于验证同站过滤、项目归属判定与失败隔离。

## 详情页解析策略（三层回退）

1. **结构化标记**：`data-field="published-at|deadline|cancelled|..."` 元素或
   `<meta property="article:published_time">`；
2. **标签匹配**：`发布时间 / 投标截止时间 / 响应文件递交截止时间 / 取消原因` 等
   中文标签，兼容 `th/td`、`dt/dd`、`li/p` 等布局，支持
   `2026-11-10 09:30`、`2026年11月15日 14时00分` 等常见写法；
3. **正文正则兜底**：在含“截止/递交/提交/受理”等时限语义的块级元素中识别日期，
   并排除“发布时间”句、资质业绩说明中的历史日期（如“近三年（2023年1月1日起）”）。

### 截止时间改期识别（更正公告专项）

解析器会汇总页面上全部来源（结构化字段、标签、正文句子）的候选时间后统一判定：

- **明确改期**：识别“由/从 X 调整（变更/延期/顺延）为/至 Y”“原……现延期至 Y”
  “新截止时间为 Y”等引导词，**只取引导词之后的现行时间 Y**；“原投标截止时间为 X”
  这类旧时间从句（即使被逗号切分到单独句子）一律忽略；
- **声明未改期**：出现“未调整/不变/仍按原招标文件执行/其他内容不变”等表述时，
  句子里重述的旧日期不产生新期限，合并阶段自然沿用已生效的截止时间；
- **同页矛盾**：不同来源给出两个及以上不一致的现行时间时，标记
  `deadline_conflict=true` 并清空本页期限，候选时间保留在 `deadline_candidates`
  供人工核实；合并器据此把项目置为 `pending`，不按页面先后猜测。

取消采购识别支持 `data-field="cancelled"` 标记、取消原因标签以及
“本项目取消采购 / 终止本次招标”等显式句式。

> 接入真实采购平台时，若页面结构不同，优先在详情页模板中补 `data-field` 标记，
> 或在 `app/parser.py` 顶部的标签/关键词元组中登记该平台的字段叫法。

## 本地开发与测试（不使用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 终端 A：托管示例公告页
cd sample-site && python -m http.server 8080
# 终端 B：启动 API
uvicorn app.main:app --reload --port 8000

# 测试（自带静态服务器，无需外部依赖）
pytest -q
```

## 设计说明

- 列表页抓取失败直接返回 `502`；详情页失败不影响整体响应，而是按业务规则沉淀为
  `pending` + 失败来源链接，便于投标专员人工核实。
- 关联归属判定结合详情页项目编号与列表行上下文（含编号列），
  即使详情页下线（只能看到列表行）也能正确判定失败是否与目标项目相关；
  无关项目的坏链接不会污染查询结果。
- 详情页并发抓取（上限 5），统一超时、自定义 UA、跟随重定向。
