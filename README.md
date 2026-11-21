# 增量多重集合运算数据集（UNION ALL / INTERSECT ALL / EXCEPT ALL）

从零搭建的前后端 + 持久化存储系统，把两张带重复行的表变成可持续更新的
**多重集合（multiset）运算数据集**。支持文件导入、比较列选择、逐种元组的
左右出现次数查看，以及结果“为什么保留这些份数”的来源追溯。

## 语义约定

* **完整带类型元组判等**：每个单元格带类型标签（`null/bool/int/float/text`），
  因此整数 `1` 与浮点 `1.0` 是不同元组，不会合并。
* **SQL 集合运算空值归并语义**：`NULL = NULL` 在集合运算中成立（等价于
  `IS NOT DISTINCT FROM`），与普通等值关联（`NULL = NULL` 为 unknown、永不匹配）
  完全不同。DuckDB 的 `INTERSECT ALL / EXCEPT ALL` 原生采用同一套语义，测试直接
  以它为判定基准。
* 对同一不同元组，记左侧重数 `l`、右侧重数 `r`：
  * `UNION ALL` 保留 `l + r` 份；
  * `INTERSECT ALL` 保留 `min(l, r)` 份（左右按出现顺序配对）；
  * `EXCEPT ALL` 保留 `max(l - r, 0)` 份（右侧逐份抵消左侧）。
  * 例：左 3 份、右 2 份 → 交集 2 份、差集 1 份。
* **更正 = 同批移除旧元组 + 加入新元组**，在一个事务内完成。
* 一个批次可同时修改左右两侧；**结果只在整批提交后统一切换**，任何中间状态、
  负重数都不可能被读到；批次内冲突会整体回滚。
* 客户端批次号去重：同号批次重发为幂等重放，不产生新版本；撤销/更正不存在的
  来源行返回 **409 冲突**。
* 来源行号来自单一全局递增计数器，永不复用；每条结果都带行号级 provenance，
  可追溯到实际参与的输入行。

## 目录结构

```
app/
  types.py    带类型单元格/元组、规范化哈希、CSV 解析与类型推断
  store.py    SQLite 存储：来源行、事件台账、批次、版本、三张结果仓表
  engine.py   增量引擎：批次事务、冲突检测、重数物化、来源追溯
  server.py   stdlib HTTP 服务（JSON API + 静态前端）
  sample.py   手算样本与预置变更批次
web/          原生 HTML/CSS/JS 前端（无框架、无构建）
tests/        手算样本、DuckDB 随机回放对拍、原子性/并发、HTTP 端到端
run.py        服务入口
run_tests.py  一键测试入口
```

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # 仅测试需要 duckdb
.venv/bin/python run.py --db data/multiset.db --port 8000
# 浏览器打开 http://localhost:8000
```

页面操作顺序：

1. 两侧分别选择 CSV 并导入（空单元格 = NULL；单元格可用 `f:1.0`、`i:7`、
   `b:true`、`t:文本`、`n:` 前缀强制类型）。
2. 勾选两侧参与比较的列（按位置一一对应、数量必须相同）并应用。
3. 在批次编辑器里组合 add / retract / correct（可混左右侧），提交整批；
   同一批次号重发即幂等重放。
4. 在结果区按版本读取三张仓表：展示元组、左/右出现次数、结果重数、保留理由
   与行号级来源。版本下拉框可回看任意历史版本。
5. 台账区查看活动来源行、事件流水与批次记录。「载入手算样本」一键写入预置数据。

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 当前版本、比较列配置、两侧行数、已知列 |
| POST | `/api/import` | 导入 CSV（`side/csv/client_batch_id/auto_configure/type_overrides`） |
| POST | `/api/config` | 设置比较列（`left_columns/right_columns/client_batch_id`） |
| POST | `/api/batch` | 提交变更批次（add/retract/correct 列表） |
| GET | `/api/results?version=N` | 三表同版本快照（缺省最新版本） |
| GET | `/api/results/{union,intersect,except}?version=N` | 单表版本化读取 |
| GET | `/api/source/{left,right}` | 活动来源行（含持续保留的行号） |
| GET | `/api/events` `/api/batches` `/api/versions` | 台账与版本历史 |
| POST | `/api/sample` | 一键载入手算样本 |
| DELETE | `/api/state` | 清空重建 |

写入类请求都接受 `client_batch_id`；缺省时服务端自动生成，但**重发幂等**需要
客户端固定批次号。冲突返回 HTTP 409 且 `conflict: true`，参数错误返回 400。

## 存储与一致性

全部状态落在一个 SQLite 文件（WAL 模式）：

* 每个写批次在单个 `BEGIN IMMEDIATE` 事务内完成来源行变更、事件记录、版本登记
  与三张结果表物化，要么全部生效要么全部回滚；
* 三张结果表按 `(version, tuple_hash)` 追加保存，是**可查询的仓表**；
* 读取端在一个只读快照事务里同时取三张表，保证读到的永远是同一批次版本，
  不会出现一张已切换、另一张停留在旧版本；
* 服务重启后直接重开同一数据库文件即可继续接收变更（行号计数器、批次去重表、
  事件台账均持久化）。

## 测试

```bash
.venv/bin/python run_tests.py
```

四个无第三方测试框架依赖的模块（duckdb 仅作对拍基准）：

1. `test_hand_sample.py` — 手算样本逐项断言，钉住“左 3 右 2 → 交集 2、差集 1”、
   NULL 归并、int/float 不合并、provenance 行号全部可解析。
2. `test_duckdb_replay.py` — 120 步随机 add/retract/correct 回放（含混合批次、
   幻影撤销冲突、同号重放、两次中途重启），每个版本把增量结果与 DuckDB 对同一
   输入直接执行三类 `... ALL` 运算的结果**逐行比较重数与左右次数**。
3. `test_atomicity.py` — 批次内冲突整体回滚、负重数不可达；4 写 3 读并发下
   快照版本一致性与批次幂等。
4. `test_http_api.py` — 真实 HTTP 端口端到端：导入、配置、混合批次、409/400、
   历史版本读取、重启后继续提交。
