# 追踪因果与占用时间

spans为1..2000项全局唯一id、trace、parent(null或span id)、service、start/end。标识ASCII；时间为相同单位的精确十进制字符串，end>=start。所有父引用必须存在且同trace，整个父图无环；先验证所有引用再遍历，任意输入顺序下坏引用均为ValueError。

每trace输出排序的roots、服务集合、直接children，以及wall_time=max(end)-min(start)。wall_time是整体墙钟跨度，绝不是因果关键路径。self_time按每span区间减去直接子span与它相交区间的并集，重叠子调用只扣一次；零时长合法，子span越过父边界允许（异步/时钟偏差），仅裁剪后扣除，不假设子调用必被父包含。不能把各self_time相加解释为trace关键路径或集群CPU时间。

这是离线因果数据处理，不接入真实追踪平台，不做监控看板或工单客服。核心 `process` 仍不做跨服务时钟校准、links、多父或采样补全；跨时钟区间校准只由独立的 `plan_trace_replay` API 提供。原有输入错误拒绝、不输出部分图，以及 IO 严格重复键检查、只创建结果不覆盖的契约不变。

## 跨时钟证据回放规划

`plan_trace_replay(data)` 接收恰好含 `reference`、`clocks`、`constraints`、`spans`、`segments`、`requests` 的对象，并返回 `trace-replay-plan-v1` 清单。所有校验先于规划；任一未知字段、类型/范围错误、重复身份、约束矛盾或相对 reference 无界都会抛 `ValueError`。函数不修改输入，也不读取 segment 指向的原始对象。

时钟偏移满足 `global = local + O_clock`，且 `O_reference = 0`。约束 `min <= O_right - O_left <= max` 用差分约束最短路求每个偏移相对 reference 的可达紧下界和紧上界；不能只取单个名义偏移。span 的可能匹配条件是完整可行偏移投影区间 `[min_offset,max_offset]` 与 `[from-end, to-start]` 有闭区间交集。matched spans 的递归父祖先构成 protected spans；不加入兄弟、后代或其他 trace。

每个请求按 clock 合并 protected spans 的本地闭区间。segment 同样是本地闭区间，可重叠、同边界或为零长度点。可用 segment 全集不能覆盖时返回 `status="incomplete"` 和规范合并后的 `missing`，不返回部分 segments；其他请求仍独立规划。可覆盖时，segment 集合依次最小化 bytes 总和、segment 数量、按 id 排序后的列表字典序。返回的 `matched_spans`、`protected_spans`、`segments` 均为排序后的 id 列表。

所有十进制值使用 `Fraction` 精确运算并规范化输出：零为 `0`，无正号和无意义前导/末尾零。结果顶层恰为 `schema`、`clocks`、`requests`；clock 项恰为 `clock`、`min_offset`、`max_offset`，ready 请求含 `segments`、`total_bytes` 而无 `missing`，incomplete 请求相反。
