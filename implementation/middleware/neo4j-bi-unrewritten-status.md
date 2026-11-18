# Neo4j BI 原始查询：NeuG FILL 索引改写进度（2026-11-18）

本文件由 `middleware/tests/render_bi_status.py` 从实测结果生成：FILL 文本、查询原文、中间件返回的原因和输出均取自 `middleware/tests/neug_fill_bi_status.py` 的运行结果（[JSON](validation/neo4j-bi-neug-fill-status-20261118.json)），卡点分析和修复计划为人工撰写。历史定位见 [BI 改写失败定位](neo4j-bi-neug-fill-rewrite-diagnosis.md)。

## 口径

- 索引：实验文档 `NeuG/queries/index/ldbc-bi/<case>/`（提交 `08f1a265`），NeuG DDL/FILL 直接注册；参数文件按 SF1 第 0 组参数渲染（BI13 的 `{}` 按 `run_workload.py` 用 `endDate` 渲染，BI15w 按文件注释的占位顺序渲染）。
- 查询：`Neo4j/queries/baseline/ldbc-bi/bi-N.cypher` 原文；Neo4j profile 输入、输出。
- 中间件：提交 `f0c587cf`（`develop`）。每个索引目录单独注册、单独改写一次；只编译，不物化索引、不执行 BI 查询。
- 复现：`python3 middleware/tests/neug_fill_bi_status.py --output s.json && python3 middleware/tests/render_bi_status.py s.json`。

## 总览：8/17 改写（上一轮诊断 5/17）

| 索引目录 | 查询 | 状态 | 读取的索引 | 路径 / 结论 |
| --- | --- | --- | --- | --- |
| bi2 | bi-2.cypher | ✅ 本轮新增 | `TAG(bi2Window1MessageCount)`, `TAG(bi2Window2MessageCount)` | parts:1,2；只对该组 $date 有效 |
| bi3 | bi-3.cypher | ✅ 原有 | `ROOT_POST()` | parts:2 |
| bi5 | bi-5.cypher | ✅ 原有 | `COMMENT(likeCount)`, `COMMENT(replyCount)`, `POST(likeCount)`, `POST(replyCount)` | parts:1,2 |
| bi6 | bi-6.cypher | ✅ 原有 | `PERSON(likeCount)` | subqueries |
| bi9 | bi-9.cypher | ✅ 原有 | `ROOT_POST()` | initial |
| bi13 | bi-13.cypher | ✅ 原有 | `PERSON(bi13MessageCount)` | parts:2；只对该组 $endDate 有效 |
| bi14 | bi-14.cypher | ✅ 本轮新增 | `KNOWS(bi14_case1_fwd)`, `KNOWS(bi14_case1_rev)`, `KNOWS(bi14_case3_fwd)`, `KNOWS(bi14_case3_rev)` | parts:2,3,4,5 |
| bi17 | bi-17.cypher | ✅ 本轮新增 | `ROOT_POST()` | path_carriers |
| bi4 | bi-4.cypher | ❌ | — | 可改写，需新的聚合分解规则 |
| bi8 | bi-8.cypher | ❌ | — | 可改写，需 `Tag.name` 唯一 + 前端补充 |
| bi10 | bi-10.cypher | ❌ | — | 可改写，下一步修复（声明约束重试阶段） |
| bi11 | bi-11.cypher | ❌ | — | 可改写，依赖 BI10 的重试阶段 + `Country.name` 唯一 |
| bi15 | bi-15.cypher | ❌ | — | 不建议 |
| bi15w | bi-15.cypher | ❌ | — | 不建议（同 BI15） |
| bi16 | bi-16.cypher | ❌ | — | 理论上可改写，收益低，暂缓 |
| bi19a | bi-19.cypher | ❌ | — | 查询正文无可改写部分；需把 create-graph 脚本纳入范围 |
| bi20 | bi-20.cypher | ❌ | — | 查询正文无可改写部分；需把 create-graph 脚本纳入范围 |

本轮新增改写的验证：

- BI2：6 个单测（等价瞬间同一字面量；其他窗口不改写；命名时区、月份单位不折叠）。
- BI14：真实 Neo4j 上 30 个随机夹具 × 2 条查询，60/60 结果多重集一致（含双向、平行 KNOWS；无 KNOWS 时走 EXISTS 回退分支），事务全部回滚。[结果](validation/neo4j-bi14-carrier-existence-20261118.json)
- BI17：真实 Neo4j 上 20 个随机夹具 × delta∈{0,4,24}，60/60 一致（40 组非空，含零长度路径与同一棵回复树）。[结果](validation/neo4j-bi17-path-carrier-20261118.json)。BI17 不经 NeuG 绑定校验：NeuG 规划器对这个含 11 条关系的单个 MATCH 在 24 GB 上限下仍内存耗尽（README 7.7，上游问题），证明中记为 `neug_bind: skipped`，只做了转换和 schema 检查。

## 声明约束

用户 2026-11-18 同意。写在 `middleware/profiles/ldbc-snb-neo4j.yaml` 的 `constraints:`，是个别改写步骤的前提（不是定理本身的前提），用到的写入 `Result.premises`。`middleware/tests/constraint_checks.py` 在 SF1 实例（`bolt://127.0.0.1:7693`，只读）上 8/8 条 0 违反（[结果](validation/neo4j-constraint-checks-sf1-20261118.json)）。

| 约束 | 已用于 | 计划用于 |
| --- | --- | --- |
| `(:Message)-[:REPLY_OF]->` 至多一条 | BI17 | BI4 |
| `-[:CONTAINER_OF]->(:Post)` 至多一条 | BI17 | BI4 |
| `(:Person)-[:IS_LOCATED_IN]->` 至多一条 | | BI10、BI11 |
| `(:City)-[:IS_PART_OF]->` 至多一条 | | BI10、BI11 |
| `(:Person)-[:IS_LOCATED_IN]->(:City)` | | BI10、BI11 |
| `(:City)-[:IS_PART_OF]->(:Country)` | | BI10、BI11 |
| `Tag.name` 唯一 | | BI8（BI16） |
| `Country.name` 唯一 | | BI11 |

## 未改写的查询（按计划修复顺序）

### bi10 → `bi-10.cypher`：可改写，下一步修复（声明约束重试阶段）

- 中间件返回：`rewritten=False`，`reason=no_index_selected`
- 注册的索引：`PERSON(countryName)`

**注册的 FILL（NeuG 方言，参数已渲染）**

```cypher
FILL PERSON(countryName) FROM (
MATCH (country:PLACE)<-[:ISPARTOF]-(:PLACE)<-[:ISLOCATEDIN]-(person:PERSON)
RETURN person.id, country.name)
```

**原始 Neo4j 查询**

```cypher
// Q10. Experts in social circle
// Requires the Neo4j APOC library
/*
:params {
    personId: 30786325588624,
    country: 'China',
    tagClass: 'MusicalArtist',
    minPathDistance: 3,
    maxPathDistance: 4
}
*/
MATCH (startPerson:Person {id: $personId})
CALL apoc.path.subgraphNodes(startPerson, {
	relationshipFilter: "KNOWS",
    minLevel: 1,
    maxLevel: $minPathDistance-1
})
YIELD node
WITH startPerson, collect(DISTINCT node) AS nodesCloserThanMinPathDistance
CALL apoc.path.subgraphNodes(startPerson, {
	relationshipFilter: "KNOWS",
    minLevel: 1,
    maxLevel: $maxPathDistance
})
YIELD node
WITH nodesCloserThanMinPathDistance, collect(DISTINCT node) AS nodesCloserThanMaxPathDistance
// compute the difference of sets: nodesCloserThanMaxPathDistance - nodesCloserThanMinPathDistance
WITH [n IN nodesCloserThanMaxPathDistance WHERE NOT n IN nodesCloserThanMinPathDistance] AS expertCandidatePersons
UNWIND expertCandidatePersons AS expertCandidatePerson
MATCH
  (expertCandidatePerson)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(:Country {name: $country}),
  (expertCandidatePerson)<-[:HAS_CREATOR]-(message:Message)-[:HAS_TAG]->(:Tag)-[:HAS_TYPE]->
  (:TagClass {name: $tagClass})
MATCH
  (message)-[:HAS_TAG]->(tag:Tag)
RETURN
  expertCandidatePerson.id,
  tag.name,
  count(DISTINCT message) AS messageCount
ORDER BY
  messageCount DESC,
  tag.name ASC,
  expertCandidatePerson.id ASC
LIMIT 100
```

**中间件返回的 detail**

```
part 1: frontend_unsupported CALL procedure: no internal equivalent | part 4: no_index_selected
```

**卡点**

- 第 1 段（`MATCH (startPerson) CALL apoc.path.subgraphNodes ...`）是 APOC 过程，转换报 `CALL procedure: no internal equivalent`，按设计原样保留；第 2、3 段同理。
- 第 4 段（`UNWIND expertCandidatePersons ... RETURN`）可转换并绑定（APOC 输出类型由 profile 过程契约推出为 `PERSON[]`），但 RewriterV4 返回 `no_index_selected`。
- 不匹配的原因是 **$(M_F)$**：查询写 `(:City)`、`(:Country {name: $country})`，转换后是 `(:PLACE {type: 'City'})`、`(:PLACE {name: $country, type: 'Country'})`；FILL 源 `(country:PLACE)<-[:ISPARTOF]-(:PLACE)<-[:ISLOCATEDIN]-(person:PERSON)` 没有这两个类型谓词。查询过滤比索引过滤多出 `type` 条件，而这两个条件引用的是索引的隐藏顶点（City、Country），无法作为残余过滤 $h$ 补做（$(M_A)$(ii)）。

第 4 段原文与转换后的内部 Cypher（用于与 FILL 源对照；该段单独转换、未加输入桩，实际探针中输入变量带标签，关系表会按端点裁剪）：

```cypher
UNWIND expertCandidatePersons AS expertCandidatePerson
MATCH
  (expertCandidatePerson)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(:Country {name: $country}),
  (expertCandidatePerson)<-[:HAS_CREATOR]-(message:Message)-[:HAS_TAG]->(:Tag)-[:HAS_TYPE]->
  (:TagClass {name: $tagClass})
MATCH
  (message)-[:HAS_TAG]->(tag:Tag)
RETURN
  expertCandidatePerson.id,
  tag.name,
  count(DISTINCT message) AS messageCount
ORDER BY
  messageCount DESC,
  tag.name ASC,
  expertCandidatePerson.id ASC
LIMIT 100
```

```cypher
UNWIND expertCandidatePersons AS expertCandidatePerson
MATCH
  (expertCandidatePerson)-[:ISLOCATEDIN|ORGANISATION_ISLOCATEDIN]->(:PLACE {type: 'City'})-[:ISPARTOF]->(:PLACE {name: $country, type: 'Country'}),
  (expertCandidatePerson)<-[:HASCREATOR]-(message:COMMENT:POST)-[:HASTAG]->(:TAG)-[:HASTYPE]->
  (:TAGCLASS {name: $tagClass})
MATCH
  (message)-[:HASTAG]->(tag:TAG)
RETURN
  expertCandidatePerson.id,
  tag.name,
  count(DISTINCT message) AS messageCount
ORDER BY
  messageCount DESC,
  tag.name ASC,
  expertCandidatePerson.id ASC
LIMIT 100
```

**修复计划**

- 声明约束 `(:Person)-[:IS_LOCATED_IN]->(:City)`、`(:City)-[:IS_PART_OF]->(:Country)` 使这两个类型谓词在每个匹配上恒真，去掉后查询过滤 = 索引过滤，$(M_F)$ 成立。
- `countryName` 是非聚合的投影载体，定理只覆盖可加聚合；需 `(:Person)-[:IS_LOCATED_IN]->` 至多一条、`(:City)-[:IS_PART_OF]->` 至多一条，保证每个 Person 至多一个见证（$|G_k|\le 1$），存的值有定义，行重数不变。
- 实现：常规改写失败后的“声明约束重试”阶段（不改变 BI3 等已有输出形状，README 3.13）；重试结果读取投影载体时检查上述函数依赖，否则拒绝。代码已写好待部署（`p10`），见文末。

### bi11 → `bi-11.cypher`：可改写，依赖 BI10 的重试阶段 + `Country.name` 唯一

- 中间件返回：`rewritten=False`，`reason=no_index_selected`
- 注册的索引：`PERSON(countryName)`

**注册的 FILL（NeuG 方言，参数已渲染）**

```cypher
FILL PERSON(countryName) FROM (
MATCH (country:PLACE)<-[:ISPARTOF]-(:PLACE)<-[:ISLOCATEDIN]-(person:PERSON)
RETURN person.id, country.name)
```

**原始 Neo4j 查询**

```cypher
// Q11. Friend triangles
/*
:params {
  country: 'India',
  startDate: datetime('2012-09-29'),
  endDate: datetime('2013-01-01')
}
*/
MATCH (a:Person)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(country:Country {name: $country}),
      (a)-[k1:KNOWS]-(b:Person)
WHERE a.id < b.id
  AND $startDate <= k1.creationDate AND k1.creationDate <= $endDate
WITH DISTINCT country, a, b
MATCH (b)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(country)
WITH DISTINCT country, a, b
MATCH (b)-[k2:KNOWS]-(c:Person),
      (c)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(country)
WHERE b.id < c.id
  AND $startDate <= k2.creationDate AND k2.creationDate <= $endDate
WITH DISTINCT a, b, c
MATCH (c)-[k3:KNOWS]-(a)
WHERE $startDate <= k3.creationDate AND k3.creationDate <= $endDate
WITH DISTINCT a, b, c
RETURN count(*) AS count
```

**中间件返回的 detail**

```
part 1: no_index_selected | part 2: no_index_selected | part 3: no_index_selected | part 4: no_index_selected
```

**卡点**

- 四段都能转换并绑定，均 `no_index_selected`。
- 除 BI10 的类型谓词问题外，第 1 段把 `country` **节点**投影出去（`WITH DISTINCT country, a, b`），第 2、3 段用 `(b)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(country)` 与输入的同一个节点做身份连接。
- 索引 `PERSON(countryName)` 只公开 Person 和国家**名称**，Country 是隐藏顶点；第 2、3 段中 `country` 属于输入绑定（$H$），于是 $A\cup(H\cap V_I)\subseteq I$ 不成立；第 1 段把隐藏顶点投影出去，$(K)$ 不成立。

第 1 段原文与转换后的内部 Cypher（用于与 FILL 源对照；该段单独转换、未加输入桩，实际探针中输入变量带标签，关系表会按端点裁剪）：

```cypher
MATCH (a:Person)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(country:Country {name: $country}),
      (a)-[k1:KNOWS]-(b:Person)
WHERE a.id < b.id
  AND $startDate <= k1.creationDate AND k1.creationDate <= $endDate
WITH DISTINCT country, a, b
```

```cypher
MATCH (a:PERSON)-[:ISLOCATEDIN]->(:PLACE {type: 'City'})-[:ISPARTOF]->(country:PLACE {name: $country, type: 'Country'}),
      (a)-[k1:KNOWS]-(b:PERSON)
WHERE a.id < b.id
  AND $startDate <= k1.creationDate AND k1.creationDate <= $endDate
WITH DISTINCT country, a, b
```

第 2 段原文与转换后的内部 Cypher（用于与 FILL 源对照；该段单独转换、未加输入桩，实际探针中输入变量带标签，关系表会按端点裁剪）：

```cypher
MATCH (b)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(country)
WITH DISTINCT country, a, b
```

```cypher
MATCH (b:COMMENT:ORGANISATION:PERSON:POST)-[:ISLOCATEDIN|ORGANISATION_ISLOCATEDIN]->(:PLACE {type: 'City'})-[:ISPARTOF]->(country:PLACE)
WITH DISTINCT country, a, b
```

第 3 段原文与转换后的内部 Cypher（用于与 FILL 源对照；该段单独转换、未加输入桩，实际探针中输入变量带标签，关系表会按端点裁剪）：

```cypher
MATCH (b)-[k2:KNOWS]-(c:Person),
      (c)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(country)
WHERE b.id < c.id
  AND $startDate <= k2.creationDate AND k2.creationDate <= $endDate
WITH DISTINCT a, b, c
```

```cypher
MATCH (b:PERSON)-[k2:KNOWS]-(c:PERSON),
      (c)-[:ISLOCATEDIN]->(:PLACE {type: 'City'})-[:ISPARTOF]->(country:PLACE)
WHERE b.id < c.id
  AND $startDate <= k2.creationDate AND k2.creationDate <= $endDate
WITH DISTINCT a, b, c
```

**修复计划**

- 先做 BI10 的类型推导。
- 再用声明约束 `Country.name` 唯一：路径终点是 Country 且名称等于 `country.name`，当且仅当它就是输入的 `country` 节点（需同时确认输入节点是 Country）。把身份连接改写为 `b.countryName = country.name`，Country 不再作为接口顶点，接口条件成立。
- 第 1 段：保留 `MATCH (country:Country {name: $country})`（唯一名称上的索引查找），把 `a` 的地点路径换成 `a.countryName = $country`。

### bi4 → `bi-4.cypher`：可改写，需新的聚合分解规则

- 中间件返回：`rewritten=False`，`reason=no_index_selected`
- 注册的索引：`BI4_MESSAGE_COUNT()`

**注册的 FILL（NeuG 方言，参数已渲染）**

```cypher
FILL BI4_MESSAGE_COUNT() FROM (
  MATCH
    (forum:FORUM)-[:CONTAINEROF]->(post:POST)<-[:REPLYOF*0..]-
    (message:COMMENT:POST)-[:HASCREATOR]->(person:PERSON)
  RETURN forum.id, person.id, count(DISTINCT message)
)
```

**原始 Neo4j 查询**

```cypher
// Q4. Top message creators in a country
/*
:params { date: datetime('2010-01-29') }
*/
MATCH (country:Country)<-[:IS_PART_OF]-(:City)<-[:IS_LOCATED_IN]-(person:Person)<-[:HAS_MEMBER]-(forum:Forum)
WHERE forum.creationDate > $date
WITH country, forum, count(person) AS numberOfMembers
ORDER BY numberOfMembers DESC, forum.id ASC, country.id
WITH DISTINCT forum AS topForum
LIMIT 100

WITH collect(topForum) AS topForums

CALL {
  WITH topForums
  UNWIND topForums AS topForum1
  MATCH (topForum1)-[:CONTAINER_OF]->(post:Post)<-[:REPLY_OF*0..]-(message:Message)-[:HAS_CREATOR]->(person:Person)<-[:HAS_MEMBER]-(topForum2:Forum)
  WITH person, message, topForum2
  WHERE topForum2 IN topForums
  RETURN person, count(DISTINCT message) AS messageCount
UNION ALL
  // Ensure that people who are members of top forums but have 0 messages are also returned.
  // To this end, we return each person with a 0 messageCount
  WITH topForums
  UNWIND topForums AS topForum1
  MATCH (person:Person)<-[:HAS_MEMBER]-(topForum1:Forum)
  RETURN person, 0 AS messageCount
}
RETURN
  person.id AS personId,
  person.firstName AS personFirstName,
  person.lastName AS personLastName,
  person.creationDate AS personCreationDate,
  sum(messageCount) AS messageCount
ORDER BY
  messageCount DESC,
  person.id ASC
LIMIT 100
```

**中间件返回的 detail**

```
part 1: no_index_selected | CALL: no_index_selected: part 1: no_index_selected | part 2: no_index_selected | no_index_selected: part 1: no_index_selected | part 2: no_index_selected
```

**卡点**

- 外层 `CALL { ... UNION ALL ... }`：两条分支在导入绑定下逐一编译，均 `no_index_selected`；去掉 CALL 的展平写法也不匹配。
- 不匹配的原因是 **聚合粒度**：查询第一分支按 Person 做 `count(DISTINCT message)`，消息来自 `topForums` 中的任意 `topForum1`，且作者必须是某个 `topForum2`（同样属于 `topForums`）的成员；索引 `BI4_MESSAGE_COUNT` 按 (Forum, Person) 分组存 `count(DISTINCT message)`，不公开 message，也不含会员约束。把按 Person 的 `count(DISTINCT)` 拆成按 (Forum, Person) 的计数之和，需要证明同一条消息不会出现在两个 Forum 的组里——RewriterV4 没有这类证明，$(L)$ 无法建立。

**修复计划**

- 新规则：消息的根帖唯一（`(:Message)-[:REPLY_OF]->` 至多一条 + 无环），帖子只属于一个论坛（`-[:CONTAINER_OF]->(:Post)` 至多一条），因此 message ↦ forum 是函数，不同 `topForum1` 的消息集合互不相交；`topForums` 来自 `WITH DISTINCT forum ... collect(...)`，元素互不相同。于是 $\mathrm{count}_{\mathrm{DISTINCT}}(\text{按 Person}) = \sum_{f\in topForums} \mathrm{messageCount}(f, person)$。
- 改写为 `UNWIND topForums AS topForum1 MATCH (topForum1)-[c:BI4_MESSAGE_COUNT]->(person) WHERE EXISTS { (person)<-[:HAS_MEMBER]-(f2) WHERE f2 IN topForums } RETURN person, sum(c.messageCount)`；UNION ALL 的零消息分支原样保留。

### bi8 → `bi-8.cypher`：可改写，需 `Tag.name` 唯一 + 前端补充

- 中间件返回：`rewritten=False`，`reason=no_index_selected`
- 注册的索引：`PERSON(bi8MessageCount)`

**注册的 FILL（NeuG 方言，参数已渲染）**

```cypher
FILL PERSON(bi8MessageCount) FROM (
  MATCH(p:PERSON)<-[:HASCREATOR]-(m:POST:COMMENT)-[:HASTAG]->(t:TAG)
  WHERE t.name='Che_Guevara' AND m.creationDate > TIMESTAMP('2010-06-12 00:00:00') AND m.creationDate < TIMESTAMP('2010-06-22 00:00:00') 
  RETURN p.id,count(m)
)
```

**原始 Neo4j 查询**

```cypher
// Q8. Central Person for a Tag
/*
:params { tag: 'Che_Guevara', startDate: datetime('2011-07-20'), endDate: datetime('2011-07-25') }
*/
MATCH (tag:Tag {name: $tag})
// score
OPTIONAL MATCH (tag)<-[interest:HAS_INTEREST]-(person:Person)
WITH tag, collect(person) AS interestedPersons
OPTIONAL MATCH (tag)<-[:HAS_TAG]-(message:Message)-[:HAS_CREATOR]->(person:Person)
         WHERE $startDate < message.creationDate
           AND message.creationDate < $endDate
WITH tag, interestedPersons, interestedPersons + collect(person) AS persons
UNWIND persons AS person
WITH DISTINCT tag, person
WITH
  tag,
  person,
  100 * size([(tag)<-[interest:HAS_INTEREST]-(person) | interest]) + size([(tag)<-[:HAS_TAG]-(message:Message)-[:HAS_CREATOR]->(person) WHERE $startDate < message.creationDate AND message.creationDate < $endDate | message])
  AS score
OPTIONAL MATCH (person)-[:KNOWS]-(friend)
// We need to use a redundant computation due to the lack of composable graph queries in the currently supported Cypher version.
// This might change in the future with new Cypher versions and GQL.
WITH
  person,
  score,
  100 * size([(tag)<-[interest:HAS_INTEREST]-(friend) | interest]) + size([(tag)<-[:HAS_TAG]-(message:Message)-[:HAS_CREATOR]->(friend) WHERE $startDate < message.creationDate AND message.creationDate < $endDate | message])
  AS friendScore
RETURN
  person.id,
  score,
  sum(friendScore) AS friendsScore
ORDER BY
  score + friendsScore DESC,
  person.id ASC
LIMIT 100
```

**中间件返回的 detail**

```
part 1: no_index_selected | part 2: frontend_unsupported NeuG: Binder exception: Cannot match a built-in function for given function +(NODE(LABELS(PERSON,))[],NODE(LABELS(PERSON,))[]). Supported inputs are
(INT128,INT128) -> INT128
(INT64,INT64) -> INT64
(I | part 5: frontend_unsupported function size(): not in the NeuG catalog | expressions: scalar count: scalar count lacks a fresh non-null graph witness | scalar count: no_index_selected: statement changed without reading an index | scalar count: scalar count lacks a fresh non-null graph witness | scalar count: no_index_selected: statement changed without reading an index
```

**卡点**

- 第 2 段 `interestedPersons + collect(person)`（`NODE[] + NODE[]`）NeuG 不支持：`Binder exception: Cannot match a built-in function for given function +(NODE[],NODE[])`，第 3 段以后的前缀因此无法绑定（已有的列表拼接类型推断可给出类型桩）。
- 第 5、6 段的 `size([(tag)<-[:HAS_TAG]-(message:Message)-[:HAS_CREATOR]->(person) WHERE ... | message])` 交给相关标量计数规则，被拒绝：`scalar count lacks a fresh non-null graph witness` / `statement changed without reading an index`。
- 即使前端都通过，**接口条件**仍不成立：计数绑定外层**同一个 Tag 节点**（`MATCH (tag:Tag {name: $tag})` 得到的节点），索引 `PERSON(bi8MessageCount)` 按 Tag **名称**过滤、只公开 Person，Tag 是隐藏顶点却属于输入绑定 $H$。两个同名 Tag 时，索引存的是两者之和，原查询按单个节点计数。

第 5 段原文与转换后的内部 Cypher（用于与 FILL 源对照；该段单独转换、未加输入桩，实际探针中输入变量带标签，关系表会按端点裁剪）：

```cypher
OPTIONAL MATCH (person)-[:KNOWS]-(friend)


WITH
  person,
  score,
  100 * size([(tag)<-[interest:HAS_INTEREST]-(friend) | interest]) + size([(tag)<-[:HAS_TAG]-(message:Message)-[:HAS_CREATOR]->(friend) WHERE $startDate < message.creationDate AND message.creationDate < $endDate | message])
  AS friendScore
```

```cypher
OPTIONAL MATCH (person:PERSON)-[:KNOWS]-(friend:PERSON)


WITH
  person,
  score,
  100 * size([EXISTS { MATCH (tag:TAG)<-[interest:HASINTEREST]-(friend:PERSON) } | interest]) + size([EXISTS { MATCH (tag:TAG)<-[:HASTAG]-(message:COMMENT:POST)-[:HASCREATOR]->(friend:PERSON) } WHERE $startDate < message.creationDate AND message.creationDate < $endDate | message])
  AS friendScore
```

**修复计划**

- 声明约束 `Tag.name` 唯一：外层 `tag` 由名称唯一确定，相关计数里的 `(tag)` 可换成新变量 `(t:Tag {name: $tag})`，Tag 不再是输入接口，$(M_P)$(ii) 成立，计数等于 `person.bi8MessageCount`。
- 前端：`size()` 区域的见证选择（`message` 是新鲜非空节点）需要修正；时间窗字面量问题已随 BI2 修复。

### bi16 → `bi-16.cypher`：理论上可改写，收益低，暂缓

- 中间件返回：`rewritten=False`，`reason=no_index_selected`
- 注册的索引：`PERSON(bi16MessageCountA)`, `PERSON(bi16MessageCountB)`

**注册的 FILL（NeuG 方言，参数已渲染）**

```cypher
FILL PERSON(bi16MessageCountA) FROM (
  MATCH(p:PERSON)<-[:HASCREATOR]-(m:POST:COMMENT)-[:HASTAG]->(t:TAG) WHERE t.name='Marc_Gicquel' AND m.creationDate >= TIMESTAMP('2012-07-26 00:00:00') AND m.creationDate < TIMESTAMP('2012-07-27 00:00:00') RETURN p.id,count(DISTINCT m)
)
```

```cypher
FILL PERSON(bi16MessageCountB) FROM (
  MATCH(p:PERSON)<-[:HASCREATOR]-(m:POST:COMMENT)-[:HASTAG]->(t:TAG) WHERE t.name='Romania' AND m.creationDate >= TIMESTAMP('2012-05-08 00:00:00') AND m.creationDate < TIMESTAMP('2012-05-09 00:00:00') RETURN p.id,count(DISTINCT m)
)
```

**原始 Neo4j 查询**

```cypher
// Q16. Fake news detection
// These parameters return a 'false positive' as the maxKnowsLimit is set too high.
/*
:params {
  tagA: 'Meryl_Streep',
  dateA: datetime('2012-09-16'),
  tagB: 'Hank_Williams',
  dateB: datetime('2012-05-08'),
  maxKnowsLimit: 4
}
*/
UNWIND [
    {letter: 'A', tag: $tagA, date: $dateA},
    {letter: 'B', tag: $tagB, date: $dateB}
  ] AS param
WITH param.letter AS paramLetter, param.tag AS paramTagX, param.date AS paramDateX
CALL {
  WITH paramTagX, paramDateX
  MATCH (person1:Person)<-[:HAS_CREATOR]-(message1:Message)-[:HAS_TAG]->(tag:Tag {name: paramTagX})
  WHERE date(message1.creationDate) = date(paramDateX)
  // filter out Persons with more than $maxKnowsLimit friends who created the same kind of Message
  OPTIONAL MATCH (person1)-[:KNOWS]-(person2:Person)<-[:HAS_CREATOR]-(message2:Message)-[:HAS_TAG]->(tag)
  WHERE date(message2.creationDate) = date(paramDateX)
  WITH person1, count(DISTINCT message1) AS cm, count(DISTINCT person2) AS cp2
  WHERE cp2 <= $maxKnowsLimit
  // return count
  RETURN person1, cm
}
WITH person1, collect({letter: paramLetter, messageCount: cm}) AS results
WHERE size(results) = 2
RETURN
  person1.id,
  [r IN results WHERE r.letter = 'A' | r.messageCount][0] AS messageCountA,
  [r IN results WHERE r.letter = 'B' | r.messageCount][0] AS messageCountB
ORDER BY messageCountA + messageCountB DESC, person1.id ASC
LIMIT 20
```

**中间件返回的 detail**

```
CALL: no declared opaque type prefix
```

**卡点**

- `UNWIND [{letter:'A', tag:$tagA, date:$dateA}, {...B...}] AS param` 后进入相关 `CALL { WITH paramTagX, paramDateX ... }`；CALL 前缀的类型推断报 `no declared opaque type prefix`，CALL 分支未编译。
- CALL 内 `count(DISTINCT message1)`（`cm`）对应索引 A/B 列，但同一个 CALL 里还有 `OPTIONAL MATCH (person1)-[:KNOWS]-(person2)<-[:HAS_CREATOR]-(message2)-[:HAS_TAG]->(tag)`，再次使用第一个 MATCH 的**同一个 Tag 节点**并计算 `count(DISTINCT person2)`（`cp2`），索引不覆盖。
- `date(message1.creationDate) = date(paramDateX)` 与索引的 `[dateXStart, dateXEnd)` 区间之间的等价需要按 UTC 日边界证明。

**修复计划**

- 可行路径：有限记录 CALL 按 `letter` 分支、`Tag.name` 唯一、日期按 UTC 规范化，只把 `cm` 换成 `person1.bi16MessageCountA/B`。
- 判断：`cp2` 的好友 OPTIONAL 仍需现场计算，而改写后要按全体 Person 读属性筛选，可能比原查询从 Tag 出发更慢。暂缓，除非你需要。

### bi19a → `bi-19.cypher`：查询正文无可改写部分；需把 create-graph 脚本纳入范围

- 中间件返回：`rewritten=False`，`reason=frontend_unsupported`
- 注册的索引：`KNOWS(bi14_case1_fwd,bi15_post_fwd)`, `KNOWS(bi14_case1_rev,bi15_post_rev)`, `KNOWS(weight_bi19)`

**注册的 FILL（NeuG 方言，参数已渲染）**

```cypher
FILL  KNOWS(bi14_case1_fwd, bi15_post_fwd) FROM (
  MATCH (pA:PERSON)-[:KNOWS]->(pB:PERSON)
  WITH pA, pB
  MATCH (pA)<-[:HASCREATOR]-(comment:COMMENT)-[:REPLYOF]->(target:COMMENT:POST)-[:HASCREATOR]->(pB)
  RETURN pA.id, pB.id, count(comment),
         sum(CASE label(target) = 'POST' WHEN TRUE THEN 1 ELSE 0 END))
```

```cypher
FILL  KNOWS(bi14_case1_rev, bi15_post_rev) FROM (
  MATCH (pA:PERSON)-[:KNOWS]->(pB:PERSON)
  WITH pA, pB
  MATCH (pB)<-[:HASCREATOR]-(comment:COMMENT)-[:REPLYOF]->(target:COMMENT:POST)-[:HASCREATOR]->(pA)
  RETURN pA.id, pB.id, count(comment),
         sum(CASE label(target) = 'POST' WHEN TRUE THEN 1 ELSE 0 END))
```

```cypher
FILL  KNOWS(weight_bi19) FROM (
  MATCH (pA:PERSON)-[knows:KNOWS]->(pB:PERSON)
  WITH pA, pB, knows,
       knows.bi14_case1_fwd + knows.bi14_case1_rev AS replyCount
  RETURN pA.id, pB.id,
         CAST(CASE replyCount = 0
               WHEN TRUE THEN 10000000000000.0
               ELSE CASE ROUND(40.0 - SQRT(replyCount), 0) > 1.0
                    WHEN TRUE THEN ROUND(40.0 - SQRT(replyCount), 0)
                    ELSE 1.0
                    END
               END, 'DOUBLE'))
```

**原始 Neo4j 查询**

```cypher
// Q19. Interaction path between cities
// Requires the Neo4j Graph Data Science library
/*
:params { city1Id: 669, city2Id: 648 }
*/
MATCH
  (person1:Person)-[:IS_LOCATED_IN]->(city1:City {id: $city1Id}),
  (person2:Person)-[:IS_LOCATED_IN]->(city2:City {id: $city2Id})
CALL gds.shortestPath.dijkstra.stream('bi19', {
  sourceNode: person1,
  targetNode: person2,
  relationshipWeightProperty: 'weight'
})
YIELD totalCost
WITH person1.id AS person1Id, person2.id AS person2Id, totalCost AS totalWeight
ORDER BY totalWeight ASC, person1.id ASC, person2.id ASC
WITH collect({person1Id: person1Id, person2Id: person2Id, totalWeight: totalWeight}) AS results
UNWIND results AS result
WITH result.person1Id AS person1Id, result.person2Id AS person2Id, result.totalWeight AS totalWeight
WHERE totalWeight = results[0].totalWeight
RETURN person1Id, person2Id, totalWeight
ORDER BY person1Id, person2Id
```

**中间件返回的 detail**

```
part 1: frontend_unsupported CALL procedure: no internal equivalent
```

**卡点**

- `bi-19.cypher` 只在已存在的 GDS 图 `bi19` 上调用 `gds.shortestPath.dijkstra.stream`，转换报 `CALL procedure: no internal equivalent`；正文里没有权重计算，没有可匹配的片段。
- 权重在独立脚本 `bi-19-create-graph.cypher` 的 `gds.graph.project.cypher` **字符串参数**里：`count(replyOf)` over `(personA)<-[:HAS_CREATOR]-(:Message)-[replyOf:REPLY_OF]-(:Message)-[:HAS_CREATOR]->(personB)`，权重 `round(40-sqrt(n))`（至少 1）。

**修复计划**

- 若纳入范围：解析投影字符串，交互次数 = `knows.bi14_case1_fwd + knows.bi14_case1_rev`（无向 REPLY_OF 拆成两个方向），权重可直接读 `weight_bi19`。
- **必须加 `n > 0` 过滤**：原投影是 MATCH（只含有交互的人对），NeuG 的 `weight_bi19` 给无交互边 1e13；若直接投影全部 KNOWS 边会改变可达性。
- 需要你确认是否把 `bi-19-create-graph.cypher` 纳入改写范围。

### bi20 → `bi-20.cypher`：查询正文无可改写部分；需把 create-graph 脚本纳入范围

- 中间件返回：`rewritten=False`，`reason=frontend_unsupported`
- 注册的索引：`KNOWS(bi20_direct)`, `KNOWS(bi20_weight)`

**注册的 FILL（NeuG 方言，参数已渲染）**

```cypher
FILL  KNOWS(bi20_direct) FROM (
  MATCH
    (personA:PERSON)-[:KNOWS]->(personB:PERSON),
    (personA)-[saA:STUDYAT]->(university:ORGANISATION)<-[saB:STUDYAT]-(personB)
  RETURN
    personA.id,
    personB.id,
    min(CASE saA.classYear >= saB.classYear
        WHEN TRUE THEN saA.classYear - saB.classYear
        ELSE saB.classYear - saA.classYear
        END) + 1)
```

```cypher
FILL  KNOWS(bi20_weight) FROM (
  MATCH (personA:PERSON)-[knows:KNOWS]->(personB:PERSON)
  RETURN
    personA.id,
    personB.id,
    CASE knows.bi20_direct = 0
    WHEN TRUE THEN 10000000000000.0
    ELSE cast(knows.bi20_direct, 'DOUBLE')
    END)
```

**原始 Neo4j 查询**

```cypher
// Q20. Recruitment
// Requires the Neo4j Graph Data Science library
/*
:params { company: 'Falcon_Air', person2Id: 66 }
*/
MATCH
  (company:Company {name: $company})<-[:WORK_AT]-(person1:Person),
  (person2:Person {id: $person2Id})
CALL gds.shortestPath.dijkstra.stream('bi20', {
  sourceNode: person1,
  targetNode: person2,
  relationshipWeightProperty: 'weight'
})
YIELD totalCost
WHERE person1.id <> $person2Id
WITH person1.id AS person1Id, totalCost AS totalWeight
ORDER BY totalWeight ASC
WITH collect({person1Id: person1Id, totalWeight: totalWeight}) AS results
UNWIND results AS result
WITH result.person1Id AS person1Id, result.totalWeight AS totalWeight
WHERE totalWeight = results[0].totalWeight
RETURN person1Id, totalWeight
ORDER BY person1Id ASC
LIMIT 20
```

**中间件返回的 detail**

```
part 1: frontend_unsupported CALL procedure: no internal equivalent
```

**卡点**

- `bi-20.cypher` 同样只在已存在的 GDS 图 `bi20` 上调用 Dijkstra（`CALL procedure: no internal equivalent`）。
- 权重在 `bi-20-create-graph.cypher` 的投影字符串里：`min(abs(saA.classYear - saB.classYear)) + 1` over `(personA)-[:KNOWS]-(personB), (personA)-[saA:STUDY_AT]->(u:University)<-[saB:STUDY_AT]-(personB)`。

**修复计划**

- 若纳入范围：投影改读 `knows.bi20_direct`，并加 `bi20_direct <> 0` 保持原投影的边集（只含有共同大学的人对）。
- FILL 用 `(university:ORGANISATION)` 不区分 University/Company，需另声明约束 `(:Person)-[:STUDY_AT]->(:University)`。
- 需要你确认是否纳入范围。

### bi15 → `bi-15.cypher`：不建议

- 中间件返回：`rewritten=False`，`reason=no_index_selected`
- 注册的索引：`KNOWS(bi14_case1_fwd,bi15_post_fwd)`, `KNOWS(bi14_case1_rev,bi15_post_rev)`, `KNOWS(bi15_weight)`

**注册的 FILL（NeuG 方言，参数已渲染）**

```cypher
FILL  KNOWS(bi14_case1_fwd, bi15_post_fwd) FROM (
  MATCH (pA:PERSON)-[:KNOWS]->(pB:PERSON)
  WITH pA, pB
  MATCH (pA)<-[:HASCREATOR]-(comment:COMMENT)-[:REPLYOF]->(target:COMMENT:POST)-[:HASCREATOR]->(pB)
  RETURN pA.id, pB.id, count(comment),
         sum(CASE label(target) = 'POST' WHEN TRUE THEN 1 ELSE 0 END))
```

```cypher
FILL  KNOWS(bi14_case1_rev, bi15_post_rev) FROM (
  MATCH (pA:PERSON)-[:KNOWS]->(pB:PERSON)
  WITH pA, pB
  MATCH (pB)<-[:HASCREATOR]-(comment:COMMENT)-[:REPLYOF]->(target:COMMENT:POST)-[:HASCREATOR]->(pA)
  RETURN pA.id, pB.id, count(comment),
         sum(CASE label(target) = 'POST' WHEN TRUE THEN 1 ELSE 0 END))
```

```cypher
FILL  KNOWS(bi15_weight) FROM (
  MATCH (pA:PERSON)-[knows:KNOWS]->(pB:PERSON)
  WITH pA, pB, knows,
       knows.bi14_case1_fwd + knows.bi14_case1_rev AS replyCount,
       knows.bi15_post_fwd + knows.bi15_post_rev AS postReplyCount
  RETURN pA.id, pB.id,
         1.0 / (0.5 * (replyCount + postReplyCount) + 1.0))
```

**原始 Neo4j 查询**

```cypher
// Q15. Weighted interaction paths
// Requires the Neo4j Graph Data Science library
/*
:params {
    person1Id: 14,
    person2Id: 16,
    startDate: datetime('2010-11-01'),
    endDate: datetime('2010-12-01')
}
*/
CALL gds.graph.drop('bi15', false)
YIELD graphName

// ----------------------------------------------------------------------------------------------------
WITH count(*) AS dummy
// ----------------------------------------------------------------------------------------------------

CALL gds.graph.project.cypher(
  'bi15',
  'MATCH (p:Person) RETURN id(p) AS id',
  'MATCH (pA:Person)-[knows:KNOWS]-(pB:Person)
      OPTIONAL MATCH (pA)<-[:HAS_CREATOR]-(m1:Message)-[r:REPLY_OF]-(m2:Message)-[:HAS_CREATOR]->(pB)
      OPTIONAL MATCH (m1)-[:REPLY_OF*0..]->(:Post)<-[:CONTAINER_OF]-(forum:Forum)
              WHERE forum.creationDate >= datetime({epochmillis: ' + $startDate.epochMillis + '})
                AND forum.creationDate <= datetime({epochmillis: ' + $endDate.epochMillis   + '})
      WITH pA, pB,
          sum(CASE forum IS NOT NULL
              WHEN true THEN
                  CASE (m1:Post OR m2:Post) WHEN true THEN 1.0
                  ELSE 0.5 END
              ELSE 0.0 END
          ) AS w
      RETURN
        id(pA) AS source,
        id(pB) AS target,
        1/(w+1) AS weight
  '
)
YIELD graphName

// ----------------------------------------------------------------------------------------------------
WITH count(*) AS dummy
// ----------------------------------------------------------------------------------------------------

CALL {
  MATCH (person1:Person {id: $person1Id}), (person2:Person {id: $person2Id})
  CALL gds.shortestPath.dijkstra.stream('bi15', {
    sourceNode: person1,
    targetNode: person2,
    relationshipWeightProperty: 'weight'
  })
  YIELD index, sourceNode, targetNode, totalCost, nodeIds, costs, path
  RETURN totalCost
  UNION ALL
  RETURN -1.0 AS totalCost
}
RETURN max(totalCost) AS totalCost
```

**中间件返回的 detail**

```
CALL: frontend_unsupported: CALL procedure: no internal equivalent | no_index_selected: 
```

**卡点**

- 权重计算写在 `gds.graph.project.cypher` 的字符串参数里，窗口日期经 `$startDate.epochMillis` 拼进字符串；前端在 CALL 处拒绝（`CALL procedure: no internal equivalent`），QueryPart 中看不到权重计算。
- 原投影的形状是 `OPTIONAL MATCH (pA)<-[:HAS_CREATOR]-(m1)-[r:REPLY_OF]-(m2)-[:HAS_CREATOR]->(pB)` + `OPTIONAL MATCH (m1)-[:REPLY_OF*0..]->(:Post)<-[:CONTAINER_OF]-(forum) WHERE 窗口` + `sum(CASE forum IS NOT NULL ... (m1:Post OR m2:Post) ...)`；FILL 是按方向拆开的两个计数 FILL 加一个合成 FILL（DAG），没有可复用的匹配。

**修复计划**

- 需要“GDS 投影字符串内嵌改写 + 专门的权重等价证明”（方向拆分、`label(target)='POST'` 与 `m1:Post OR m2:Post` 的对应、无交互默认值 1.0）。投入大，不建议。

### bi15w → `bi-15.cypher`：不建议（同 BI15）

- 中间件返回：`rewritten=False`，`reason=no_index_selected`
- 注册的索引：`KNOWS(bi15w_reply_fwd,bi15w_post_fwd)`, `KNOWS(bi15w_reply_rev,bi15w_post_rev)`, `KNOWS(bi15w_weight)`

**注册的 FILL（NeuG 方言，参数已渲染）**

```cypher
FILL KNOWS(bi15w_reply_fwd, bi15w_post_fwd) FROM (
  MATCH (pA:PERSON)-[:KNOWS]->(pB:PERSON)
  WITH pA, pB
  MATCH
    (pA)<-[:HASCREATOR]-(comment:COMMENT)-[:REPLYOF]->(target:COMMENT:POST)-[:HASCREATOR]->(pB),
    (comment)-[:REPLYOF*0..]->(:POST)<-[:CONTAINEROF]-(forum:FORUM)
  WHERE forum.creationDate >= TIMESTAMP('2012-11-06 00:00:00')
    AND forum.creationDate <= TIMESTAMP('2012-11-07 00:00:00')
  RETURN pA.id, pB.id, count(comment),
         sum(CASE label(target) = 'POST' WHEN TRUE THEN 1 ELSE 0 END))
```

```cypher
FILL KNOWS(bi15w_reply_rev, bi15w_post_rev) FROM (
  MATCH (pA:PERSON)-[:KNOWS]->(pB:PERSON)
  WITH pA, pB
  MATCH
    (pB)<-[:HASCREATOR]-(comment:COMMENT)-[:REPLYOF]->(target:COMMENT:POST)-[:HASCREATOR]->(pA),
    (comment)-[:REPLYOF*0..]->(:POST)<-[:CONTAINEROF]-(forum:FORUM)
  WHERE forum.creationDate >= TIMESTAMP('2012-11-06 00:00:00')
    AND forum.creationDate <= TIMESTAMP('2012-11-07 00:00:00')
  RETURN pA.id, pB.id, count(comment),
         sum(CASE label(target) = 'POST' WHEN TRUE THEN 1 ELSE 0 END))
```

```cypher
FILL KNOWS(bi15w_weight) FROM (
  MATCH (pA:PERSON)-[knows:KNOWS]->(pB:PERSON)
  WITH pA, pB, knows,
       knows.bi15w_reply_fwd + knows.bi15w_reply_rev AS replyCount,
       knows.bi15w_post_fwd + knows.bi15w_post_rev AS postReplyCount
  RETURN pA.id, pB.id, 1.0 / (0.5 * (replyCount + postReplyCount) + 1.0))
```

**原始 Neo4j 查询**

```cypher
// Q15. Weighted interaction paths
// Requires the Neo4j Graph Data Science library
/*
:params {
    person1Id: 14,
    person2Id: 16,
    startDate: datetime('2010-11-01'),
    endDate: datetime('2010-12-01')
}
*/
CALL gds.graph.drop('bi15', false)
YIELD graphName

// ----------------------------------------------------------------------------------------------------
WITH count(*) AS dummy
// ----------------------------------------------------------------------------------------------------

CALL gds.graph.project.cypher(
  'bi15',
  'MATCH (p:Person) RETURN id(p) AS id',
  'MATCH (pA:Person)-[knows:KNOWS]-(pB:Person)
      OPTIONAL MATCH (pA)<-[:HAS_CREATOR]-(m1:Message)-[r:REPLY_OF]-(m2:Message)-[:HAS_CREATOR]->(pB)
      OPTIONAL MATCH (m1)-[:REPLY_OF*0..]->(:Post)<-[:CONTAINER_OF]-(forum:Forum)
              WHERE forum.creationDate >= datetime({epochmillis: ' + $startDate.epochMillis + '})
                AND forum.creationDate <= datetime({epochmillis: ' + $endDate.epochMillis   + '})
      WITH pA, pB,
          sum(CASE forum IS NOT NULL
              WHEN true THEN
                  CASE (m1:Post OR m2:Post) WHEN true THEN 1.0
                  ELSE 0.5 END
              ELSE 0.0 END
          ) AS w
      RETURN
        id(pA) AS source,
        id(pB) AS target,
        1/(w+1) AS weight
  '
)
YIELD graphName

// ----------------------------------------------------------------------------------------------------
WITH count(*) AS dummy
// ----------------------------------------------------------------------------------------------------

CALL {
  MATCH (person1:Person {id: $person1Id}), (person2:Person {id: $person2Id})
  CALL gds.shortestPath.dijkstra.stream('bi15', {
    sourceNode: person1,
    targetNode: person2,
    relationshipWeightProperty: 'weight'
  })
  YIELD index, sourceNode, targetNode, totalCost, nodeIds, costs, path
  RETURN totalCost
  UNION ALL
  RETURN -1.0 AS totalCost
}
RETURN max(totalCost) AS totalCost
```

**中间件返回的 detail**

```
CALL: frontend_unsupported: CALL procedure: no internal equivalent | no_index_selected: 
```

**卡点**

- 与 BI15 同一条原查询；定窗 FILL 只是把窗口条件放进了计数 FILL，卡点相同。

**修复计划**

- 同 BI15。

## 已改写的查询（本轮新增）

### bi2 → `bi-2.cypher`

- 路径：`parts:1,2`；读取：`TAG(bi2Window1MessageCount)`, `TAG(bi2Window2MessageCount)`；参数特化：$date
- 前提（声明约束）：无
- 证明记录：RewriterV4 + 回译验证

<details><summary>FILL、原始查询与改写输出</summary>

```cypher
FILL TAG(bi2Window1MessageCount) FROM (
  MATCH(m:POST:COMMENT)-[:HASTAG]->(t:TAG) WHERE m.creationDate >= TIMESTAMP('2010-06-09 00:00:00') AND m.creationDate < TIMESTAMP('2010-09-17 00:00:00') RETURN t.id,count(m)
)
```

```cypher
FILL TAG(bi2Window2MessageCount) FROM (
  MATCH(m:POST:COMMENT)-[:HASTAG]->(t:TAG) WHERE m.creationDate >= TIMESTAMP('2010-09-17 00:00:00') AND m.creationDate < TIMESTAMP('2010-12-26 00:00:00') RETURN t.id,count(m)
)
```

原始查询：

```cypher
// Q2. Tag evolution
/*
:params { date: datetime('2012-06-01'), tagClass: 'MusicalArtist' }
*/
MATCH (tag:Tag)-[:HAS_TYPE]->(:TagClass {name: $tagClass})
// window 1
OPTIONAL MATCH (message1:Message)-[:HAS_TAG]->(tag)
  WHERE $date <= message1.creationDate
    AND message1.creationDate < $date + duration({days: 100})
WITH tag, count(message1) AS countWindow1
// window 2
OPTIONAL MATCH (message2:Message)-[:HAS_TAG]->(tag)
  WHERE $date + duration({days: 100}) <= message2.creationDate
    AND message2.creationDate < $date + duration({days: 200})
WITH
  tag,
  countWindow1,
  count(message2) AS countWindow2
RETURN
  tag.name,
  countWindow1,
  countWindow2,
  abs(countWindow1 - countWindow2) AS diff
ORDER BY
  diff DESC,
  tag.name ASC
LIMIT 100
```

改写输出：

```cypher
MATCH (tag:Tag)-[cico_r2:HAS_TYPE]->(cico_n1:TagClass) WHERE (cico_n1.name = $tagClass) WITH tag, sum(CASE WHEN (tag.bi2Window1MessageCount IS NULL) THEN 0 ELSE tag.bi2Window1MessageCount END) AS countWindow1 WITH tag, countWindow1, sum(CASE WHEN (tag.bi2Window2MessageCount IS NULL) THEN 0 ELSE tag.bi2Window2MessageCount END) AS countWindow2 RETURN
  tag.name,
  countWindow1,
  countWindow2,
  abs(countWindow1 - countWindow2) AS diff
ORDER BY
  diff DESC,
  tag.name ASC
LIMIT 100
```

</details>

### bi14 → `bi-14.cypher`

- 路径：`parts:2,3,4,5`；读取：`KNOWS(bi14_case1_fwd)`, `KNOWS(bi14_case1_rev)`, `KNOWS(bi14_case3_fwd)`, `KNOWS(bi14_case3_rev)`
- 前提（声明约束）：无
- 证明记录：relationship_carrier_existence

<details><summary>FILL、原始查询与改写输出</summary>

```cypher
FILL KNOWS(bi14_case1_fwd) FROM (
MATCH (pA:PERSON)-[knows:KNOWS]->(pB:PERSON)
WITH pA, pB
MATCH (pA)<-[:HASCREATOR]-(c:COMMENT)-[:REPLYOF]->(:COMMENT:POST)-[:HASCREATOR]->(pB)
RETURN pA.id, pB.id, count(c) AS cnt)
```

```cypher
FILL KNOWS(bi14_case1_rev) FROM (
MATCH (pA:PERSON)-[knows:KNOWS]->(pB:PERSON)
WITH pA, pB
MATCH (pB)<-[:HASCREATOR]-(c:COMMENT)-[:REPLYOF]->(:COMMENT:POST)-[:HASCREATOR]->(pA)
RETURN pA.id, pB.id, count(c) AS cnt)
```

```cypher
FILL KNOWS(bi14_case2_fwd) FROM (
MATCH (pA:PERSON)-[knows:KNOWS]->(pB:PERSON)
WITH pA, pB
MATCH (pA)<-[:HASCREATOR]-(m:COMMENT:POST)<-[:REPLYOF]-(:COMMENT)-[:HASCREATOR]->(pB)
RETURN pA.id, pB.id, count(m) AS cnt)
```

```cypher
FILL KNOWS(bi14_case2_rev) FROM (
MATCH (pA:PERSON)-[knows:KNOWS]->(pB:PERSON)
WITH pA, pB
MATCH (pB)<-[:HASCREATOR]-(m:COMMENT:POST)<-[:REPLYOF]-(:COMMENT)-[:HASCREATOR]->(pA)
RETURN pA.id, pB.id, count(m) AS cnt)
```

```cypher
FILL KNOWS(bi14_case3_fwd) FROM (
MATCH (pA:PERSON)-[knows:KNOWS]->(pB:PERSON)
WITH pA, pB
MATCH (pA)-[:LIKES]->(m:COMMENT:POST)-[:HASCREATOR]->(pB)
RETURN pA.id, pB.id, count(m) AS cnt)
```

```cypher
FILL KNOWS(bi14_case3_rev) FROM (
MATCH (pA:PERSON)-[knows:KNOWS]->(pB:PERSON)
WITH pA, pB
MATCH (pB)-[:LIKES]->(m:COMMENT:POST)-[:HASCREATOR]->(pA)
RETURN pA.id, pB.id, count(m) AS cnt)
```

```cypher
FILL KNOWS(bi14_case4_fwd) FROM (
MATCH (pA:PERSON)-[knows:KNOWS]->(pB:PERSON)
WITH pA, pB
MATCH (pA)<-[:HASCREATOR]-(m:COMMENT:POST)<-[:LIKES]-(pB)
RETURN pA.id, pB.id, count(m) AS cnt)
```

```cypher
FILL KNOWS(bi14_case4_rev) FROM (
MATCH (pA:PERSON)-[knows:KNOWS]->(pB:PERSON)
WITH pA, pB
MATCH (pB)<-[:HASCREATOR]-(m:COMMENT:POST)<-[:LIKES]-(pA)
RETURN pA.id, pB.id, count(m) AS cnt)
```

原始查询：

```cypher
// Q14. International dialog
/*
:params { country1: 'Chile', country2: 'Argentina' }
*/
MATCH
  (country1:Country {name: $country1})<-[:IS_PART_OF]-(city1:City)<-[:IS_LOCATED_IN]-(person1:Person),
  (country2:Country {name: $country2})<-[:IS_PART_OF]-(city2:City)<-[:IS_LOCATED_IN]-(person2:Person),
  (person1)-[:KNOWS]-(person2)
WITH person1, person2, city1, 0 AS score
// case 1
OPTIONAL MATCH (person1)<-[:HAS_CREATOR]-(c:Comment)-[:REPLY_OF]->(:Message)-[:HAS_CREATOR]->(person2)
WITH DISTINCT person1, person2, city1, score + (CASE WHEN c IS NULL THEN 0 ELSE  4 END) AS score
// case 2
OPTIONAL MATCH (person1)<-[:HAS_CREATOR]-(m:Message)<-[:REPLY_OF]-(:Comment)-[:HAS_CREATOR]->(person2)
WITH DISTINCT person1, person2, city1, score + (CASE WHEN m IS NULL THEN 0 ELSE  1 END) AS score
// case 3
OPTIONAL MATCH (person1)-[:LIKES]->(m:Message)-[:HAS_CREATOR]->(person2)
WITH DISTINCT person1, person2, city1, score + (CASE WHEN m IS NULL THEN 0 ELSE 10 END) AS score
// case 4
OPTIONAL MATCH (person1)<-[:HAS_CREATOR]-(m:Message)<-[:LIKES]-(person2)
WITH DISTINCT person1, person2, city1, score + (CASE WHEN m IS NULL THEN 0 ELSE  1 END) AS score
// preorder
ORDER BY
  city1.name ASC,
  score DESC,
  person1.id ASC,
  person2.id ASC
WITH city1, collect({score: score, person1Id: person1.id, person2Id: person2.id})[0] AS top
RETURN
  top.person1Id,
  top.person2Id,
  city1.name,
  top.score
ORDER BY
  top.score DESC,
  top.person1Id ASC,
  top.person2Id ASC
LIMIT 100
```

改写输出：

```cypher
MATCH
  (country1:Country {name: $country1})<-[:IS_PART_OF]-(city1:City)<-[:IS_LOCATED_IN]-(person1:Person),
  (country2:Country {name: $country2})<-[:IS_PART_OF]-(city2:City)<-[:IS_LOCATED_IN]-(person2:Person),
  (person1)-[:KNOWS]-(person2)
WITH person1, person2, city1, 0 AS score OPTIONAL MATCH (person1)-[cico_k:KNOWS]-(person2) WITH person1, person2, city1, score, CASE WHEN cico_k IS NULL THEN EXISTS { MATCH (person1)<-[:HAS_CREATOR]-(c:Comment)-[:REPLY_OF]->(:Message)-[:HAS_CREATOR]->(person2) } WHEN startNode(cico_k) = person1 THEN coalesce(cico_k.bi14_case1_fwd, 0) > 0 ELSE coalesce(cico_k.bi14_case1_rev, 0) > 0 END AS cico_exists WITH DISTINCT person1, person2, city1, score + (CASE WHEN (NOT cico_exists) THEN 0 ELSE  4 END) AS score OPTIONAL MATCH (person1)-[cico_k:KNOWS]-(person2) WITH person1, person2, city1, score, CASE WHEN cico_k IS NULL THEN EXISTS { MATCH (person1)<-[:HAS_CREATOR]-(m:Message)<-[:REPLY_OF]-(:Comment)-[:HAS_CREATOR]->(person2) } WHEN startNode(cico_k) = person1 THEN coalesce(cico_k.bi14_case1_rev, 0) > 0 ELSE coalesce(cico_k.bi14_case1_fwd, 0) > 0 END AS cico_exists WITH DISTINCT person1, person2, city1, score + (CASE WHEN (NOT cico_exists) THEN 0 ELSE  1 END) AS score OPTIONAL MATCH (person1)-[cico_k:KNOWS]-(person2) WITH person1, person2, city1, score, CASE WHEN cico_k IS NULL THEN EXISTS { MATCH (person1)-[:LIKES]->(m:Message)-[:HAS_CREATOR]->(person2) } WHEN startNode(cico_k) = person1 THEN coalesce(cico_k.bi14_case3_fwd, 0) > 0 ELSE coalesce(cico_k.bi14_case3_rev, 0) > 0 END AS cico_exists WITH DISTINCT person1, person2, city1, score + (CASE WHEN (NOT cico_exists) THEN 0 ELSE 10 END) AS score OPTIONAL MATCH (person1)-[cico_k:KNOWS]-(person2) WITH person1, person2, city1, score, CASE WHEN cico_k IS NULL THEN EXISTS { MATCH (person1)<-[:HAS_CREATOR]-(m:Message)<-[:LIKES]-(person2) } WHEN startNode(cico_k) = person1 THEN coalesce(cico_k.bi14_case3_rev, 0) > 0 ELSE coalesce(cico_k.bi14_case3_fwd, 0) > 0 END AS cico_exists WITH DISTINCT person1, person2, city1, score + (CASE WHEN (NOT cico_exists) THEN 0 ELSE  1 END) AS score

ORDER BY
  city1.name ASC,
  score DESC,
  person1.id ASC,
  person2.id ASC WITH city1, collect({score: score, person1Id: person1.id, person2Id: person2.id})[0] AS top RETURN
  top.person1Id,
  top.person2Id,
  city1.name,
  top.score
ORDER BY
  top.score DESC,
  top.person1Id ASC,
  top.person2Id ASC
LIMIT 100
```

</details>

### bi17 → `bi-17.cypher`

- 路径：`path_carriers`；读取：`ROOT_POST()`
- 前提（声明约束）：`at_most_one: (:Message)-[:REPLY_OF]->`, `at_most_one: -[:CONTAINER_OF]->(:Post)`
- 证明记录：relation_carrier_path

<details><summary>FILL、原始查询与改写输出</summary>

```cypher
FILL ROOT_POST() FROM (
  MATCH (post:POST)<-[:REPLYOF*0..]-(message:COMMENT:POST)
  RETURN DISTINCT message.id, post.id
)
```

原始查询：

```cypher
// Q17. Information propagation analysis
/*
:params { tag: 'Slavoj_Žižek', delta: 4 }
*/
MATCH
  (tag:Tag {name: $tag}),
  (person1:Person)<-[:HAS_CREATOR]-(message1:Message)-[:REPLY_OF*0..]->(post1:Post)<-[:CONTAINER_OF]-(forum1:Forum),
  (message1)-[:HAS_TAG]->(tag),
// Having two HAS_MEMBER edges in the same MATCH clause ensures that person2 and person3 are different
// as Cypher's edge-isomorphic matching does not allow for such a match in a single MATCH clause.
  (forum1)<-[:HAS_MEMBER]->(person2:Person)<-[:HAS_CREATOR]-(comment:Comment)-[:HAS_TAG]->(tag),
  (forum1)<-[:HAS_MEMBER]->(person3:Person)<-[:HAS_CREATOR]-(message2:Message),
  (comment)-[:REPLY_OF]->(message2)-[:REPLY_OF*0..]->(post2:Post)<-[:CONTAINER_OF]-(forum2:Forum)
// The query allows message2 = post2. If this is the case, their HAS_TAG edges to tag overlap,
// and Cypher's edge-isomorphic matching does not allow for such a match in a single MATCH clause.
// To work around this, we add them in separate MATCH clauses.
MATCH (comment)-[:HAS_TAG]->(tag)
MATCH (message2)-[:HAS_TAG]->(tag)
WHERE forum1 <> forum2
  AND message2.creationDate > message1.creationDate + duration({hours: $delta})
  AND NOT (forum2)-[:HAS_MEMBER]->(person1)
RETURN person1.id, count(DISTINCT message2) AS messageCount
ORDER BY messageCount DESC, person1.id ASC
LIMIT 10
```

改写输出：

```cypher
MATCH
  (tag:Tag {name: $tag}),
  (person1:Person)<-[:HAS_CREATOR]-(message1:Message)-[:ROOT_POST]->(post1:Post)<-[:CONTAINER_OF]-(forum1:Forum),
  (message1)-[:HAS_TAG]->(tag),


  (forum1)<-[:HAS_MEMBER]->(person2:Person)<-[:HAS_CREATOR]-(comment:Comment)-[:HAS_TAG]->(tag),
  (forum1)<-[:HAS_MEMBER]->(person3:Person)<-[:HAS_CREATOR]-(message2:Message),
  (comment)-[:REPLY_OF]->(message2)-[:ROOT_POST]->(post2:Post)<-[:CONTAINER_OF]-(forum2:Forum)



MATCH (comment)-[:HAS_TAG]->(tag)
MATCH (message2)-[:HAS_TAG]->(tag)
WHERE forum1 <> forum2
  AND message2.creationDate > message1.creationDate + duration({hours: $delta})
  AND NOT (forum2)-[:HAS_MEMBER]->(person1)
RETURN person1.id, count(DISTINCT message2) AS messageCount
ORDER BY messageCount DESC, person1.id ASC
LIMIT 10
```

</details>

## 本轮新增的中间件能力

| 能力 | 文件 | 对应理论条件 |
| --- | --- | --- |
| 时间字面量规范化、定长 duration 折叠 | `cico/normalize.py` | $(M_F)$：同一瞬间同一字面量 |
| 关系载体存在性（OPTIONAL + DISTINCT） | `cico/edge_carriers.py` | $(M_P)$ 图同构，$(M_F)$，$(L)$ 取 `count>0`；DISTINCT 另有语义论证 |
| 声明约束与检查语句 | `cico/constraints.py`、`tests/constraint_checks.py` | 改写步骤的前提，`Result.premises` |
| 变长关系唯一性的根路径证明 | `cico/neo4j.py`（`_root_path`、`_uniqueness_implied`、`contradiction`） | Neo4j 关系同构与内部同态等价的前提 |
| 关系载体替换变长路径（回退阶段） | `cico/path_carriers.py` | $(L)$：至多一条路径 ⇒ $|G_k|=1$；同一子句的载体唯一性由过滤推出矛盾 |
| （待部署）声明约束重试：类型谓词推导、投影载体函数依赖检查 | `cico/neo4j.py`、`cico/middleware.py` | $(M_F)$；投影载体 $|G_k|\le 1$ |
