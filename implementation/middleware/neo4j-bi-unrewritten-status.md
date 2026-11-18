# Neo4j BI 原始查询：NeuG FILL 索引改写进度（2026-11-18）

本文件按查询列出当前**仍未被索引改写**的 BI 查询，以及每条不能被哪个索引改写、卡在哪一步、下一步怎么做，供同步进度使用。逐条的历史定位见 [BI 改写失败定位](neo4j-bi-neug-fill-rewrite-diagnosis.md)。

## 口径

- 索引：实验文档 `NeuG/queries/index/ldbc-bi/<case>/`（提交 `08f1a265`），NeuG DDL/FILL 直接注册；参数文件按 SF1 第 0 组参数渲染（BI13 的 `{}` 按 `run_workload.py` 用 `endDate` 渲染，BI15w 按文件注释的占位顺序渲染）。
- 查询：`Neo4j/queries/baseline/ldbc-bi/bi-N.cypher` 原文，Neo4j profile 输入输出。
- 每个索引目录单独注册、单独改写一次；只编译，不物化索引、不执行 BI 查询。
- 复现：`python3 middleware/tests/neug_fill_bi_status.py --output status.json`（[结果](validation/neo4j-bi-neug-fill-status-20261118.json)）。

## 总览：9/17 改写（上一轮诊断为 5/17）

| 索引目录 | 查询 | 状态 | 读取的索引 | 说明 |
| --- | --- | --- | --- | --- |
| bi2 | bi-2 | ✅ 本轮修复 | `TAG(bi2Window1/2MessageCount)` | 时间字面量规范化；只对该组 `$date` 有效（参数特化） |
| bi3 | bi-3 | ✅ 原有 | `ROOT_POST` | RewriterV4 |
| bi5 | bi-5 | ✅ 原有 | `likeCount`/`replyCount` | RewriterV4 |
| bi6 | bi-6 | ✅ 原有 | `PERSON(likeCount)` | RewriterV4 |
| bi9 | bi-9 | ✅ 原有 | `ROOT_POST` | RewriterV4 |
| bi13 | bi-13 | ✅ 原有 | `PERSON(bi13MessageCount)` | RewriterV4 |
| bi14 | bi-14 | ✅ 本轮修复 | `KNOWS(bi14_case1_fwd/rev, bi14_case3_fwd/rev)` | 新规则：关系载体存在性（四个评分段全部改写） |
| bi17 | bi-17 | ✅ 本轮修复 | `ROOT_POST` | 新规则：关系载体替换变长路径；声明约束 |
| bi4 | bi-4 | ❌ | — | 见下 |
| bi8 | bi-8 | ❌ | — | 见下 |
| bi10 | bi-10 | ❌ | — | 见下 |
| bi11 | bi-11 | ❌ | — | 见下 |
| bi15 | bi-15 | ❌ | — | 见下 |
| bi15w | bi-15 | ❌ | — | 见下 |
| bi16 | bi-16 | ❌ | — | 见下 |
| bi19a | bi-19 | ❌ | — | 见下 |
| bi20 | bi-20 | ❌ | — | 见下 |

本轮修复的验证：

- BI2：6 个单测（等价瞬间同一字面量、其他窗口不改写、命名时区/月份不折叠）。
- BI14：真实 Neo4j 上 30 个随机夹具、两条查询共 60/60 结果多重集一致（含双向/平行 KNOWS、无 KNOWS 走回退分支），全部事务回滚；[结果](validation/neo4j-bi14-carrier-existence-20261118.json)。
- BI17：真实 Neo4j 随机夹具比对**进行中**（结果写入 `validation/neo4j-bi17-path-carrier-20261118.json`）。BI17 的改写不经 NeuG 绑定校验：NeuG 规划器对这个 11 条关系的单个 MATCH 在 24 GB 上限下仍然内存耗尽（README 7.7，上游问题），因此只做了转换与 schema 检查，证明中记录了 `neug_bind: skipped`。

## 声明约束（用户 2026-11-18 同意）

写在 `middleware/profiles/ldbc-snb-neo4j.yaml` 的 `constraints:`，是个别改写步骤的前提，不是定理本身的前提；用到的约束写入 `Result.premises`。`middleware/tests/constraint_checks.py` 在 SF1 实例（`bolt://127.0.0.1:7693`，只读）上检查，**8 条全部 0 违反**：

| 约束 | 用于 |
| --- | --- |
| `(:Message)-[:REPLY_OF]->` 至多一条 | BI17（变长路径唯一性、路径唯一）；BI4 计划使用 |
| `-[:CONTAINER_OF]->(:Post)` 至多一条 | BI17（根帖相同 ⇒ 论坛相同）；BI4 计划使用 |
| `(:Person)-[:IS_LOCATED_IN]->` 至多一条 | BI10/BI11 计划使用 |
| `(:City)-[:IS_PART_OF]->` 至多一条 | BI10/BI11 计划使用 |
| `(:Person)-[:IS_LOCATED_IN]->(:City)` | BI10/BI11 计划使用（去掉冗余的 City 类型谓词） |
| `(:City)-[:IS_PART_OF]->(:Country)` | BI10/BI11 计划使用（去掉冗余的 Country 类型谓词） |
| `Tag.name` 唯一 | BI8/BI16 计划使用 |
| `Country.name` 唯一 | BI11 计划使用 |

## 未改写的查询

按计划修复顺序排列。

### BI10 · 可由 `PERSON(countryName)` 改写 —— 下一步

- **索引**：`PERSON(countryName)`，源为 `(country:PLACE)<-[:ISPARTOF]-(:PLACE)<-[:ISLOCATEDIN]-(person)`，无地点类型限制。
- **卡点**：第 1 段 APOC 过程不可转换（保留原文，正常）；第 4 段（UNWIND 之后）能转换绑定，但查询写的是 `(:City)`、`(:Country {name})`，转换后带 `type='City'/'Country'` 谓词，FILL 没有，$(M_F)$ 不成立，RewriterV4 不匹配。
- **修复**：在“声明约束重试”阶段，用 `endpoint_labels` 推出 City/Country 类型谓词恒真后去掉，再交给 RewriterV4；`at_most_one` 保证 `countryName` 对每个 Person 取值唯一。只在常规改写失败后重试，不改变 BI3 等已有输出形状（README 3.13）。

### BI11 · 可由 `PERSON(countryName)` 改写 —— 依赖 BI10 的重试阶段

- **索引**：同 BI10。
- **卡点**：除类型谓词外，原查询把同一个 `country` **节点**跨 WITH 带到后两段，与 `b`、`c` 的地点路径做身份连接；`countryName` 只公开名称，不公开 Country 节点，接口条件 $A\cup(H\cap V_I)\subseteq I$ 不成立。
- **修复**：BI10 的类型推导之外，用 `Country.name` 唯一，把“b 的国家 = 输入的 country 节点”改写为“b 的国家名 = country.name，且 country 是 Country”，使 country 不再是索引的隐藏顶点。

### BI4 · 可由 `BI4_MESSAGE_COUNT` 改写 —— 需新的聚合分解规则

- **索引**：`BI4_MESSAGE_COUNT(Forum→Person, messageCount)`，每个 (Forum, Person) 的 `count(DISTINCT message)`。
- **卡点**：查询在 CALL 内按 Person 对跨多个 topForum 的消息做 `count(DISTINCT message)`，并要求作者是某个 topForum 的成员；索引按 (Forum, Person) 分组，粒度不同，RewriterV4 不匹配（CALL 分支和展平都试过，均 `no_index_selected`）。
- **修复**：新规则——消息的根帖唯一、帖子只属于一个论坛（两条 `at_most_one`），且 `topForums` 来自 DISTINCT 后的 collect、元素互不相同，于是各 topForum 的消息集合互不相交，跨论坛 `count(DISTINCT)` 等于各论坛计数之和；会员过滤与 UNION ALL 零消息分支原样保留。

### BI8 · 可由 `PERSON(bi8MessageCount)` 改写 —— 需 Tag 唯一性 + 前端

- **索引**：`PERSON(bi8MessageCount)`，按固定 Tag **名称**和时间窗统计每人的消息数。
- **卡点**：① 第 2 段 `interestedPersons + collect(person)`（`NODE[] + NODE[]`）NeuG 不支持，第 5 段 `size([pattern | x])` 不在 NeuG 目录；已有标量计数规则因 `size` 区域缺少新鲜非空见证而拒绝。② 原查询的计数绑定外层**同一个 Tag 节点**，索引只公开 Person，隐藏 Tag，接口条件不成立。
- **修复**：用 `Tag.name` 唯一，把相关计数中的外层 tag 换成按名称定位的新变量，使 Tag 不再属于输入接口；再补 `size()` 区域的见证处理。时间窗字面量问题已随 BI2 修复。

### BI16 · 理论上可由 `PERSON(bi16MessageCountA/B)` 改写 —— 收益低，暂缓

- **索引**：A/B 两列，各为固定 Tag 名称、一天半开区间内每人的 `count(DISTINCT message)`。
- **卡点**：`UNWIND` 两条记录后进入相关 CALL，前缀类型推断失败（`no declared opaque type prefix`）；CALL 内后续 OPTIONAL 再次使用同一个 Tag 节点；`date(x) = date($p)` 与固定时间区间的等价需按 UTC 日边界证明。
- **判断**：即使改写，也只替换 `cm`，耗时的好友 OPTIONAL（`cp2`）仍要现场计算，按全体 Person 读属性可能更慢。暂缓。

### BI19 / BI20 · 查询正文无可改写部分 —— 需扩大范围

- **索引**：BI19 `KNOWS(weight_bi19)`（依赖 `bi14_case1_fwd/rev`），BI20 `KNOWS(bi20_direct, bi20_weight)`。
- **卡点**：`bi-19.cypher`、`bi-20.cypher` 只在已存在的 GDS 图上调用 Dijkstra（`CALL procedure` 不可转换，也无须转换）；权重计算在独立的 `bi-19-create-graph.cypher`、`bi-20-create-graph.cypher` 的 `gds.graph.project.cypher` 字符串参数里。
- **若纳入范围**：改写投影字符串。BI19：交互次数 = `bi14_case1_fwd + bi14_case1_rev`，并加 `> 0` 过滤以保持原投影只含有交互的边（NeuG 权重给无交互边 1e13，直接用会改变可达性）。BI20：用 `bi20_direct <> 0` 保持边集，另需声明 `STUDY_AT` 只指向 University。**需要你确认是否把 create-graph 脚本纳入改写范围。**

### BI15 / BI15w · 不建议

- **索引**：BI15 静态 `KNOWS(bi15_weight)`，BI15w 定窗 `KNOWS(bi15w_weight)`。
- **卡点**：权重写在 `gds.graph.project.cypher` 的字符串参数里，窗口日期还经 `epochMillis` 拼接进字符串；原投影的 OPTIONAL 加按 `m1:Post OR m2:Post` 计权，与 FILL 的“有向拆分 + 两步 DAG”结构差别大，没有可复用的匹配。
- **判断**：需要“GDS 字符串内嵌改写 + 专门的权重等价证明”，投入大，不建议。

## 本轮新增的中间件能力（代码入口）

| 能力 | 文件 | 理论对应 |
| --- | --- | --- |
| 时间字面量规范化、定长 duration 折叠 | `cico/normalize.py` | $(M_F)$：同一瞬间同一字面量 |
| 关系载体存在性（OPTIONAL + DISTINCT） | `cico/edge_carriers.py` | $(M_P)$ 图同构、$(M_F)$、$(L)$ 取 `count>0`；DISTINCT 另有语义证明 |
| 声明约束与检查语句 | `cico/constraints.py`、`tests/constraint_checks.py` | 改写步骤的前提，`Result.premises` |
| 变长关系唯一性的根路径证明 | `cico/neo4j.py`（`_root_path`、`_uniqueness_implied`、`contradiction`） | Neo4j 关系同构 → 内部同态的等价前提 |
| 关系载体替换变长路径（回退阶段） | `cico/path_carriers.py` | $(L)$：至多一条路径 ⇒ $|G_k|=1$；新增的载体唯一性由过滤推出矛盾 |
