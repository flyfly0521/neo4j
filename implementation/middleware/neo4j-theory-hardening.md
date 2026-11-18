# 以 QueryPart 正确性定义增强中间件（2026-11-17）

本轮把 wiki 的 binding、索引接口和多重集语义作为新增改写规则的约束，扩展了原本因外层 Neo4j 表达式不受支持而无法利用的索引。没有添加按 BI 编号、文件名、固定变量名或完整查询文本选择的分支，也未修改实验文档原查询、索引脚本和共享 NeuG 内核。

## 理论来源和适用范围

- [QueryPart Binding 与 Index 定义](https://wiki.pdy.app:8443/doc/querypart-binding-index-TZdc4RPfD1)，文档 ID `f1c848e8-ef47-4eef-8e49-faacf5950a2c`，本轮读取修订 **768**。
- [QueryPart 索引改写的语义等价定理与 Lean 形式化](https://wiki.pdy.app:8443/doc/querypart-lean-6hQ7o2SiWN)，文档 ID `5bc285dd-f22f-42fd-8880-34b69231ac3b`，本轮读取修订 **47**。

输入是 binding `B=(S,L)`：`S` 给出列及类型，`L` 保留重复赋值。图模式按已有绑定扩展，不能重新赋值已有变量。索引只公开声明的接口和聚合结果，隐藏变量不能被后续图连接、过滤、分组或投影继续依赖。

新增规则继续通过现有 NeuG 图模式与计算匹配，再通过打印、回译绑定和规范化比对。实现另行检查外层输入接口、局部作用域、剩余读取和重数条件。`Result.proofs` 记录这些本地检查，不是 Lean 证书，也不意味着 NeuG 所有匹配条件已被机械化证明。

wiki 第 5.4 节明确没有机械化覆盖图/语法到证书的构造、NULL 三值逻辑、DISTINCT、OPTIONAL MATCH、排序和 LIMIT。本轮没有运行 Lean；新增的 NULL 扩展、相关标量计数和有限记录分支有单独的语义理由及真实 Neo4j 小图测试。

## 新增通用能力

### 相关标量图计数和存在性

以下表达式现在可以独立参与索引改写，而外层表达式与查询继续保留：

```cypher
size([(anchor)-[:EDGE]->(witness:Label) | witness])
COUNT { MATCH(anchor)-[:EDGE]->(witness:Label) WHERE ... }
EXISTS { MATCH(anchor)-[:EDGE]->(witness:Label) WHERE ... }
```

实现先绑定实际前缀，取得当前作用域的输入 schema。把所有被引用的外层节点作为输入 anchor，不能只留下候选索引载体；导入的关系、递归路径、未知作用域等尚无规则的情况保守拒绝。

计数单元在单条固定输入 binding 下编译为 OPTIONAL MATCH 加新鲜、非空图见证的 COUNT。`size` 目前只接受图变量的恒等投影；不会删除可能抛错的任意投影表达式。所有 anchor 以**图对象身份**分组，不假定存在名为 `id` 的唯一属性，也不把不同标签域里同值的属性当作同一节点。

只有改写结果通过常规回译验证，并同时满足以下条件，才把结果替回原来的表达式：

- 输入桩打印不变；每个相关 anchor 的身份键都保留。
- 局部模式已完全消除，没有残留图读取、更新或过滤，也没有改变投影修饰符。
- 结果是单行上的 SUM/MAX/MIN，其参数中没有嵌套聚合、隐藏图引用或探针参数。仅此时可以将单行聚合还原为其标量参数。
- 外层节点为 NULL 时显式返回零；EXISTS 再检查计数大于零。

对每个非空 anchor 输入赋值 `ρ`，记 `c(ρ)` 为模式见证数。普通匹配的见证都使新鲜图变量非空，故其 COUNT 等于见证数；没有见证时 OPTIONAL 的填充行使 COUNT 为零。完全消除图模式后，单条输入上的聚合是对一个标量求聚合，可以还原为该标量。原查询中的每一次输入出现仍在原位置求值，所以重复输入、外层聚合、排序及截取不会被这一替换移走或去重。

代码入口：[scalar_subqueries.py](../../middleware/cico/scalar_subqueries.py)。当前仅在 WITH/RETURN 表达式中寻找这些区域，不宣称支持任意嵌套子查询。

### 有限记录和标量参数进入 CALL

例如：

```cypher
UNWIND [{cutoff:$first,kind:1},{cutoff:$second,kind:2}] AS record
WITH record.cutoff AS threshold,record.kind AS kind
CALL { WITH threshold,kind ... }
```

[binding_values.py](../../middleware/cico/binding_values.py) 保留每条可能记录中字段之间的对应关系，跨字段访问、WITH 别名和有限 UNWIND 传播来源。它分析的是可能取值的上界，不假定实际行数；MATCH、WHERE、DISTINCT 可以改变行数，实际输入记录仍保留在原查询中。只分析封闭字面量、参数、已知记录和列表；未知表达式丢弃事实，超过 **32** 种情况就放弃该路径，不做无界展开。

[subqueries.py](../../middleware/cico/subqueries.py) 对每种已证明的输入尝试普通索引改写。成功后生成有守卫的 UNION ALL 分支，没有匹配索引的情况保留原计算。守卫具有以下要求：

- NULL 安全，并要求原始标量类型和值的表示一致。只用 `=` 不足以证明可替换：`10 = 10.0`，但 `toString(10)` 与 `toString(10.0)` 不同；时间点相等的日期时间也可能有不同的本地日期。
- 各分支互斥，后续分支排除之前已满足的守卫。因此两个参数在运行时取相同值，也只执行一个分支。
- 分支过滤放在内层 CALL 之外。否则一个未激活分支的全局 COUNT 仍可能产生一行零，错误增加结果行。
- 保留输出列名，保留每一条外层输入的出现次数。去重的是抽象取值情况，不是实际 UNWIND 记录。

当前守卫使用 Neo4j 5.26 可用的 `valueType` 与 `toString`，仅用于已经绑定为支持的基本标量类型的来源。记录/列表参数、未声明的类型、嵌套作用域及不支持的投影形式继续拒绝，不以猜测替代类型检查。

### 外层类型推断与绑定隔离

- 相同元素类型的节点列表拼接可以在**临时类型探针**中推断结果类型；实际列表拼接保留原文，不用其中一个操作数替换运行时列表。
- 已知图计数区域可在临时类型探针中提供 INT64/BOOL 类型，帮助分析后续 QueryPart；类型占位表达式不输出到目标查询。
- 输入桩的辅助节点和参数名避开已有列、查询参数和本次内联参数；输入标量不能与用户参数意外共用同一探针参数。否则 `k=$user` 可能在探针中变成恒真，破坏“对任意输入 B 成立”的前提。
- 参数使用通过词法 token 判断，字符串中的 `$name` 不算参数；变量和参数名称统一正确引用。泄漏检查只拒绝实际生成的参数，不把用户合法使用的 `cico_in_` 名称一概拒绝。

入口：[queryparts.py](../../middleware/cico/queryparts.py)、[middleware.py](../../middleware/cico/middleware.py)、[syntax.py](../../middleware/cico/syntax.py)。这些是带平衡括号与上下文的保守语法片段分析，作用域及类型最终由真实 binder 确认，不是完整 Neo4j AST 或完整类型系统。

## 当前验证结果

| 检查 | 结果 |
| --- | --- |
| 单元及真实编译器集成回归 | **65/65** 通过，新增 13 个理论约束回归 |
| 独立小图，原查询与改写查询实际执行比对 | **13/13** 结果多重集一致，均通过 EXPLAIN |
| 同一小图禁用两个新局部规则的控制 | 1/13 可以改写；启用后 13/13，新增 **12** 个成功用例 |
| 文档已有 15 组 Neo4j BI 注册 | **9/15**，注册能力范围未变 |
| 对应原始 BI 查询，各 10 组参数 | **8/15** 条全部样本成功，80/150 个样本成功 |
| BI 成功输出 EXPLAIN | **80/80** 通过，`invalid_rewrites=0` |
| 原 BI2/8/10/16 六个精确索引源 | **6/6** 命中 |

小图包括平行边、零计数组、NULL anchor、重复外层输入、外层 SUM、列表拼接、重复有限记录、NULL 记录值、相同/不同参数、数值相等但类型不同的输入、未激活分支的全局 COUNT，以及用户参数与探针参数隔离。所有节点和索引值在带唯一临时标签的**同一事务内创建、读取并回滚**，没有持久改变 SF1 数据。

小图控制仅禁用新加的标量区域和有限 CALL 规则，仍使用同一引擎、schema、索引和其他现有实现；不能把这个数字当作全部 BI 覆盖率提升。原始 BI 查询没有执行，只做改写及 EXPLAIN。IC/LSQB 编译抽查共 24 条，LSQB 仍为 q1/q4/q8 成功；未声称其余文档负载覆盖率增加。

证据和入口：

- [test_theory_rewrites.py](../../middleware/tests/test_theory_rewrites.py)：正例和拒绝条件。
- [theory_rewrite_acceptance.py](../../middleware/tests/theory_rewrite_acceptance.py)、[小图结果](validation/neo4j-theory-equivalence-20261117.json)。
- [当前 BI 矩阵](validation/neo4j-bi-theory-matrix-20261117.json)、[当前分层探针](validation/neo4j-bi-theory-diagnosis-20261117.json)。
- [IC/LSQB 编译抽查](validation/neo4j-theory-ic-lsqb-20261117.json)。
- [理论版本、源码哈希与验收摘要](validation/neo4j-theory-provenance-20261117.json)：记录本轮理论修订、实现文件 SHA-256、引擎/动态库哈希及验证口径。

## BI8、BI16 为什么仍不能计为成功

之前把阻塞主要描述为前端缺口不够完整。本轮继续按理论检查后，确认还必须满足索引接口和扩展条件：

- **BI8**：相关计数使用外层已经绑定的 Person 和 Tag。现有索引只公开 Person，隐藏了 Tag；不满足 `A ∪ (H ∩ V_I) ⊆ I`。即使 Tag.name 相同，当前 schema 没有声明该属性唯一，不能将“这个 Tag 的消息数”替换成“所有同名 Tag 的消息数”。列表拼接输入类型和标量计数局部路径已增强，但不能因此绕过接口条件。
- **BI16**：CALL 中的后续 OPTIONAL 模式继续使用首次匹配的同一个 Tag。Tag 是索引侧与剩余模式之间的接口顶点，却未公开在 Person 计数索引中；组内不同 Tag 可能带来不同的剩余扩展，不能默认满足 `(X)`。有限记录字段已能传播并用于尝试匹配，但仍不能把每个 Person 的总数直接当作这些相关匹配的结果。

一个 BI8 的反例：两个不同 Tag 都名为 `x`，Person 分别创建一条只带其中一个 Tag 的消息。Person 上按名称 `x` 物化的计数为 2；绑定任意一个具体 Tag 后，相关计数为 1。用名称代替 Tag 身份会把 1 错改成 2。

因此不能靠放松模板完成这两条。后续需要证明存在且稳定成立的唯一性/函数依赖，或者采用公开 **Person 与 Tag** 的索引接口，并继续验证过滤、聚合和剩余扩展条件。不能从某一份 SF1 数据碰巧唯一推导所有图上成立；本轮没有自动增加索引，也没有修改用户文档中的定义。

## 复现

```sh
python3 -m unittest discover -s middleware/tests -v
python3 middleware/tests/theory_rewrite_acceptance.py \
  --output implementation/middleware/validation/neo4j-theory-equivalence-20261117.json
python3 middleware/tests/neo4j_bi_rewrite_matrix.py --index-queries --samples 10 \
  --output implementation/middleware/validation/neo4j-bi-theory-matrix-20261117.json
python3 middleware/tests/neo4j_bi_rewrite_diagnosis.py \
  --output implementation/middleware/validation/neo4j-bi-theory-diagnosis-20261117.json
```

BI 矩阵仍返回 1，表示所选查询尚未全部改写；成功输出全部 EXPLAIN 通过。接续时先看机器可读结果和上述理论边界，不能把探针成功、原始查询成功、物理索引构建和形式化证明混为同一种验收。
