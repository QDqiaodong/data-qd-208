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
