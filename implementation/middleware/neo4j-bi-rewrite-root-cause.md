# Neo4j BI 已注册索引未命中：分层定位（2026-11-17）

**本文保留修复前的定位证据。当前实现和结果见 [修复记录](neo4j-bi-rewrite-fix.md)：BI2、BI10 原始查询已新增成功，BI8、BI16 的精确索引源已匹配；当前为 8/15 条对应查询成功、80/80 条输出通过 EXPLAIN。** 下文“本次仅定位”和 6 条成功均指修复前轮次。

这次定位覆盖 BI2、BI8、BI10、BI16。结论是**前端缺口、NeuG 索引元数据缺陷、旧版内核覆盖三个因素叠加**，不能把 `no_index_selected` 直接归因于 Neo4j 方言转换，也不能把默认 patched 引擎的行为当成当前共享 NeuG 的行为。

本次仅新增诊断脚本和证据，没有修改生产转换器、默认引擎选择或共享 IncGQ 工作区，没有执行 BI 查询或构建物理索引。原交接中的 9/15 组可注册、6 条对应查询成功的统计仍然有效。下面的成功对照用于定位，不能计入原始 Neo4j baseline 的成功率。

## 各组结论

| 组 | 原始 Neo4j 查询的前端限制 | 已隔离出的内部问题 | 关键对照 |
| --- | --- | --- | --- |
| BI2 | 原始窗口查询可以转换，但保留了 timestamp + duration 表达式 | 转换后的 OPTIONAL FILL 关系分区未规范化；时间加法谓词不能生成支持的索引谓词键；默认旧 RewriterV4 还缺少新的 OPTIONAL gate 更新 | 原 FILL 的源查询也不能命中；只去 OPTIONAL 或只折叠日期均无效，两者同时改变后两个窗口各自的精确源查询均命中。文档 NeuG 查询配文档 NeuG 索引：默认引擎命中窗口 2，stock 命中窗口 1、2 |
| BI8 | 节点列表相加、size、模式列表推导等绑定失败 | 转换器为零计数补入的 OPTIONAL 触发 FILL 关系分区不一致 | 同样谓词、同样 count 的精确源查询，OPTIONAL FILL 不命中，恢复稀疏 MATCH FILL 后命中；文档 NeuG 查询配文档 NeuG 索引也可命中 |
| BI10 | APOC 路径过程没有内部表示，影响后续 QueryPart 的输入 | countryName 本身的转换和索引匹配正常 | 从原查询抽出的 Person→City→Country 片段，直接用 Neo4j 输入可成功转换、改写和通过中间件回译绑定守卫，读取 `PERSON(countryName)` |
| BI16 | UNWIND 的映射列表、CALL 输入和 collect 映射等无法完整转换 | OPTIONAL FILL 关系分区不一致；DATE 谓词不能生成支持的键；默认旧内核缺少聚合后过滤和 OPTIONAL gate 的已有修复 | 纯 NeuG 的 count 和 count DISTINCT 精确源查询都能命中，默认内核加 `WITH ... WHERE cm>0` 则失败，stock 可命中；文档完整 NeuG 查询配文档 NeuG 索引，默认不命中，stock 同时命中 A、B |

BI8、BI16 的中间件最终 reason 可能是 `no_index_selected`，同时在 detail 中记录子片段的 frontend 错误。定位时要同时读取 detail 和各阶段证据，不能只读顶层 reason。

## 1. 中间件的旧 patched 引擎覆盖了共享内核已有修复

默认选择逻辑在 [`engine.py`](../../middleware/cico/engine.py)：先选 `incgq_rewrite_engine_patched`，再选 stock。当前默认二进制为 `middleware/engine/build/incgq_rewrite_engine_patched`，它把快照版 `rewriter_v4.cpp` 编入可执行文件，通过符号 interposition 覆盖共享库中的对应实现。

[`prepare_patch.py`](../../middleware/engine/prepare_patch.py) 从 bundled patch 记录的 **Git blob** 重建整个源文件，而不是在当前 IncGQ 工作区源文件上叠加补丁。现有 ROOT_POST/REL group 补丁基于旧快照，因而构建可复现并不代表包含共享库已有的新修复。

本轮显式使用 `middleware/engine/build/incgq_rewrite_engine`，直接链接同一个 `/mnt/sdb1/feiyang/IncGQ/build/src/libneug.so` 作为对照：

| 控制条件 | 默认 patched | stock / 当前共享库 |
| --- | --- | --- |
| 纯 NeuG、稀疏 MATCH FILL、相同源查询 | 命中 | 命中 |
| 上述查询经 WITH 投影，无过滤 | 命中 | 命中 |
| 上述查询经 WITH 投影，加 `WHERE cm>0` | 未命中 | 命中 |
| 上述稀疏 FILL，查询使用 OPTIONAL count | 未命中 | 命中 |
| 文档 NeuG BI16 查询 + 文档 NeuG BI16 索引 | 未命中 | A、B 均命中 |
| 文档 NeuG BI2 查询 + 文档 NeuG BI2 索引 | 只命中窗口 2 | 两个窗口均命中 |

这里的纯 NeuG 控制分别测试普通 count、count DISTINCT，以及无谓词、tag + 固定日期范围。带 DATE 函数的控制仍失败，见后文。

源码对照 [`validation/neo4j-bi-rewriter-version-20261117.diff`](validation/neo4j-bi-rewriter-version-20261117.diff) 明确显示，当前共享工作区比默认覆盖版本多了：

- 聚合后过滤只检查剩余属性读取，排除本 QueryPart 自己的聚合子树。默认旧实现会在 `validateRewriteCandidate` 的 `projectionBodyPredicate` 属性覆盖检查处拒绝 `cm>0`，调试日志标记为 `CICO_REJECT projection_body_predicate_coverage`。
- `pruneLabel` 后重建 `mergedQueryIR_`，使全局 gate 与局部图使用一致的已裁剪关系分区。旧版本用裁剪前的 merged IR，导致某些 OPTIONAL count 无法通过全局匹配。
- 还有 carried projection、UNWIND、已绑定 anchor 过滤等更新；本轮不把未逐一隔离的更新归因到具体查询。

**这部分是中间件的内核版本集成问题。** 后续应将 ROOT_POST/REL group 修复移植到当前内核基线，并复查已有成功查询；不能简单删除 patched 引擎或覆盖默认文件就宣称完成，因为旧补丁还承担 ROOT_POST/REL group 支持。

## 2. OPTIONAL FILL 的关系标签在注册侧与查询侧不一致

[`neo4j_indexes.py`](../../middleware/cico/neo4j_indexes.py) 在零初始化 + 稀疏分组计数后，为声明补上：

```cypher
MATCH (t:TAG)
OPTIONAL MATCH (m:POST)-[:HASTAG]->(t)
RETURN t.id, count(m)
```

其目的是保留无匹配节点的零计数，不能因为改写不成功就直接删掉 OPTIONAL。本轮没有证明这一步语义错误。

但将上面这条**纯 NeuG 查询同时作为 FILL 源和待改写查询**时，两个引擎都注册成功、绑定成功，却不读索引。插桩默认引擎得到：

```text
CICO_QUERY_IR m:5, t:6, edge:0/0:15,
CICO_FILL_IR t:6, m:5, edge:0/0:14,15,16,
CICO_SHAPE 0 missingRels=1
```

同一 `HASTAG` 的 FILL 模式保留了三个物理关系分区，查询图已按 POST→TAG 裁成一个。`tryRewriteMatchClause` 的 `missingRels` 和 `localShapePossible` 使用关系标签集合精确比较，于是在进入候选验证前就拒绝了。改成纯 mandatory MATCH 控制后，双方都只剩分区 15，`CICO_SHAPE 1`，成功读取 `TAG(probeCount)`。

当前 `/mnt/sdb1/feiyang/IncGQ/src/compiler/catalog/catalog_entry/workload_fill_index_aux_info.cpp` 的 `buildFullPatternIR` 直接从各 QueryGraph 捕获模式，查询侧则在 RewriterV4 中先 `pruneLabel` 再匹配。日志直接证明了两侧表示不一致；具体应在哪个注册阶段规范化，需要实现时结合 OPTIONAL 的 null extension 与零计数契约选择，不能只放宽标签集合比较。

**这是当前 NeuG FILL 元数据/匹配路径的缺陷，由中间件生成的 OPTIONAL 形式触发。** 与第 1 节的“查询 merged gate 未更新”是两个独立问题：stock 已有后者修复，但 OPTIONAL FILL 源的精确自匹配仍然失败。

## 3. 能绑定的时间表达式不一定能生成索引谓词键

对实际注册目录取出 `FillIndexAuxInfo::getPredicateTemplates()`，得到：

- BI2 的 `TIMESTAMP(...) + duration('100 days')`、`+ duration('200 days')` 相关谓词含 `@unsupported-index-predicate`。
- BI16 的 `date(m.creationDate)=date(TIMESTAMP(...))` 也含该标记。
- BI8 的 tag 和固定时间范围全部有有效 typed predicate key，排除了它由时间谓词导致失败的可能。

当前内核 `workload_fill_index_aux_info.cpp` 的 `collectPredicateTemplates` 在 `makeIndexPredicateKey` 失败时保留该占位符，**不会因此拒绝注册**。`index_predicate_key.cpp` 的 scalar function 白名单只有 `+ - * / % LABEL STRUCT_EXTRACT TIMESTAMP`，DATE 和 interval 表达式路径未得到完整支持。注册通过只证明声明能建立，不能证明其谓词可用于局部替换证明。

控制结果：

| 索引源与待改写源一致 | 默认 patched | stock |
| --- | --- | --- |
| mandatory MATCH，固定 timestamp 范围，count | 命中 | 命中 |
| mandatory MATCH，timestamp + duration 范围，count | 未命中 | 未命中 |
| mandatory MATCH，DATE 等式，count / count DISTINCT | 未命中 | 未命中 |
| mandatory MATCH，tag + 固定一天范围，count / count DISTINCT | 命中 | 命中 |

BI2 源查询的双因素对照进一步确认：保留 OPTIONAL 但折叠日期，仍不命中；只移除 OPTIONAL 但保留日期加法，也不命中；同时变成稀疏 MATCH + 固定时间范围，两个窗口各自都命中。

**这是当前内核的谓词表示能力缺口，中间件也缺少对这些已特化常量的可用性规范化。** 日期加法常量折叠和 DATE→范围可以作为有条件的前端补足，但必须处理时区、日期边界、类型与零计数语义；本轮控制只使用文档样本的固定 timestamp，不是可直接上线的通用转换规则。

## 4. 证据、复现与后续修复顺序

运行可重复的分层检查，不连接 Neo4j：

```sh
python3 middleware/tests/neo4j_bi_rewrite_diagnosis.py \
  --output /tmp/neo4j-bi-root-cause.json
python3 middleware/tests/neo4j_bi_rewrite_diagnosis.py \
  --engine middleware/engine/build/incgq_rewrite_engine \
  --output /tmp/neo4j-bi-root-cause-stock.json
```

实验文档为 `/mnt/sdb1/feiyang/IncGQ-experiment-docs`，本轮读取 `jcloud-exp` 分支、提交 `08f1a2659ea2304b7940fd15db3c23382bf37eb7`；共享 manifest 每组使用 sample 0。本轮未更新文档仓库。NeuG 文档查询是人工编写的独立对照，注明了 TAG.name 唯一性假设，不能解释为已经完成自动翻译或通用等价证明。

- [`validation/neo4j-bi-root-cause-20261117.json`](validation/neo4j-bi-root-cause-20261117.json)：默认引擎，62 个直接 NeuG 编译/改写探针，加上四条原查询的中间件结果和 BI10 的 Neo4j 片段结果。每个直接探针先在 baseline 目录绑定，再调用引擎改写，并检查最终 Bound IR 实际读取的索引载体；不使用 `changed` 作为命中依据。
- [`validation/neo4j-bi-root-cause-stock-20261117.json`](validation/neo4j-bi-root-cause-stock-20261117.json)：相同 62 个探针，直接链接当前共享库。两份均记录 schema、文档源文件、引擎和共享库 SHA-256；所有直接探针的 baseline 绑定通过，控制 FILL 注册通过。
- [`validation/neo4j-bi-kernel-trace-20261117.json`](validation/neo4j-bi-kernel-trace-20261117.json)：OPTIONAL/mandatory 最小源查询、聚合后过滤和 DATE 控制的注册、元数据、实际载体读取、stderr 跟踪；另存实际 BI2/8/16 转换索引及文档 NeuG 索引的谓词模板。
- [`patches/neo4j-bi-diagnostic-engine.patch`](patches/neo4j-bi-diagnostic-engine.patch)：仅用于临时引擎插桩，增加目录输出和 stderr 日志，不改变匹配条件，不用于生产构建。生成时以已记录的 bundled RewriterV4 源文件及当前 engine CC 为基线。

复建插桩引擎时，在临时目录复制 `middleware/engine/` 中的 `CMakeLists.txt`、`incgq_rewrite_engine.cc`、`cico_exec.cc`、`bound_ir.h`，再把 `middleware/engine/build/patched/rewriter_v4.cpp` 复制进去。对临时目录执行 `patch -p1` 应用上述 diagnostic patch。配置时显式指定 `NEUG_SOURCE_DIR`、`NEUG_BUILD_DIR` 和 `CICO_INTERPOSE_SOURCES=<临时目录>/rewriter_v4.cpp`，构建 `incgq_rewrite_engine_patched`。在诊断脚本的 `--engine` 指向临时二进制时，catalog 将额外输出 diagnostics。要抓 stderr，可在 `IndexMiddleware` 构造参数传入 `engine_log`，对 trace JSON 中的 source/query 重新注册和调用 `engine.rewrite`。不要把临时源文件或插桩二进制覆盖默认构建。

建议接续顺序：

1. 先解决旧内核覆盖问题，将现有 ROOT_POST/REL group 补丁移植到当前基线；以本报告的 WITH 过滤/OPTIONAL 控制和原有六条成功查询做回归。
2. 在 NeuG 侧修正 OPTIONAL FILL 与查询图关系分区的规范化，保留 null extension、零计数及完整分组证明。
3. 补齐时间谓词表示，或在前端对满足明确条件的常量/日期表达式做语义保持的规范化。
4. 最后补 BI8 列表、BI10 APOC、BI16 映射/CALL 上下文转换，并重新检查原始 Neo4j 查询。纯 NeuG 控制成功不替代原始查询验收。
