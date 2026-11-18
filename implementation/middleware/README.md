# Cypher-in / Cypher-out：IncGQ 索引中间件 —— 设计与实现进度

> **位置（2026-11-16 起）**：本仓库是 `flyfly0521/neo4j`（分支 `2026.10`）的克隆，位于 `/mnt/sdb1/feiyang/neo4j`。
> - 代码在仓库根目录的 [`middleware/`](../../middleware/)，使用说明见 [`middleware/README.md`](../../middleware/README.md)。
> - 本目录 `implementation/middleware/` 存放目标、思路、设计说明和进度记录，以及 `query_edits/`（文档查询的等价写法）和 `patches/`（NeuG 内核补丁）。
> - 迁移前的位置是 `IncGQ/middleware` 和 `IncGQ/implementation/cypher_in_cypher_out`，那两处现在只留一个指向这里的说明，迁移记录见第 11 节。
> - 中间件仍依赖 IncGQ（NeuG）作为改写引擎：`middleware/engine/` 链接 `IncGQ/build/src/libneug.so`，通过 `INCGQ_ROOT` 找到，默认查找与本仓库同级的 `IncGQ/`。
>
> 本文件供多个 agent 或开发者协作使用。修改代码后请同步更新第 5、8、10 节。

## 当前交接入口（2026-11-18 更新）

**最新状态先读 [未改写 BI 查询进度](neo4j-bi-unrewritten-status.md)**（由 `middleware/tests/render_bi_status.py` 从实测结果生成，17 个 case 逐条给出 FILL、原始 Neo4j 查询、**方言转换后的 NeuG 查询**（整条或按 QueryPart/CALL 分支，含不能转换的原因；声明约束重试阶段实际改写的转换文本）、中间件返回、改写输出及其 NeuG 转换、卡点和计划）。口径：实验文档 `NeuG/queries/index/ldbc-bi/` 的 NeuG FILL 直接注册，改写 `Neo4j/queries/baseline/ldbc-bi/` 原始查询，SF1 第 0 组参数：**12/17 改写**（BI2、3、4、5、6、8、9、10、11、13、14、17）。用户 2026-11-18 同意使用**声明约束**（profile `constraints:`，SF1 上 12/12 成立），并要求遵循 QueryPart 定理（`theory.md`，即 wiki《QueryPart 索引改写的语义等价定理与 Lean 形式化》）。代码在分支 `develop`（`origin/develop`），每步修复单独提交。剩余：BI16 暂缓（收益低）；BI19/BI20 待用户决定是否纳入 create-graph 脚本；BI15/BI15w 不建议。

以下为 2026-11-17 的交接说明（旧 Neo4j 原生索引 15 组口径），保留作历史记录。

后续 agent 请先读 [`neo4j-bi-handoff.md`](neo4j-bi-handoff.md)：记录实验文档的绝对路径和提交、
15 组 Neo4j BI 已有索引的逐项结果、代码入口、验证方法和数据库位置。
用户已将范围明确为实验文档中已有的 Neo4j BI 索引；无需为其他查询新增索引，
也不再以全部 20 条 BI 查询成功作为本次验收目标。检测到改写成功即可，不必实际运行 BI 查询。
当前为 **9/15 组物化定义可转换注册，8 条对应查询改写成功，80/80 条成功输出通过 EXPLAIN**；BI3 查询成功复用 BI9 的 ROOT_POST，
BI3 原建索引脚本仍未支持。第 11.2、11.3 节及全量报告保留为历史记录，最新范围以交接文档为准。

逐 case 分析见 [注册失败与完整查询未改写 case 全集](neo4j-bi-failed-cases-20261117.md)：集中收录 BI3、4、8、14、15、16、19、20 共 8 个 case 的完整索引脚本、原始查询、全部已测参数、原始诊断及文档优化写法，区分注册失败、完整查询未命中和精确源探针成功。

2026-11-18 新增 [NeuG FILL 文件注册与重载](neo4j-fill-file-reload.md)：索引输入可明确为 NeuG，查询及输出仍使用 Neo4j。编译计划指纹、依赖和成功构建回执决定新增/变化项，重启不重建已有成功项。可编辑文件在 [`middleware/indexes/ldbc-bi-native.cypher`](../../middleware/indexes/ldbc-bi-native.cypher)。81/81 回归、6/6 Neo4j 小图验证通过；文档 NeuG 目录 12 个脚本的 84 条生成语句通过 EXPLAIN，未物化这些 BI 索引，未重跑完整 BI 查询矩阵。

NeuG FILL 文件注册后改写 Neo4j 原始查询的 11 个未命中案例，见 [BI 改写失败定位](neo4j-bi-neug-fill-rewrite-diagnosis.md)。

**最新进度（2026-11-18）**：NeuG FILL 注册后 Neo4j 原始查询改写 **12/17**（新增 BI2、BI4、BI8、BI10、BI11、BI14、BI17），仍未改写的查询、对应索引、卡点和下一步见 [未改写 BI 查询进度](neo4j-bi-unrewritten-status.md)。本轮引入声明约束（profile `constraints:`，SF1 上 12/12 成立）。

已注册但未命中的历史定位见 [`neo4j-bi-rewrite-root-cause.md`](neo4j-bi-rewrite-root-cause.md)，首轮修复见 [`neo4j-bi-rewrite-fix.md`](neo4j-bi-rewrite-fix.md)。**最新增强见 [以 QueryPart 正确性定义增强中间件](neo4j-theory-hardening.md)**：按 wiki 的接口、作用域和重数约束支持相关图计数/存在性表达式、有限记录传入 CALL、保守的外层类型推断及探针参数隔离。65/65 回归通过，13/13 个独立小图用例实际结果多重集一致，全部事务回滚。BI 原始覆盖仍为 8/15；BI8/16 还受未公开 Tag 接口的限制，不能只补语法或放松匹配来解决。未执行 BI 负载查询。

## 1. 目标、思路与边界

### 1.1 目标

- **输入**：Kuzu、Neo4j 或 NeuG 方言的只读 Cypher（带参数）。
- **输出**：同一方言的 Cypher。用已注册的索引改写后语义等价；做不到时原样返回输入。
- **索引从哪来**：只通过 FILL 语句（以及它需要的 DDL）注册。FILL 可能是上游 view selection（自动索引）的结果，也可能是手写的。**建哪些索引、索引在目标系统上是否划算，都不由中间件决定。**
- **验收标准**（2026-11-14 用户要求）：IncGQ-experiment-docs 中的 Neo4j、Kuzu 查询可以被改写，方言转换正确，改写正确。"正确"以真机结果为准：在 Kuzu 0.10 和 Neo4j 5.26 上执行原查询和改写结果，两者一致。
- **不做**：增量维护（用户决定暂不实现）；索引选择；针对目标系统的代价判断。

### 1.2 思路

0. **改写范围是单个 QueryPart。** 依据 wiki《QueryPart Binding 与 Index 定义》和《QueryPart 索引改写的语义等价定理与 Lean 形式化》：查询在顶层 `WITH` 处切成 QueryPart（末段以 `RETURN` 结束），第 i 段读取上一段投影出的 binding B_{i-1}。定理 1 对任意输入 B 成立，所以逐段改写、再把改写后的段拼回原文，整条查询仍然等价。中间件对每个 QueryPart 单独尝试改写（3.12）。
1. **复用 NeuG 的改写器，不重写改写逻辑。** 内部统一用 NeuG 方言。改写由 NeuG 编译器完成（binder 加 RewriterV4），在一个只含 schema、FILL 被模拟成 READY 的内存目录里进行，不打开任何数据库。
2. **方言转换放在两端。** 输入端由 frontend 把源方言翻译成 NeuG 方言（schema 映射加语法翻译）；输出端由 printer 和 dialect 把改写后的语句打印回源方言。两端的语义差异都要显式处理，比如 Neo4j 关系唯一性、trail 与 walk、Kuzu 变长路径上界，见第 3 节。
   - **转换成功即可绑定（2026-11-16 用户要求）**：每个 QueryPart 要么转换失败（不改写，原样透传），要么转换出的内部 Cypher NeuG 一定能绑定（3.16）。
   - NeuG 本身不支持的写法按设计不改写，不算中间件的限制；NeuG 的内部错误则要在翻译中避开。
   - 建索引语句同样需要方言转换：注册时 FILL 由目标方言翻译成内部方言；物化时翻译成目标系统的 DDL 和写入语句（3.9、4.4）。
3. **改写选择是确定的。** 改写用哪个 FILL、覆盖查询的哪一部分，完全由 RewriterV4 决定。不提供统计信息时（默认），NeuG 不做代价比较（`cost_compared=false`）；这时 RewriterV4 按固定规则选：FILL 按模式边数从多到少、再按名字排序，第一个通过正确性检查的就采用。中间件不额外做选择。
4. **每次输出都验证。** 输出的 Cypher 会再翻译回内部方言，在不带索引的 baseline 目录里重新 bind，与 RewriterV4 选中的语句做规范化结构比对；没读到 FILL 载体的改写也不输出。任何一步失败都回退原文（安全契约）。
5. **索引在目标系统上物化。** 每个 FILL 翻译成目标方言的建表、重置和写回语句，由中间件生成。真机验证用的索引就是这样建出来的。
6. **验收方法。** 以 NeuG 自己的 baseline 查询能被改写的范围为参照：NeuG 能改写而 Kuzu/Neo4j 版本改写不了的，都是中间件要补的缺口（第 6 节）。NeuG 本身也改写不了的，是 RewriterV4 的能力问题（第 7 节）。

### 1.3 能力

| 项 | 状态 | 说明 |
| --- | --- | --- |
| Cypher 输入 → 改写后的 Cypher 输出 | ✅ | 支持 NeuG、Kuzu、Neo4j 三种方言（profile），输入和输出使用同一方言 |
| 运行时通过 FILL 注册索引 | ✅ | `execute("FILL ...")`、`register_index()`、HTTP `POST /statement`；缺失的载体列或派生关系表自动推断并补齐 |
| 运行时 DDL | ✅ | `ALTER TABLE ADD/DROP`、`CREATE NODE/REL TABLE`、`DROP TABLE`，只作用于中间件自己的 schema |
| 删除索引 | ✅ | `drop_index(id)`、HTTP `DELETE /index/<id>` |
| 可移植性 | ✅ | 不改 NeuG 内核；只依赖已编译好的 `libneug.so` 和 PyYAML；对外提供 Python API、CLI、HTTP/JSON；新系统通过 profile（mapping + dialect + frontend）接入 |
| 索引构建（在目标系统中物化） | ✅ | `build_statements()` / `cico build` / `GET /build`：把每个 FILL 翻译成目标方言的 DDL、重置、写回语句；已在真实 Kuzu 和 Neo4j 上建出全部 Hetionet/BI/LSQB 索引 |
| 目标系统真机验证 | ✅ | `tests/target_validation.py`：用中间件自己的构建语句在 baseline 库副本上建索引，再比较原查询与改写查询的结果 |
| **增量维护** | ⏸ **暂不实现**（2026-11-14 用户决定） | 原设计保留在第 9 节，供以后参考 |

**安全契约：** 输出要么在语义上等价于输入，要么就是输入原文。只要有任何一步无法证明，就回退原文。

## 2. 架构

```
           ┌──────────────── middleware/ ────────────────────────────────────────────┐
 Cypher ─► │ queryparts         按顶层 WITH 切段；第 i 段的探针 = 桩(S_{i-1}) + 第 i 段 + RETURN 输出列 │
           │                    （下面各步对每个探针执行；只取回第 i 段，拼回原文）            │
           │ frontend(profile)  源方言 → 内部 NeuG 方言（Neo4j：schema 映射 + 语法翻译） │
 FILL/DDL  │ schema.py          中间件 schema（NeuG YAML），DDL 和推断出的载体列写在这里   │
           │ engine (C++)       两个内存编译目录：indexed（FILL 模拟 READY）、baseline     │
           │   rewrite: ClientContext::prepare（RewriterV4；无 stats 时不做代价比较）     │
           │          → 复现选中的 BoundStatement → Bound IR v2                          │
           │ printer + dialect  Bound IR → 目标方言 Cypher                                │
           │ verifier           输出 → frontend → baseline 重新绑定 → 规范化结构比对 + 输出列契约 │
           │ 索引读取检查        选中方案必须真正读取了 FILL 载体，否则返回原文              │
           └─────────────────────────────────────────────────────────────────────────────┘
```

## 3. 关键设计决定及原因

1. **复现选中方案，而不是修改内核。** `PreparedStatement` 不暴露最终选中的 BoundStatement。NeuG 自带的 `BoundRewriteTrace` 只给出了 v1 JSON，缺少 UNWIND、变长路径上下界等打印必需的信息。libneug 的 glog 不对外导出，所以也无法通过日志拿到搜索轨迹。

   引擎的做法是：沿着候选路线重新 bind，依次尝试初始路线、variadic split，以及按 `setIndexRewriteReplay` 做的前缀 BFS（上限 512 次 bind），直到候选语句的 v1 JSON 与 NeuG 自己的 trace **逐字节相等**为止。这样拿到的就是 C++ 语句对象本身，再用中间件自己的 `bound_ir.h` 输出完整的 v2 IR。在所有 BI 用例上，这一步都只需 1 次 bind。

2. **验证守卫中的规范化**，每一项都不改变语义：
   - 实体 uid 统一换成打印出的变量名。
   - 同一个 MATCH 内的图模式比较时不考虑顺序。
   - 分组键和聚合列表按集合比较（RewriterV4 会留下重复项）。
   - 对之前 WITH 投影列的引用统一写成 `{ref: alias}`。
   - 关系子表只保留与端点标签兼容的那些（例如 `c:COMMENT` 不可能匹配 `HASCREATOR_POST_PERSON`）。
   - 未起别名的输出列，比较时去掉内部 uid 前缀。
3. **用户写的变量名原样保留**，即使不同实体同名。合法 Cypher 中同名变量只可能出现在互不重叠的作用域里，例如 `WITH collect(z) AS zs UNWIND zs AS z`。
4. **必须真正读取了索引才输出改写。** RewriterV4 还会做一些与索引无关的规范化（例如 bi-2、bi-14）。重新打印这类语句没有收益，反而增加风险：bi-2 中 `duration` 被绑定成 `TO_INTERVAL`，后者在 NeuG 执行阶段不被支持。
5. **schema 由中间件自己维护。** NeuG 的 DDL 实际在存储层执行，编译目录再从 YAML 加载。中间件把 DDL 应用到自己的 YAML 上，然后重载引擎，并重新注册所有 FILL。
6. **Neo4j 采用 mapping 驱动的翻译**（下文 7–11 是本轮新增的决定）。 依据是 `Neo4j/scripts/import/import_bi.py` 的导入方式：Place 节点按 `type` 打动态标签，不保存 `type` 属性；Comment/Post 额外带有 Message 标签。具体映射为 `(:Country)` ↔ `PLACE {type:'Country'}`（输出时写成 `x:Country`），`:Message` ↔ `COMMENT|POST`，`IS_LOCATED_IN` ↔ `ISLOCATEDIN|ORGANISATION_ISLOCATEDIN`。NeuG 和 Neo4j 的列表下标都从 0 开始，已实测确认。

7. **schema 取自目标系统。** 同一数据集在不同系统里的属性类型可能不同（NeuG 的 IC 库把 `creationDate` 存成 INT64，Kuzu/Neo4j 存的是 TIMESTAMP），绑定必须用目标系统的类型。`python -m cico schema --kuzu DB --reference graph.yaml` 直接从 Kuzu 库读出 schema；Kuzu 不报告关系基数，所以从同名的 NeuG schema 借用（缺省按 MANY_TO_MANY，偏保守）。
8. **Neo4j 的关系唯一性。** Neo4j 要求同一个 MATCH 子句里的关系绑定到互不相同的边（relationship isomorphism），Kuzu/NeuG 没有这个限制（homomorphism）。中间件在两个方向都把差异显式化：
   - 输入（Neo4j → 内部）：同一子句中可能绑定同一条边的定长关系之间加上 `r1 <> r2`；表集合不相交、或 WHERE 里已有端点不等式时不加。
   - 输出（内部 → Neo4j）：必选 MATCH 拆成每个关系一个 MATCH 子句（跨子句没有唯一性约束）；OPTIONAL MATCH 无法拆分，只有能证明其中的关系不可能落到同一条边时才输出，否则回退原文。
   - 变长关系：Neo4j 是 trail、内部是 walk，无法用谓词表达；两者在无环关系类型（如 REPLY_OF）上一致，这是一个**前提假设**。
   - 这个问题是真机验证发现的：修正前 Neo4j 上 Hetionet 有 6 条结果不一致（构建语句 `MATCH (c1)-[:X]-(g)-[:X]-(c2)` 在 Neo4j 中少算了复用同一条边的路径）。
9. **参数特化。** FILL 常常针对固定参数值构建（FinBench 的时间窗口、阈值）。带参数的查询没有选中索引时，中间件把参数值内联为字面量再试；成功后逐个把参数放回去，只保留改写真正需要的那些（`Result.specialized`）。这样的结果只对这组参数值有效，缓存键里也包含参数值。
10. **索引物化（M2）。** 每个 FILL 生成三类目标方言语句：DDL（Kuzu 用 `ALTER TABLE ... ADD`、`CREATE REL TABLE`；Neo4j 不需要）；重置（把载体值恢复为 DDL 中的 DEFAULT，派生边全部删除），这样可以重复构建，Neo4j 上不在 FILL 结果里的节点也能读到与 NeuG 相同的默认值；写回（把 FILL 源查询打印成目标方言，末尾改成 `WITH 键实体, 值`，再接 `SET` 或 `CREATE`）。如果键是端点的主键，就直接携带节点本身，不再按 id 查找（Neo4j 的 LSQB 库没有 id 属性也能用）。几处目标系统差异：
    - Kuzu 的 `CREATE` 不接受绑定到标签并集的端点：按子表逐对生成语句，用 `label()` 过滤，再按主键在单一标签下重新绑定。
    - Neo4j 的大批量写入：用 `CALL {...} IN TRANSACTIONS`。新建的边落在源查询正在遍历的节点上时，会出现 outdated read：如果源查询的每个投影都以某个键节点分组，就按该节点的标签扫描分区执行；否则先 collect 再写。
11. **Kuzu 的变长上界。** Kuzu 拒绝超过 `var_length_extend_max_depth`（默认 30）的上界，`*l..` 表示 l..30。Kuzu 方言把无界路径输出为 `*l..`，等价性的前提是路径长度不超过 30（LDBC 的回复树满足）。

12. **逐个 QueryPart 改写（`scope='part'`，默认）。** 对第 i 段构造一个探针查询：

    ```
    <桩：只绑定 S_{i-1}>  <第 i 段原文>  RETURN <第 i 段的输出列>
    ```

    - **桩**按上一段输出的 schema 生成：先把第 1..i-1 段加上 `RETURN <输出列>` 在 baseline 目录里 bind，得到每列的类型；节点列写成带标签的 `MATCH`，节点列表用 `collect`，标量用带类型的参数 `$cico_in_<列名>`。桩不携带 L_{i-1} 的任何信息，所以 RewriterV4 只能依据 S_{i-1} 决定改写，这正是定理的前提（对任意 B 成立）。
    - 桩的 `WITH` 末尾加 `SKIP 0`。它在任意 binding 多重集上都是恒等变换，作用是让桩保持为独立的 QueryPart：不加时，RewriterV4 会把桩并入第 i 段，并且在 OPTIONAL MATCH + count 的形状上返回畸形语句（7.6）。
    - 改写结果只取第 i 段。桩和末尾的 `RETURN` 必须与不带索引时的打印结果完全一致，否则放弃。唯一的例外是：索引完全覆盖了以输入变量为锚点的模式时，RewriterV4 会把存在性过滤（如 `x.iv_count <> 0`）挂到桩的 `WITH` 上。这个过滤作用于第 i 段的输入行，因此移进第 i 段，写成 `WITH <输入列> WHERE ...`。
    - 探针只声明它实际引用的参数（binder 拒绝未使用的已声明参数）。
    - 没被改写的段保持原文，所以一段里有 NeuG 不支持的语法，不再拖累其他段。例如 Kuzu bi-17 的第 3 段有字符串 `+`，第 2 段仍然被改写。
    - `scope='query'` 保留旧行为（整条语句交给 RewriterV4），用于对照。在全部 docs 查询上，逐段方式的覆盖不少于整条方式（6.1）。
13. **Neo4j 子类型标签折叠进模式。** 内部的 `PLACE {type:'City'}` 原先打印成 `(x:Place) ... WHERE x:City`。Neo4j 估算多标签节点时把各标签选择率相乘，于是 bi-3 被估成 0 行，规划成"对每个 message 从 Country 重新展开"的 Apply 计划（实测 1.9s → 103s）。现在同一 MATCH 新声明的节点若有子类型谓词，直接打印成 `(x:City)`，并去掉该谓词。两种写法表示同一组节点：映射本来就把输入的 `(:City)` 读作 PLACE 加 `type='City'`。修改后 bi-3 在 Neo4j 上比原查询快（6.2）。
14. **Neo4j 关系唯一性改用 `id(r1) <> id(r2)`。** 两个关系的表集合不同时（例如 `HASCREATOR` 一个从 `COMMENT:POST` 出发、一个从 `COMMENT` 出发），NeuG binder 在比较 `r1 <> r2` 时触发 `types.cpp` 断言。比较内部 id 语义相同，而且能绑定。打印回 Neo4j 时，`_ID` 属性仍打印成变量本身，即 `r1 <> r2`。
15. **引擎内存上限与自动重启。** 引擎进程的地址空间上限默认 8 GB（`CICO_ENGINE_MEMORY_GB`，0 表示不限）。超限时引擎在请求内失败退出，中间件返回原文，并用相同的 schema 和 FILL 重启引擎（`engine_restarts` 计数）。起因：Neo4j bi-17 的翻译让 NeuG 规划器的连接枚举涨到 105 GB（7.7），在共享机器上不可接受。另外，参数特化现在也会在 `engine_error` 时尝试：例如 `duration(CAST($delta AS STRING) + ' hours')` 只有内联成字面量后才能绑定。

16. **方言转换成功即可绑定（用户要求，2026-11-15）。** 每个 QueryPart 的转换要么失败（不改写，原样透传），要么产出 NeuG 一定能绑定的内部 Cypher。
    - 实现：`_to_internal` = frontend 翻译 + 参数内联/常量折叠 + `cico/surface.py` 的 NeuG 语言检查。
    - 检查内容：函数目录、列表推导、map 字面量/投影、切片、CALL、字符串拼接、表名/属性名；NeuG 自己的不支持结论（解析失败、函数/重载不存在、`Not supported`）也算转换失败（`neug_rejects`）。
    - NeuG 内部错误必须在翻译中避开：
      - 实体比较在表集合不同时改为 `id()`（types.cpp 断言）；
      - Neo4j 未标注节点按相邻关系补标签（`x IN list` 的 vector_cast 断言）；
      - 同表关系的唯一性写成 `r1 <> r2`（多个 `id()` 比较时的 `unordered_map::at`）。
    - 新增的 Neo4j 转换：
      - 删除 `USING` 提示；
      - `round(x)` → `round(x, 0)`（已在 Neo4j 上实测等价）；
      - `shortestPath` / `allShortestPaths` → `* SHORTEST` / `* ALL SHORTEST`；
      - 非无环类型的变长关系按 `TRAIL` 翻译（映射的 `acyclic_rel_types`，目前只有 REPLY_OF）；
      - 变长关系与同子句其他关系的唯一性无法表达时判为不支持；
      - 标签析取 `A|B`；
      - LSQB 属性改名 `id` ↔ `<Label>Id`（映射的 `properties`）。
    - 回译校验把 `a = b` 与 `id(a) = id(b)` 规范化为同一形式。
    - 全量结果（`~/Downloads/IncGQ中间件全部查询改写过程.md`，由 `middleware/tests/dump_all_queries.py` 生成）：
      - 176 条只读查询中，146 条全部 QueryPart 可转换，7 条部分可转换，23 条不可转换；
      - 转换成功但无法绑定的 QueryPart 为 0 个；
      - 66 条改写，输出与改动前逐字相同。

17. **声明约束（用户 2026-11-18 同意）。** 原查询与索引只在数据模型的完整性约束下等价时（BI10/11 的地点类型、BI17 的回复树），约束写在 profile 的 `constraints:`（`cico/constraints.py`），种类为 `at_most_one`、`at_least_one`、`endpoint_labels`、`unique_property`。
    - 约束是**个别改写步骤**的前提，不是 QueryPart 定理本身的前提；改写结果在 `Result.premises` 列出实际用到的约束（含探针过程中用到的，可能偏多）。
    - 每条约束生成一条只读 Cypher 统计违反数（`IndexMiddleware.constraint_checks()`、`tests/constraint_checks.py`），信任依赖它的改写之前应为 0。SF1 实例 10/10 成立。
    - 类型标签（City、Country）是表上的谓词，不能由表成员关系推出（City 的约束不适用于任意 PLACE），只能由节点上写明的标签或 `endpoint_labels` 推导得到。
18. **时间字面量规范化（BI2）。** FILL 按绑定后的 TIMESTAMP 字面量为窗口谓词建键。`datetime('...T...Z')`、带时区的驱动值、`$date + duration({days: n})` 与 FILL 中的 `TIMESTAMP('YYYY-MM-DD hh:mm:ss')` 是同一瞬间，但拼写不同，匹配失败。现统一为 UTC 规范字面量，并折叠定长单位（周、天、时、分、秒）的加减；月、年（日历运算）、命名时区（夏令时）和非加减上下文不折叠。`cico/normalize.py`。
19. **关系载体存在性（BI14，`cico/edge_carriers.py`）。** RewriterV4 不能使用源中带 QueryPart 边界的关系载体（连 FILL 自身的源查询都不改写），因此作为独立的本地语义规则。对形如 `OPTIONAL MATCH P WITH DISTINCT items(ρ, w IS [NOT] NULL)` 的段：
    - $(M_P)$：用 NeuG binder 绑定 P 与 FILL 源中的 P'，在两个锚点固定的条件下做带标签图同构（节点、关系表集合相等，端点与方向保持；关系子表先按端点裁剪，与 canonical.py 相同）。
    - $(M_F)$：去掉由无环性推出的关系不等式（两条关系相等会使某条无环关系成为自环）后，两侧都没有过滤。
    - $(L)$：存在性取 `count > 0`；FILL 值为 (u→v 边数) × |W_P'|，>0 当且仅当见证非空。
    - 改写段在本段内匹配 `(a)-[k:R]-(b)`，按 `startNode(k)` 选 fwd/rev 列；没有 R 边时回退到原来的 `EXISTS { MATCH P }`。DISTINCT 投影且只用 `IS NULL` 测试新鲜变量，保证两种写法产生同一组元组。RewriterV4 改写成功的段优先。
20. **变长关系唯一性的根路径证明（BI17，`cico/neo4j.py`）。** 原先同一子句中变长关系与同类型关系的唯一性一律判为不支持。现在：无环类型 T、每个源至多一条出边（声明约束）、终点表没有出边时，变长路径是起点到根的唯一路径；两条根路径共边 ⇒ 根相同，定长边落在根路径上 ⇒ 根相同或成环。由此得到的节点相等沿声明为 at_most_one 的定长关系传播（如 CONTAINER_OF：帖子相同 ⇒ 论坛相同），与本段必选 MATCH 的 WHERE 中的不等式矛盾时，Neo4j 的唯一性约束不删去任何会被保留的行，可以不表达。OPTIONAL 子句的 WHERE 不过滤行，不参与证明。
21. **关系载体替换变长路径（BI17 回退阶段，`cico/path_carriers.py`）。** 只在常规流程不改写时运行。匿名、未被引用的 `(m)-[:T*l..u]->(p)` 与 DISTINCT 关系 FILL（如 ROOT_POST）类型、方向、上下界一致，端点表在 FILL 范围内时，替换为 `(m)-[:R]->(p)`。
    - $(L)$ 要求 $|G_k|=1$：两节点间至多一条路径，由无环与“至多一条出边”保证。
    - 替换后同一子句里的多条载体关系会被 Neo4j 新增唯一性约束；必须用 20 的矛盾推理证明它们不可能相等，否则该子句不替换。
    - NeuG 规划器在 BI17 这类大模式上耗尽内存（7.7），此时理智性绑定检查记为 `neug_bind: skipped`，只做转换与 schema 检查；正确性由真实 Neo4j 比对补充。
22. **声明约束重试阶段（BI10，`cico/middleware.py` 的 `_relaxed`）。** 常规改写失败后，前端把由 `endpoint_labels` 推出恒真的类型标签换成基础标签（`(:City)` → `(:Place)`），类型沿定长单类型关系传播，并经 `collect(x) AS L`、`UNWIND L AS v` 传递（只对语句中只绑定一次的名字）。不在常规阶段做，以免改变 BI3 等已有输出形状（3.13）。
    - 重试结果若读取**非聚合投影载体**（如 `countryName`），定理不直接覆盖，必须由声明约束证明每个键恰好一个见证：至多一个（值有定义、行重数不变）且至少一个（所有键都在 Read 中，列的 DDL 默认值 `''` 不会冒充缺失分组）。否则拒绝。
23. **键身份（BI11，重试阶段，`_key_identities`）。** 投影载体只能替换以**隐藏**顶点结尾的地点链（$(M_P)$(ii)）；BI11 把同一个 Country 节点跨段携带。用 `unique_property` + `required_property`（唯一且非空的键）改写节点身份：
    - T1：链末端 `(v:L {p: E})`（E 为参数或字面量）换成新变量 `(k:L {p: E})`；v 在本段只作为投影的裸项出现时，延后到投影之后按键重新绑定：`WITH [DISTINCT] 其余项 MATCH (v:L {p: E}) WITH [DISTINCT] 原投影`（RewriterV4 不接受含不连通节点模式的段）。v 在本段每一行上都是同一个节点，两种投影划分相同。v 在别处没有引用时不改写。
    - T2：前面子句绑定的 `(v)`，入边由 `endpoint_labels` 推出为 L，换成 `(k:L)` 并在本子句 WHERE 加 `k.p = v.p AND v:L`；原匹配中 v 必为 L，守卫不删行。
24. **DISTINCT 计数按键列表分区（BI4，`cico/distinct_partitions.py`）。** CALL 分支形如 `UNWIND L AS f MATCH P … RETURN g, count(DISTINCT x)`，FILL 按 (k1, k2) 存 `count(DISTINCT y)`。FILL 模式须在 f/g/x 固定下嵌入 P（$(M_P)$(i)），其余模式只接在 f、g 上（$(M_P)$(ii)），过滤不引用隐藏顶点（$(M_A)$(ii)）；声明约束证明 FILL 模式中 y→k1 是函数，于是各组互不相交，`count(DISTINCT)` 等于各组计数之和。改写对 f 先 `WITH DISTINCT`，对任意列表（含重复元素）成立；剩余模式与过滤放进 EXISTS。只在 RewriterV4 未改写该分支时运行。`FILL R()` 的列取关系表自身的属性。
25. **常量键绑定（BI8，重试阶段，`key_constants`）。** 变量 v 只由必选的顶层 `MATCH (v:L {p: E})` 绑定一次，且其间每个 WITH 都以裸项投影 v 时，v 在每一行上都是唯一键值为 E 的节点；`size([...])`/`COUNT{}`/`EXISTS{}` 区域里的 `(v)` 换成 `(k:L {p: E})`，区域不再以 v 为输入接口，按名称过滤、隐藏 Tag 的计数载体满足接口条件。另外，相关计数规则的见证优先取 `size([... | x])` 投影的新鲜节点（与 FILL 计数的变量一致；任何新鲜节点的计数都相同）。

## 4. 引擎协议（JSON lines，protocol 2）

| op | 字段 | 返回 |
| --- | --- | --- |
| `load` | `schema`（YAML 文本）、`stats?` | 重建两个目录并重新注册全部候选；返回 `failed_candidates` |
| `register` | `id`、`fills[]` | 注册的条目（全部成功或全部回滚） |
| `drop` | `candidate` | 删除的条目数 |
| `rewrite` | `cypher`、`parameter_types?` | `changed`、`route`、`ir`（v2）、`columns`、`index_entries`、代价字段 |
| `bind` | `cypher`、`catalog`（baseline/indexed） | `ir`（v2）、`columns` |
| `indexes`、`ping` | – | – |

请求关联字段是 `rid`。所有 NeuG 输出都重定向到 stderr。

## 5. 实现现状（2026-11-18）

| 模块 | 状态 | 文件 |
| --- | --- | --- |
| 引擎 v2（运行时目录、选中方案复现、IR v2）；地址空间上限 + 自动重启 | ✅ | `middleware/engine/`、`cico/engine.py` |
| **逐 QueryPart 改写**（桩 + 探针 + 拼接，3.12） | ✅ 默认 | `cico/queryparts.py`、`cico/middleware.py` |
| 打印器 v2：MATCH、OPTIONAL、WHERE、WITH、UNWIND、变长路径、聚合、ORDER/SKIP/LIMIT、CASE、列表与结构体、CAST、`label()`、EXISTS/COUNT 子查询、UNION；`final_with` 模式（供物化使用） | ✅ | `cico/printer.py` |
| 验证守卫 v2（新增：相邻必选 MATCH 合并、谓词按合取项集合比较） | ✅ | `cico/canonical.py` |
| 中间件 schema 与 DDL（记录 DEFAULT）；FILL 载体推断（含 REL 组子表 `GROUP_SRC_DST`）；重复的同一 FILL 幂等 | ✅ | `cico/schema.py`、`cico/middleware.py` |
| 目标 schema 提取（Kuzu） | ✅ | `cico/introspect.py`、`python -m cico schema` |
| profile：neug / kuzu / neo4j；Neo4j mapping：LDBC SNB（BI/IC）、LSQB、FinBench、Hetionet | ✅ | `cico/profiles.py`、`cico/frontends.py`、`cico/neo4j.py`、`profiles/*.yaml` |
| 输入规范化：`coalesce`→CASE、链式比较、`duration({hours})`/ISO 时长、端点驱动的关系表裁剪、Neo4j 关系唯一性、实体比较 `id()`、`USING`、`round(x)`、shortestPath、TRAIL、标签推断与析取、属性改名 | ✅ | `cico/normalize.py`、`cico/neo4j.py` |
| **NeuG 语言检查：转换成功即可绑定**（3.16） | ✅ 全量 0 例转换成功但不能绑定 | `cico/surface.py`、`cico/middleware.py` |
| 参数特化 | ✅ | `cico/middleware.py` |
| 索引物化（M2） | ✅ | `cico/materialize.py`、`cico build`、`GET /build` |
| NeuG FILL 文件与持久化重载 | ✅ 规范注册与计划指纹、依赖传播、成功回执、只物化待构建项、失败恢复；含隐藏聚合状态 | `cico/index_files.py`、`neo4j-fill-file-reload.md` |
| CLI、HTTP 服务 | ✅ | `cico/__main__.py`、`cico/server.py` |
| 单元与集成测试 | ✅ 110/110；BI11 80/80、BI4 30/30、BI8 30/30 随机夹具（真实 Neo4j，事务回滚）；此前 13/13 理论小图、6/6 文件重载小图；2026-11-18 另有 BI14 60/60、BI17 60/60 随机夹具（真实 Neo4j，事务回滚）与 BI10 SF1 3/3 逐行一致 | `tests/test_*.py`、`tests/theory_rewrite_acceptance.py`、`tests/index_file_acceptance.py`、`tests/carrier_existence_acceptance.py`、`tests/path_carrier_acceptance.py`、`tests/bi10_sf1_compare.py` |
| 迁移后的可重现构建与入口 | ✅ 在当前源码应用补丁并记录来源哈希，避免覆盖上游更新；统一引擎/执行器查找 | `engine/prepare_patch.py`、`engine/CMakeLists.txt`、`cico/engine.py` |
| Neo4j BI 已注册索引匹配修复 | ✅ 类型化时间谓词键、OPTIONAL 分区规范化、完整 DISTINCT 分组；原始查询 8/15 | `neo4j-bi-rewrite-fix.md`、`patches/neug-*.patch` |
| 不透明过程前缀的类型推断 | ✅ 声明式过程契约、真实前缀绑定和 schema 域闭包检查；仅生成类型探针 | `cico/queryparts.py`、`profiles/ldbc-snb-neo4j.yaml` |
| 相关图计数和存在性表达式 | ✅ 完整 anchor 接口、单输入聚合还原、NULL 扩展及回译验证；不要求外层整句可绑定 | `cico/scalar_subqueries.py`、`neo4j-theory-hardening.md` |
| 有限记录输入 CALL | ✅ 保留字段对应关系，类型及表示一致的互斥守卫，保留外层重复行和未匹配分支 | `cico/binding_values.py`、`cico/subqueries.py` |
| 输入 binding 探针隔离 | ✅ 辅助名字不与用户列/参数冲突，正确引用标识符，按词法识别参数和检查泄漏 | `cico/queryparts.py`、`cico/syntax.py`、`cico/middleware.py` |
| 文档查询的等价改写版本（覆盖层）与真机等价检查 | ✅ Kuzu bi-4、LSQB q4/q8；**待用户确认是否写回文档**（6.3） | `query_edits/`、`tests/equivalence_check.py` |
| Hetionet 语料回归 | ✅ 191 条改写全部通过验证，0 条不一致（共 312 条） | `tests/corpus_check.py` |
| 随机图 e2e（NeuG） | ⚠️ 10/12，剩余 2 条是上游问题（见 7.4） | `tests/e2e_neug.py` |
| 实验文档全部查询的覆盖率 | 见 6.1 | `tests/workloads.py`、`tests/workload_coverage.py` |
| 真机验证（Kuzu 0.10、Neo4j 5.26） | 见 6.2 | `tests/target_validation.py`、`tests/neo4j_clone.sh` |
| NeuG 内核修复：ROOT_POST 组 FILL 覆盖、组名查表（7.1、7.2） | ✅ 补丁待合入（未提交） | `patches/neug-rootpost-group-coverage.patch`，分支 `cico/rootpost-variadic-fix` |
| 时间字面量规范化、定长 duration 折叠（3.18） | ✅ BI2 | `cico/normalize.py`、`tests/test_temporal_literals.py` |
| 关系载体存在性规则（3.19） | ✅ BI14 | `cico/edge_carriers.py`、`tests/test_edge_carriers.py` |
| 声明约束与目标库检查（3.17） | ✅ SF1 10/10 成立 | `cico/constraints.py`、`profiles/ldbc-snb-neo4j.yaml`、`tests/constraint_checks.py` |
| 变长关系唯一性的根路径证明（3.20） | ✅ | `cico/neo4j.py`、`tests/test_declared_constraints.py` |
| 关系载体替换变长路径（3.21，回退阶段） | ✅ BI17 | `cico/path_carriers.py` |
| 声明约束重试：类型推导、投影载体恰好一个见证（3.22） | ✅ BI10 | `cico/neo4j.py`、`cico/middleware.py` |
| 键身份 T1/T2（3.23） | ✅ BI11 | `cico/neo4j.py`、`tests/bi11_fixture_acceptance.py` |
| DISTINCT 计数按键列表分区（3.24） | ✅ BI4 | `cico/distinct_partitions.py`、`cico/subqueries.py`、`tests/bi4_fixture_acceptance.py` |
| 常量键绑定 T3、计数见证选择（3.25） | ✅ BI8 | `cico/neo4j.py`、`cico/scalar_subqueries.py`、`tests/bi8_fixture_acceptance.py` |
| NeuG FILL → Neo4j BI 状态矩阵与进度文档生成 | ✅ 12/17 | `tests/neug_fill_bi_status.py`、`tests/render_bi_status.py`、`neo4j-bi-unrewritten-status.md` |
| 增量维护 | ⏸ 暂不实现 | – |

## 6. IncGQ-experiment-docs 中 Kuzu / Neo4j 查询的结果

**测试条件：**
- 索引：每个 workload 的 NeuG FILL 脚本（`NeuG/queries/index/<workload>/**/index.cypher`）在运行时注册到中间件，全部注册成功。
- schema：从 dwk 的 SF1 Kuzu 库提取（FinBench 取 SF10 库）。
- 参数：取自 `parameters/*/experiment-benchmark.csv`，转换方式与文档中的 Kuzu runner 相同。
- 改写：不带统计信息，由 RewriterV4 的固定规则选择（见 1.2）。
- 引擎：带 7.1/7.2 补丁的 `incgq_rewrite_engine_patched`（迁移后位于 `middleware/engine/bin/`，未设 `CICO_ENGINE` 时默认使用）。
- 范围：逐 QueryPart 改写（3.12）；Kuzu bi-4、LSQB q4/q8 使用 `query_edits/` 中的等价版本（6.3）。

### 6.1 覆盖率（被改写的查询）

| workload | 有 NeuG 索引的查询 | Kuzu | Neo4j |
| --- | --- | --- | --- |
| hetionet | 26 条元路径计数 | **26/26** | **26/26** |
| ldbc-bi | bi-3/4/5/6/9/11/13/14/17 | bi-3、bi-4（6.3）、bi-5、bi-9、bi-17 | bi-3、bi-5、bi-9 |
| lsqb | q1/2/4/5/6/7/8 | q1、q4（6.3，只改写 Post 分支）、q8（6.3） | q1、q4、q8 |
| ldbc-ic | ic5、ic14 | – | – |
| finbench | tcr6/7/9/10/11/12/13、tsr2–5 | – | – |

括号里的 `p2` 等表示只有该 QueryPart 被改写：bi-5（两个系统）和 Neo4j bi-3 改写第 2 段；Kuzu bi-17 改写第 2 段（第 3 段有字符串 `+`，保持原文；第 2 段不引用 `$delta`，不需要参数特化，输出对任意参数值有效）；Kuzu bi-4 改写第 6 段。逐段方式与整条方式（`--scope query`）相比没有丢失任何改写。

**其余查询未能改写的原因：**
- **NeuG 自己的 baseline 也不会被改写**，RewriterV4 匹配不上：LSQB q2/q5/q6/q7、bi-6、bi-14、全部 FinBench。文档里 NeuG 的 optimized 文本是人工改写过的形状，比如 FinBench 把两跳 join 拆成每侧一个 OPTIONAL MATCH 加 `WITH count`，tsr-3 先 `WITH dst, count(edge2)`。从 baseline 推出这些形状需要聚合下推或分解，超出了"改写器 + 方言转换"的范围。另外，FinBench 的聚合 FILL 即使用 NeuG optimized 文本，在当前 HEAD 上也只有 tsr-3 能改写（库内同样如此），原因见 7.3。
- **查询形状与 FILL 不同，且等价性依赖数据不变式**：
  - bi-11：Kuzu/Neo4j 写法带有 `cityA.type='City'`、`country.type='Country'` 谓词，而且跨阶段 join 的是同一个 `country` 节点；FILL 的源查询没有这些谓词，NeuG 的写法比较的是国家名。
  - bi-4（Kuzu）：`OPTIONAL MATCH ... WHERE rootForum IN topForums`，而 NeuG 写法是 `UNWIND`。
  - LSQB Kuzu 写法 q4/q8：外面包了一层 `MATCH (d:Person) WITH d LIMIT 1 RETURN count{...}+count{...}`。
  - ic5：`OPTIONAL MATCH` 并且按 forum 聚合，而 FILL 按 (forum, friend) 分组。
- **NeuG 能改写、但 Kuzu/Neo4j 写法不行的（中间件应负责的缺口）**：
  - bi-11、bi-13：查询带 `type='City'/'Country'`（Neo4j 为 `:City`/`:Country` 标签），并把 `country` 节点带到后续段；NeuG 的 `PERSON.countryName` FILL 没有类型过滤，按国家名而不是节点连接。只有在 LDBC 的数据不变式下（Person 只位于 City、City 只属于 Country、Country 名唯一）才等价，**严格语义下不等价**，因此没有改查询，见 8.2。
  - Neo4j bi-4：`CALL { ... UNION ALL ... }`；不含 CALL 的等价写法在 Neo4j 上慢 20 倍，已放弃（6.3）。
  - Neo4j bi-17：同一子句里两条 `REPLY_OF*` 与定长 `REPLY_OF`，Neo4j 要求它们的边互不相同，内部无法表达，现在在转换阶段判为不支持（3.16）；即使去掉这条约束，单个大 MATCH 也会让 NeuG 规划器内存爆炸（7.7）。
- **方言转换结果（2026-11-15 全量运行，3.16）**：

  | 系统 | 只读查询 | 全部 QueryPart 可转换 | 部分可转换 | 不可转换 | 改写 |
  | --- | --- | --- | --- | --- | --- |
  | Kuzu | 88 | 81 | 3 | 4 | 34 |
  | Neo4j | 88 | 65 | 4 | 19 | 32 |
  | 合计 | 176 | 146 | 7 | 23 | 66 |

  - 转换成功但 NeuG 无法绑定的 QueryPart：**0**。
  - 66 条改写的输出与改动前逐字相同，6.2 的真机验证结论不变。
  - 逐条原因见 `~/Downloads/IncGQ中间件全部查询改写过程.md` 的转换失败的 QueryPart表（由 `middleware/tests/dump_all_queries.py` 生成）。
- **中间件方言转换尚未覆盖（中间件的限制）**：
  - Neo4j `CALL {}` 子查询（bi-4、bi-6、bi-16、tcr-10）；
  - Neo4j 同一子句里变长关系与其他关系的唯一性（bi-17，需要列表谓词，见 3.16）；
  - 上一段输出为嵌套列表或结构体列表时无法构造桩（ic1 的最后几段）。
  - 已补上：`USING`、`round(x)`、`shortestPath` / `allShortestPaths`，以及 LSQB 的 `id` 属性改名（Neo4j q3 现在可转换）。
- **NeuG 本身不支持的写法，按设计不改写（不视为限制）**，下面是转换时给出的原因：
  - 函数不在 NeuG 目录中：`all()`（tcr-11、tcr-12）、`list_transform`（Kuzu ic14）、`shortestPath()`（Kuzu 版 tcr-3）、`WSHORTEST`（Kuzu bi-15、bi-19）；
  - 列表推导（FinBench tcr-1、tcr-2、tcr-5、tcr-8）；map 投影 / map 字面量（tsr-1）；`datetime({epochMillis})`（ic10）；
  - 字符串 `+`（Kuzu bi-17 第 3 段）；列表相加（Neo4j bi-8 第 2 段）；
  - NeuG 规划器明确不支持的 OPTIONAL MATCH 关联谓词（bi-13、Neo4j ic5 第 3 段）；
  - Neo4j 在搜索中应用的最短路谓词（tcr-3）；下界为 0 的最短路（ic14）；
  - `apoc` / `gds` 过程（bi-10、bi-19、bi-20）；
  - schema 差异（Neo4j tsr-8 的 `workIn`）。
  - 之前列为NeuG 内部错误的 bi-14、ic5，已通过翻译避开（实体比较、标签推断），现在都能绑定。
- 上面这些查询都**原样返回**，不会产生错误结果。

### 6.2 真机验证（结果是否一致）

流程：复制 baseline 库 → 执行中间件为目标方言生成的构建语句 → 对每条被改写的查询，用相同参数分别执行原查询和改写结果，比较结果多重集（浮点数保留 9 位有效数字）。

| 系统 / workload | 构建 | 结果一致 | 典型耗时（原 → 改写） |
| --- | --- | --- | --- |
| Kuzu / hetionet | 23/23 个 FILL | **26/26** | 4-02：15.7s → 0.03s |
| Kuzu / BI SF1 | 19/19 | **50/50**（bi-3/4/5/9/17 各 10 组参数） | bi-3：~24s → 0.16s；bi-4：~24s → 1.9s；bi-9：~27s → 0.2s |
| Kuzu / LSQB SF1 | 13/13 | **3/3** | q1：7.7s → 0.12s；q8：1.4s → 0.31s；q4：0.24s → 0.16s |
| Neo4j / hetionet | 23/23 | **26/26** | 4-02：12.7s → 0.30s |
| Neo4j / BI SF1 | 19/19 | **30/30**（bi-3/5/9 各 10 组参数） | bi-3：18.4s → 0.09s、41.5s → 0.04s、54.8s → 1.05s；bi-9：~1.0–1.7s → ~0.8–1.5s；bi-5 变慢，见下 |
| Neo4j / LSQB SF1 | 13/13 | **3/3** | q1：7.7s → 0.79s；q4：6.9s → 4.0s；q8：15.0s → 2.2s（热缓存；冷启动时首轮明显更慢） |

以上为 2026-11-15 逐 QueryPart 版本的结果（索引沿用之前用中间件语句建好的库）。上一版（整条语句改写）的 116 次比对也全部一致。

- Neo4j bi-3 之前改写后变慢（1.9s → 103s），原因是输出形状（3.13）：`(x:Place) ... WHERE x:City` 让 Neo4j 低估行数。改成 `(x:City)` 后，改写比原查询快。建哪些索引由上游的 view selection 决定、通过 FILL 注册进中间件；中间件只负责改写和方言转换，不判断索引在目标系统上是否划算。
- Neo4j bi-5 改写后比原查询慢（~0.02s → ~0.6s），结果一致；属于同类的输出形状问题，尚未分析（8.3）。
- Hetionet、BI 用的是 dwk 已导入的 SF1 baseline 库的副本（只读复制，原库未改动），见 `/mnt/sdb1/dwk/benchmark-work/README.md`。Neo4j 服务器是用 `tests/neo4j_clone.sh` 单独启动的副本（127.0.0.1:769x，用完即停）。

### 6.3 文档查询的等价改写（用户授权，2026-11-15）

用户允许：原始查询的写法无法在一个 QueryPart 内改写时，可以修改实验文档里的查询和人工索引，只要语义一致。做法与结论：

- 每个改写版本都先用 `tests/equivalence_check.py` 在真实系统上与文档原文比对（同参数，原文、改写版本、改写版本经中间件改写后的结果三者一致），再放进 `implementation/middleware/query_edits/`（与文档相同的相对路径）。`tests/workloads.py` 自动用它们替换文档原文（`CICO_QUERY_OVERLAY=''` 关闭）。
- **没有直接写回 IncGQ-experiment-docs**：exp-1 的 `manifest.json` 和 exp-6 的 `sf10-inputs.json` 用 sha256 固定了这些文件，`run_query_speedup.py`、`run_scalability.py` 会拒绝哈希不符的文件，而且已发表的 exp-1 结果是用原文测的。写回需要同时更新清单哈希并重测 baseline，请用户决定（8.1）。`query_edits/kuzu-edits.diff` 是对文档的补丁。

| 查询 | 原写法的问题 | 改写版本 | 等价性依据 | 真机比对 |
| --- | --- | --- | --- | --- |
| Kuzu bi-4 | 可改写的那段含 `OPTIONAL MATCH ... WHERE rootForum IN topForums`，NeuG 不支持这种相关谓词 | `UNWIND topForums AS topForum WITH person, topForum.id AS rootForumId OPTIONAL MATCH ...(rootForum:FORUM {id: rootForumId})` | 每行的 topForums 非空（person 来自 UNWIND topForums）；FORUM.id 是主键；随后只有 `count(DISTINCT message)`，按 forum 展开产生的重复不影响结果。Kuzu 不能把 UNWIND 出来的节点放进模式，所以用 id | 10/10 一致；baseline ~24s（原文 38–64s），改写 ~2s |
| Kuzu LSQB q4 | `MATCH (d:Person) WITH d LIMIT 1 RETURN count{Post 分支} + count{Comment 分支}`，改写落在 count{} 子查询里 | 第 1 段统计 Post 分支（`WITH count(*)`），第 2 段用 `OPTIONAL MATCH ... count(message)` 统计 Comment 分支（无匹配时为 0，与 count{} 相同），最后 `MATCH (d:Person) WITH ..., d LIMIT 1` 保持"Person 非空才有一行" | 逐分支计数与 count{} 相同；行存在性不变 | 一致；0.24s → 0.16s（只改写 Post 分支；OPTIONAL MATCH 计数 RewriterV4 不改写） |
| Kuzu LSQB q8 | 同 q4 | 用 `(message:Post:Comment)` 和关系表并集写成一个模式，`WITH count(*)`，再接 `MATCH (d:Person) ... LIMIT 1` | 每张关系表的端点标签固定，所以并集模式的匹配恰好是两个分支匹配的不相交并 | 一致；1.4s → 0.31s（改写版本的 baseline 1.3s，原文 2.0s） |

尝试过但放弃的：

- Kuzu q4 的并集写法（与 q8 相同）结果一致，但 Kuzu 处理标签并集很慢：baseline 1.7s → 31s，改写 64s，因此改用逐分支写法。
- Neo4j bi-4 去掉 `CALL {}` 的写法（`UNWIND topForums AS rootForum OPTIONAL MATCH ...`）结果一致，但 Neo4j 上 baseline 11–25s → 250–370s，改写也要 190–295s。没有采用（文件留在 `cico_work/rejected_edits/`）。

## 7. 上游（NeuG / RewriterV4）问题，均有最小复现

1. ✅ **已定位并修复（补丁待合入）**：ROOT_POST 变长路径改写从不生效（影响 bi-3、bi-4、bi-9、bi-17）。
   - **根因**：`c8996bd9`（2026-11-07）引入的 `orderedRecursiveCoverage` 要求端点标签的每个组合各有一个按标签分派的 FILL 条目，并只读取条目的 `getSrcTableId()/getDstTableId()`。ROOT_POST 是**一个** FILL 写入 REL 组，因此 (COMMENT, POST) 被认为没有覆盖。放行后，`getFillTableContext` 按名字查表又会抛异常。
   - **修复**：覆盖检查改为逐个子表判断，子表的端点对落在源模式标签乘积内即算覆盖；按名字查表时，组名返回 `nullopt`。
   - **测试**：新增 2 个单测。详见上一轮记录和补丁中的注释。
2. ✅ **已修复（同一补丁）：FILL 写入 REL 组的某个子表（LSQB `q5_index_Post_Tag`）后，所有查询的 prepare 都会抛 "q5_index does not exist in catalog"**，连不使用该索引的 q7、q8 也受影响。
   - **根因**：`hasOrderedDerivedRelEndpoints` 等 8 处辅助函数用 `getTableCatalogEntry(name)` 查 FILL 目标表，组名不是表；它们本来都写了 `nullptr` 检查，但该函数在找不到时会抛异常。
   - **修复**：新增 `tableByName`，`containsTable` 为假时返回 `nullptr`。`requiresCompleteAggregateRead` 在这种情况下按保守处理，返回 true。
   - **测试**：NeuG 相关测试二进制（约 480 个用例）的结果与 HEAD 一致；`index_rewrite_metadata_test` 145/145 通过。
3. **FinBench 聚合 FILL 在当前 HEAD 上不能改写**（只有 tsr-3 例外）：NeuG optimized 文本在库内同样不改写，经 trace 确认。
   - 拒绝点是 `validateRewriteCandidate` 中的 `requiresCompleteAggregateRead(...) && !completePairDistinctCountCandidate && !aggregateEndpointPresenceOnlyCandidate`。这条规则同样来自 `c8996bd9`，是有意的收紧（SUM/COUNT 列需要"完整读取"证明）。tcr 系列的 OPTIONAL MATCH + `WITH count/sum` 形状通不过已有的证明函数。
   - 这部分代码其他 agent 正在开发，本轮没有改动；需要为这类形状补充正确的证明。
4. M1 阶段已发现的存在性问题（随机图 e2e 中剩余的 2 条）：
   - 点属性下推在不完全的 MANY_ONE 链上去掉了内连接语义，而 FILL 对缺失分组写入的是 `''`。
   - REL 上的 COUNT 改写缺少 `<> 0` 存在性过滤。
5. Exp-6 的 bi-6 文本触发 `prepare` 中的 `unordered_map::at` 异常（SF1 schema）。

6. **RewriterV4 在"普通 WITH + OPTIONAL MATCH 计数"上返回畸形语句**（已绕开）。复现（ldbc-bi schema，注册 bi5 的 `replyCount` FILL）：

   ```
   MATCH (person:PERSON), (message:COMMENT:POST) WITH person, message, $p AS likeCount
   OPTIONAL MATCH (message)<-[:REPLYOF]-(reply:COMMENT)
   WITH person, message, likeCount, count(reply) AS replyCount
   RETURN person, message, likeCount, replyCount
   ```

   改写结果只有一个 part，其投影有 4 个表达式却只有 3 个名字，末尾的 `RETURN` 也不见了。前一个 `WITH` 带聚合或 `SKIP 0` 时正常。打印器拒绝这种语句，所以不会输出错误结果；中间件的桩通过加 `SKIP 0` 避开了它。
7. **NeuG 规划器在大模式上内存爆炸**（未修，中间件已加内存上限，见 3.15）。下面的语句仅 `prepare`（不带任何索引）就会涨到 100 GB 以上，栈位于 `Planner::planGetV` ← `planInnerJoin` ← `enumerateQueryGraph`，即连接顺序的 DP 枚举：

   ```
   MATCH (tag:TAG), (person1:PERSON)<-[:HASCREATOR]-(message1:COMMENT:POST)-[:REPLYOF*0..4294967295]->(post1:POST)<-[:CONTAINEROF]-(forum1:FORUM),
         (message1)-[:HASTAG]->(tag), (forum1)-[:HASMEMBER]-(person2:PERSON)<-[:HASCREATOR]-(comment:COMMENT)-[:HASTAG]->(tag),
         (forum1)-[:HASMEMBER]-(person3:PERSON)<-[:HASCREATOR]-(message2:COMMENT:POST),
         (comment)-[:REPLYOF]->(message2)-[:REPLYOF*0..4294967295]->(post2:POST)<-[:CONTAINEROF]-(forum2:FORUM)
   RETURN count(*)
   ```

   把两条变长路径换成单跳后，1.4 秒即可完成。这就是 Neo4j bi-17（单个大 MATCH）无法改写的原因；Kuzu bi-17 把同一模式分在几个 QueryPart 里，能改写。

   2026-11-18 补充：带上 Neo4j 关系唯一性的不等式后，即便两条路径都换成单跳 REPLYOF 或 ROOT_POST，甚至只保留第一个 MATCH，`bind` 在 8 GB 和 24 GB 上限下都会耗尽内存（约 6 秒、20 秒后退出）。中间件不再依赖 NeuG 规划这条语句：根路径证明解除转换阶段的拒绝（3.20），回退阶段在 Neo4j 文本上替换 ROOT_POST（3.21）。规划器问题本身仍需 NeuG 侧修复。

## 8. 下一步（认领时写上名字和日期）

**2026-11-18 起的优先项**（详见 [未改写 BI 查询进度](neo4j-bi-unrewritten-status.md)，按顺序）：

1. ✅ BI11（3.23）、BI4（3.24）、BI8（3.25）已完成，当前 12/17。
2. BI16 暂缓：只能替换 `cm`，好友 OPTIONAL 仍需现场计算，收益低；若要做，需把 T3 扩展到 CALL 内 MATCH 绑定的 tag，并证明 `date(x)=date(p)` 与 UTC 半开区间等价。
3. 【需用户决定】BI19/BI20 是否把 `bi-*-create-graph.cypher` 纳入改写（改写 GDS 投影字符串；BI19 需 `>0` 过滤保持边集，BI20 需声明 STUDY_AT 只指向 University）。
4. BI15/BI15w 不建议（GDS 字符串内嵌改写 + 专门的权重等价证明）。

2026-11-18 已实现 NeuG FILL 文件驱动的持久化加载，入口见 [文件注册与重载](neo4j-fill-file-reload.md)。接续保持编译后指纹和真实构建回执的区分，优先补齐 nullable COUNT 的独立分组存在性证明，再解除新文件目录的保守拒绝；不要仅凭“已注册”跳过物化。新的 NeuG 文档脚本审计与旧 Neo4j 15 组查询矩阵是不同口径。

**当前优先范围**：按用户 2026-11-17 的最新确认，接续开发先遵循
[`Neo4j BI 已有索引交接`](neo4j-bi-handoff.md) 中的 15 组索引清单和接续顺序。
下面的历史功能清单不自动扩展本次任务范围。

本轮已增加相关标量计数、有限记录 CALL 和外层列表类型推断。接续先按理论核对 BI8/16 的 Tag 接口及剩余扩展条件：当前 Person 计数索引没有公开 Tag，不能仅凭名称相同消除其身份；只有正式的唯一性/函数依赖依据或公开 Person、Tag 的索引接口才能进一步推进。然后扩展尚未覆盖的表达式和作用域，并处理六组注册缺口。当前不自动新增索引，不修改文档定义，也不添加查询编号或全文模板分支。

迁移收尾已由 Codex 于 2026-11-16 完成（11.1）：新位置独立重建两个补丁版程序，30 个测试通过，210 条查询和 10 条注册记录与上轮结果逐字段一致。以下为后续功能开发事项，不是目录迁移的未完成项。

1. 【需用户决定】是否把 `query_edits/` 写回 IncGQ-experiment-docs：要同时更新 exp-1/exp-6 清单中的 sha256，并重测相应 baseline（Kuzu bi-4 的 baseline 会变快）。
2. ✅【用户 2026-11-18 选择 (a)】已实现声明约束及目标库检查（3.17），BI10 已改写（3.22），BI11 进行中。原文：bi-11、bi-13 只在 LDBC 数据不变式下等价（6.1）。可选：(a) 在 profile 中声明这些不变式（并在建索引时用 Cypher 在目标库上检查它们成立），中间件据此去掉冗余的类型谓词；(b) 增加带类型过滤、并以节点为载体的索引（例如派生边 person→country），严格等价但改变索引集合；(c) 保持不改写。
3. 【Neo4j 输出形状】bi-5 改写后在 Neo4j 上变慢，用 EXPLAIN 找原因，在方言输出层规避（参照 3.13）。
4. 【上游】合入 `patches/neug-rootpost-group-coverage.patch`（7.1、7.2）并重编 `libneug.so`；7.6 的畸形语句与 7.7 的规划器内存爆炸需要 NeuG 侧修复。FinBench 形状的完整读取证明（7.3）。
5. 【frontend】Neo4j 的 `CALL {}` 展平（逐条评估性能）；`queryparts.stub` 支持嵌套列表和结构体列表（ic1）。`USING`、`round(x)`、shortestPath 已完成（3.16）；NeuG 本身缺失的写法不在中间件范围内。
6. 【部署】Bolt 代理；多进程 engine 池。
7. ⏸ 增量维护：暂不做。

## 9. （暂缓）增量维护设计备忘

维护 IR：`rules[{on, needs, affected, action}]`、`depends_on`。维护语句统一以 `UNWIND $delta AS d` 开头，各系统只需写一段把变更事件转成 `d` 的前导。`action` 有 recompute（幂等）、increment（ΔQ）、closure 三种。下沉通道包括代理写穿透、trigger、CDC、原生实现。注意事项：先 `sum()` 再 SET；同一批多条 delta 要用有序 IVM 避免重复计数；删除需要前镜像；increment 在异步通道上不幂等。详见早期对话记录，**当前不实现**。

## 10. 协作约定与变更日志

- 不修改 `src/`、`include/` 下的 NeuG 内核。需要修改时在这里登记，并保持向后兼容。内核修改登记：
  - 7.1 ROOT_POST 组覆盖、7.2 组名查表：只在独立 worktree 的分支 `cico/rootpost-variadic-fix` 中修改，未改动主工作区。
- 不使用共享的 `build/` 做增量编译；引擎的构建目录是 `middleware/engine/build`。
- 不重编 NeuG 也能测试内核补丁：`cmake -S middleware/engine -B /tmp/x -DCICO_INTERPOSE_SOURCES=<补丁后的 .cpp>` 会生成 `incgq_rewrite_engine_patched` 和 `cico_exec_patched`，借助 `-rdynamic` 用补丁版本的函数覆盖 `libneug.so` 中的同名函数。设置 `CICO_ENGINE=` 或 `CICO_EXEC=` 环境变量，测试脚本就会使用补丁版二进制。前提是头文件与 `build/` 编译时一致。
- 新方言放在 `cico/dialects/`，或参考 `cico/neo4j.py` 写成 mapping + frontend + dialect 的形式，并在 `tests/` 中补充用例。
- 已编好的引擎在 `middleware/engine/bin/`（迁移时从 IncGQ 的 `engine/build` 和 `/tmp/cico_fix/build` 复制而来，都通过绝对路径 rpath 链接 `IncGQ/build/src/libneug.so`）。
- 可用 `-DCICO_BUNDLED_PATCH=ON` 在 `middleware/engine/build/` 生成补丁版程序；`prepare_patch.py` 在共享 NeuG 当前源码上应用补丁，记录各编译单元输入、输出和补丁哈希，上下文不兼容时失败，不修改 NeuG 工作区。不要恢复历史源码覆盖当前修复。头文件与动态库的 ABI 仍需匹配。二进制、构建目录和 Python 缓存由 `middleware/.gitignore` 排除。
- 临时文件放在 `/tmp/cico_*`；真机验证用的库副本、提取出的 schema 和报告放在 `/mnt/sdb1/feiyang/cico_work/`（`kuzu/`、`neo4j/`、`schemas/`、`tv/*.json|log`、`cov-*.json`）。
- Git：开发在分支 `develop`（`origin/develop`，基于 `2026.10`），每个修复步骤单独提交并推送；有问题回退到对应提交。改动前的完整快照另存 `/mnt/sdb1/feiyang/backups/neo4j-middleware-impl-20261117-pre-constraints.tgz`。
- 真实 Neo4j 夹具测试使用 SF1 实例（`bolt://127.0.0.1:7693`）：夹具用唯一名称隔离、按变量一次性创建（不要用表达式按 id 查找，会全表扫描数百万 Message），每个种子一个事务，结束回滚。
- 共享机器：dwk 在同机做基准测试。真机验证一次只开一个 Neo4j 服务器（8 GB 堆），Kuzu 用 4 线程，按顺序执行。

变更日志：

- 2026-11-18（三，BI11/BI4/BI8）：12/17。新增 `required_property` 约束（Tag.name、Country.name 非空），SF1 12/12 成立；键身份 T1/T2（BI11），DISTINCT 计数按键列表分区规则（BI4），常量键绑定 T3 与计数见证选择（BI8）。验证：110/110 回归；BI11 80/80（SF1 参数样本计数全为 0，另用夹具，25 组非零）、BI4 30/30（含重复列表元素；去掉 `WITH DISTINCT` 的变体被检出）、BI8 30/30 随机夹具结果一致，均为真实 Neo4j、事务回滚。发现并修正：T1 曾对只出现一次的键节点再次改写，产生 RewriterV4 拒绝的不连通模式。提交 `3818f688`、`ea549ab8` 及本次文档提交（`develop`）。
- 2026-11-18（二，声明约束与理论约束改写）：按 `theory.md`（QueryPart 定理）与用户同意的声明约束，NeuG FILL 文件 → Neo4j 原始 BI 查询从 5/17 到 **9/17**。新增时间字面量规范化（BI2）、关系载体存在性规则（BI14）、声明约束与检查（10 条，SF1 全部成立）、变长关系唯一性的根路径证明与 ROOT_POST 回退替换（BI17）、声明约束重试阶段与投影载体“恰好一个见证”检查（BI10）。验证：100/100 回归；BI14、BI17 各 60/60 随机夹具结果多重集一致（真实 Neo4j，事务回滚）；BI10 在 SF1 上 3/3 组参数逐行一致（13.7s→0.09s）。修正了自己实现中的两处问题：类型标签曾可由表成员关系满足；同一子句的两条载体关系缺少唯一性证明。BI2 的根因更正为时间字面量拼写（原诊断推测为 OPTIONAL 零计数）。提交 `ab313d02`、`dd22408e`、`f0c587cf`、`5f333bd5`、`edf17e8f`（`develop`）。进度见 [未改写 BI 查询进度](neo4j-bi-unrewritten-status.md)。
- 2026-11-18：新增显式 NeuG 注册输入，以及文件加载、规范注册文本、编译物化计划 SHA-256、属性/派生关系依赖、物理数据库身份和原子成功回执。CLI/HTTP 支持只构建待构建项和重新加载，编译或 EXPLAIN 失败不触碰旧目录；执行失败禁用未完成项。引擎新增 `describe_fill`，新路径物化隐藏聚合状态并保持稳定列名，支持实体端点键和按组常量状态。81/81 回归、6/6 Neo4j 小图通过；12 个原文 NeuG BI 脚本共 84 条目标语句通过 EXPLAIN。发现 nullable COUNT 零值组的内核存在性问题，新文件目录在执行前保守拒绝；没有按 BI 查询特化，没有执行 BI 负载或重建 SF1 既有索引。见 [`neo4j-fill-file-reload.md`](neo4j-fill-file-reload.md)。
- 2026-11-17（理论约束增强）：以 wiki binding/索引定义修订 768 和正确性证明修订 47 为核心，支持相关 `size([pattern|entity])`、COUNT/EXISTS 区域的局部改写及有限记录进入 CALL；检查全部相关 anchor、剩余图读取、单行聚合、NULL 和输入重数。CALL 分支保留原输入，并使用类型和表示一致的互斥守卫，避免数值相等但表达式不同及全局 COUNT 零行的问题。修复输入桩名字冲突、引用和参数识别。65/65 测试通过，13/13 小图用例实际结果一致，两个新规则的控制为 1/13→13/13；事务全部回滚。已有 BI 注册 9/15、原查询 8/15、EXPLAIN 80/80 不变；进一步明确 BI8/16 的 Tag 接口缺口。详见 [`neo4j-theory-hardening.md`](neo4j-theory-hardening.md)。
- 2026-11-17：修复已注册 Neo4j BI 索引的通用匹配路径。当前源码补丁构建替代历史 blob 覆盖；规范化固定长度关系分区，支持类型化 DATE/INTERVAL/CAST 谓词键，扩展有完整证明的总 DISTINCT 计数和独立 UNWIND，允许属性复制索引保留不受影响的 DISTINCT COUNT。过程输出类型改由 profile 契约声明，真实 binder 推断种子类型并验证关系域闭包。52/52 测试通过，包含另一套 schema、过程名和别名的回归。15 条原始查询 × 10 组参数中 8 条全部改写，80/80 输出通过 Neo4j EXPLAIN；BI8/16 索引源已匹配，完整查询仍有前端缺口。见 [`neo4j-bi-rewrite-fix.md`](neo4j-bi-rewrite-fix.md)。未实际运行 BI 查询、未修改共享 NeuG 或实验文档查询。
- 2026-11-16（二）：迁移收尾。新增补丁源码重建与来源哈希记录；CMake 默认依赖路径改为同级 `IncGQ/`；引擎与 e2e 执行器共用 `resolve_binary`，保留 patched 优先、同版本 build 优先的策略，并支持显式相对路径；补齐 Python/CLI 的工作目录说明，排除构建产物。新编译的两个补丁版程序构建成功，30/30 测试通过；10 个 system/workload 的 220 条记录（210 条查询 + 10 条注册）与 `/tmp/cico_moved` 逐字段完全一致，66 条改写。验证摘要见 [`validation/migration-20261116.json`](validation/migration-20261116.json)。本轮没有重跑目标数据库上的结果比对，6.2 仍为此前真机验证结果。
- 2026-11-14：M1 完成，包括引擎 v1、打印器、验证守卫；Hetionet 191/191；随机图 e2e 10/12。
- 2026-11-14：M1.5 完成。代码迁移到 `middleware/`。新增：引擎 v2（运行时 FILL/DDL/drop、选中方案复现、IR v2）；打印器支持 UNWIND、变长路径、列表、CAST、子查询；验证守卫的语义保持规范化；只有读取了索引才输出改写；Kuzu 与 Neo4j profile；HTTP 与 CLI；17 个测试；LDBC BI 覆盖率与 e2e（5/5、1/1 结果一致）；记录了上游问题 7.1 和 7.2。按用户决定，增量维护标记为暂不实现。
- 2026-11-14：修复 7.1（ROOT_POST 组 FILL 覆盖，补丁见 `patches/`，未提交）。中间件同步修改：打印器支持 binder 合成的无名 `WITH DISTINCT 实体`（变长锚点拆分产生）；验证守卫中实体投影的列名按实体本身归一化；Neo4j 的非字面量 `datetime(x)` 翻译为 `CAST(x AS TIMESTAMP)`，与 binder 隐式插入的类型转换一致。CMake 新增 `CICO_INTERPOSE_SOURCES`；`e2e_neug.py` 支持 `CICO_EXEC`。BI e2e：NeuG/Exp-6 12/12、Kuzu 3/3；Hetionet 191/312 不变；随机图 e2e 仍为 10/12。
- 2026-11-16：迁到 `/mnt/sdb1/feiyang/neo4j`（第 11 节）。新位置验证：27 个单测通过；全部查询 210 条记录与迁移前逐字段比对**无差异**（66 条改写、0 例"转换成功但不能绑定"）。旧位置已打包备份到 `cico_work/incgq_middleware_before_move_20261116.tar.gz`，现在只留 `MOVED.md`。
- 2026-11-15（二）：方言转换成功即可绑定（3.16）：NeuG 语言检查 `surface.py`；Neo4j `USING`、`round(x)`、shortestPath、TRAIL、标签析取、LSQB 属性改名；实体比较与标签推断避开 NeuG 断言。全量 0 个转换成功但不能绑定的 QueryPart，66 条改写不变；27 个单测。
- 2026-11-15：按 wiki 的 QueryPart 定理改为逐段改写（3.12，默认 `scope='part'`）。Neo4j：子类型标签折叠进模式（3.13，bi-3 不再变慢）、关系唯一性改用 `id(r1) <> id(r2)`（3.14）。引擎内存上限与自动重启（3.15），参数特化也在 engine_error 时尝试。新增上游问题 7.6、7.7。文档查询的等价改写覆盖层与 `equivalence_check.py`（6.3）。结果：Kuzu BI 改写 5 条（新增 bi-4、bi-17），Kuzu LSQB 3 条（新增 q4、q8），真机比对全部一致（6.2）；25 个单测。
- 2026-11-14：M2 与多系统覆盖。新增：目标 schema 提取（`cico schema --kuzu`）；LSQB、FinBench、Hetionet 的 Neo4j mapping；Kuzu frontend；输入规范化（coalesce、链式比较、duration）；端点驱动的关系表裁剪；Neo4j 关系唯一性的双向处理；参数特化；索引物化（`build_statements`、`cico build`、`GET /build`）；验证守卫合并相邻 MATCH；工具 `workload_coverage.py`、`target_validation.py`、`neo4j_clone.sh`。内核补丁增加 7.2。结果：覆盖率见 6.1；真机验证中 Kuzu 与 Neo4j 共 116 次比对全部一致（6.2）；23 个单测；Hetionet 语料回归 191/312 不变。

## 11. 迁移记录（2026-11-16）

用户要求把中间件迁到 `/mnt/sdb1/feiyang/neo4j`，并把目标、思路和进度记录在 `neo4j/implementation/middleware`。

| 迁移前 | 迁移后 |
| --- | --- |
| `IncGQ/middleware/`（不含 `engine/build/` 中的 CMake 中间文件） | `neo4j/middleware/` |
| `IncGQ/middleware/engine/build/{incgq_rewrite_engine,cico_exec}` | `neo4j/middleware/engine/bin/` |
| `/tmp/cico_fix/build/{incgq_rewrite_engine,cico_exec}_patched`（`/tmp` 可能被清理） | `neo4j/middleware/engine/bin/` |
| `IncGQ/implementation/cypher_in_cypher_out/{README.md,query_edits/,patches/}` | `neo4j/implementation/middleware/` |
| `/tmp/cico_all.py`（全部查询逐步输出） | `neo4j/middleware/tests/dump_all_queries.py` |

- 改动的路径：
  - `engine/CMakeLists.txt` 的 `NEUG_SOURCE_DIR` 默认取 `INCGQ_ROOT`，未设置时查找同级 `IncGQ/`（本机为 `/mnt/sdb1/feiyang/IncGQ`）；
  - `cico/engine.py` 默认引擎依次查找 `build/patched`、`bin/patched`、`build/原版`、`bin/原版`；
  - `tests/workloads.py` 的覆盖层改为 `implementation/middleware/query_edits`；
  - `tests/e2e_neug.py` 的 `cico_exec` 也会在 `engine/bin` 中查找。
- 迁移后在新位置重跑：27 个单测通过；全部查询的 210 条记录与迁移前逐字段比对，无差异。
- 旧位置先完整打包备份到 `/mnt/sdb1/feiyang/cico_work/incgq_middleware_before_move_20261116.tar.gz`，再替换为只含 `MOVED.md` 的目录。
- 新仓库里的这些文件都没有提交（未跟踪），是否提交到 `flyfly0521/neo4j` 由用户决定。

### 11.1 迁移收尾（Codex，2026-11-16）

在仓库根目录执行：

```sh
cmake -S middleware/engine -B middleware/engine/build -DCICO_BUNDLED_PATCH=ON
cmake --build middleware/engine/build --target incgq_rewrite_engine_patched cico_exec_patched -j 2
python3 -m unittest discover -s middleware/tests -v
cd middleware
python3 -m cico --help
```

- **历史方式，已在 2026-11-17 被当前源码补丁构建替代**：`prepare_patch.py` 曾从 IncGQ 的 Git 对象 `428a0904` 取出原始 rewriter 源码，应用仓库保存的补丁，校验结果为 `e8550aa4be137af3ab81f86d0707d06ab024342c`。迁移时源码 SHA-256 与迁移前一致，但该方式会覆盖共享内核较新的修复，故不再使用。现行方式和验证见 [`neo4j-bi-rewrite-fix.md`](neo4j-bi-rewrite-fix.md)。
- Python 与 e2e 执行器统一选择补丁版优先，防止编译原版后丢失 ROOT_POST/REL-group 修复。环境变量 `CICO_ENGINE`、`CICO_EXEC` 或 Python 的 `engine_binary` 可显式指定程序；显式相对路径先转为绝对路径，避免不带 `/` 的文件名被按系统 PATH 查找。
- 迁移携带的 `engine/bin/` 二进制仍保留在本机，加入忽略规则；源码检出后可以用上述命令重建。Python 命令在 `middleware/` 下执行，从其他目录使用时设置 `PYTHONPATH`。
- 验证：新引擎的 30 个测试全部通过（HTTP 测试需要回环端口）；显式相对路径启动、protocol 2 ping、默认 Neo4j profile 和 CLI 入口通过。全量查询逐步记录输出到 `/tmp/cico_migration_verified/`，与 `/tmp/cico_moved/` 的 JSON 逐字段比对，220/220 记录一致、66 条改写；持久化摘要见 [`validation/migration-20261116.json`](validation/migration-20261116.json)。
- 当前迁移范围为独立中间件代码、文档、构建与使用入口。NeuG 动态库仍是外部依赖；Bolt 代理、engine 池及第 8 节其他功能项继续保留。

### 11.2 Neo4j 原生索引注册与 BI SF1 验收（2026-11-17）

实验文档已快进更新到 `08f1a265`。新增 `cico/neo4j_indexes.py`，把显式注册的 Neo4j
物化脚本转换成内部 FILL，随后复用已有的绑定、改写和回译验证链路。Python、CLI、HTTP
均有入口；普通写查询不注册元数据。失败注册回滚 schema 和索引定义。原生 schema index /
constraint DDL 按 `target_ddl` 返回供目标库执行。

独立数据库位于 `/mnt/sdb1/feiyang/neo4j/middleware/runtime/bi_sf1`，Neo4j Community
5.26.28，Java 21，Bolt `127.0.0.1:7693`。从用户指定 datasets 下的 BI SF1 原始 CSV
重新离线导入，输入逐表计数通过；导入后确认 2,997,352 个节点、17,196,776 条基础关系。
数据、prepared CSV、运行配置及日志在忽略的 runtime 目录内，不覆盖已有实验库。

从 Neo4j 文档索引目录选用 bi10、bi9、bi5、bi6、bi11，注册 8 个定义并实际完成 14 条
索引构建语句。ROOT_POST 合并自环与递归分支，并推断全部端点分区；COUNT 子查询经
OPTIONAL MATCH 保留零计数。BI3、BI5、BI9、BI11 改写成功，4/4 经 Neo4j EXPLAIN
确认目标语法有效；未实际运行 BI 查询，也未进行结果等价性或性能测量。

38 个测试全部通过，其中新增 8 个测试覆盖原生属性、派生边、两种批处理 CALL、DDL
路由、ROOT_POST 分区、写查询隔离和注册失败回滚。验收工具为
`middleware/tests/neo4j_bi_acceptance.py`，schema 固化在 `middleware/schemas/ldbc-bi.yaml`
（来源为此前从目标数据模型提取的 `cico_work/schemas/ldbc-bi.yaml`）。
完整证据见 [`validation/neo4j-bi-sf1-20261117.json`](validation/neo4j-bi-sf1-20261117.json)。

范围限制：仍有 CALL/GDS、变长关系唯一性及其他 NeuG 不支持语法的查询回退原文；
任意复杂条件 SET、多属性关系索引脚本不在本轮新增翻译范围内。逐条状态在验收 JSON
中列出，不能把本轮通过解释为全部 BI 查询或全部 Neo4j 原生索引脚本已支持。

### 11.3 全量 BI 检查与 CALL/参数化注册（2026-11-17）

继续验证全部原始查询，新增 CALL/UNION 分支的类型上下文与局部改写、参数值固定的 Neo4j
索引注册、零计数声明、COUNT 和时间比较规范化，以及 NeuG 多候选异常的隔离重试。
原生输入和回译绑定守卫保留；嵌套写入的整条语句不改写。

20 条查询共检查 191 组参数，当前 6 条查询全部样本成功（BI3/5/6/9/11/13），60 条输出
全部通过 EXPLAIN，其他 131 个样本原样返回，44/44 单元与集成测试通过。
本轮没有执行 BI 工作负载查询；参数化索引按样本注册临时元数据，没有逐组物理构建。

“全部查询均能改写”的目标尚未达到。剩余问题包含缺失的物化定义、复杂 Neo4j 关系物化
脚本注册、NeuG 索引选择，以及 CALL 输入的映射/列表、APOC/GDS 和变长关系唯一性表达。
没有关闭验证或通过修改原查询来规避这些限制。
完整逐条结果、物理索引状态、脚本注册清单和复现入口见
[`validation/neo4j-bi-all-20261117.md`](validation/neo4j-bi-all-20261117.md) 与同名 JSON。
