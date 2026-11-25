# 追踪因果与占用时间

spans为1..2000项全局唯一id、trace、parent(null或span id)、service、start/end。标识ASCII；时间为相同单位的精确十进制字符串，end>=start。所有父引用必须存在且同trace，整个父图无环；先验证所有引用再遍历，任意输入顺序下坏引用均为ValueError。

每trace输出排序的roots、服务集合、直接children，以及wall_time=max(end)-min(start)。wall_time是整体墙钟跨度，绝不是因果关键路径。self_time按每span区间减去直接子span与它相交区间的并集，重叠子调用只扣一次；零时长合法，子span越过父边界允许（异步/时钟偏差），仅裁剪后扣除，不假设子调用必被父包含。不能把各self_time相加解释为trace关键路径或集群CPU时间。

这是离线因果数据处理，不接入真实追踪平台，不做监控看板或工单客服。核心 `process` 仍不做跨服务时钟校准、links、多父或采样补全；跨时钟区间校准只由独立的 `plan_trace_replay` API 提供。原有输入错误拒绝、不输出部分图，以及 IO 严格重复键检查、只创建结果不覆盖的契约不变。

## 跨时钟证据回放规划

`plan_trace_replay(data)` 接收恰好含 `reference`、`clocks`、`constraints`、`spans`、`segments`、`requests` 的对象，并返回 `trace-replay-plan-v1` 清单。所有校验先于规划；任一未知字段、类型/范围错误、重复身份、约束矛盾或相对 reference 无界都会抛 `ValueError`。函数不修改输入，也不读取 segment 指向的原始对象。

时钟偏移满足 `global = local + O_clock`，且 `O_reference = 0`。约束 `min <= O_right - O_left <= max` 用差分约束最短路求每个偏移相对 reference 的可达紧下界和紧上界；不能只取单个名义偏移。span 的可能匹配条件是完整可行偏移投影区间 `[min_offset,max_offset]` 与 `[from-end, to-start]` 有闭区间交集。matched spans 的递归父祖先构成 protected spans；不加入兄弟、后代或其他 trace。

每个请求按 clock 合并 protected spans 的本地闭区间。segment 同样是本地闭区间，可重叠、同边界或为零长度点。可用 segment 全集不能覆盖时返回 `status="incomplete"` 和规范合并后的 `missing`，不返回部分 segments；其他请求仍独立规划。可覆盖时，segment 集合依次最小化 bytes 总和、segment 数量、按 id 排序后的列表字典序。返回的 `matched_spans`、`protected_spans`、`segments` 均为排序后的 id 列表。

所有十进制值使用 `Fraction` 精确运算并规范化输出：零为 `0`，无正号和无意义前导/末尾零。结果顶层恰为 `schema`、`clocks`、`requests`；clock 项恰为 `clock`、`min_offset`、`max_offset`，ready 请求含 `segments`、`total_bytes` 而无 `missing`，incomplete 请求相反。

## 分析凭证审计与证据包信任状态

`audit_trace_attestations(data)` 接收恰含 `snapshots`、`attestations`、`withdrawals`、`bundles` 的对象，返回 `trace-attestation-audit-v1` 结果。全部输入先完整校验再审计：任一未知字段、类型/范围错误、越界、重复身份、坏引用、非法摘要、替代分叉或成环、依赖成环均为 `ValueError`，不输出部分结果，不修改入参。该能力只交付可追溯的当前信任判断：不重新编译采集事件，不选择归档对象或候选集合，不连接外部平台，也不重写冻结印章。

### 快照与规范摘要

`snapshots` 为 1..256 项，每项恰含 `id`、`spans`；id 互不重复，全部快照 span 总数不超过 100000。每个 `spans` 独立满足 `process({"spans": ...})` 的完整输入规则；任一快照非法即整批失败，不把单个快照标成不可信。同一 span id 可出现在不同快照，因为快照是独立证据对象。规范 JSON 指 UTF-8、对象键排序、无多余空白（`,` 与 `:` 分隔）的编码；十进制字符串按原文参与摘要，不得转成浮点数。

- `source_digest`：span 按 id 排序，每项只保留 `id`、`trace`、`parent`、`service`、`start`、`end` 六字段的原始字符串，组成 `{"spans":[...]}` 规范编码后的 SHA-256 小写十六进制。
- `analysis_digest`：对同一快照调用 `process` 的返回对象作同样规范编码后的 SHA-256。

摘要口径与输入列表顺序及字典插入顺序无关。

### 凭证生命周期

`attestations` 为 1..4096 项，每项恰含 `id`、`snapshot`、`source_digest`、`analysis_digest`、`replaces`；id 互不重复；`snapshot` 必须存在；两个摘要必须是 64 位小写十六进制；`replaces` 为 `null` 或同一 snapshot 的另一凭证 id。每个凭证最多被一个直接后继替代，整组替代关系无环；输入顺序不表示新旧。

凭证声明的两个摘要分别与快照实际摘要比较：都相同为 `verified`，否则为 `mismatch`，且 `mismatches` 按字段名列出 `source_digest`、`analysis_digest` 中的不一致项（一致时为 `[]`）；不能只核 source 摘要就假定分析结果无误。只有 verified 后继使替代链上的祖先进入 superseded；`superseded_by` 是链上距离最近的 verified 后继，摘要错误的更正稿不能挤掉原本有效的凭证。`withdrawals` 为 0..4096 项，每项恰含 `id`、`attestation`、`reason`；id 互不重复，同一凭证最多一项撤回，`attestation` 必须存在，`reason` 为 1..256 个 Unicode 字符且首尾无空白。撤回与内容核对互相独立，摘要错误的凭证也可被撤回；verified 后继被撤回后祖先保持 superseded，不自动恢复为 current。凭证 usable 当且仅当 verified、未撤回且没有 verified 后继。

### 证据包与冻结印章

`bundles` 为 0..4096 项，每项恰含 `id`、`attestations`、`depends_on`、`seal`；id 互不重复；`attestations` 为 1..256 个互不重复且存在的凭证 id；`depends_on` 为 0..256 个互不重复且存在的 bundle id，不得引用自己，依赖图无环；两个列表的顺序都不表达含义；`seal` 为 64 位小写十六进制。

包的 expected seal 是规范对象 `{"id":包id,"attestations":[{"id","source_digest","analysis_digest"},...],"depends_on":[{"id","expected_seal"},...]}` 的 SHA-256：两个列表均按 id 排序，凭证项取凭证声明的两个摘要，依赖项取依赖包递归计算的 expected seal；父包不能用子包自报的错误 seal 使自身看似有效。seal 只冻结签发时引用的证据身份，不随当前撤回或 superseded 状态重写。

包状态按优先级唯一确定：`invalid_seal`（自身 seal 不等于 expected seal）> `withdrawn`（任一直接凭证被撤回，或任一依赖包为 withdrawn）> `superseded`（任一直接凭证 superseded，或任一依赖包为 superseded）> `blocked`（任一直接凭证内容 mismatch，或任一依赖包为 invalid_seal/blocked）> `usable`。依赖的 withdrawn/superseded 沿任意深度传播；依赖自身 seal 错误只使父包 blocked，不把父包标成 invalid_seal。`issues` 保留所有直接和传递原因，每项恰含 `kind`、`ref`，kind 为 `attestation_mismatch`、`attestation_withdrawn`、`attestation_superseded`、`dependency_invalid_seal`、`dependency_blocked`、`dependency_withdrawn`、`dependency_superseded` 之一，按 kind、ref 排序并去重。包当前不可用时仍返回原 seal 与 expected seal，不改写冻结印章，也不自动换用更正后的凭证。

### 输出

结果顶层恰含 `schema`、`snapshots`、`attestations`、`bundles`。snapshots 按 id 排序，每项恰含 `id`、`source_digest`、`analysis_digest`；attestations 按 id 排序，每项恰含 `id`、`snapshot`、`content_status`、`mismatches`、`withdrawn`、`withdrawal_id`、`superseded_by`、`usable`，两个可空 id 为 `null`；bundles 按 id 排序，每项恰含 `id`、`seal`、`expected_seal`、`status`、`issues`。

## 资源用量结算与配额准入

`settle_trace_metering(data)` 接收恰好含 `spans`、`meters`、`ownership`、`budgets`、`requests` 的对象，返回 `trace-meter-settlement-v1` 结果。spans 直接复用 `process({"spans": spans})` 的完整契约校验；其余字段也必须先完整校验。任何未知字段、类型/范围错误、重复身份、坏引用、非法累计读数，或已产生正整数归因/被请求引用但缺少唯一 budget 政策，都抛 `ValueError`。函数不修改输入、不连接外部平台，时间只用 `Fraction`，数值累计只用整数或 `Fraction`。

`ownership` 必须恰好覆盖每个不同 trace 一次；每项为 `trace`、`account`。meter 是独立物理累计器：`id` 在 meters 内唯一，相同 `service`/`unit` 的多只 meter 不合并。readings 按精确 `at` 时间排序（同一 meter 的时间点必须按 `Fraction` 互不相同），相邻读数形成半开区间 `[from,to)`。上升或持平增量为后值减前值；下降只有在 `modulus` 非空且所有读数小于 modulus 时解释为恰好一次回卷，增量为 `modulus - 前值 + 后值`。无 modulus 的下降、越界值或 modulus 上的读数均拒绝。零增量也输出 interval；首个读数只是基线。

同 service 所有权按任意深度祖先关系确定：把 meter 边界和同 service span 的 start/end 作为分点，在每个基本时间段内，只有不存在同时活动的同 service 严格后代的活动 span 拥有该时间。多个无祖先关系的分支可同时拥有同一时间；跨 service 后代不剥夺所有权。零时长 span exposure 为零；允许父子区间越界，只按真实同时活动判断，不能使用只看直接孩子的 self_time。

interval 内各所有 span 的拥有时长之和为 exposure 总和。总和为零时整个 delta 计入 `unassigned`；否则精确份额为 `delta * exposure / exposure总和`，先取整数下界，剩余单位按小数余数从大到小补 1，余数相同按 span id 字典序升序补 1。每个 interval 严格满足 `sum(allocations.units) + unassigned = delta`。分配单位经 trace 归入 account；unassigned 不归账户。

budgets 的 `(account,service,unit)` 唯一，account 必须出现在 ownership，`(service,unit)` 必须出现在 meters。所有正整数结算组合和 requests 引用组合都必须有 budget。`settled` 是该组合所有 meter interval 分配整数之和，`used=committed+settled`，`remaining=limit-used`；允许 used 超过 limit，此时 `status="exceeded"` 且 remaining 为负，否则为 `within`。每个 request 是独立 what-if，不修改 budget 或后续请求观察到的用量；当任一 item 的 `used + units > limit` 时 `status="blocked"`，并在 `deficits` 中按 `service`、`unit` 排序列出全部失败项的 `deficit=used+units-limit`，否则 `allowed` 且 deficits 为空。

结果顶层恰含 `schema`、`meters`、`spans`、`budgets`、`requests`。meters 按 id 排序，每项恰含 `id`、`service`、`unit`、`intervals`、`total_delta`、`total_allocated`、`total_unassigned`；intervals 按时间排序，每项恰含 `from`、`to`、`delta`、`allocations`、`unassigned`；allocations 只含正单位并按 span id 排序，每项恰含 `span`、`trace`、`account`、`units`。spans 按 id 排序，每项恰含 `id`、`trace`、`service`、`account`、`usage`，usage 按 unit 排序且只含正用量。budgets 按 account、service、unit 排序，包含输入五字段及 `settled`、`used`、`remaining`、`status`。requests 按 id 排序，每项恰含 `id`、`account`、`status`、`deficits`。`from`/`to` 原样保留输入中的十进制字符串。

## 离线数据保留与合规擦除规划

`plan_trace_retention(data)` 只处理对象生命周期：在原始片段被多个分析制品复用、同时存在分级保留策略、调查冻结和擦除请求时，给出当前允许销毁的对象、必须保留的最小对象集，以及每个请求完成或被阻塞的原因。它不连接外部平台、不改变其他业务模块、不新增运行时第三方依赖，不修改输入，且所有时间运算只用 `Fraction`，绝不转成浮点数。输入顶层恰含 `now`、`objects`、`products`、`policies`、`holds`、`requests`；任何未知字段、类型错误、越界、重复身份、坏引用、依赖成环、互斥策略或不可能的生命周期都抛 `ValueError`，先完整校验再规划，不返回部分结果。

### 对象与依赖图

`objects` 为 1..100000 项，每项恰含 `id`、`trace`、`kind`、`created`、`expires`、`subjects`、`depends_on`。标识沿用 ASCII 契约；`kind` 为 `raw`、`derived` 或 `catalog`。`created<=now`；`expires` 为 `null` 或不早于 `created`。`subjects` 为 0..64 个互不重复的主体标识；`depends_on` 为 0..256 个互不重复且存在的对象 id，不能引用自己。raw 不得依赖其他对象；derived 至少依赖一个对象；catalog 依赖可为空。整个依赖图必须无环；所有坏引用在完整 id 表建立后统一检查，因此拒绝行为与输入顺序无关。同一对象可被多个 product 或上游对象共享。

### 分级保留策略与对象期限

`policies` 为 1..4096 项，每项恰含 `id`、`trace`、`kind`、`keep_until`、`priority`；`trace` 为对象中存在的 trace 或 `*`，`kind` 为三种对象 kind 或 `*`，`priority` 为 0..1000000000 的整数，`keep_until` 不得早于 `now`。每个对象必须能被至少一条策略命中，并按最高 `priority` 选出唯一胜选策略；同一最高 priority 若有多条策略同时可命中某对象，只有 `keep_until` 完全相同才合法，否则整份文档拒绝（通配选择器按数据中实际出现的 trace/kind 展开判定）。对象的政策期限是胜选策略的 `keep_until` 与自身非空 `expires` 中的较晚者；`expires` 为 `null` 时只采用策略期限。对象自身 `expires` 已过不能忽略更长的合规策略。`now >= 政策期限` 时输出 `expired=true`（边界相等即过期）。

### 产品、证据闭包与冻结

`products` 为 0..10000 项，每项恰含 `id`、`roots`、`status`；roots 为 1..256 个互不重复且存在的对象，`status` 为 `active` 或 `retired`。产品的证据闭包是 roots 及其递归 `depends_on`。active product 的闭包必须保持可重建；retired product 不施加保留义务，但其对象仍可能因别的职责保留。

`holds` 为 0..4096 项，每项恰含 `id`、`roots`、`mode`、`closed`，`mode` 为 `evidence` 或 `rebuild`，`closed` 为严格布尔。未关闭的 evidence hold 只冻结 roots 本身；未关闭的 rebuild hold 冻结 roots 的完整依赖闭包。已关闭 hold 不保留任何对象，但仍在输出中报告且其 `objects` 为空。冻结对象无论期限和擦除请求如何都不能销毁。

### 擦除请求与规范目标集

`requests` 为 0..4096 项，每项恰含 `id`、`subject`、`submitted`、`scope`，id 全局唯一，`submitted<=now`，`scope` 为 `direct` 或 `rebuildable`。direct 请求只以直接含该 subject 的对象为目标；rebuildable 请求还把这些对象的全部递归祖先（沿反向 depends_on 可达的对象）纳入目标，因为保留这种不含 subject 但闭包内含 subject 的祖先仍可重建受影响证据。多个请求彼此独立评估，都基于同一份当前数据，不因前一个请求的拟议删除改变后一个请求看到的状态。

对每个请求先计算政策期限、active product 最小重建闭包和未关闭 hold 的冻结集合，再独立判定：

1. 目标对象若被未关闭 hold 冻结，或 `now` 严格早于其政策期限，即阻塞请求；两类阻塞独立报告。
2. active product 的职责是请求执行后每个 root 仍至少有一条完整可重建证据路线，而不是所有现有依赖永远保留。目标覆盖某对象时，可在同一 product 已引用的共享依赖中改用不含该 subject 的路线；不能为保住 product 而保留仍可重建该 subject 的目标对象。依赖按 AND 解释：保留某 derived/catalog 对象时其全部直接依赖都必须保留；product 的多个 roots 按 OR 解释，只需至少一个 root 的完整闭包可用。排除请求 targets 后没有合格 root 的 product 阻塞请求。对每个请求，排除 targets 后仍有两个及以上合格 root 的 active products 最多 18 个，且这些产品的合格 roots 总数最多 36；其余未阻塞 active product 必须恰有一个合格 root。任一请求越界都整体抛 `ValueError`，不做局部或近似规划。
3. 请求 blocked 时不提出部分删除：`delete` 与 `selections` 为空，并列出全部阻塞对象对应的 policy/hold/product。请求 completed 时，`delete` 是目标对象连同因失去全部使用方而变得可回收的过期对象构成的最大安全集合；仍被 active product 选中路线、未关闭 hold、未到期政策，或集合外保留对象的 AND 依赖所要求的对象不得作为级联垃圾删除。
4. completed 请求同时给出每个 active product 选择的保留 root：先最小化所有选中 root 完整闭包的对象并集大小，再最小化选中 root 数量（产品数固定时即退化为决胜规则），仍相同则比较按 product id 排序后的 `product:root` 列表字典序。选择与 delete 集共同求解，不逐 product 独立取局部最小闭包，也不在选择后再贪心删除共享对象。

### 输出

结果可 JSON 序列化，顶层恰含 `schema`、`objects`、`holds`、`requests`，`schema` 固定为 `trace-retention-plan-v1`。objects 按 id 排序，每项恰含 `id`、`trace`、`kind`、`policy_until`、`expired`、`held_by`、`active_products`；`policy_until` 为规范化精确十进制字符串（零为 `0`，无正号和无意义零），`held_by` 与 `active_products` 均按 id 排序。holds 按 id 排序，每项恰含 `id`、`mode`、`closed`、`objects`，`objects` 是按 mode 计算的规范冻结集合并按 object id 排序，已关闭时为空。requests 按 id 排序，每项始终含 `id`、`subject`、`scope`、`status`、`targets`、`delete`、`blockers`、`selections`；`targets`、`delete` 按 object id 排序，`blockers` 按 `kind`、`ref` 排序且去重，每项恰含 `kind`（`policy`、`hold` 或 `product`）、`ref`，`selections` 按 product id 排序，每项恰含 `product`、`root`。blocked 时 `delete` 与 `selections` 必须为空；completed 时 `blockers` 必须为空。输出不依赖任何输入列表顺序，也不复用可被调用方修改的可变输入对象。
