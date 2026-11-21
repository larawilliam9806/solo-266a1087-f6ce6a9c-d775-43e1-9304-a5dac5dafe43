# 口述史转录稿勘误供数 API

FastAPI + SQLite 实现的转录稿勘误版本管理服务。支持：导入正文与带编号的人名/
地名标注、按当前版应用勘误（左闭右开区间替换，含插入与删除）、按勘误号撤回
（带冲突检测）、标注待复核跟踪、实体检索，以及同一版本的一致性查询。

## 启动

```bash
docker compose up --build
# API 位于 http://localhost:8000 ，交互文档 http://localhost:8000/docs
```

SQLite 数据文件落在命名卷 `app_data`（容器内 `/data/app.db`）。

本地开发（无需 Docker）：

```bash
pip install -r requirements-dev.txt
DB_PATH=./app.db uvicorn app.main:app --reload
python3 -m pytest tests/          # 全部测试
python3 -m unittest discover -s tests -p 'test_engine.py'   # 仅引擎（零依赖）
```

## 核心概念

- **勘误**：把当前版的 `[start, end)` 左闭右开区间替换为 `text`。
  `start == end` 为纯插入，`text` 为空为纯删除，其余为替换。应用后返回
  文档内递增的**勘误号**。
- **撤回**：按勘误号撤回。若仍生效的后续勘误存在以下任一情况，撤回被拒绝，
  返回 `409` 与冲突勘误号列表，**现版保持不变**：
  1. 改删了该笔写入的文字；
  2. 在该笔写入的文字中插字；
  3. 该笔为纯删除时，在其留下的接缝上插字，或跨缝改删。
  
  否则只还原这一笔，其他勘误（包括更晚的）的成果原样保留。冲突勘误本身
  撤回后，原勘误即可撤回。
- **标注待复核**：勘误改删了某标注区间内的文字，或在其中插字，该标注即转为
  待复核；影响它的勘误全部撤回后自动恢复有效。落在标注边界上的插入不算。
- **版本查询**：正文、有效标注的当前字符区间、待复核标注在同一次读事务中
  返回，并附单调递增的 `revision`，保证三者来自同一版。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/documents` | 导入正文与实体标注 |
| GET | `/documents` | 文档列表 |
| GET | `/documents/{doc}` | 版本查询：正文 + 有效标注区间 + 待复核标注 |
| POST | `/documents/{doc}/corrections` | 应用勘误，返回勘误号 |
| POST | `/documents/{doc}/corrections/{n}/retract` | 按号撤回（冲突 409） |
| GET | `/documents/{doc}/corrections` | 勘误流水（含状态与触及关系） |
| GET | `/documents/{doc}/entities?kind=&q=` | 实体检索（按人名/地名、名称子串） |
| GET | `/documents/{doc}/entities/{number}` | 单个实体 |

## 示例

```bash
# 1. 导入正文与标注（人名“张三”[0,2)，地名“北京市”[4,7)）
curl -X POST localhost:8000/documents -H 'Content-Type: application/json' -d '{
  "text": "张三住在北京市东城区。",
  "entities": [
    {"number": 1, "kind": "person", "name": "张三", "start": 0, "end": 2},
    {"number": 2, "kind": "place", "name": "北京市", "start": 4, "end": 7}
  ]}'
# => {"document_id": 1, "revision": 0, "entity_count": 2}

# 2. 应用勘误：把“京”改为“津”（触及地名标注）
curl -X POST localhost:8000/documents/1/corrections \
  -H 'Content-Type: application/json' \
  -d '{"start": 5, "end": 6, "text": "津"}'
# => {"correction_number": 1, "kind": "replace", "revision": 1}

# 3. 版本查询：正文已更新，地名标注转为待复核
curl localhost:8000/documents/1
# => {"text": "张三住在津市东城区。", ...,
#     "valid_entities": [{"number": 1, ...}],
#     "pending_entities": [{"number": 2, "affecting_corrections": [1], ...}]}

# 4. 撤回勘误 1：无冲突，只还原这一笔，标注恢复有效
curl -X POST localhost:8000/documents/1/corrections/1/retract
# => {"retracted": 1, "revision": 2}

# 5. 实体检索
curl 'localhost:8000/documents/1/entities?kind=place&q=北京'
```

冲突示例：勘误 1 插入 `XYZ`，勘误 2 改掉了其中的 `Y`，此时撤回勘误 1：

```bash
curl -X POST localhost:8000/documents/1/corrections/1/retract
# => 409 {"detail": "存在仍生效的冲突后续勘误，保留现版", "conflicts": [2]}
```

## 实现要点

当前版正文用**片段表**表示：每个片段引用原文的一段、某笔勘误写入文本的一
段，或纯删除勘误留下的零长**接缝**标记。应用勘误时在片段表上切分/替换，
并记录本笔删掉的片段（供撤回还原）与触及的更早勘误（供冲突检测）。冲突
规则保证：一笔勘误可撤回时，它写入的片段必然完整连续、接缝必然在位，因此
撤回是精确的整体还原，不影响其他勘误的成果。标注区间按原文坐标存储，查询
时经片段表映射到当前版坐标。

```
app/
  engine.py   # 片段表核心算法（纯 Python，零依赖）
  db.py       # SQLite 模式与存取
  service.py  # 业务操作（导入/勘误/撤回/查询/检索）
  main.py     # FastAPI 路由与事务
tests/
  test_engine.py  # 引擎单元测试（标准库即可运行）
  test_api.py     # API 集成测试
```
