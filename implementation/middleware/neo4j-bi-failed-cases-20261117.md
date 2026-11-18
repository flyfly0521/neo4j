# Neo4j BI 注册失败与完整查询未改写 case 全集（2026-11-17）

供逐个 case 人工分析。本文件按实验文档已有的 15 组 Neo4j BI 索引筛选，收录所有“注册失败”或“完整原始查询未改写”的 case，并内嵌完整源脚本、原始查询、全部已测参数和诊断。文件中的程序输出取自当前持久化验证结果；本次汇总没有重新运行 BI 查询。

## 范围与结论

| 口径 | 数量 | case |
| --- | --- | --- |
| 文档已有索引组 | 15 | BI2、3、4、5、6、8、9、10、11、13、14、15、16、19、20 |
| 注册成功 | 9/15 | BI2、5、6、8、9、10、11、13、16 |
| 注册失败 | 6/15 | BI3、4、14、15、19、20 |
| 完整原始查询未改写 | 7/15 | BI4、8、14、15、16、19、20 |
| 注册成功但完整原始查询未改写 | 2/9 | BI8、16 |
| 本文件收录，以上失败集合去重 | 8 | BI3、4、8、14、15、16、19、20 |

完整查询成功为 8/15 条，每条 10 组参数，共 80/150 个样本改写成功，80/80 个成功输出通过 Neo4j EXPLAIN。注册成功的 9 组中有 7 组对应完整查询成功；额外的 BI3 查询借用了其他已注册索引。BI3 因自身索引脚本注册失败仍收录在本文件。

“改写成功”表示完整原始查询经过中间件后实际读取了至少一个已注册索引，允许只改写其中的 QueryPart；不要求消除全部原图读取。这里不把仅发生文本变化、精确索引源探针成功或手工优化查询存在计为完整查询成功。

## 证据版本与测试上下文

- 实验文档：`/mnt/sdb1/feiyang/IncGQ-experiment-docs`。
- 分支：`jcloud-exp`，跟踪 `origin/jcloud-exp`；本次汇总时工作区干净。
- 文档完整提交：`08f1a2659ea2304b7940fd15db3c23382bf37eb7`。原索引和原查询的 SHA-256 已逐项核对验证矩阵，全部一致。
- [当前注册/查询矩阵](validation/neo4j-bi-theory-matrix-20261117.json)：15 组索引、150 个查询参数样本。
- [当前分层诊断](validation/neo4j-bi-theory-diagnosis-20261117.json)：用于 BI8/16 的注册、FILL、精确源和整条转换对照。
- [理论约束与已知边界](neo4j-theory-hardening.md)、[开发交接](neo4j-bi-handoff.md)。
- Schema：[`middleware/schemas/ldbc-bi.yaml`](../../middleware/schemas/ldbc-bi.yaml)；profile：[`middleware/profiles/ldbc-snb-neo4j.yaml`](../../middleware/profiles/ldbc-snb-neo4j.yaml)。
- 查询测试每个样本新建独立元数据目录，先注册 BI10、BI9、BI5、BI6、BI11 静态定义；BI8/16 另外按各样本参数注册对应 `parameter.cypher`。注册失败的定义不会作为可用索引加入查询测试。
- 参数化索引只注册元数据，没有重建对应物理属性；只做改写与成功输出的 EXPLAIN，没有执行 BI 负载，也不提供实际结果等价或性能结论。
- 参数 JSON 中日期为验证产物的字符串序列化形式；实际测试由 workload 参数加载器提供日期时间类型，复现时不要直接把 JSON 字符串当成原参数类型。样本编号为 0–9。脚本内 `:params` 注释是文档示例，实际使用下方列出的样本参数。
- 每个 case 的“分析线索”单独标明现象和待分析事项；原始诊断原样保留，包括上游工具已经截断的错误。BI8/16 的语义分析不能代替完整的机械化证明。

## Case 目录

- [BI3](#bi3)：注册失败；完整原始查询改写成功，复用其他索引。
- [BI4](#bi4)：注册失败；完整原始查询未改写。
- [BI8](#bi8)：注册成功；完整原始查询未改写。
- [BI14](#bi14)：注册失败；完整原始查询未改写。
- [BI15](#bi15)：注册失败；完整原始查询未改写。
- [BI16](#bi16)：注册成功；完整原始查询未改写。
- [BI19](#bi19)：注册失败；完整原始查询未改写。
- [BI20](#bi20)：注册失败；完整原始查询未改写。

## BI3

**注册：** 失败。**完整原始查询：** 10/10 个样本改写成功。

### 原始注册结果

```json
{
  "file": "/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi3/index.cypher",
  "sha256": "40b0b1ec04a5a3d225a728b99bf38405ef7b58bf26bba5c7ea287c8f5e6de865",
  "status": "unsupported",
  "error": "derived CREATE requires DISTINCT endpoint pairs; use MERGE"
}
```

### 完整索引脚本 index.cypher

源文件：[Neo4j/queries/index/ldbc-bi/bi3/index.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi3/index.cypher)  
SHA-256：`40b0b1ec04a5a3d225a728b99bf38405ef7b58bf26bba5c7ea287c8f5e6de865`

```cypher
CYPHER runtime=slotted
MATCH ()-[rel:ROOT_POST]->()
CALL (rel) {
  DELETE rel
} IN 16 CONCURRENT TRANSACTIONS OF 2000 ROWS;

CYPHER runtime=slotted
MATCH (post:Post)
CALL (post) {
  CREATE (post)-[:ROOT_POST]->(post)
} IN 32 CONCURRENT TRANSACTIONS OF 5000 ROWS;

CYPHER runtime=slotted
MATCH (message:Comment)
CALL (message) {
  MATCH (message)-[:REPLY_OF*1..]->(post:Post)
  CREATE (message)-[:ROOT_POST]->(post)
} IN 16 CONCURRENT TRANSACTIONS OF 2000 ROWS;
```

### 完整原始查询 baseline

源文件：[Neo4j/queries/baseline/ldbc-bi/bi-3.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/baseline/ldbc-bi/bi-3.cypher)  
SHA-256：`a8980d2e0abda909da81c7f56fb7252fc4ded0b7c69f1eb0eea622c9d1d8df45`

```cypher
// Q3. Popular topics in a country
/*
:params { tagClass: 'MusicalArtist', country: 'Burma' }
*/
MATCH
  (:TagClass {name: $tagClass})<-[:HAS_TYPE]-(:Tag)<-[:HAS_TAG]-
  (message:Message)
WITH DISTINCT message
MATCH
  (message)-[:REPLY_OF*0..]->(post:Post)<-[:CONTAINER_OF]-
  (forum:Forum)-[:HAS_MODERATOR]->(person:Person)-[:IS_LOCATED_IN]->
  (:City)-[:IS_PART_OF]->(:Country {name: $country})
RETURN
  forum.id,
  forum.title,
  forum.creationDate,
  person.id,
  count(DISTINCT message) AS messageCount
ORDER BY
  messageCount DESC,
  forum.id ASC
LIMIT 20
```

### 全部已测参数

```json
[
  {
    "sample": 0,
    "parameters": {
      "country": "India",
      "tagClass": "Congressman"
    }
  },
  {
    "sample": 1,
    "parameters": {
      "country": "China",
      "tagClass": "Boxer"
    }
  },
  {
    "sample": 2,
    "parameters": {
      "country": "India",
      "tagClass": "NascarDriver"
    }
  },
  {
    "sample": 3,
    "parameters": {
      "country": "China",
      "tagClass": "Actor"
    }
  },
  {
    "sample": 4,
    "parameters": {
      "country": "India",
      "tagClass": "MusicalArtist"
    }
  },
  {
    "sample": 5,
    "parameters": {
      "country": "China",
      "tagClass": "AmericanFootballPlayer"
    }
  },
  {
    "sample": 6,
    "parameters": {
      "country": "India",
      "tagClass": "Chancellor"
    }
  },
  {
    "sample": 7,
    "parameters": {
      "country": "India",
      "tagClass": "Athlete"
    }
  },
  {
    "sample": 8,
    "parameters": {
      "country": "China",
      "tagClass": "Cricketer"
    }
  },
  {
    "sample": 9,
    "parameters": {
      "country": "China",
      "tagClass": "Thing"
    }
  }
]
```

### 完整原始查询的实际诊断

相同诊断合并展示，`samples` 列出其覆盖的全部样本；保留 reason、detail、命中索引、route、proofs 和回退/EXPLAIN 状态。

```json
{
  "samples": [
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9
  ],
  "result": {
    "rewritten": true,
    "reason": "rewritten",
    "detail": "part 1: no_index_selected",
    "indexes": [
      "BI11_PERSON_COUNTRY()",
      "ROOT_POST()"
    ],
    "specialized": [],
    "route": "parts:2",
    "proofs": [],
    "explain": "ok",
    "query_type": "r"
  }
}
```

### BI3 的成功输出，仅用于说明跨索引复用

以下是 sample 0 的中间件输出；该成功不能视为上面的 BI3 原索引注册成功。其余样本输出见验证矩阵。

```cypher
MATCH
  (:TagClass {name: $tagClass})<-[:HAS_TYPE]-(:Tag)<-[:HAS_TAG]-
  (message:Message)
WITH DISTINCT message MATCH (message)-[cico_r2:ROOT_POST]->(post:Post) MATCH (forum:Forum)-[cico_r3:CONTAINER_OF]->(post) MATCH (forum)-[cico_r4:HAS_MODERATOR]->(person:Person) MATCH (person)-[BI11_PERSON_COUNTRY:BI11_PERSON_COUNTRY]->(cico_n1:Place) WHERE (cico_n1.name = $country) RETURN forum.id, forum.title, forum.creationDate, person.id, count(DISTINCT message) AS messageCount ORDER BY messageCount DESC, forum.id LIMIT 20
```

### 分析线索与尚待判断的问题

**已确认的现象：** 注册器拒绝没有端点去重证明的派生关系 `CREATE`。原脚本先创建 Post 自环，再沿 Comment 的 REPLY_OF 路径创建 ROOT_POST；当前注册结果没有给出可以保证每个端点对只产生一条派生关系的证明。

**不要混淆：** 该查询 10/10 改写成功，实际读取 `ROOT_POST()` 和 `BI11_PERSON_COUNTRY()`；本次查询测试中的 ROOT_POST 来自成功注册的 BI9，而不是本 case 的 BI3 脚本。

**待分析：** 是否可以由模式和正式 schema 约束证明端点唯一，或用能保留原脚本多重集语义的通用规则支持派生 CREATE。报错中的 “use MERGE” 是注册器提示，不代表可以直接把脚本改成 MERGE/DISTINCT 而不验证语义。

下面的优化写法来自实验文档，作为人工比较索引用途的参考。本轮矩阵测试的是上面的 baseline；没有将此脚本作为自动改写输出，也未在本次汇总中测试其能否再次改写。

### 文档手工优化查询（参考）

源文件：[Neo4j/queries/optimized/ldbc-bi/bi-3.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/optimized/ldbc-bi/bi-3.cypher)  
SHA-256：`0c8618f795c959e112c2e395c3e948e4e3c29f42dfe57f0dbea39fcac6c16c87`

```cypher
// Q3. Choose the expansion side from the actual distinct message count.
// The 100000-row threshold changes only the plan, never the result or path depth.
// Uses the shared ROOT_POST and BI11_PERSON_COUNTRY materializations.
MATCH(:TagClass {name:$tagClass})<-[:HAS_TYPE]-(:Tag)<-[:HAS_TAG]-(message:Message)
WITH collect(DISTINCT message) AS messages
WHERE size(messages)>0
CALL(messages) {
 WITH messages WHERE size(messages)<100000
 UNWIND messages AS message
 MATCH(message)-[:ROOT_POST]->(:Post)<-[:CONTAINER_OF]-(forum:Forum)-[:HAS_MODERATOR]->(person:Person)-[:BI11_PERSON_COUNTRY]->(:Country {name:$country})
 RETURN forum,person,count(DISTINCT message) AS messageCount
 UNION ALL
 WITH messages WHERE size(messages)>=100000
 MATCH(country:Country {name:$country})<-[:BI11_PERSON_COUNTRY]-(person:Person)<-[:HAS_MODERATOR]-(forum:Forum)-[:CONTAINER_OF]->(:Post)<-[:ROOT_POST]-(message:Message)
 USING INDEX country:Country(name)
 WHERE message IN messages
 RETURN forum,person,count(DISTINCT message) AS messageCount
}
RETURN forum.id,forum.title,forum.creationDate,person.id,messageCount
ORDER BY messageCount DESC,forum.id ASC LIMIT 20
```

## BI4

**注册：** 失败。**完整原始查询：** 0/10 个样本改写成功。

### 原始注册结果

```json
{
  "file": "/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi4/index.cypher",
  "sha256": "17caaecb9541144adaf96fc6cffcf63b67f3ee53961b9e33980f0f8f8715ff79",
  "status": "unsupported",
  "error": "mixed index writes; use one declaration per statement"
}
```

### 完整索引脚本 index.cypher

源文件：[Neo4j/queries/index/ldbc-bi/bi4/index.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi4/index.cypher)  
SHA-256：`17caaecb9541144adaf96fc6cffcf63b67f3ee53961b9e33980f0f8f8715ff79`

```cypher
// Cache the distinct message count for every root-forum/author pair. A person
// need not be a member of the forum where their message is rooted.
MATCH ()-[rel:BI4_MESSAGE_COUNT]->()
CALL (rel) {
  DELETE rel
} IN TRANSACTIONS OF 10000 ROWS;

MATCH (forum:Forum)
CALL (forum) {
  MATCH
    (forum)-[:CONTAINER_OF]->(post:Post)<-[:REPLY_OF*0..]-
    (message:Message)-[:HAS_CREATOR]->(person:Person)
  WITH forum, person, count(DISTINCT message) AS messageCount
  MERGE (forum)-[aggregate:BI4_MESSAGE_COUNT]->(person)
  SET aggregate.messageCount = messageCount
} IN TRANSACTIONS OF 100 ROWS;

// Rank the distinct forums by the maximum member count over countries.
// This is independent of the query date and must be maintained when member
// relationships or member locations change. Zero-member forums stay at zero.
MATCH (forum:Forum)
CALL (forum) {
  SET forum.bi4MaxCountryMemberCount = 0
} IN TRANSACTIONS OF 10000 ROWS;

MATCH (forum:Forum)-[:HAS_MEMBER]->(person:Person)-[:IS_LOCATED_IN]->
      (:City)-[:IS_PART_OF]->(country:Country)
WITH forum, country, count(person) AS numberOfMembers
WITH forum, max(numberOfMembers) AS maxCountryMembers
CALL (forum, maxCountryMembers) {
  SET forum.bi4MaxCountryMemberCount = maxCountryMembers
} IN TRANSACTIONS OF 10000 ROWS;
```

### 完整原始查询 baseline

源文件：[Neo4j/queries/baseline/ldbc-bi/bi-4.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/baseline/ldbc-bi/bi-4.cypher)  
SHA-256：`92f3385fc493b725b0785b76d929854af18969bc8090fe652894723c20ba1be2`

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

### 全部已测参数

```json
[
  {
    "sample": 0,
    "parameters": {
      "date": "2010-02-12 00:00:00"
    }
  },
  {
    "sample": 1,
    "parameters": {
      "date": "2010-01-22 00:00:00"
    }
  },
  {
    "sample": 2,
    "parameters": {
      "date": "2010-01-31 00:00:00"
    }
  },
  {
    "sample": 3,
    "parameters": {
      "date": "2010-01-07 00:00:00"
    }
  },
  {
    "sample": 4,
    "parameters": {
      "date": "2010-01-24 00:00:00"
    }
  },
  {
    "sample": 5,
    "parameters": {
      "date": "2010-01-23 00:00:00"
    }
  },
  {
    "sample": 6,
    "parameters": {
      "date": "2010-02-02 00:00:00"
    }
  },
  {
    "sample": 7,
    "parameters": {
      "date": "2010-01-27 00:00:00"
    }
  },
  {
    "sample": 8,
    "parameters": {
      "date": "2010-01-12 00:00:00"
    }
  },
  {
    "sample": 9,
    "parameters": {
      "date": "2010-01-15 00:00:00"
    }
  }
]
```

### 完整原始查询的实际诊断

相同诊断合并展示，`samples` 列出其覆盖的全部样本；保留 reason、detail、命中索引、route、proofs 和回退/EXPLAIN 状态。

```json
{
  "samples": [
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9
  ],
  "result": {
    "rewritten": false,
    "reason": "no_index_selected",
    "detail": "part 1: no_index_selected | CALL: no_index_selected: part 1: no_index_selected | part 2: no_index_selected | no_index_selected: part 1: no_index_selected | part 2: no_index_selected",
    "indexes": [],
    "specialized": [],
    "route": "",
    "proofs": [],
    "original_preserved": true
  }
}
```

### 分析线索与尚待判断的问题

**已确认的现象：** 脚本包含派生关系 MERGE、关系属性写入、节点属性清零和分组后节点属性写回。当前注册在混合写入识别阶段拒绝，因此尚未进入该索引的正常候选匹配。

**待分析：** 将脚本归纳为多个有明确载体、输入模式、分组键和结果表达式的物化定义，尤其是 `BI4_MESSAGE_COUNT.messageCount` 关系聚合与 `Forum.bi4MaxCountryMemberCount` 的初始化/覆盖语义。不能把当前完整查询的 `no_index_selected` 单独归因为 NeuG 图模式匹配缺陷，因为对应定义尚未注册。

下面的优化写法来自实验文档，作为人工比较索引用途的参考。本轮矩阵测试的是上面的 baseline；没有将此脚本作为自动改写输出，也未在本次汇总中测试其能否再次改写。

### 文档手工优化查询（参考）

源文件：[Neo4j/queries/optimized/ldbc-bi/bi-4.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/optimized/ldbc-bi/bi-4.cypher)  
SHA-256：`e9bbc349b29681b3b6ebb1c49ee20b40e3b91f1b8b2fcf01e7a17bd69284a3ed`

```cypher
// Q4. Uses bi4MaxCountryMemberCount and BI4_MESSAGE_COUNT from BI4 setup.
// Union membership eligibility with forward message counts; retain zero counts.
MATCH (forum:Forum)
WHERE forum.creationDate > $date AND forum.bi4MaxCountryMemberCount > 0
WITH forum ORDER BY forum.bi4MaxCountryMemberCount DESC, forum.id ASC LIMIT 100
WITH forum AS topForum
WITH collect(topForum) AS topForums
CALL (topForums) {
 UNWIND topForums AS forum
 MATCH (forum)-[:HAS_MEMBER]->(person:Person)
 RETURN person, 0 AS messages, 1 AS member
 UNION ALL
 UNWIND topForums AS forum
 MATCH (forum)-[aggregate:BI4_MESSAGE_COUNT]->(person:Person)
 RETURN person, aggregate.messageCount AS messages, 0 AS member
}
WITH person, sum(messages) AS messageCount, max(member) AS isMember
WHERE isMember = 1
WITH person, messageCount ORDER BY messageCount DESC, person.id ASC LIMIT 100
RETURN person.id AS personId, person.firstName AS personFirstName,
 person.lastName AS personLastName, person.creationDate AS personCreationDate, messageCount
ORDER BY messageCount DESC, personId ASC
```

## BI8

**注册：** 成功。**完整原始查询：** 0/10 个样本改写成功。

### 原始注册结果

```json
{
  "file": "/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi8/index.cypher",
  "sha256": "fcddb3bdadf019a87f3692aad1b3bb8e7a124b6c369319f825009423da27f617",
  "parameter_file": "/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi8/parameter.cypher",
  "status": "registered",
  "indexes": [
    "PERSON(bi8MessageCount)"
  ]
}
```

### 完整索引脚本 index.cypher

源文件：[Neo4j/queries/index/ldbc-bi/bi8/index.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi8/index.cypher)  
SHA-256：`fcddb3bdadf019a87f3692aad1b3bb8e7a124b6c369319f825009423da27f617`

```cypher
// Run parameter.cypher after this schema step; rebuild when its indexed parameters change.
MATCH(n:Person) SET n.bi8MessageCount=0;
```

该组 `index.cypher` 是清理脚本；本轮注册输入为下方 `parameter.cypher`，并绑定对应样本参数。

### 实际注册输入 parameter.cypher

源文件：[Neo4j/queries/index/ldbc-bi/bi8/parameter.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi8/parameter.cypher)  
SHA-256：`9eeafbba02a22f220e2b28a54fccc2f09930cdbe88ff66c17ac05a30fcdfbb62`

```cypher
// Snapshot rebuild on the optimized copy only; do not query during this step.
MATCH(n:Person) SET n.bi8MessageCount=0;
MATCH(p:Person)<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag)
WHERE t.name=$tag AND m.creationDate>$startDate AND m.creationDate<$endDate
WITH p,count(m) AS value
SET p.bi8MessageCount=value;
```

### 完整原始查询 baseline

源文件：[Neo4j/queries/baseline/ldbc-bi/bi-8.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/baseline/ldbc-bi/bi-8.cypher)  
SHA-256：`dc4c117b58fbe671ae4d2dbb846d3f5227d652e851ca4b127d8794b9cf10d77b`

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

### 全部已测参数

```json
[
  {
    "sample": 0,
    "parameters": {
      "startDate": "2010-06-12 00:00:00",
      "endDate": "2010-06-22 00:00:00",
      "tag": "Che_Guevara"
    }
  },
  {
    "sample": 1,
    "parameters": {
      "startDate": "2010-06-13 00:00:00",
      "endDate": "2010-06-25 00:00:00",
      "tag": "Charles_Darwin"
    }
  },
  {
    "sample": 2,
    "parameters": {
      "startDate": "2010-06-26 00:00:00",
      "endDate": "2010-07-09 00:00:00",
      "tag": "Bosnia_and_Herzegovina"
    }
  },
  {
    "sample": 3,
    "parameters": {
      "startDate": "2010-06-24 00:00:00",
      "endDate": "2010-07-08 00:00:00",
      "tag": "China"
    }
  },
  {
    "sample": 4,
    "parameters": {
      "startDate": "2010-06-09 00:00:00",
      "endDate": "2010-06-23 00:00:00",
      "tag": "Abbas_I_of_Persia"
    }
  },
  {
    "sample": 5,
    "parameters": {
      "startDate": "2010-06-27 00:00:00",
      "endDate": "2010-07-07 00:00:00",
      "tag": "Charles_Gounod"
    }
  },
  {
    "sample": 6,
    "parameters": {
      "startDate": "2010-06-27 00:00:00",
      "endDate": "2010-07-07 00:00:00",
      "tag": "County_of_Edessa"
    }
  },
  {
    "sample": 7,
    "parameters": {
      "startDate": "2010-06-17 00:00:00",
      "endDate": "2010-06-27 00:00:00",
      "tag": "Arnold_Schwarzenegger"
    }
  },
  {
    "sample": 8,
    "parameters": {
      "startDate": "2010-06-14 00:00:00",
      "endDate": "2010-06-28 00:00:00",
      "tag": "Barack_Obama"
    }
  },
  {
    "sample": 9,
    "parameters": {
      "startDate": "2010-06-27 00:00:00",
      "endDate": "2010-07-07 00:00:00",
      "tag": "Bosnia_and_Herzegovina"
    }
  }
]
```

### 完整原始查询的实际诊断

相同诊断合并展示，`samples` 列出其覆盖的全部样本；保留 reason、detail、命中索引、route、proofs 和回退/EXPLAIN 状态。

```json
{
  "samples": [
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9
  ],
  "result": {
    "parameter_indexes": [
      "PERSON(bi8MessageCount)"
    ],
    "rewritten": false,
    "reason": "no_index_selected",
    "detail": "part 1: no_index_selected | part 2: frontend_unsupported NeuG: Binder exception: Cannot match a built-in function for given function +(NODE(LABELS(PERSON,))[],NODE(LABELS(PERSON,))[]). Supported inputs are\n(INT128,INT128) -> INT128\n(INT64,INT64) -> INT64\n(I | part 5: frontend_unsupported function size(): not in the NeuG catalog | expressions: scalar count: scalar count lacks a fresh non-null graph witness | scalar count: no_index_selected: statement changed without reading an index | scalar count: scalar count lacks a fresh non-null graph witness | scalar count: no_index_selected: statement changed without reading an index",
    "indexes": [],
    "specialized": [],
    "route": "",
    "proofs": [],
    "original_preserved": true
  }
}
```

### 注册后内部 FILL 与精确源对照（sample 0）

下方 FILL 是程序实际生成的内部声明，参数已实例化。精确源 probe 仅说明源模式可以绑定并使用索引，未执行，也不代表完整查询能改写。

```cypher
FILL PERSON(bi8MessageCount) FROM (MATCH (p:PERSON) OPTIONAL MATCH (p:PERSON)<-[:HASCREATOR]-(m:COMMENT:POST)-[:HASTAG]->(t:TAG)
WHERE t.name='Che_Guevara' AND m.creationDate>TIMESTAMP('2010-06-12 00:00:00') AND m.creationDate<TIMESTAMP('2010-06-22 00:00:00') RETURN p.id, count(m))
```

```json
{
  "sample": 0,
  "parameters": {
    "startDate": "2010-06-12 00:00:00",
    "endDate": "2010-06-22 00:00:00",
    "tag": "Che_Guevara"
  },
  "exact_sources": [
    {
      "query": "MATCH (p:PERSON) OPTIONAL MATCH (p:PERSON)<-[:HASCREATOR]-(m:COMMENT:POST)-[:HASTAG]->(t:TAG)\nWHERE t.name='Che_Guevara' AND m.creationDate>TIMESTAMP('2010-06-12 00:00:00') AND m.creationDate<TIMESTAMP('2010-06-22 00:00:00') RETURN p.id, count(m)",
      "baseline_bound": true,
      "used": [
        "PERSON(bi8MessageCount)"
      ],
      "changed": true,
      "cost_compared": false,
      "index_entries": [
        "fill_PERSON_bi8MessageCount___neug_fill_state_n_rows_62d875d5fe49a9c0_1f6cc3448bd3"
      ]
    }
  ],
  "whole_translation_error": "function size(): not in the NeuG catalog"
}
```

`whole_translation_error` 来自整条查询直接转换探针；“完整原始查询的实际诊断”来自会尝试局部改写的中间件，两条路径的错误粒度不同。

### 分析线索与尚待判断的问题

**已确认的现象：** `parameter.cypher` 注册成功，精确索引源能转换、绑定并使用 `PERSON(bi8MessageCount)`；完整查询仍 0/10。原始诊断同时记录列表拼接的 NeuG 类型错误、未直接支持的 `size()` 和相关标量计数未命中。当前增强已有局部列表类型探针和相关标量计数路径，所以这些错误不是“整个查询从未尝试局部匹配”的证据。

**已有语义分析：** score 中的消息计数同时依赖已绑定的 Person 和 Tag，现有 Person 属性索引只公开 Person，把 Tag 隐藏在按名称过滤的索引源内。当前 schema 没有声明 Tag.name 唯一，名称相同不能替代节点身份，接口条件 `A ∪ (H ∩ V_I) ⊆ I` 无法由现有信息保证。

反例：两个不同 Tag 都名为 `x`；某 Person 各创建一条只连接其中一个 Tag 的消息。按名称 `x` 物化的 Person 计数为 2，但绑定某个具体 Tag 后该表达式的计数为 1。直接替换会改变结果。该反例是语义分析，不是这次在 BI 数据上执行得到的结果。

**待分析：** 正式声明并利用可证明的唯一性/函数依赖，或公开 Person 与 Tag 的索引接口，并验证重数和 OPTIONAL 语义。不能仅放松模板。下方保留实际生成的 FILL 与精确源探针，可用于区分转换和完整上下文的限制。

下面的优化写法来自实验文档，作为人工比较索引用途的参考。本轮矩阵测试的是上面的 baseline；没有将此脚本作为自动改写输出，也未在本次汇总中测试其能否再次改写。

### 文档手工优化查询（参考）

源文件：[Neo4j/queries/optimized/ldbc-bi/bi-8.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/optimized/ldbc-bi/bi-8.cypher)  
SHA-256：`822552d2b4254630235a62c7b7b526d222fc5ae06633a8712639e3cef829e1f2`

```cypher
MATCH(p:Person)
OPTIONAL MATCH(p)-[i:HAS_INTEREST]->(it:Tag) WHERE it.name=$tag
WITH p,count(i) AS ic
WITH p,100*ic+p.bi8MessageCount AS score
WHERE score>0
OPTIONAL MATCH(p)-[k:KNOWS]-(friend:Person)
WITH p.id AS pid,score,friend.id AS fid,count(k) AS multiplicity
OPTIONAL MATCH(friend:Person {id:fid})-[fi:HAS_INTEREST]->(ft:Tag) WHERE ft.name=$tag
WITH pid,score,fid,multiplicity,count(fi) AS fic
OPTIONAL MATCH(friend:Person {id:fid})
WITH pid,score,multiplicity,fic,coalesce(friend.bi8MessageCount,0) AS fmc
RETURN pid AS personId,score,coalesce(sum(multiplicity*(100*fic+fmc)),0) AS friendsScore
ORDER BY score+friendsScore DESC,personId ASC LIMIT 100;
```

## BI14

**注册：** 失败。**完整原始查询：** 0/10 个样本改写成功。

### 原始注册结果

```json
{
  "file": "/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi14/index.cypher",
  "sha256": "98ea2cd06fd58598d676038e57a931135b7f79fc3aa50d347600ae2a717fb4ae",
  "status": "unsupported",
  "error": "mixed index writes; use one declaration per statement"
}
```

### 完整索引脚本 index.cypher

源文件：[Neo4j/queries/index/ldbc-bi/bi14/index.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi14/index.cypher)  
SHA-256：`98ea2cd06fd58598d676038e57a931135b7f79fc3aa50d347600ae2a717fb4ae`

```cypher
// Store the four BI14 interaction-case counts in both directions of each
// KNOWS relationship. Fwd means lower Person.id to higher Person.id. The timed
// query converts nonzero counts into the benchmark's 4/1/10/1 score.
MATCH ()-[knows:KNOWS]->()
REMOVE knows.bi14_score_fwd,
       knows.bi14_score_rev
SET knows.bi14_case1_fwd = 0,
    knows.bi14_case1_rev = 0,
    knows.bi14_case2_fwd = 0,
    knows.bi14_case2_rev = 0,
    knows.bi14_case3_fwd = 0,
    knows.bi14_case3_rev = 0,
    knows.bi14_case4_fwd = 0,
    knows.bi14_case4_rev = 0;

MATCH (from:Person)<-[:HAS_CREATOR]-(reply:Comment)-[:REPLY_OF]->
      (:Message)-[:HAS_CREATOR]->(to:Person)
WITH from, to, count(reply) AS interactionCount
MATCH (from)-[knows:KNOWS]-(to)
SET knows.bi14_case1_fwd =
      CASE WHEN from.id < to.id
        THEN interactionCount ELSE knows.bi14_case1_fwd END,
    knows.bi14_case1_rev =
      CASE WHEN from.id < to.id
        THEN knows.bi14_case1_rev ELSE interactionCount END,
    knows.bi14_case2_fwd =
      CASE WHEN from.id < to.id
        THEN knows.bi14_case2_fwd ELSE interactionCount END,
    knows.bi14_case2_rev =
      CASE WHEN from.id < to.id
        THEN interactionCount ELSE knows.bi14_case2_rev END;

MATCH (from:Person)-[:LIKES]->(message:Message)-[:HAS_CREATOR]->(to:Person)
WITH from, to, count(message) AS interactionCount
MATCH (from)-[knows:KNOWS]-(to)
SET knows.bi14_case3_fwd =
      CASE WHEN from.id < to.id
        THEN interactionCount ELSE knows.bi14_case3_fwd END,
    knows.bi14_case3_rev =
      CASE WHEN from.id < to.id
        THEN knows.bi14_case3_rev ELSE interactionCount END,
    knows.bi14_case4_fwd =
      CASE WHEN from.id < to.id
        THEN knows.bi14_case4_fwd ELSE interactionCount END,
    knows.bi14_case4_rev =
      CASE WHEN from.id < to.id
        THEN interactionCount ELSE knows.bi14_case4_rev END;
```

### 完整原始查询 baseline

源文件：[Neo4j/queries/baseline/ldbc-bi/bi-14.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/baseline/ldbc-bi/bi-14.cypher)  
SHA-256：`d37afff37a136cffa37c7650dcec2e6952a1b169d7134f479a4b6dc7f88c50f6`

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

### 全部已测参数

```json
[
  {
    "sample": 0,
    "parameters": {
      "country1": "China",
      "country2": "Canada"
    }
  },
  {
    "sample": 1,
    "parameters": {
      "country1": "Russia",
      "country2": "Iran"
    }
  },
  {
    "sample": 2,
    "parameters": {
      "country1": "Vietnam",
      "country2": "Burma"
    }
  },
  {
    "sample": 3,
    "parameters": {
      "country1": "Brazil",
      "country2": "Philippines"
    }
  },
  {
    "sample": 4,
    "parameters": {
      "country1": "India",
      "country2": "Canada"
    }
  },
  {
    "sample": 5,
    "parameters": {
      "country1": "Germany",
      "country2": "Vietnam"
    }
  },
  {
    "sample": 6,
    "parameters": {
      "country1": "Pakistan",
      "country2": "Afghanistan"
    }
  },
  {
    "sample": 7,
    "parameters": {
      "country1": "Japan",
      "country2": "Pakistan"
    }
  },
  {
    "sample": 8,
    "parameters": {
      "country1": "Germany",
      "country2": "Egypt"
    }
  },
  {
    "sample": 9,
    "parameters": {
      "country1": "Indonesia",
      "country2": "Russia"
    }
  }
]
```

### 完整原始查询的实际诊断

相同诊断合并展示，`samples` 列出其覆盖的全部样本；保留 reason、detail、命中索引、route、proofs 和回退/EXPLAIN 状态。

```json
{
  "samples": [
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9
  ],
  "result": {
    "rewritten": false,
    "reason": "no_index_selected",
    "detail": "part 1: no_index_selected | part 2: no_index_selected statement changed without reading an index | part 3: no_index_selected statement changed without reading an index | part 4: no_index_selected | part 5: no_index_selected",
    "indexes": [],
    "specialized": [],
    "route": "",
    "proofs": [],
    "original_preserved": true
  }
}
```

### 分析线索与尚待判断的问题

**已确认的现象：** 脚本对 KNOWS 关系执行 REMOVE、八个属性初始化，以及依赖端点 id 顺序的多属性 CASE 写回。当前首先以混合写入错误拒绝，完整查询没有读取索引。

**待分析：** 关系作为索引载体、多个结果列、方向规范化、条件写回保留旧值和初始化的组合语义，以及后续查询按 4/1/10/1 权重计算分数的等价条件。当前结果只证明注册入口不支持该脚本，不能据此判断补齐注册后内核一定能匹配。

下面的优化写法来自实验文档，作为人工比较索引用途的参考。本轮矩阵测试的是上面的 baseline；没有将此脚本作为自动改写输出，也未在本次汇总中测试其能否再次改写。

### 文档手工优化查询（参考）

源文件：[Neo4j/queries/optimized/ldbc-bi/bi-14.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/optimized/ldbc-bi/bi-14.cypher)  
SHA-256：`faddb75f5e56b1b32162166277ae4ec2ce0423f1f0a7d30a333e622ab519f497`

```cypher
// Q14. Read four directional interaction counts from KNOWS and calculate the
// benchmark score at query time.
MATCH
  (country1:Country {name: $country1})<-[:IS_PART_OF]-(city1:City)<-[:IS_LOCATED_IN]-(person1:Person),
  (country2:Country {name: $country2})<-[:IS_PART_OF]-(city2:City)<-[:IS_LOCATED_IN]-(person2:Person),
  (person1)-[knows:KNOWS]-(person2)
WITH person1, person2, city1,
     CASE WHEN person1.id < person2.id
       THEN coalesce(knows.bi14_case1_fwd, 0)
       ELSE coalesce(knows.bi14_case1_rev, 0)
     END AS case1Count,
     CASE WHEN person1.id < person2.id
       THEN coalesce(knows.bi14_case2_fwd, 0)
       ELSE coalesce(knows.bi14_case2_rev, 0)
     END AS case2Count,
     CASE WHEN person1.id < person2.id
       THEN coalesce(knows.bi14_case3_fwd, 0)
       ELSE coalesce(knows.bi14_case3_rev, 0)
     END AS case3Count,
     CASE WHEN person1.id < person2.id
       THEN coalesce(knows.bi14_case4_fwd, 0)
       ELSE coalesce(knows.bi14_case4_rev, 0)
     END AS case4Count
WITH person1, person2, city1,
     CASE WHEN case1Count = 0 THEN 0 ELSE 4 END
     + CASE WHEN case2Count = 0 THEN 0 ELSE 1 END
     + CASE WHEN case3Count = 0 THEN 0 ELSE 10 END
     + CASE WHEN case4Count = 0 THEN 0 ELSE 1 END AS score
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

## BI15

**注册：** 失败。**完整原始查询：** 0/10 个样本改写成功。

### 原始注册结果

```json
{
  "file": "/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi15/index.cypher",
  "sha256": "0358ac3df7689d4476ae55dbb6c123ebf5b3c2c34bc8c0b321931071cc4a709d",
  "status": "unsupported",
  "error": "index carrier must have an explicit node label"
}
```

### 完整索引脚本 index.cypher

源文件：[Neo4j/queries/index/ldbc-bi/bi15/index.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi15/index.cypher)  
SHA-256：`0358ac3df7689d4476ae55dbb6c123ebf5b3c2c34bc8c0b321931071cc4a709d`

```cypher
// BI15 optimized materialization for Neo4j.
//
// Parameters: $startDate, $endDate (datetime).
//
// Every KNOWS edge starts at weight 1.0, which is what the baseline
// projection yields for a pair with no in-window interaction (GDS falls back
// to defaultWeight when the projected weight is null). Edges with a reply
// exchange whose root post lives in a forum created inside the window get
// 1 / (interactionWeight + 1) with the official 1.0 / 0.5 scoring.
//
// The root lookup lives in its own MATCH clause. Cypher enforces
// relationship uniqueness within a pattern, so writing the reply edge and the
// (m1)-[:REPLY_OF*0..]->(:Post) chain in one pattern drops every reply whose
// parent is the root post itself -- the most common case. Kept in one pattern
// this scored 27.5 where the baseline counts 32.0 for person pair 7937 /
// 8796093030850 over the full date range.
MATCH ()-[knows:KNOWS]->()
SET knows.bi15_weight = 1.0;

MATCH
  (personA:Person)-[knows:KNOWS]->(personB:Person),
  (personA)<-[:HAS_CREATOR]-(m1:Message)-[:REPLY_OF]-(m2:Message)-[:HAS_CREATOR]->(personB)
WITH knows, m1, m2
MATCH (m1)-[:REPLY_OF*0..]->(:Post)<-[:CONTAINER_OF]-(forum:Forum)
WHERE forum.creationDate >= $startDate AND forum.creationDate <= $endDate
WITH knows, sum(CASE WHEN m1:Post OR m2:Post THEN 1.0 ELSE 0.5 END) AS interactionWeight
SET knows.bi15_weight = 1.0 / (interactionWeight + 1.0)
```

### 完整原始查询 baseline

源文件：[Neo4j/queries/baseline/ldbc-bi/bi-15.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/baseline/ldbc-bi/bi-15.cypher)  
SHA-256：`59611c9f59c39a7c670340bdf253761d121bf808342d37a88c16bb1854662475`

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

### 全部已测参数

```json
[
  {
    "sample": 0,
    "parameters": {
      "startDate": "2012-11-06 00:00:00",
      "endDate": "2012-11-07 00:00:00",
      "person1Id": 1450,
      "person2Id": 21990232556222
    }
  },
  {
    "sample": 1,
    "parameters": {
      "startDate": "2012-11-07 00:00:00",
      "endDate": "2012-11-07 00:00:00",
      "person1Id": 3388,
      "person2Id": 8796093023774
    }
  },
  {
    "sample": 2,
    "parameters": {
      "startDate": "2012-11-07 00:00:00",
      "endDate": "2012-11-13 00:00:00",
      "person1Id": 3388,
      "person2Id": 17592186051628
    }
  },
  {
    "sample": 3,
    "parameters": {
      "startDate": "2012-11-06 00:00:00",
      "endDate": "2012-11-10 00:00:00",
      "person1Id": 1450,
      "person2Id": 5155
    }
  },
  {
    "sample": 4,
    "parameters": {
      "startDate": "2012-11-02 00:00:00",
      "endDate": "2012-11-09 00:00:00",
      "person1Id": 2756,
      "person2Id": 19791209303042
    }
  },
  {
    "sample": 5,
    "parameters": {
      "startDate": "2012-11-03 00:00:00",
      "endDate": "2012-11-09 00:00:00",
      "person1Id": 396,
      "person2Id": 6597069769453
    }
  },
  {
    "sample": 6,
    "parameters": {
      "startDate": "2012-11-06 00:00:00",
      "endDate": "2012-11-07 00:00:00",
      "person1Id": 1450,
      "person2Id": 15393162792434
    }
  },
  {
    "sample": 7,
    "parameters": {
      "startDate": "2012-11-03 00:00:00",
      "endDate": "2012-11-07 00:00:00",
      "person1Id": 396,
      "person2Id": 4676
    }
  },
  {
    "sample": 8,
    "parameters": {
      "startDate": "2012-11-07 00:00:00",
      "endDate": "2012-11-13 00:00:00",
      "person1Id": 3388,
      "person2Id": 8796093022968
    }
  },
  {
    "sample": 9,
    "parameters": {
      "startDate": "2012-11-07 00:00:00",
      "endDate": "2012-11-08 00:00:00",
      "person1Id": 2975,
      "person2Id": 30786325587872
    }
  }
]
```

### 完整原始查询的实际诊断

相同诊断合并展示，`samples` 列出其覆盖的全部样本；保留 reason、detail、命中索引、route、proofs 和回退/EXPLAIN 状态。

```json
{
  "samples": [
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9
  ],
  "result": {
    "rewritten": false,
    "reason": "no_index_selected",
    "detail": "CALL: frontend_unsupported: CALL procedure: no internal equivalent | no_index_selected: ",
    "indexes": [],
    "specialized": [],
    "route": "",
    "proofs": [],
    "original_preserved": true
  }
}
```

### 分析线索与尚待判断的问题

**已确认的现象：** 写入目标是关系变量 `knows.bi15_weight`，注册器走到要求显式节点标签的载体检查并拒绝；不应把它理解成只给某个 Person 补标签就能解决。脚本还包含参数窗口、默认值 1.0 和聚合后倒数写回。

完整原始查询调用 GDS 并把投影 Cypher 放在动态拼接的字符串里；当前诊断显示过程没有内部等价实现。关系属性注册和 GDS/字符串内查询处理是两层问题。

**待分析：** 关系聚合载体及默认值/覆盖规则；GDS 过程契约，以及是否有可验证的通用方式处理图投影中的查询。下方附文档优化查询和独立投影脚本，供人工比较，不表示中间件已能自动完成该变换。

下面的优化写法来自实验文档，作为人工比较索引用途的参考。本轮矩阵测试的是上面的 baseline；没有将此脚本作为自动改写输出，也未在本次汇总中测试其能否再次改写。

### 文档手工优化查询（参考）

源文件：[Neo4j/queries/optimized/ldbc-bi/bi-15.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/optimized/ldbc-bi/bi-15.cypher)  
SHA-256：`876d31e3cd403942da9600390ef5f1c8530d60094f8416fefeb7bf75b9114da1`

```cypher
// Q15. The parameter-specific bi15_optimized GDS graph is built during setup.
CALL {
  MATCH (person1:Person {id: $person1Id}), (person2:Person {id: $person2Id})
  CALL gds.shortestPath.dijkstra.stream('bi15_optimized', {
    sourceNode: person1,
    targetNode: person2,
    relationshipWeightProperty: 'weight'
  })
  YIELD totalCost
  RETURN totalCost
  UNION ALL
  RETURN -1.0 AS totalCost
}
RETURN max(totalCost) AS totalCost
```

### optimized 的 GDS 投影依赖（参考）

源文件：[Neo4j/queries/optimized/ldbc-bi/bi-15-project-graph.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/optimized/ldbc-bi/bi-15-project-graph.cypher)  
SHA-256：`b228377fddde55a5fa4552d0e45a700cef700aa0872371623c07da8e7a6fa3fb`

```cypher
// Project the BI15 optimized graph from the materialized KNOWS.bi15_weight.
// The weight column must be filled by queries/index/ldbc-bi/bi15/index.cypher
// with the same date window the baseline projection uses.
CALL gds.graph.drop('bi15_optimized', false) YIELD graphName RETURN graphName;

CALL gds.graph.project.cypher(
  'bi15_optimized',
  'MATCH (p:Person) RETURN id(p) AS id',
  'MATCH (pA:Person)-[knows:KNOWS]->(pB:Person)
   WHERE knows.bi15_weight IS NOT NULL
   RETURN id(pA) AS source, id(pB) AS target, knows.bi15_weight AS weight
   UNION ALL
   MATCH (pA:Person)<-[knows:KNOWS]-(pB:Person)
   WHERE knows.bi15_weight IS NOT NULL
   RETURN id(pA) AS source, id(pB) AS target, knows.bi15_weight AS weight'
)
```

## BI16

**注册：** 成功。**完整原始查询：** 0/10 个样本改写成功。

### 原始注册结果

```json
{
  "file": "/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi16/index.cypher",
  "sha256": "5d73ab962697e998ba0d125e1abf97d371e5ecbd25edde9bed50873807772be4",
  "parameter_file": "/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi16/parameter.cypher",
  "status": "registered",
  "indexes": [
    "PERSON(bi16MessageCountA)",
    "PERSON(bi16MessageCountB)"
  ]
}
```

### 完整索引脚本 index.cypher

源文件：[Neo4j/queries/index/ldbc-bi/bi16/index.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi16/index.cypher)  
SHA-256：`5d73ab962697e998ba0d125e1abf97d371e5ecbd25edde9bed50873807772be4`

```cypher
// Run parameter.cypher after this schema step; rebuild when its indexed parameters change.
MATCH(n:Person) SET n.bi16MessageCountA=0, n.bi16MessageCountB=0;
```

该组 `index.cypher` 是清理脚本；本轮注册输入为下方 `parameter.cypher`，并绑定对应样本参数。

### 实际注册输入 parameter.cypher

源文件：[Neo4j/queries/index/ldbc-bi/bi16/parameter.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi16/parameter.cypher)  
SHA-256：`100c103835fac89def698b84fbb5dbb886a8e0ba9b98358a7fa1095865796ef7`

```cypher
// Snapshot rebuild on the optimized copy only; do not query during this step.
MATCH(n:Person)
SET n.bi16MessageCountA=0, n.bi16MessageCountB=0;
MATCH(p:Person)<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag)
WHERE t.name=$tagA AND date(m.creationDate)=date($dateA)
WITH p,count(DISTINCT m) AS value
SET p.bi16MessageCountA=value;
MATCH(p:Person)<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag)
WHERE t.name=$tagB AND date(m.creationDate)=date($dateB)
WITH p,count(DISTINCT m) AS value
SET p.bi16MessageCountB=value;
```

### 完整原始查询 baseline

源文件：[Neo4j/queries/baseline/ldbc-bi/bi-16.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/baseline/ldbc-bi/bi-16.cypher)  
SHA-256：`5fb867224780a00d4a2e1d9fc7870496389b32da832d2ffaceca764addc843d1`

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

### 全部已测参数

```json
[
  {
    "sample": 0,
    "parameters": {
      "dateA": "2012-07-26 00:00:00",
      "dateB": "2012-05-08 00:00:00",
      "maxKnowsLimit": 4,
      "tagA": "Marc_Gicquel",
      "tagB": "Romania"
    }
  },
  {
    "sample": 1,
    "parameters": {
      "dateA": "2011-06-14 00:00:00",
      "dateB": "2012-12-11 00:00:00",
      "maxKnowsLimit": 6,
      "tagA": "David_Hockney",
      "tagB": "Thomas_Jefferson"
    }
  },
  {
    "sample": 2,
    "parameters": {
      "dateA": "2012-07-14 00:00:00",
      "dateB": "2012-12-29 00:00:00",
      "maxKnowsLimit": 3,
      "tagA": "Ivan_Ljubičić",
      "tagB": "Turkey"
    }
  },
  {
    "sample": 3,
    "parameters": {
      "dateA": "2012-07-14 00:00:00",
      "dateB": "2012-12-16 00:00:00",
      "maxKnowsLimit": 6,
      "tagA": "Ivan_Ljubičić",
      "tagB": "Niccolò_Machiavelli"
    }
  },
  {
    "sample": 4,
    "parameters": {
      "dateA": "2012-05-20 00:00:00",
      "dateB": "2012-12-12 00:00:00",
      "maxKnowsLimit": 3,
      "tagA": "Vinicius_de_Moraes",
      "tagB": "Abraham_Lincoln"
    }
  },
  {
    "sample": 5,
    "parameters": {
      "dateA": "2012-10-08 00:00:00",
      "dateB": "2012-12-05 00:00:00",
      "maxKnowsLimit": 5,
      "tagA": "Diosdado_Macapagal",
      "tagB": "Czechoslovakia"
    }
  },
  {
    "sample": 6,
    "parameters": {
      "dateA": "2012-04-20 00:00:00",
      "dateB": "2012-12-11 00:00:00",
      "maxKnowsLimit": 4,
      "tagA": "John_Rhys-Davies",
      "tagB": "Thomas_Jefferson"
    }
  },
  {
    "sample": 7,
    "parameters": {
      "dateA": "2012-06-22 00:00:00",
      "dateB": "2012-11-17 00:00:00",
      "maxKnowsLimit": 3,
      "tagA": "Bobby_Hull",
      "tagB": "Jim_Carrey"
    }
  },
  {
    "sample": 8,
    "parameters": {
      "dateA": "2012-05-09 00:00:00",
      "dateB": "2012-12-26 00:00:00",
      "maxKnowsLimit": 6,
      "tagA": "Imelda_Marcos",
      "tagB": "Paul_Keres"
    }
  },
  {
    "sample": 9,
    "parameters": {
      "dateA": "2010-12-30 00:00:00",
      "dateB": "2011-10-12 00:00:00",
      "maxKnowsLimit": 4,
      "tagA": "Chulalongkorn",
      "tagB": "William_Blake"
    }
  }
]
```

### 完整原始查询的实际诊断

相同诊断合并展示，`samples` 列出其覆盖的全部样本；保留 reason、detail、命中索引、route、proofs 和回退/EXPLAIN 状态。

```json
{
  "samples": [
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9
  ],
  "result": {
    "parameter_indexes": [
      "PERSON(bi16MessageCountA)",
      "PERSON(bi16MessageCountB)"
    ],
    "rewritten": false,
    "reason": "no_index_selected",
    "detail": "CALL: no declared opaque type prefix",
    "indexes": [],
    "specialized": [],
    "route": "",
    "proofs": [],
    "original_preserved": true
  }
}
```

### 注册后内部 FILL 与精确源对照（sample 0）

下方 FILL 是程序实际生成的内部声明，参数已实例化。精确源 probe 仅说明源模式可以绑定并使用索引，未执行，也不代表完整查询能改写。

```cypher
FILL PERSON(bi16MessageCountA) FROM (MATCH (p:PERSON) OPTIONAL MATCH (p:PERSON)<-[:HASCREATOR]-(m:COMMENT:POST)-[:HASTAG]->(t:TAG)
WHERE t.name='Marc_Gicquel' AND date(m.creationDate)=date(TIMESTAMP('2012-07-26 00:00:00')) RETURN p.id, count(DISTINCT m))
```

```cypher
FILL PERSON(bi16MessageCountB) FROM (MATCH (p:PERSON) OPTIONAL MATCH (p:PERSON)<-[:HASCREATOR]-(m:COMMENT:POST)-[:HASTAG]->(t:TAG)
WHERE t.name='Romania' AND date(m.creationDate)=date(TIMESTAMP('2012-05-08 00:00:00')) RETURN p.id, count(DISTINCT m))
```

```json
{
  "sample": 0,
  "parameters": {
    "dateA": "2012-07-26 00:00:00",
    "dateB": "2012-05-08 00:00:00",
    "maxKnowsLimit": 4,
    "tagA": "Marc_Gicquel",
    "tagB": "Romania"
  },
  "exact_sources": [
    {
      "query": "MATCH (p:PERSON) OPTIONAL MATCH (p:PERSON)<-[:HASCREATOR]-(m:COMMENT:POST)-[:HASTAG]->(t:TAG)\nWHERE t.name='Marc_Gicquel' AND date(m.creationDate)=date(TIMESTAMP('2012-07-26 00:00:00')) RETURN p.id, count(DISTINCT m)",
      "baseline_bound": true,
      "used": [
        "PERSON(bi16MessageCountA)"
      ],
      "changed": true,
      "cost_compared": false,
      "index_entries": [
        "fill_PERSON_bi16MessageCountA___neug_fill_state_n_rows_f56a0986a1113c1a_1f1ac5abbf6d"
      ]
    },
    {
      "query": "MATCH (p:PERSON) OPTIONAL MATCH (p:PERSON)<-[:HASCREATOR]-(m:COMMENT:POST)-[:HASTAG]->(t:TAG)\nWHERE t.name='Romania' AND date(m.creationDate)=date(TIMESTAMP('2012-05-08 00:00:00')) RETURN p.id, count(DISTINCT m)",
      "baseline_bound": true,
      "used": [
        "PERSON(bi16MessageCountB)"
      ],
      "changed": true,
      "cost_compared": false,
      "index_entries": [
        "fill_PERSON_bi16MessageCountB___neug_fill_state_n_rows_3fc385e8873dd096_63cfbcc9e080"
      ]
    }
  ],
  "whole_translation_error": "CALL subquery"
}
```

`whole_translation_error` 来自整条查询直接转换探针；“完整原始查询的实际诊断”来自会尝试局部改写的中间件，两条路径的错误粒度不同。

### 分析线索与尚待判断的问题

**已确认的现象：** A/B 两个参数化计数索引均注册成功，两个精确索引源均命中。完整原始查询 0/10，原始顶层诊断为 `CALL: no declared opaque type prefix`。该报错是外层回退路径的可见结果，不能仅凭此认定唯一障碍就是 CALL 语法。

**已有分层分析：** 有限记录字段传播已经支持逐个尝试 A/B 参数值；即使把字段显式实例化，整个 CALL 内体也没有命中。首次消息匹配得到的同一个 Tag 被后续 OPTIONAL 好友消息模式继续使用；该 Tag 是被覆盖模式与剩余模式之间的接口，但 Person 计数索引未公开它。不同 Tag 对应的剩余扩展可能不同，现有约束不能证明组内扩展条件 `(X)`。外层 `collect({letter, messageCount})`、列表过滤等仍有转换边界。

**待分析：** 先确定 Tag 接口或可证明的唯一性/函数依赖，再检查分组、DISTINCT 以及好友过滤的扩展条件。单纯把 UNWIND/record/CALL 改成 NeuG 可接受的语法，不能保证该 Person 总计数可以替换原来的相关匹配。精确源成功不等于完整 CALL 成功。

下面的优化写法来自实验文档，作为人工比较索引用途的参考。本轮矩阵测试的是上面的 baseline；没有将此脚本作为自动改写输出，也未在本次汇总中测试其能否再次改写。

### 文档手工优化查询（参考）

源文件：[Neo4j/queries/optimized/ldbc-bi/bi-16.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/optimized/ldbc-bi/bi-16.cypher)  
SHA-256：`05cfb32fe1c2abd6f2123441905256202f866c89bf820d9e03241154e3a27a9c`

```cypher
MATCH(p:Person) WHERE p.bi16MessageCountA>0 AND p.bi16MessageCountB>0
OPTIONAL MATCH(p)-[:KNOWS]-(fa:Person) WHERE fa.bi16MessageCountA>0
WITH p.id AS personId,p.bi16MessageCountA AS messageCountA,p.bi16MessageCountB AS messageCountB,count(DISTINCT fa) AS cpA
WHERE cpA<=$maxKnowsLimit
MATCH(p:Person {id:personId})
OPTIONAL MATCH(p)-[:KNOWS]-(fb:Person) WHERE fb.bi16MessageCountB>0
WITH personId,messageCountA,messageCountB,count(DISTINCT fb) AS cpB
WHERE cpB<=$maxKnowsLimit
RETURN personId,messageCountA,messageCountB ORDER BY messageCountA+messageCountB DESC,personId ASC LIMIT 20;
```

## BI19

**注册：** 失败。**完整原始查询：** 0/10 个样本改写成功。

### 原始注册结果

```json
{
  "file": "/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi19/index.cypher",
  "sha256": "3c861e1d75b02dc2a3875fcb6c62d6d1faa8949e20faff3f15204dfca3cb9a4e",
  "status": "unsupported",
  "error": "conditional or multiple index assignments"
}
```

### 完整索引脚本 index.cypher

源文件：[Neo4j/queries/index/ldbc-bi/bi19/index.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi19/index.cypher)  
SHA-256：`3c861e1d75b02dc2a3875fcb6c62d6d1faa8949e20faff3f15204dfca3cb9a4e`

```cypher
// BI19 optimized materialization for Neo4j.
//
// One symmetric reply count per KNOWS pair, scored with the official
// 40 - sqrt(numInteractions) weight floored at 1. The count is taken over the
// undirected REPLY_OF pattern, so a reply from A to B and a reply from B to A
// land in the same group, exactly like bi-19-create-graph.cypher. A pair that
// never exchanged a reply keeps no property, which drops it from the
// projected graph the same way the baseline projection does.
MATCH (personA:Person)-[knows:KNOWS]->(personB:Person)
OPTIONAL MATCH
  (personA)<-[:HAS_CREATOR]-(m1:Message)-[replyOf:REPLY_OF]-(m2:Message)-[:HAS_CREATOR]->(personB)
WITH knows, count(replyOf) AS numInteractions
SET knows.weight_bi19 = CASE
  WHEN numInteractions = 0 THEN null
  WHEN round(40 - sqrt(numInteractions)) > 1 THEN round(40 - sqrt(numInteractions))
  ELSE 1
END
```

### 完整原始查询 baseline

源文件：[Neo4j/queries/baseline/ldbc-bi/bi-19.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/baseline/ldbc-bi/bi-19.cypher)  
SHA-256：`48efc099d7709f3a0c601fbcf26602fca0d7f9bbb44b526c0f57d136d8363d06`

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

### 全部已测参数

```json
[
  {
    "sample": 0,
    "parameters": {
      "city1Id": 363,
      "city2Id": 578
    }
  },
  {
    "sample": 1,
    "parameters": {
      "city1Id": 499,
      "city2Id": 1021
    }
  },
  {
    "sample": 2,
    "parameters": {
      "city1Id": 399,
      "city2Id": 1435
    }
  },
  {
    "sample": 3,
    "parameters": {
      "city1Id": 604,
      "city2Id": 980
    }
  },
  {
    "sample": 4,
    "parameters": {
      "city1Id": 124,
      "city2Id": 440
    }
  },
  {
    "sample": 5,
    "parameters": {
      "city1Id": 399,
      "city2Id": 915
    }
  },
  {
    "sample": 6,
    "parameters": {
      "city1Id": 941,
      "city2Id": 1253
    }
  },
  {
    "sample": 7,
    "parameters": {
      "city1Id": 227,
      "city2Id": 1100
    }
  },
  {
    "sample": 8,
    "parameters": {
      "city1Id": 553,
      "city2Id": 737
    }
  },
  {
    "sample": 9,
    "parameters": {
      "city1Id": 422,
      "city2Id": 1121
    }
  }
]
```

### 完整原始查询的实际诊断

相同诊断合并展示，`samples` 列出其覆盖的全部样本；保留 reason、detail、命中索引、route、proofs 和回退/EXPLAIN 状态。

```json
{
  "samples": [
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9
  ],
  "result": {
    "rewritten": false,
    "reason": "frontend_unsupported",
    "detail": "part 1: frontend_unsupported CALL procedure: no internal equivalent",
    "indexes": [],
    "specialized": [],
    "route": "",
    "proofs": [],
    "original_preserved": true
  }
}
```

### 分析线索与尚待判断的问题

**已确认的现象：** 脚本写入 KNOWS 关系属性，以 CASE 表达式处理零交互和权重下界；当前注册错误为 `conditional or multiple index assignments`。完整查询的直接阻塞是 GDS Dijkstra 过程没有内部等价实现。

**待分析：** 关系载体、OPTIONAL 的零计数、CASE/NULL 属性删除语义、round/sqrt 表达式，以及对称建图和最短路过程契约。注册器还未通过，不能把该 case 归为“索引已可用但匹配器拒绝”。下方同时收录 baseline 建图脚本和优化投影脚本，保持前置依赖可见。

下面的优化写法来自实验文档，作为人工比较索引用途的参考。本轮矩阵测试的是上面的 baseline；没有将此脚本作为自动改写输出，也未在本次汇总中测试其能否再次改写。

### 文档手工优化查询（参考）

源文件：[Neo4j/queries/optimized/ldbc-bi/bi-19.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/optimized/ldbc-bi/bi-19.cypher)  
SHA-256：`7a0446111c84b100fd0a25c026dce39584891794aff0e9521a1c98ab2092151c`

```cypher
// Q19. Batch targets per source and use the smaller city as the source side.
// The prepared graph is symmetric. Restore city orientation and duplicate
// location matches after Dijkstra, then return every minimum-cost tie.
MATCH(person1:Person)-[:IS_LOCATED_IN]->(:City {id:$city1Id})
WITH collect(person1) AS lefts WHERE size(lefts)>0
MATCH(person2:Person)-[:IS_LOCATED_IN]->(:City {id:$city2Id})
WITH lefts,collect(person2) AS rights WHERE size(rights)>0
WITH size(lefts)>size(rights) AS flipped,
 CASE WHEN size(lefts)<=size(rights) THEN lefts ELSE rights END AS sources,
 CASE WHEN size(lefts)<=size(rights) THEN rights ELSE lefts END AS targets
CALL(targets) {
 UNWIND targets AS target
 RETURN collect(DISTINCT target) AS uniqueTargets
}
UNWIND sources AS source
CALL gds.shortestPath.dijkstra.stream('bi19_optimized',{
 sourceNode:source,targetNodes:uniqueTargets,relationshipWeightProperty:'weight',logProgress:false,concurrency:1
}) YIELD targetNode,totalCost
// Match the baseline GDS source-target contract, which omits self-pairs.
WITH flipped,source,targets,targetNode,totalCost WHERE targetNode<>id(source)
UNWIND [p IN targets WHERE id(p)=targetNode] AS target
WITH CASE WHEN flipped THEN target.id ELSE source.id END AS person1Id,
 CASE WHEN flipped THEN source.id ELSE target.id END AS person2Id,totalCost
WITH person1Id,person2Id,totalCost AS totalWeight
WITH collect({person1Id:person1Id,person2Id:person2Id,totalWeight:totalWeight}) AS results,min(totalWeight) AS best
UNWIND results AS result
WITH result,best WHERE result.totalWeight=best
RETURN result.person1Id AS person1Id,result.person2Id AS person2Id,result.totalWeight AS totalWeight
ORDER BY person1Id,person2Id
```

### baseline 的 GDS 建图依赖（参考）

源文件：[Neo4j/queries/baseline/ldbc-bi/bi-19-create-graph.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/baseline/ldbc-bi/bi-19-create-graph.cypher)  
SHA-256：`7b9a6c0798facb4d5f498d3dc6f4a1b48d48245706c219f5c4c85e0698be5f41`

```cypher
CALL gds.graph.project.cypher(
    'bi19',
    'MATCH (p:Person) RETURN id(p) AS id',
    'MATCH
      (personA:Person)-[:KNOWS]-(personB:Person),
      (personA)<-[:HAS_CREATOR]-(:Message)-[replyOf:REPLY_OF]-(:Message)-[:HAS_CREATOR]->(personB)
    WITH
      id(personA) AS source,
      id(personB) AS target,
      count(replyOf) AS numInteractions
    RETURN
      source,
      target,
      CASE WHEN round(40-sqrt(numInteractions)) > 1 THEN round(40-sqrt(numInteractions)) ELSE 1 END AS weight'
)
```

### optimized 的 GDS 投影依赖（参考）

源文件：[Neo4j/queries/optimized/ldbc-bi/bi-19-project-graph.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/optimized/ldbc-bi/bi-19-project-graph.cypher)  
SHA-256：`18eace7a6a7befd9dfbfcd71d2f9bab53f06d45a3e64078ac96d0d3d787e77d7`

```cypher
// Project the BI19 optimized graph from the materialized KNOWS.weight_bi19.
// KNOWS is stored in one direction only, so both orientations are emitted to
// keep the graph undirected, matching the baseline projection.
CALL gds.graph.drop('bi19_optimized', false) YIELD graphName RETURN graphName;

CALL gds.graph.project.cypher(
  'bi19_optimized',
  'MATCH (p:Person) RETURN id(p) AS id',
  'MATCH (pA:Person)-[knows:KNOWS]->(pB:Person)
   WHERE knows.weight_bi19 IS NOT NULL
   RETURN id(pA) AS source, id(pB) AS target, knows.weight_bi19 AS weight
   UNION ALL
   MATCH (pA:Person)<-[knows:KNOWS]-(pB:Person)
   WHERE knows.weight_bi19 IS NOT NULL
   RETURN id(pA) AS source, id(pB) AS target, knows.weight_bi19 AS weight'
)
```

## BI20

**注册：** 失败。**完整原始查询：** 0/10 个样本改写成功。

### 原始注册结果

```json
{
  "file": "/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi20/index.cypher",
  "sha256": "062063ca47b4d5a1bd01d5624da645d94d817ee0813c8c2b917b69ed74c1b9a4",
  "status": "unsupported",
  "error": "index carrier must have an explicit node label"
}
```

### 完整索引脚本 index.cypher

源文件：[Neo4j/queries/index/ldbc-bi/bi20/index.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/index/ldbc-bi/bi20/index.cypher)  
SHA-256：`062063ca47b4d5a1bd01d5624da645d94d817ee0813c8c2b917b69ed74c1b9a4`

```cypher
// BI20 optimized materialization for Neo4j.
//
// Weight of a KNOWS pair is the smallest absolute class-year gap over every
// university both persons studied at, plus one. A pair without a shared
// university keeps no property, so the projected graph drops it just like
// bi-20-create-graph.cypher, whose pattern requires one.
MATCH (personA:Person)-[knows:KNOWS]->(personB:Person)
OPTIONAL MATCH (personA)-[saA:STUDY_AT]->(u:University)<-[saB:STUDY_AT]-(personB)
WITH knows, min(abs(saA.classYear - saB.classYear)) + 1 AS weight
SET knows.bi20_weight = weight
```

### 完整原始查询 baseline

源文件：[Neo4j/queries/baseline/ldbc-bi/bi-20.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/baseline/ldbc-bi/bi-20.cypher)  
SHA-256：`2df4d3c1bcdb45c51a0eef974971123963cdca8163c147b11f75134796ea9f94`

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

### 全部已测参数

```json
[
  {
    "sample": 0,
    "parameters": {
      "person2Id": 30786325583226,
      "company": "Avioquintana"
    }
  },
  {
    "sample": 1,
    "parameters": {
      "person2Id": 2199023264575,
      "company": "Cebu_Pacific"
    }
  },
  {
    "sample": 2,
    "parameters": {
      "person2Id": 17592186049061,
      "company": "Tandem_Aero"
    }
  },
  {
    "sample": 3,
    "parameters": {
      "person2Id": 26388279072250,
      "company": "Pamir_Airways"
    }
  },
  {
    "sample": 4,
    "parameters": {
      "person2Id": 4398046515696,
      "company": "Wizz_Air"
    }
  },
  {
    "sample": 5,
    "parameters": {
      "person2Id": 2199023255672,
      "company": "Tanzania_Government_Flight_Agency"
    }
  },
  {
    "sample": 6,
    "parameters": {
      "person2Id": 15393162790150,
      "company": "Coastal_Aviation"
    }
  },
  {
    "sample": 7,
    "parameters": {
      "person2Id": 21990232564654,
      "company": "Bakhtar_Afghan_Airlines"
    }
  },
  {
    "sample": 8,
    "parameters": {
      "person2Id": 21990232561641,
      "company": "Pacific_East_Asia_Cargo_Airlines"
    }
  },
  {
    "sample": 9,
    "parameters": {
      "person2Id": 15393162790150,
      "company": "Pacific_East_Asia_Cargo_Airlines"
    }
  }
]
```

### 完整原始查询的实际诊断

相同诊断合并展示，`samples` 列出其覆盖的全部样本；保留 reason、detail、命中索引、route、proofs 和回退/EXPLAIN 状态。

```json
{
  "samples": [
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9
  ],
  "result": {
    "rewritten": false,
    "reason": "frontend_unsupported",
    "detail": "part 1: frontend_unsupported CALL procedure: no internal equivalent",
    "indexes": [],
    "specialized": [],
    "route": "",
    "proofs": [],
    "original_preserved": true
  }
}
```

### 分析线索与尚待判断的问题

**已确认的现象：** 写回目标是 KNOWS 关系上的 `bi20_weight`，当前节点载体检查拒绝；完整查询另外受 GDS Dijkstra 过程内部表示缺失阻塞。

**待分析：** 关系载体上的 OPTIONAL 聚合、共同大学条件、`min(abs(classYear 差)) + 1` 与无匹配时 NULL/缺失属性的语义；之后再分析图投影读取物化权重及最短路过程。注册与过程转换须分别验证。下方保留 baseline 建图和文档优化投影作为参考。

下面的优化写法来自实验文档，作为人工比较索引用途的参考。本轮矩阵测试的是上面的 baseline；没有将此脚本作为自动改写输出，也未在本次汇总中测试其能否再次改写。

### 文档手工优化查询（参考）

源文件：[Neo4j/queries/optimized/ldbc-bi/bi-20.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/optimized/ldbc-bi/bi-20.cypher)  
SHA-256：`8ea7d1745425bf07afac95c4f0e47a2460ee4638478a181c54e1583f1af238fc`

```cypher
// Q20. The bi20_optimized GDS graph is built once during setup from
// KNOWS.bi20_weight.
MATCH
  (company:Company {name: $company})<-[:WORK_AT]-(person1:Person),
  (person2:Person {id: $person2Id})
WHERE person1.id <> $person2Id
CALL gds.shortestPath.dijkstra.stream('bi20_optimized', {
  sourceNode: person1,
  targetNode: person2,
  relationshipWeightProperty: 'weight'
})
YIELD totalCost
WITH person1.id AS person1Id, totalCost AS totalWeight
ORDER BY totalWeight ASC
WITH collect({person1Id: person1Id, totalWeight: totalWeight}) AS results
UNWIND results AS result
WITH result.person1Id AS person1Id, result.totalWeight AS totalWeight, results
WHERE totalWeight = results[0].totalWeight
RETURN person1Id, totalWeight
ORDER BY person1Id ASC
LIMIT 20
```

### baseline 的 GDS 建图依赖（参考）

源文件：[Neo4j/queries/baseline/ldbc-bi/bi-20-create-graph.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/baseline/ldbc-bi/bi-20-create-graph.cypher)  
SHA-256：`054c4acbbd300aad7d8fac9c0f38d0858dd754d7dcfdd677b4bc4a28884f6599`

```cypher
CALL gds.graph.project.cypher(
    'bi20',
    'MATCH (p:Person) RETURN id(p) AS id',
    'MATCH
      (personA:Person)-[:KNOWS]-(personB:Person),
      (personA)-[saA:STUDY_AT]->(u:University)<-[saB:STUDY_AT]-(personB)
    RETURN
      id(personA) AS source,
      id(personB) AS target,
      min(abs(saA.classYear - saB.classYear)) + 1 AS weight'
)
```

### optimized 的 GDS 投影依赖（参考）

源文件：[Neo4j/queries/optimized/ldbc-bi/bi-20-project-graph.cypher](/mnt/sdb1/feiyang/IncGQ-experiment-docs/Neo4j/queries/optimized/ldbc-bi/bi-20-project-graph.cypher)  
SHA-256：`65f2414adc9256ee86d313108eeef1a73b03c4213cb2c7c5981733a32fe9368e`

```cypher
// Project the BI20 optimized graph from the materialized KNOWS.bi20_weight.
CALL gds.graph.drop('bi20_optimized', false) YIELD graphName RETURN graphName;

CALL gds.graph.project.cypher(
  'bi20_optimized',
  'MATCH (p:Person) RETURN id(p) AS id',
  'MATCH (pA:Person)-[knows:KNOWS]->(pB:Person)
   WHERE knows.bi20_weight IS NOT NULL
   RETURN id(pA) AS source, id(pB) AS target, knows.bi20_weight AS weight
   UNION ALL
   MATCH (pA:Person)<-[knows:KNOWS]-(pB:Person)
   WHERE knows.bi20_weight IS NOT NULL
   RETURN id(pA) AS source, id(pB) AS target, knows.bi20_weight AS weight'
)
```

## 复现与后续记录

可在仓库根目录使用现有验证入口重新生成矩阵，建议写到新的输出文件保留本次证据。该入口仅执行改写及成功输出的 EXPLAIN，需要当前本地 Neo4j 实例可用；参数化索引仍只注册元数据。

```sh
python3 middleware/tests/neo4j_bi_rewrite_matrix.py --index-queries --samples 10 \
  --output /tmp/neo4j-bi-recheck.json
```

矩阵脚本退出码 1 表示仍有查询未改写，退出码 2 表示发现无效输出。索引源分层对照可使用 `middleware/tests/neo4j_bi_rewrite_diagnosis.py`。分析时分别记录：注册是否通过、哪个源/QueryPart 命中、完整查询是否返回改写、回译与 EXPLAIN 是否通过；不要用其中一项代替其余项。

逐 case 可补充：可保持语义的最小例子、期待读取的索引、阻塞阶段、需要的正式约束、通用匹配规则及反例。当前文件保留原脚本，不把建议的 MERGE/DISTINCT、标签补充或唯一性假设自动应用到实验定义。

