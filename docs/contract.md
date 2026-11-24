# 追踪因果与占用时间

spans为1..2000项全局唯一id、trace、parent(null或span id)、service、start/end。标识ASCII；时间为相同单位的精确十进制字符串，end>=start。所有父引用必须存在且同trace，整个父图无环；先验证所有引用再遍历，任意输入顺序下坏引用均为ValueError。

每trace输出排序的roots、服务集合、直接children，以及wall_time=max(end)-min(start)。wall_time是整体墙钟跨度，绝不是因果关键路径。self_time按每span区间减去直接子span与它相交区间的并集，重叠子调用只扣一次；零时长合法，子span越过父边界允许（异步/时钟偏差），仅裁剪后扣除，不假设子调用必被父包含。不能把各self_time相加解释为trace关键路径或集群CPU时间。

这是离线因果数据处理，不接入真实追踪平台，不做监控看板或工单客服。无跨服务时钟校准、links、多父或采样补全；未来时钟域与异步连接可接入span/trace及父引用。输入错误拒绝，不输出部分图。IO严格重复键检查、只创建结果不覆盖。
