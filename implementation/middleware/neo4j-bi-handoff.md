# Neo4j BI 已有索引：开发交接（2026-11-17）

> **2026-11-18 更新，先读 [未改写 BI 查询进度](neo4j-bi-unrewritten-status.md)。** 范围已扩展为实验文档 `NeuG/queries/index/ldbc-bi/` 的 NeuG FILL 直接注册、改写 Neo4j 原始查询（17 个索引目录），当前 **9/17**。用户同意使用声明约束（主 README 3.17–3.22），并要求遵循 QueryPart 定理。下文是旧 Neo4j 原生索引 15 组口径的交接记录，其中“没有声明唯一性时不允许用名称代替节点身份”等限制，现在通过 profile 中经目标库检查的声明约束解除。代码在分支 `develop`。

## 当前用户确认的范围

用户最新要求：**“只需要看一看实验文档里 neo4j 的索引是否都能被改写就行了”。**
后续工作以实验文档已有的 Neo4j BI 索引为范围，共 15 组。核对索引脚本能否转换、注册，以及对应原始查询能否命中改写。无需为了其他查询新增索引，不以全部 20 条 BI 查询成功作为本次验收要求。查询验证检测到改写成功即可，可用 Neo4j `EXPLAIN` 检查输出；不必实际执行 BI 查询、比较结果或测量性能。

本文为当前交接入口。主 README 第 11.2、11.3 节以及全量 20 条查询报告保留为历史过程和证据；其中“全部查询均能改写”的目标已由上述最新范围收窄。

用户逐 case 分析的单文件材料见 [注册失败与完整查询未改写 case 全集](neo4j-bi-failed-cases-20261117.md)：收录 6 个注册失败 case 与 7 个完整查询未改写 case，去重后共 8 个，包含完整脚本、全部 80 组已测参数及诊断。BI3 自身注册失败，但查询复用其他索引成功，已单独注明。

2026-11-18 后续要求已实现为 [NeuG FILL 文件注册与重载](neo4j-fill-file-reload.md)。可编辑 [`ldbc-bi-native.cypher`](../../middleware/indexes/ldbc-bi-native.cypher)，启动/HTTP 重载后按编译计划指纹和成功回执跳过已有构建。81/81 回归、6/6 独立小图通过；NeuG 目录 12 个脚本的 84 条生成语句通过 Neo4j EXPLAIN。该路径使用另一套索引定义，不替换下方旧 Neo4j 15 组矩阵，也不声称完整 BI 查询成功数增加；现有 SF1 索引未重建。

**用户后续要求以 wiki 正确性定义增强通用改写能力。最新实现见 [理论约束增强](neo4j-theory-hardening.md)**：相关图计数/存在性、有限记录进入 CALL、外层类型推断和探针参数隔离均已实现，65/65 回归及 13/13 小图结果比对通过。BI 注册及原查询覆盖率不变。BI8/16 的剩余问题包含 Tag 接口和扩展条件，不能再仅归为前端语法限制；没有声明唯一性时不允许用名称代替节点身份。

**最新修复和通用匹配约束**见 [`neo4j-bi-rewrite-fix.md`](neo4j-bi-rewrite-fix.md)。已修复 OPTIONAL FILL 分区、类型化时间谓词键、总 DISTINCT 计数证明，以及历史源码覆盖共享内核更新的问题；声明式过程类型契约让 BI10 后续 QueryPart 可改写。当前 BI2、BI10 原始查询新增成功，BI8、BI16 的索引源模式已命中，但完整查询仍有前端缺口。此前根因证据保留在 [`neo4j-bi-rewrite-root-cause.md`](neo4j-bi-rewrite-root-cause.md)。用户明确要求通用模板匹配，禁止按查询编号、文件名或固定变量名特化。

## 实验文档位置和版本

| 内容 | 本机位置 |
| --- | --- |
| 实验文档仓库 | `/mnt/sdb1/feiyang/IncGQ-experiment-docs` |
| 本次检查的完整提交 | `08f1a2659ea2304b7940fd15db3c23382bf37eb7` |
| Neo4j BI 索引 | `/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/biN/index.cypher` |
| 参数化物化定义 | 同一 `biN/` 下的 `parameter.cypher`，本轮涉及 BI2/8/13/16 |
| Neo4j 原始查询 | `/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/baseline/ldbc-bi/bi-N.cypher` |
| Neo4j 文档优化查询 | `/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/optimized/ldbc-bi/` |
| 索引依赖及参数化约定 | `/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/README.md` |
| 共享 BI 参数 | `/mnt/sdb1/feiyang/IncGQ-experiment-docs/parameters/bi-sf1/experiment-benchmark.csv` |

实验文档已在本次会话中执行 `git pull --ff-only` 更新；本轮确认分支为 **`jcloud-exp`，跟踪 `origin/jcloud-exp`**，不是名为 `jcloud` 的分支。后续若再次更新，应记录新的提交并重查受影响脚本；本文结果仅对应上面的提交。编辑实验文档前先读其根目录 `AGENTS.md`。本次没有修改文档仓库中的索引或 baseline 查询。

## 已确认的 15 组结果

“注册支持”指中间件能够把物化定义转换成内部 FILL 并注册，不表示目标数据库已完成物理构建。“查询成功”指原始 baseline 输入命中索引改写，并通过回译绑定守卫和 Neo4j EXPLAIN。

| 索引组 | 脚本转换、注册 | 对应原始查询改写 | 待处理点或备注 |
| --- | --- | --- | --- |
| BI2 | 支持 `parameter.cypher` | 10/10 | 两个窗口匹配；保留参数值一致性检查 |
| BI3 | 原 `index.cypher` 不支持 | 10/10 | 原脚本的派生 CREATE 缺少 DISTINCT，被当前翻译器拒绝；查询成功复用 BI9 的 ROOT_POST，不能据此把 BI3 原脚本标为已支持 |
| BI4 | 不支持 | 0/10 | 混合 MERGE、SET 的聚合物化脚本尚不能注册；CALL/UNION 分支已能遍历 |
| BI5 | 支持 | 10/10 | likeCount、replyCount 均可命中 |
| BI6 | 支持 | 10/10 | CALL 内的 Person.likeCount；多候选目录异常由隔离重试处理 |
| BI8 | 支持 `parameter.cypher` | 0/10 | 索引源已命中，列表输入类型及标量计数局部路径已增强；现有 Person 索引未公开外层 Tag anchor，不能按名称替代身份 |
| BI9 | 支持 | 10/10 | ROOT_POST，自环与正长度路径合并注册为零长度闭包 |
| BI10 | 支持 | 10/10 | 声明式过程类型契约支持后续 countryName 改写；原 APOC 调用保留 |
| BI11 | 支持 | 10/10 | BI11_PERSON_COUNTRY |
| BI13 | 支持 `parameter.cypher` | 10/10 | 按 endDate 特化；本轮只验证参数化元数据，物理 bi13MessageCount 尚未构建 |
| BI14 | 不支持 | 0/10 | 条件、多属性关系写入，混合 REMOVE/SET 等操作尚不能转换 |
| BI15 | 不支持 | 0/10 | 关系属性物化脚本尚不支持；查询另有 GDS 图投影和字符串内查询 |
| BI16 | 支持 `parameter.cypher` | 0/10 | A/B 索引源均已命中，有限记录 CALL 已可分析；Person 索引未公开后续 OPTIONAL 所需的共享 Tag，不满足接口/扩展条件 |
| BI19 | 不支持 | 0/10 | 条件关系属性写入尚不支持；查询另有 GDS 路径过程 |
| BI20 | 不支持 | 0/10 | 关系属性物化脚本尚不支持；查询另有 GDS 路径过程 |

汇总：**9/15 组物化定义可转换注册，8 条对应查询成功改写，80/80 条成功输出通过 EXPLAIN。** 参数化四组检查的是 `parameter.cypher` 的定义，不能把它解释成 `index.cypher` 的全部清理、DDL、初始化语句也已经转换注册。

查询结果来自同一静态元数据集合（BI10/9/5/6/11），并为 BI2/8/13/16 的各样本追加该组参数化定义；不是把 15 组全部注册到同一目录后测得的结果。所有未改写输入保留原文。此前全量 20 条、191 组参数报告还包含当前范围外的查询，交接统计以本表的 15 组为准。

## 实现和验证入口

- [`../../middleware/cico/neo4j_indexes.py`](../../middleware/cico/neo4j_indexes.py)：Neo4j 物化脚本到内部 FILL 的转换；优先处理 BI3/4/14/15/19/20 的注册缺口。
- [`../../middleware/cico/middleware.py`](../../middleware/cico/middleware.py)：`register_neo4j_index(script, index_id=None, parameters=None)`、参数特化、候选隔离重试、验证守卫和注册回滚。
- [`../../middleware/cico/neo4j.py`](../../middleware/cico/neo4j.py)、[`../../middleware/cico/subqueries.py`](../../middleware/cico/subqueries.py)：方言转换和 CALL/UNION 局部改写。
- [`../../middleware/profiles/ldbc-snb-neo4j.yaml`](../../middleware/profiles/ldbc-snb-neo4j.yaml)、[`../../middleware/schemas/ldbc-bi.yaml`](../../middleware/schemas/ldbc-bi.yaml)：标签、关系、类型映射和 schema。
- [`../../middleware/tests/neo4j_bi_rewrite_matrix.py`](../../middleware/tests/neo4j_bi_rewrite_matrix.py)：`inventory()` 单独核对全部 15 组索引注册，不连接 Neo4j、不构建物理索引；`--index-queries` 检查这 15 条对应查询，省略该标志则检查历史全量 20 条。
- [`validation/neo4j-bi-theory-matrix-20261117.json`](validation/neo4j-bi-theory-matrix-20261117.json)：当前 15 组注册和 150 组参数结果；保存参数、原文件 SHA-256、输出、索引命中和 EXPLAIN 状态。带 `fixed` 的旧报告保留首轮修复结果。
- [`validation/neo4j-bi-theory-diagnosis-20261117.json`](validation/neo4j-bi-theory-diagnosis-20261117.json)：当前 62 个内部探针、完整查询及 Neo4j 片段对照；精确索引源 6/6 命中。反事实控制仅用于诊断，不是生产替换模板。
- [`validation/neo4j-theory-equivalence-20261117.json`](validation/neo4j-theory-equivalence-20261117.json)：13 个小图用例的实际结果比对和控制，临时数据在同一事务内回滚；没有执行 BI 查询。
- [`validation/neo4j-bi-all-20261117.md`](validation/neo4j-bi-all-20261117.md)：历史全量检查说明；[`validation/neo4j-bi-sf1-20261117.json`](validation/neo4j-bi-sf1-20261117.json) 保存前一轮导入和实际静态索引构建记录。

在本仓库根目录仅复查文档已有的 15 组注册能力：

```sh
PYTHONPATH=middleware/tests python3 - <<'PY'
import json
from neo4j_bi_rewrite_matrix import inventory
print(json.dumps(inventory(), ensure_ascii=False, indent=2))
PY
```

完整回归命令为 `python3 -m unittest discover -s middleware/tests -v`，最近一次 **65/65 通过**。最新矩阵复现命令：

```sh
python3 middleware/tests/neo4j_bi_rewrite_matrix.py --index-queries --samples 10 \
  --output implementation/middleware/validation/neo4j-bi-theory-matrix-20261117.json
```

矩阵仍返回 1，因为所选 15 条没有全部改写；JSON 中 `invalid_rewrites=0`、`explain_ok=80`。不能把 8 条成功描述成 15 条全部通过，也不能把未支持误判成输出语法错误。

## 本机数据库及外部依赖

- 独立 BI SF1 实例目录：`/mnt/sdb1/feiyang/neo4j/middleware/runtime/bi_sf1`；Bolt 配置为 `bolt://127.0.0.1:7693`，数据库 `neo4j`，仅监听本机，无认证。接续时先检查进程状态，不假定实例持续运行。
- 导入原始数据：`/mnt/sdb1/feiyang/datasets/bi-sf1-composite-projected-fk/graphs/csv/bi/composite-projected-fk/initial_snapshot`。基础数据为 2,997,352 个节点、17,196,776 条关系；物化后总关系数会增加。
- 实际完成物理构建的静态脚本：BI10/9/5/6/11，共 14 条语句。参数化索引未按样本逐组物化；EXPLAIN 通过不等于物理属性已就绪。
- 软件：`/mnt/sdb1/dwk/imported_data/neo4j/neo4j-5.26.28/tools/neo4j-community-5.26.28`；Java：同一 `tools/` 下 `jdk-21.0.12+8-jre`。数据及运行配置独立存于本仓库的 runtime 目录。
- 为 EXPLAIN BI10 输出，已将软件自带 `labs/apoc-5.26.28-core.jar` 复制到独立实例的 `plugins/`，配置 `server.directories.plugins` 指向此目录及 `dbms.security.procedures.unrestricted=apoc.path.*`，只重启该实例。APOC 调用未实际执行。
- NeuG 依赖：`/mnt/sdb1/feiyang/IncGQ/build/src/libneug.so`。默认优先使用本仓库 `middleware/engine/build/incgq_rewrite_engine_patched`；不要直接改共享 NeuG 工作区。历史补丁构建、ABI 及协作约定见主 README 第 10、11.1 节。
- `runtime/`、构建目录和本机二进制不进入 Git；本仓库的 `implementation/` 和 `middleware/` 当前仍未跟踪、未提交，保留用户已有工作。

## 接续顺序

1. 先读最新理论约束增强及 62 探针。BI8/16 先核对 Tag 接口及共享 Tag 后的扩展条件，不能假设 Tag.name 唯一；所需索引接口或正式函数依赖确定后再继续表达式转换。保留通用结构匹配、参数有效域、完整分组和失败回退，不删除 OPTIONAL 或按全文匹配。
2. 再按上述提交下的原脚本补齐六组注册缺口，区分 BI3 原脚本失败与 BI9 可复用。BI4/14/15/19/20 注册通过后再查查询阶段。
3. 保留参数值绑定、关系唯一性、失败回退和回译验证。一次索引注册通过、一次 EXPLAIN 通过、物理构建完成分别记录，避免混为同一种成功。
4. 后续更新本表及机器可读证据。当前任务无需新增文档范围外索引，也无需运行 BI 负载或扩展到其他数据集。
