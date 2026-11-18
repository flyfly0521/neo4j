# NeuG BI 索引改写 Neo4j 原始查询的失败定位

> **2026-11-18 更新：** 本文 11 个案例中 BI2、BI10、BI14、BI17 已修复，最新状态、FILL 与查询原文见 [未改写 BI 查询进度](neo4j-bi-unrewritten-status.md)。
> - **BI2 根因更正**：不是 OPTIONAL 零计数，而是时间字面量拼写。Neo4j 的 `datetime('...T...Z')` 转换为 `timestamp('2010-06-09T00:00:00Z')`，FILL 中是 `TIMESTAMP('2010-06-09 00:00:00')`，换成同一拼写即命中；参数内联后 `TIMESTAMP(...) + duration(...)` 又被当作字符串拼接拒绝。两处已修（README 3.18）。
> - **BI14**：RewriterV4 连 FILL 自身的源查询都不改写，问题不在方向或评分的匹配，改由独立规则处理（README 3.19）。
> - **BI17**：除关系唯一性外，NeuG 规划器在该模式上耗尽内存（README 7.7 补充），改由根路径证明和回退替换处理（3.20、3.21）。
> - **BI10**：类型谓词由声明约束推出恒真后去掉，并检查投影载体每个键恰好一个见证（3.22）。
>
> 以下为 2026-11-17 的原始定位，保留不改。

实验文档来自 `/mnt/sdb1/feiyang/IncGQ-experiment-docs` 的 `jcloud-exp`，提交 `08f1a2659ea2304b7940fd15db3c23382bf37eb7`。**索引定义取自 `NeuG/queries/index/ldbc-bi/bi*/`，使用 NeuG DDL/FILL 文件直接注册。待改写查询取自 `Neo4j/queries/baseline/ldbc-bi/bi-N.cypher`，使用 Neo4j profile 输入和输出。** 合并文件去重后有 29 个索引定义，89 条 Neo4j 物化语句通过 EXPLAIN。以下针对 SF1 第 0 组参数下未改写的 11 条原始查询。BI15 与 BI15w 共用 BI15 原始查询。成功的五条为 BI3、BI5、BI6、BI9、BI13。

分类依据是原始查询中**可能利用索引的区域**。有些外层结构可以留在 Neo4j，内部 QueryPart 仍能改写。整条查询的 `no_index_selected` 仅说明没有返回索引读取，定位时还需要查片段的转换、绑定和索引接口。使用上一轮合并文件的结果，并用空索引目录对相关原文和最小片段运行转换和 NeuG 绑定。本轮未逐个插桩确认 RewriterV4 的候选拒绝点，也未物化索引或执行 BI 查询。

| 阶段 | 查询 | 首要问题 |
| --- | --- | --- |
| 查询转换或外部过程边界 | BI15、BI17、BI19、BI20 | 权重计算位于 GDS 字符串参数或独立投影脚本，或 MATCH 中的变长关系需要跨关系唯一性 |
| 前端和索引接口都有缺口 | BI8、BI16 | 列表、相关计数或 CALL 输入的转换有限，且索引没有公开原查询仍需使用的 Tag 身份 |
| 相关片段可以转换并绑定，随后没有匹配 | BI2、BI4、BI10、BI11、BI14 | 窗口谓词、聚合粒度、地点类型、国家节点身份、方向与存在性评分 |

## BI2 · 时间窗口和 OPTIONAL 零计数

Neo4j 原查询的两个 `OPTIONAL MATCH` 都能转成 NeuG，整条转换结果绑定成功。第一个窗口的上界仍是 `TIMESTAMP('2010-06-09 00:00:00') + duration('100 days')`，第二个窗口也保留加法。NeuG 的 `bi2/parameter.cypher` 按已绑定的三个 timestamp 边界注册两条普通 MATCH 分组计数。已保存的独立 NeuG 对照表明，使用文档里的 NeuG 查询与这两条索引时，两个窗口都能命中。

当前问题在**转换后的匹配**。候选需要识别同一组参数下窗口边界等价，并在保留已有 Tag、TagClass 过滤和空匹配零计数的条件下，用稀疏计数替换 OPTIONAL 聚合。本轮确认了谓词和分组形状差异，尚未隔离两项中哪一项先拒绝了候选。跨参数复用还必须受物化窗口的有效域约束。见[已保存的 NeuG 对照](validation/neo4j-bi-theory-diagnosis-20261117.json)。

## BI4 · 按 Forum/Person 的计数与跨论坛 DISTINCT

原查询的 CALL 中，消息从 `topForum1` 取得，作者必须属于 `topForums` 中的某个 `topForum2`，最后按 Person 对消息去重计数。另一支 UNION ALL 补入有会员身份但没有消息的 Person。去掉 CALL 和列表外壳后，保留递归回复、作者、会员关系和 `count(DISTINCT message)` 的核心片段可以转换并绑定。

`BI4_MESSAGE_COUNT` 只返回每个 `(Forum, Person)` 的不同消息数，不公开消息身份，也不包含独立的会员约束。当前没有将这组计数接入 Top Forums 的成员筛选和跨论坛归并。直接把各 Forum 的计数相加，需要另有证明保证同一作者的消息不会落入多个被选分组，同时仍要保留独立的会员过滤和零消息分支。单纯加宽图模式模板不足以保证正确。

## BI8 · 列表和相关计数，以及 Tag 身份

原查询对节点列表做拼接，再用 `size([pattern | message])` 为当前 Person 和可选的 friend 计分。整条方言转换在 `size()` 处拒绝，局部路径还记录了 NeuG 绑定器无法对两个 `NODE[]` 做加法。现有局部类型探针和相关计数规则只能覆盖满足其作用域约束的片段。

即便识别出相关计数，`PERSON(bi8MessageCount)` 也只按 Person 存储固定 **Tag 名称**下的匹配数。Neo4j 的标量计算要求使用外层**已绑定的那个 Tag 节点**。两个 Tag 节点若同名，Person 对两者各有一条消息，载体存的是 2，固定任意一个 Tag 的标量计数是 1。需要公开 Person 与 Tag 的身份，或者先证明名称在使用域内唯一。见 [Tag 接口与相关计数](neo4j-theory-hardening.md)。

## BI10 · APOC 可保留，地点路径仍缺匹配

`apoc.path.subgraphNodes` 无 NeuG 对应过程，较早的 QueryPart 会报告该错误。编译探针用原前缀和 profile 的过程类型契约，成功绑定 `expertCandidatePersons` 为 `PERSON[]`，因此后续地点路径有类型上下文。该路径单独转换绑定成功，其中 City 和 Country 变成 `PLACE {type: 'City'}` 与 `PLACE {type: 'Country'}`。

当前 NeuG 文档里的 `PERSON(countryName)` 两跳都只声明 PLACE，并投影终点的名称。查询要求两个地点类型和国家名过滤。定位落在下游匹配和证明，需要确认物化路径满足 City/Country 类型与所需重数，才能把该过滤替换为属性读取。原文中的 APOC 报错解释了前缀不可直接翻译，后续未命中仍需检查地点索引约束。见 [Neo4j profile 的过程契约](../../middleware/profiles/ldbc-snb-neo4j.yaml)。

## BI11 · countryName 不公开 Country 节点

Person→City→Country 片段能转换并绑定。Neo4j 原查询先绑定一个 Country 节点，再把同一节点跨 WITH 带入另外两条地点路径，要求三人关联的是同一 Country。NeuG 文档中的 BI11 注册的是共享的 `PERSON(countryName)`，只保存国家名称字符串，也缺少 City/Country 类型限制。

将后两处身份比较改为名称比较需要 Country 名称唯一及地点映射的证明。当前注册目录没有能直接返回 Country 节点的载体，因而匹配器没有等价的身份连接可以替换。这个缺口在索引接口，不是 Neo4j 三角模式无法转换。

## BI14 · 无向 KNOWS 上的存在性评分

代表性评分片段 `OPTIONAL MATCH … RETURN DISTINCT … CASE WHEN m IS NULL THEN 0 ELSE 10 END` 连同无向 KNOWS 已转换并通过 NeuG 绑定。原查询的四个分支分别对某种交互**是否存在**给一次固定分值。NeuG FILL 按有向 KNOWS 端点存储正向和反向 `COUNT`。

要使用这些列，需要先关联原查询绑定的那条 KNOWS 边并选择与人对顺序一致的方向，再将计数大于零映射成存在性分值，并保留 OPTIONAL 空行和阶段性 DISTINCT 的重数。当前编译没有选中这样的替换。只按关系名或只比较 COUNT 与 CASE 的文本，无法表达这组等价条件。

## BI15 · 时间窗权重藏在 GDS 投影语句中

原查询调用 `gds.graph.drop`、`gds.graph.project.cypher` 和 Dijkstra。需要替换的关系权重是传给投影过程的**字符串参数**，两端日期还参与字符串拼接。常规 Neo4j→NeuG 转换在 CALL 处拒绝，QueryPart 也没有可见的权重计算来与 FILL 匹配。

NeuG 静态 BI15 和定窗 BI15w 的权重均可注册。要消费定窗权重，需要能改写投影内部的查询，并证明窗口、方向、权重和无交互时的默认值一致，保持 GDS 投影与路径过程的接口。扩大普通 MATCH 的模板不会使字符串内容进入绑定图。

## BI16 · 有限记录输入和跨 OPTIONAL 的 Tag 身份

原查询以 `UNWIND` 两条记录产生 tag/date 参数，再经相关 CALL 计算。整条转换报告 `CALL subquery`，对 CALL 之前的记录列表单独做前缀绑定也报告 `no declared opaque type prefix`。已有有限记录局部分析能尝试部分分支，但本轮原查询无命中。

NeuG 的 A/B FILL 分别按 Person 和固定名称、一天的半开时间段存储 `COUNT(DISTINCT message)`。原查询在后续 OPTIONAL 中再次使用首个 MATCH 的**同一个 Tag 节点**，再按不同朋友数过滤。Person 上的计数没有这个连接接口。即使前端支撑整段转换，仍需保留 Tag 身份，证明后续扩展和 DISTINCT 不变。Neo4j 的 `date(creationDate) = date(paramDateX)` 与固定 timestamp 区间之间也需要按明确时区和日期边界规范化。见 [有限记录与 Tag 接口](neo4j-theory-hardening.md)。

## BI17 · 同一 MATCH 中多条回复路径的关系唯一性

首个 MATCH 含两段 `REPLY_OF*0..` 和一条固定长度 `REPLY_OF`。Neo4j 要求同一 MATCH 内参与匹配的关系边不能被重复使用，包括变长路径与其他关系之间。转换器能为两条定长关系表示不等式，缺少跨路径列表的约束表示，实测在转换阶段报 `relationship uniqueness involving a variable-length relationship`。同一目录里的 ROOT_POST 已能用于 BI9。这里还需证明路径间的边不重叠，或显式保留相应约束，才能以仅公开消息和根帖端点的 ROOT_POST 替代。见 [关系唯一性转换](../../middleware/cico/neo4j.py)。

## BI19 · Dijkstra 消费的是外部投影

BI19 原查询在已存在的 `bi19` GDS 图上运行 Dijkstra，提交给中间件的正文没有计算交互权重。权重由实验文档中独立的 `bi-19-create-graph.cypher` 生成。原查询的过程调用未转换，注册于原图 `KNOWS` 的 `weight_bi19` 也不会自动进入已有 GDS 图。

要利用该索引，接入点在投影构建。原投影只输出有回复交互的人对，NeuG 权重定义则可给无交互的 KNOWS 边一个高成本权重。若把这些边也放入投影，可能改变可达性。除了权重算式，还需要对齐边集合和方向。

## BI20 · 路径正文看不到同校边权计算

BI20 原查询也在已有 GDS 图 `bi20` 上运行 Dijkstra。其独立投影脚本才计算同校者的最小入学年份差加一。NeuG 版先物化 `bi20_direct`，再生成 `bi20_weight`，为无匹配同校记录的边设高成本值。原查询实测在过程调用处无法转换，且没有片段可匹配这两段 FILL 计算。

这里同样要接入投影构建，并确认组织标签条件、边集合和方向。Neo4j 的原投影排除无共同学校的人对，避免引入仅靠高成本边才能连通的路径是必要的语义条件。
