# Neo4j BI 索引通用匹配修复（2026-11-17）

**首轮修复记录。后续实现及结论见 [理论约束增强](neo4j-theory-hardening.md)**：当前 65/65 回归和 13/13 小图结果比对通过。相关标量计数与有限记录 CALL 已扩展；BI8/16 还受未公开 Tag 接口及剩余扩展条件限制，不能只补语法。下文 52 项测试和实现边界对应首轮修复。

本轮修复此前“可以注册但不能命中”的内部原因，以及阻塞 BI10 后续 QueryPart 的类型推断。用户明确要求不针对查询特化匹配；实现依据绑定后的图模式、类型化表达式、分组闭包和声明式过程类型契约，不根据 BI 编号、输入文件、固定变量名或完整查询字符串选择改写。

实验文档位置为 `/mnt/sdb1/feiyang/IncGQ-experiment-docs`，分支 `jcloud-exp`，跟踪 `origin/jcloud-exp`，本轮提交为 `08f1a2659ea2304b7940fd15db3c23382bf37eb7`。未修改实验文档中的索引或查询，也未修改、重编共享 IncGQ 源码及 `libneug.so`。此前诊断保留在 [根因报告](neo4j-bi-rewrite-root-cause.md)，当前交接入口为 [handoff](neo4j-bi-handoff.md)。

## 修复结果和边界

| 检查项 | 当前结果 |
| --- | --- |
| 文档已有 15 组 Neo4j BI 索引定义注册 | 9/15，注册支持范围未扩大 |
| 对应 15 条原始查询，每条 10 组参数 | 8 条全部样本改写，80/150 个样本成功 |
| 成功输出的回译绑定和 Neo4j EXPLAIN | 80/80 通过，`invalid_rewrites=0` |
| 新增原始查询成功 | BI2、BI10，各 10/10 |
| 原有成功查询 | BI3、BI5、BI6、BI9、BI11、BI13；BI3 复用 BI9 ROOT_POST，原 BI3 建索引脚本仍不支持 |
| BI2/8/10/16 精确索引源匹配 | 6/6 命中，分别为两个窗口、一个计数、一个属性复制及两个日期计数 |
| BI8、BI16 完整原始查询 | 仍为 0/10；列表、映射、CALL 输入等前端表达缺口尚未补齐 |
| 单元和编译器集成回归 | 52/52 通过 |

这些结果只证明检测到索引改写并通过绑定、语法计划检查；未执行 BI 查询，未做本轮结果等价性或性能测量。参数化索引只注册元数据，没有逐参数物理构建。之前已经物理构建的静态索引及 SF1 实例位置见 handoff。

## 通用规则如何工作

1. **在当前源码上叠加补丁，保留共享内核更新。** 旧 `prepare_patch.py` 恢复历史 RewriterV4，覆盖了共享内核已有的聚合后过滤及 OPTIONAL gate 修复。现在以当前源码为基底，在本地生成三个编译单元；不兼容的补丁上下文直接报错，失败不会覆盖已有输出。来源 JSON 记录输入、输出及补丁哈希。通过可执行程序的符号覆盖链接现有 `libneug.so`，不修改共享工作区。
2. **按 schema 裁剪不可能的物理关系分区。** `normalizeFillPartitions` 在 FILL 注册后、标记 READY 前，根据固定长度关系的端点标签及目录声明的 src/dst table 排除不可能的分区，同步重建邻接信息。无向边检查两个方向，递归边不处理。不删除 OPTIONAL，不把总计数索引改成缺失零值组的稀疏索引，也不假设数据唯一性。
3. **为时间表达式生成类型化谓词键。** 在谓词键的解析与结构生成两端支持 `TO_DATE`、`TO_INTERVAL`、`CAST`，保留原来的日期/时长表达式树。类型、函数签名、操作数和字面量仍参加匹配，不依赖固定 100 天窗口，也不在生产匹配中把某条 BI 日期表达式折叠成专用模板。
4. **扩展有证明的完整 DISTINCT 分组。** 允许形如 `MATCH(anchor) OPTIONAL MATCH(witness...) RETURN anchor.key,count(DISTINCT witness)` 的节点总计数源进入既有完整分组证明。前缀必须是无谓词、无边的单载体节点扫描；计数对象必须是新的图实体身份，不能是外层 anchor 或可空属性。必选消费模式保留非零 guard。独立于被消去绑定的 UNWIND 可以通过闭包检查，涉及被消去绑定的仍拒绝。
5. **属性复制索引可以保留不受影响的 DISTINCT COUNT。** 私有行计数状态不再导致所有聚合都必须成为索引替换目标。仅在没有公开聚合列、计数引用的图绑定仍保留、并通过既有闭包检查时允许；普通 COUNT 仍拒绝，避免重复行折叠改变计数。
6. **过程前缀通过声明式类型契约提供后续输入类型。** profile 的 `procedure_types` 声明过程参数、输出类型来源和允许的配置项。通用代码用真实 binder 绑定过程之前的前缀，处理 WITH 别名和作用域，再根据 schema 证明指定关系的所有端点都落在种子标签域中。当前只支持 `reachable_node` 规则；未声明的过程、未知配置、跨域遍历均保守拒绝。恒等列表筛选只在临时类型探针中保留元素类型，原过程和筛选表达式保留在最终输出。类型探针不是查询执行替代物。

`apoc.path.subgraphNodes` 只在 LDBC Neo4j profile 中出现，匹配器不会识别它的名字。添加另一种可证明的过程时应提供类型契约；不能仅为绕过绑定而给输出贴上未验证的节点类型。

已有的参数值绑定仍必要：例如按日期物化的计数，不能用于其他日期窗口。结果中的 `specialized` 表示这种索引有效域约束，与按查询文本或编号硬编码匹配不同。未匹配、转换不支持或验证失败的输入仍返回原文。

## 代码和补丁入口

- [prepare_patch.py](../../middleware/engine/prepare_patch.py)、[CMakeLists.txt](../../middleware/engine/CMakeLists.txt)：当前源码补丁生成、依赖跟踪和三个编译单元的符号覆盖。
- [incgq_rewrite_engine.cc](../../middleware/engine/incgq_rewrite_engine.cc)：`normalizeFillPartitions` 及注册阶段调用。
- [neug-temporal-predicate-keys.patch](patches/neug-temporal-predicate-keys.patch)：谓词键。
- [neug-total-distinct-count.patch](patches/neug-total-distinct-count.patch)：总 DISTINCT 计数及独立 UNWIND 闭包证明。
- [neug-unrelated-distinct-count.patch](patches/neug-unrelated-distinct-count.patch)：属性复制索引下保留不受影响的 DISTINCT COUNT；与既有 ROOT_POST 补丁共同应用。
- [queryparts.py](../../middleware/cico/queryparts.py)、[middleware.py](../../middleware/cico/middleware.py)、[neo4j.py](../../middleware/cico/neo4j.py)：临时类型探针、binder 回调和契约加载。
- [ldbc-snb-neo4j.yaml](../../middleware/profiles/ldbc-snb-neo4j.yaml)：过程类型声明。
- [test_bi_rewrite_fixes.py](../../middleware/tests/test_bi_rewrite_fixes.py)：8 个新增回归，包含另一套 schema、过程名、YIELD 字段、变量别名及索引属性的改写，以及窗口不匹配、可空计数、额外外层过滤、普通 COUNT 和跨类型域的拒绝检查。

## 复现和证据

在仓库根目录执行：

```sh
cmake -S middleware/engine -B middleware/engine/build -DCICO_BUNDLED_PATCH=ON
cmake --build middleware/engine/build --target incgq_rewrite_engine_patched cico_exec_patched -j 2
python3 -m unittest discover -s middleware/tests -v
python3 middleware/tests/neo4j_bi_rewrite_diagnosis.py \
  --output implementation/middleware/validation/neo4j-bi-fixed-diagnosis-20261117.json
python3 middleware/tests/neo4j_bi_rewrite_matrix.py --index-queries --samples 10 \
  --output implementation/middleware/validation/neo4j-bi-fixed-matrix-20261117.json
```

矩阵命令需要本机 Bolt 实例，会对成功输出运行 EXPLAIN，不执行 BI 负载。因为当前仍有未改写查询，矩阵退出码为 1；查看 JSON 的分组状态和 `invalid_rewrites`，不要把该退出码描述成全部改写失败。

- [当前矩阵](validation/neo4j-bi-fixed-matrix-20261117.json)：15 条查询、150 组参数、注册清单及拒绝原因、输入 SHA-256、输出和 EXPLAIN 状态。
- [修复后分层探针](validation/neo4j-bi-fixed-diagnosis-20261117.json)：与历史定位相同的 62 个内部探针，以及四条原始查询和 BI10 片段对照。诊断中的反事实控制不用于生产改写。报告记录引擎和动态库哈希；本轮引擎 SHA-256 为 `f8894dc30c4c42f3fdc6eb74b65c66f3de3f1ad61bfbc5e184f5f9266a8ac76e`。

构建目录 `patched/rewriter_v4.json` 本轮基底 blob 为 `c4d9ef4ca6bb44bfbb815d9d0415c0c0676609eb`，输出为 `530311b5ca7a7c15d108053d8764f017e0228176`；其他两个单元也各有 JSON。构建目录不入 Git，接续时应重新生成并核对报告，不能把历史二进制当成当前源码。

为使 BI10 输出能在独立实例 EXPLAIN，已把软件自带 `apoc-5.26.28-core.jar` 放入该实例的 `middleware/runtime/bi_sf1/plugins/`，配置其插件目录和 `apoc.path.*` 权限并仅重启此实例。APOC 调用保留在输出但未实际运行。

后续优先补齐 BI8/16 的通用表达式和作用域转换；其次处理六组建索引脚本注册缺口。不要添加以 BI 编号或全文形状选择的替换结果，不要为了匹配删除 OPTIONAL、放松类型证明或跳过回译验证。
