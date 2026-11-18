# NeuG FILL 文件注册、Neo4j 输出与增量加载（2026-11-18）

用户要求：索引定义可使用实验文档中的 NeuG 语法；通过编辑 FILL 文件并重新加载中间件增添索引；之前已成功构建、定义未变化的索引不重新构建。查询仍按 Neo4j 方言输入，先转换为内部方言，再匹配改写并回译。

本轮实现了这条文件加载路径。低层注册 API 仍保留“调用方负责已有物理数据”的约定；新的文件目录则只启用有成功物化回执的定义。

## 文件与入口

- 可编辑的静态注册文件：[ldbc-bi-native.cypher](../../middleware/indexes/ldbc-bi-native.cypher)。从实验文档 NeuG BI10、BI9、BI5、BI6 复制 countryName、ROOT_POST 及消息/Person 计数定义，共 7 个载体定义。BI10/BI11 共用的 countryName 只保留一份。该文件尚未在 SF1 中执行物化。
- 实验源仓库：`/mnt/sdb1/feiyang/IncGQ-experiment-docs`，`jcloud-exp`，提交 `08f1a2659ea2304b7940fd15db3c23382bf37eb7`。
- 源脚本：`NeuG/queries/index/ldbc-bi/biN/index.cypher`；参数化定义另见对应 `parameter.cypher`。它们与 Neo4j 目录下的物化定义不保证完全相同。
- 核心代码：[index_files.py](../../middleware/cico/index_files.py)；CLI：[__main__.py](../../middleware/cico/__main__.py)；HTTP：[server.py](../../middleware/cico/server.py)。
- 索引定义显式声明为 NeuG 输入，跳过 Neo4j frontend 的输入转换。查询仍使用 Neo4j frontend，输出和物化计划使用 Neo4j dialect。
- FILL 文件只接受 NeuG DDL、FILL 和无操作语句。普通 MATCH/SET、查询或 GDS 调用拒绝加载；物理重置与写回由物化器生成。`DROP TABLE` 只在本轮新建的临时元数据 schema 中执行，不直接删除 Neo4j 数据。

## 怎样判断定义是否变化

旧 `register_index()` 的简单重复注册判断仍是 `existing == texts`。新 `IndexFileCatalog` 使用编译后的定义指纹，文件名、mtime、原始全文不是其相等条件。

1. 分词解析文件，在独立 schema 中处理 DDL。绑定 FILL 源，使用内部打印器产生规范语句；为最终标量投影生成稳定且不与用户标识符冲突的别名。实际向 NeuG 注册的也是这份规范语句。
2. 同一载体的分支组成一个索引；相同的规范源去重。不同列组重叠写同一属性会拒绝，避免互相覆盖。
3. 从真实绑定 IR 提取属性读取和派生关系读取，连接到其他 FILL 的写入载体，形成依赖图；依赖循环拒绝加载。
4. 生成完整目标物化计划，包含 DDL、重置、写回以及 NeuG 隐藏聚合状态。保留字符串字面量、标识符、类型、方向、表达式和输出列。
5. 对规范计划的 token 序列、载体类型/键/端点/default、基础 schema、profile/mapping，以及上游索引指纹做稳定 JSON 序列化，计算 SHA-256。
6. 同一物理数据库中，该 id 的指纹相同且持久状态为 `ready`，才跳过构建。状态为 `pending`、`building` 或无记录时均不跳过。

| 修改 | 判断 |
| --- | --- |
| 注释、空白、FILL 源中的关键字/函数名大小写 | 规范输出相同，不重建 |
| 同一文件再次列入加载清单、重复的同一规范定义 | 去重，不重建 |
| 字符串 `'Paris'` 改为 `'PARIS'` | 视为变化；不会把字面量转为小写 |
| 过滤、关系方向、聚合、DISTINCT、列、类型、默认值、实际绑定参数改变 | 指纹变化，需要构建 |
| 上游索引改变，下游自身文本未改 | 下游指纹包含上游指纹，因此也需构建 |
| 新增互不覆盖的索引 | 保留旧定义回执，只构建新增项及受影响的依赖 |
| 删除定义 | 从改写目录移除，不自动删除物理属性或派生关系 |

这是保守的相等判断，不是任意 Cypher 的语义等价判定。变量重命名、不同但等价的表达式等可能被视为变化。若规范物化计划无法表示某种合法 NeuG 结构，本轮加载报错并保留旧目录，不猜测等价。

规范输入还有一个必要用途：NeuG 隐藏状态列名含源文本的哈希。只规范比较、实际仍注册原始文本，会让格式修改改变状态列名，却错误复用旧物理状态。本实现同时规范注册文本，避免这个问题。

## 成功回执与重载过程

状态文件记录物理数据库身份、每个索引的指纹、依赖和 `pending/building/ready` 状态。Neo4j CLI 连接时读取 `db.info().id`，将其与 URI、数据库名一起作为身份；同一状态文件不能复用于另一身份。

加载先编译全部定义，验证物化计划；通过 `Neo4jIndexExecutor` 构建时，还会对所有待执行的目标语句先做 EXPLAIN。全部通过后，启用未变化且已有成功回执的定义，并按依赖顺序构建其余索引。每个索引依次执行自身 DDL、reset、fill，完成后才落盘为 `ready`，然后可供查询改写。

回执使用同目录临时文件、fsync、原子替换保存；重载持有中间件锁及状态文件的进程文件锁。执行物理写入前先落盘 `building`。失败或进程中断后，该索引不会作为已成功索引恢复；重试从它继续，已经成功的上游跳过。文件编译或目标 EXPLAIN 失败发生在任何物理重置之前，保留已有改写目录和回执。

基础图数据应保持为同一快照。本功能管理定义及构建生命周期，不实现图更新后的增量维护，也不检测外部进程手工修改索引值。数据库恢复、外部重建或数据更换后应重新核对状态文件；同一物理库的物化更新应由一个服务实例负责。

首次使用全新状态文件时，所有定义都为待构建。旧流程只有“已注册”记录，不能据此自动认定物理数据及隐藏状态已构建成功。本轮没有将旧记录冒充成功回执，也没有重建现有 SF1 索引。已有物理索引仍可沿用低层注册 API，但文件托管的跳过构建保证要求本流程保存过成功回执。

## 命令行与 HTTP

以下从仓库根目录执行。先检查待构建计划，不写数据库：

```sh
PYTHONPATH=middleware python3 -m cico build \
  --schema middleware/schemas/ldbc-bi.yaml --profile neo4j \
  --fill-file middleware/indexes/ldbc-bi-native.cypher \
  --index-state middleware/runtime/bi_sf1/native-index-build-state.json \
  --neo4j-uri bolt://127.0.0.1:7693 --json
```

该命令只恢复有成功回执的索引；没有回执的定义出现在 `pending/plans`，不加入活跃改写目录。离线检查可将 `--neo4j-uri` 换成 `--index-target some-offline-generation`，离线身份应使用独立状态文件，不能与实际数据库状态文件混用。

启动并只构建待构建部分：

```sh
PYTHONPATH=middleware python3 -m cico serve \
  --schema middleware/schemas/ldbc-bi.yaml --profile neo4j \
  --fill-file middleware/indexes/ldbc-bi-native.cypher \
  --index-state middleware/runtime/bi_sf1/native-index-build-state.json \
  --neo4j-uri bolt://127.0.0.1:7693 --materialize-new --port 8765
```

之后编辑该 FILL 文件，使用相同命令重启服务，或让运行中的实例重载已配置文件：

```sh
curl -sS -X POST http://127.0.0.1:8765/index-files/reload \
  -H 'Content-Type: application/json' -d '{}'
curl -sS http://127.0.0.1:8765/index-files
```

报告区分 `added/changed/unchanged/removed/pending/built/active`。`plans` 只保留尚未完成的物化计划。`--fill-file` 可重复指定，路径清单在启动时固定；不做文件监听。HTTP 文件托管模式禁止另行用 `/index`、`/script` 等入口修改注册元数据，避免绕过回执目录。

需要认证时用 `--neo4j-user`，密码由环境变量 `CICO_NEO4J_PASSWORD` 读取。参数可用 `--fill-params` JSON；时间参数建议在 FILL 中显式使用 `TIMESTAMP($date)` 或通过 Python 传 `datetime`。`{}` 位置模板应先渲染，禁止把未渲染的模板当作实际定义。

## Python 与单次注册

```python
from cico import IndexMiddleware, IndexFileCatalog
from cico.index_files import Neo4jIndexExecutor

runner = Neo4jIndexExecutor('bolt://127.0.0.1:7693')
try:
    with IndexMiddleware('middleware/schemas/ldbc-bi.yaml', profile='neo4j') as mw:
        catalog = IndexFileCatalog(
            mw, ['middleware/indexes/ldbc-bi-native.cypher'],
            'middleware/runtime/bi_sf1/native-index-build-state.json', runner.target)
        report = catalog.reload(runner)   # 注册、只物化待构建项、保存回执
        result = mw.execute(neo4j_query, parameters=parameters)
        report = catalog.reload(runner)   # 文件未变：零条物化执行
finally:
    runner.close()
```

目录对象应在新中间件实例上创建，不与外部直接注册混用。自定义 executor 必须在语句成功提交且结果已消费后才返回；不能把“发出异步请求”当作物化成功。若实现 `validate(statements)`，重载会在首条 reset 前调用它。

只要显式 NeuG 元数据注册、不需要文件托管时：

```python
mw.apply_ddl('ALTER TABLE PERSON ADD countryName STRING', input_dialect='neug')
mw.register_index([
    'FILL PERSON(countryName) FROM ('
    'MATCH (c:PLACE)<-[:ISPARTOF]-(:PLACE)<-[:ISLOCATEDIN]-(p:PERSON) '
    'RETURN p.id,c.name)'
], input_dialect='neug')
plan = mw.build_statements(include_internal_state=True)
```

CLI 对应 `--index-script path.cypher --index-script-dialect neug`。这条低层路径不执行物理构建，也不产生成功回执；调用方负责物理状态有效。`include_internal_state=True` 需要本轮新增的引擎 `describe_fill` 操作，旧二进制必须重编；旧默认 `build_statements()` 路径保留原行为。

## 验证与剩余边界

- 当前全部回归 **81/81** 通过，覆盖真实编译器、文件变更、依赖传播、默认值/参数、字符串大小写、失败重试、目标身份、CLI 和 HTTP。
- Neo4j 独立小图 **6/6**：首次构建；重启零构建；格式修改零构建；只构建新增项；按依赖重建；派生关系物化。生成写入与改写输出通过 EXPLAIN，原查询/输出实际结果多重集一致。仅操作临时专属标签，数据已清理。没有执行 BI 负载。
- [小图证据](validation/neo4j-index-files-20261118.json)，入口 [index_file_acceptance.py](../../middleware/tests/index_file_acceptance.py)。
- [回归测试日志](validation/neo4j-index-files-tests-20261118.txt)、[实现/引擎哈希及验收口径](validation/neo4j-index-files-provenance-20261118.json)。可编辑的 7 个静态定义另经检查，其 20 条目标语句全部通过 EXPLAIN，未在 SF1 物化。
- [NeuG 文档脚本审计](validation/neug-fill-neo4j-registration-20261118.json)：逐个原样检查 17 个 `index.cypher`，12 个生成计划、共 **84 条** Neo4j 语句通过 EXPLAIN；4 个文件仅 schema 初始化（BI2/8/13/16），须另加参数化 FILL；BI15w 是未填 `{}` 的位置模板，不能原样加载。参数文件不计入此轮审计。没有构建这些 BI 索引。
- 审计入口 [neug_fill_registration_audit.py](../../middleware/tests/neug_fill_registration_audit.py)。NeuG BI3/4/14 等文件已可以走此注册路径，但定义可能与 Neo4j 文档不同；这些结果不能替换旧 15 组 Neo4j 索引矩阵，也未证明完整 BI 查询覆盖率提升。
- 原生 `FILL GROUP_SRC_DST(...)` 子关系载体目前应改为显式声明关系组、以组为目标；该文件协议对这种子载体保守拒绝。
- 多个文件若重叠写同一属性的不同列组，会拒绝加载。BI14 与 BI15 等共享计数属性应在实际注册文件中整理为一致的定义和依赖。

本轮真实小图还发现内核 `COUNT(nullable property)` 的存在性边界：Person 1 有一个非空值，Person 2 有匹配行但值全为 NULL，原结果为 `(1,1),(2,0)`；当前内核的一条路由用 `cached <> 0` 作为存在性条件，会丢掉 `(2,0)`。回译结构相等不能证明这个内核变换正确。因此新文件目录在预检中拒绝这类 nullable/computed COUNT，报 `kernel group-presence proof is missing`，发生在物理写入之前。有分组的零值不能与空组混同，后续应将独立行存在性状态正式接入该路由；不能用数据碰巧没有 NULL 作为证明。此限制及回归没有按 BI 编号或查询文本特化。

构建和复现：

```sh
cmake --build middleware/engine/build --target incgq_rewrite_engine_patched -j 2
python3 -m unittest discover -s middleware/tests -v
python3 middleware/tests/index_file_acceptance.py \
  --output implementation/middleware/validation/neo4j-index-files-20261118.json
python3 middleware/tests/neug_fill_registration_audit.py --uri bolt://127.0.0.1:7693 \
  --output implementation/middleware/validation/neug-fill-neo4j-registration-20261118.json
```
