# 文档解析与知识抽取平台

面向多格式文档批量接入、版式解析、分块切分、实体关系抽取、向量化嵌入与抽取质量校验入库的非结构化知识抽取平台。

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
| 文件接入 | `file_intake` | 接入批次 | 批次编号、来源系统、文件清单 |
| 格式解析 | `format_parse` | 解析任务 | 任务编号、文档编号、解析器类型 |
| 版式还原 | `layout_restore` | 版式结构 | 文档编号、段落结构、标题层级 |
| 表格抽取 | `table_extract` | 表格区域 | 表格编号、文档编号、页码位置 |
| OCR识别 | `ocr_engine` | 识别任务 | 任务编号、图片区域、识别语言 |
| 分块切分 | `chunk_split` | 文本分块 | 分块编号、文档编号、分块长度 |
| 实体识别 | `entity_recognize` | 命名实体 | 实体编号、分块编号、实体类型 |
| 关系抽取 | `relation_extract` | 实体关系 | 关系编号、头实体、尾实体 |
| 术语归一 | `term_normalize` | 术语映射 | 映射编号、源术语、标准术语 |
| 敏感词过滤 | `sensitive_filter` | 过滤规则 | 规则编号、匹配模式、命中词条 |
| 向量嵌入 | `vector_embed` | 向量化任务 | 任务编号、分块编号、嵌入模型 |
| 知识图谱 | `kg_build` | 图谱三元组 | 三元组编号、头实体、谓词关系 |
| 抽取质量 | `extract_quality` | 质检报告 | 报告编号、批次编号、抽样比例 |
| 人工复核 | `annot_review` | 复核任务 | 复核编号、分块编号、复核对象 |
| 文档版本 | `doc_version` | 版本快照 | 快照编号、文档编号、版本号 |
| 抽取血缘 | `extract_lineage` | 血缘链路 | 链路编号、源文件、解析节点 |
| 结果交付 | `result_deliver` | 交付任务 | 交付编号、快照编号、交付格式 |
| 失败重采 | `parse_retry` | 重采任务 | 重采编号、失败环节、原始批次 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 版式复跑包

外地复查只有结果、无法复现抽取时，用版式复跑包把「页 + 区域 + 确认依据」导出成
可离线复跑的 ZIP。

- 版本库：`app/services/layout_revision.py`，按页追加不可变修订；区域更正只生成新页修订，
  同一页的页记录、区域集合和确认依据永远来自同一修订。
- 范围：`POST /api/layout-rerun/documents/{doc_id}/seed` 从解析/版式还原产物建档，
  `PUT .../pages/{page}` 更正区域或确认，`POST /api/layout-rerun/packages` 导出。
- 包内容：`manifest.json`（所选页、可立即复跑页、未确认页、批次表格数、是否覆盖整份、
  离线命令）、`pages.json`（逐页页校验和与区域校验和）、`evidence.json`（逐页逐区域的
  确认时间、确认人、来源与校验和）、`README.md`、`rerun.py`。
- 导出先取一次快照再写 ZIP，与区域更新并发时包内每一页仍自洽；旧包重开仍复现导出时的修订。
- 离线执行（`rerun.py` 只依赖标准库）：
  ```bash
  python3 rerun.py verify .                                   # 校验清单/页/区域/确认依据
  python3 rerun.py rerun . --out ./rerun-output                # 按页复跑，未确认页跳过
  python3 rerun.py extract-tables . --out ./cells.jsonl        # 逐单元格输出，可逐页对照
  ```
  `extract-tables` 默认只抽已确认页，未确认页列在 `skipped_unconfirmed_pages`；
  加 `--include-unconfirmed` 可把未确认页一起导出。
- 表格抽取服务同样直接消费包：`TableExtractService.extract_from_package(...)` 或
  `POST /api/layout-rerun/packages/{package_id}/table-extract`，单元格带页码、页修订、
  确认依据、表格区域号和行列坐标。
- 前端入口：`/layout-rerun`（`frontend/src/views/LayoutRerunView.vue`）。

领域：数据工程
子方向：数据采集、爬虫与RPA
