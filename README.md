# data-qd-208 · 分布式追踪因果链

校验同一trace的父子span、检测环路，输出根、服务集合、直接子节点、扣除子调用重叠后的自身时间及墙钟跨度。跨时钟区间校准下的最小证据回放清单由 `plan_trace_replay(data)` 提供。基于 trace 快照的分析凭证一致性核对与证据包信任状态传播由 `audit_trace_attestations(data)` 提供。累计计量器增量的守恒 span 归因、账户汇总与独立配额准入由 `settle_trace_metering(data)` 提供。

本仓主要交付是Python业务API和可供下游使用的结构化数据，不是通用CLI、报表或CRUD模板。字段、身份、单位、上限与失败边界见 [数据契约](docs/contract.md)。

## 安装与验证

Python >=3.11，实测CPython3.12.14、uv0.12.19。运行依赖仅标准库，构建固定setuptools80.9.0、wheel0.45.1；每仓独立 `.venv`。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/python -I examples/run_sample.py
```

上述命令已在本地缓存和独立环境真实执行；样例输出写入 `run/sample.json`，已有目标拒绝覆盖。API可这样调用：

```python
from trace_causality import process
from trace_causality.io import load, publish
publish("run/another.json", process(load("fixtures/input.json")))
```

样例摘要：

```json
{"roots": 1, "spans": 3, "traces": 1}
```

已通过 7 项业务测试，并核对非法输入拒绝；样例数据全部原创合成，见 [来源](fixtures/SOURCES.md)。

## 边界

业务结果仅覆盖契约中定义的有限模型。后续需求可沿已有身份和证据字段扩展，不把当前样例当成行业标准或生产认证。
