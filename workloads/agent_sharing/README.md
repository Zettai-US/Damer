# arVM 共享内存数据流 motivation 实验

当前实现把真实 OpenCode 的共享对象工具调用接到 arVM/DSec：
先从可信 harness 的任务读写声明生成共享、发布依赖和保留约束，再在执行时检查
grant、CXL 对象版本与 pin；执行后重建对象版本数据流。重点是共享状态及其跨阶段
依赖，锁竞争作为附属观测。

只使用 [mutex_benchmark/fast_exp](https://github.com/EIRNf/mutex_benchmark/tree/fast_exp)
的 region-backed `SpinLock`，固定提交
`55b707259c65d79b5f12a2f216b773c2a2a84124`。没有文件锁 fallback。
上游 `SpinLock` 对读写都互斥；不是读写锁，也不具备 owner-death 自动恢复。

## 多轮协作与回收消融（2026-10-11）

已在真实 CXL node 1 上完成 **12 个 episode、84 个 OpenCode 任务会话、484 次对象操作**。
2/4 个 reviewer 各运行 draft 和 peer-review 两轮，再由独立 synthesis 会话汇总；
每个并发度分别运行 scope-only/dataflow 两种保留策略，各重复 3 次，按固定种子分块随机排序。
全部发布最终结果，11/12 完整遵循工具步骤；`w4-scope-r1` 的 synthesis 漏读写入前的
`answer`，但 CAS 写入成功。所有尝试都进入统计，没有删除这个样本。

输入是四个真实原型模块的相同 43.6 KiB 快照。任务、角色、effects 由 harness 指定，
真实模型负责执行工具及生成审查文本。这不是自然生成任务图的比例调查或答案质量评测。
模型仍为本地 Qwen3.5-35B-A3B-UD-Q4_K_XL，服务只有 2 个推理 slot；4 个 reviewer
不代表 4 路并行推理。每轮使用新的 task session，逻辑 reviewer 身份跨轮保持。

| 指标 | 2 个 reviewer | 4 个 reviewer |
|---|---:|---:|
| Scope-only 平均 live payload，中位数 | 575.7 KiB | 959.6 KiB |
| Dataflow 平均 live payload，中位数 | 446.5 KiB | 736.4 KiB |
| 两策略占用中位数之比的降幅 | 22.4% | 23.3% |
| 同一 dataflow 时间线重放 scope 的 byte-time 降幅中位数 | 22.2% | 23.3% |
| Dataflow 非空读取中同版本被多个逻辑 agent 读取 | 28/37，75.7% | 51/66，77.3% |
| Dataflow corpus 首次至末次读取跨度中位数 | 19.7 s | 37.4 s |
| Dataflow corpus 读取临界区并集中位数 | 24.2 ms | 43.4 ms |

“平均 live payload”是实际分配页数乘存活时间的积分除以观测时间，再对三个 trial 取中位数。
每个对象为 64 KiB 固定槽，包括尚未写满的输出预留空间；控制 registry、版本记录及锁字
另行计量，不计入此表。它不是全进程 RSS、CXL 总线流量或模型内存，也不据此宣称推理加速。
同时间线重放属于容量计算，不能充当另一次实际延迟测量。

两种策略使用相同权限、accepted effects、槽大小与 CXL 锁。Scope-only 按未结束任务的
宽读权限保留数据；dataflow 按实际声明的未来用途、pin 和 session root 保留。
新实现会在任务 namespace 退出后真正关闭、unlink 已死 payload：私有 scratch 在 draft
结束后回收，draft 等待 peer 消费完，review 等待 synthesis，answer 等待根释放。
超出已接受 effects 的访问必须重新规划；未知后续会话需要显式 root 保留。

单独的并发 PSS probe 同时保持所有映射，核对 device/inode 和真实 NUMA 驻留页。
2/4 个进程共享 CXL corpus 均合计 **64 KiB**；保留同一 CXL 源并给每个进程各建 DRAM
副本则为 **192/320 KiB**（分别包含 128/256 KiB 私有 DRAM）。每种情况实测三次。

对全部六个 dataflow episode 做 **离线 idle-only 回收重放**：1 秒阈值下，62/103
次非空读取（60.2%）发生在超时的访问间隙之后。这表示仍需保留或可恢复的 backing，
没有实际执行不安全回收，也没有声称发生 use-after-free。论文因此论证的是：
**锁和访问活跃度无法回答“还有哪些未来消费者”；arVM 需要对象版本、别名与剩余用途信息。**
等价的 effect/reference 表也能表达这些信息，数据没有证明 DAG 是唯一实现。

- [论文 PDF](../../6ac4087cf755acfa13cf5a59/main.pdf) / [实测小节源码](../../6ac4087cf755acfa13cf5a59/profile_evaluation.tex)
- [完整汇总](results/arvm-profile-20261011/summary.json) / [验证](results/arvm-profile-20261011/validation.json) / [全部尝试](results/arvm-profile-20261011/episodes.jsonl)
- [实际对象版本图](results/arvm-profile-20261011/observed-dataflow.pdf) / [占用与 idle 重放图](results/arvm-profile-20261011/sharing-profile.pdf) / [存活时间线](results/arvm-profile-20261011/retention-timeline.pdf)
- [并发 PSS 原始记录](results/arvm-sharing-probe-20261011/runs.jsonl)

重建图表不需要模型或仍存活的 payload。完整重跑需要真实 CXL 主机和运行中的本地模型：

```bash
# 使用归档结果重建、验证并导出论文图表和数值宏
make -C 6ac4087cf755acfa13cf5a59 profile
make -C 6ac4087cf755acfa13cf5a59 check

# 新实验：先按下方“复现”构建 CXL 库并启动 serve_model.py
python3 workloads/agent_sharing/run_profile_suite.py \
  --output workloads/agent_sharing/results/my-profile --repeats 3
python3 workloads/agent_sharing/probe_sharing.py \
  --corpus workloads/agent_sharing/results/my-profile/w4-scope-r0/corpus.txt \
  --output workloads/agent_sharing/results/my-probe
python3 workloads/agent_sharing/report_profile.py \
  workloads/agent_sharing/results/my-profile \
  --probe workloads/agent_sharing/results/my-probe
```

## 第一组 live 结果（2026-10-10）

日期：2026-10-10 UTC。OpenCode v2.0.24；本地 RTX 5090 上运行
Qwen3.5-35B-A3B-UD-Q4_K_XL，2 个 16K context slot。
模型权重、服务二进制 SHA-256、启动参数和 GPU 信息保存在
[model-provenance.json](results/arvm-live-final-20261010/model-provenance.json)。

正式套件包含两种指定任务，各重复 3 次，每次 2 个并发分析 agent 加 1 个汇总 agent。
代码审查使用仓库中真实的 `experiments/runtime.py` Registry 源码；复现副本位于
`fixtures/arvm_registry.py`，原始路径及 SHA-256 保存在 `fixtures/provenance.json`。事件审查输入是
明确标为虚构的故障记录。任务图和角色是指定的，不能用这些比例估计自然 agent 工作负载的普遍性。

| 指标 | arVM 代码审查 | 事件审查 |
|---|---:|---:|
| 尝试 episode | 3 | 3 |
| 完成结果发布 | 3 | 3 |
| 完整遵循要求的工具步骤 | 2 | 3 |
| 实际共享对象操作 | 53 | 54 |
| 非空读取中，同一版本被多 agent 读取 | 14/23，60.9% | 17/24，70.8% |
| 直接读取其他 agent 发布版本 | 9 | 11 |
| 发生竞争的对象锁获取 | 2 | 2 |
| evidence 首次至末次访问跨度中位数 | 8.532 s | 7.687 s |
| evidence 访问临界区并集中位数 | 13.688 ms | 13.133 ms |
| episode 总耗时中位数 | 17.054 s | 16.398 s |

6 次尝试、18 个任务 agent 会话、107 次对象操作全部保留，工具操作无错误/版本冲突。
`runtime-review-r0` 的 synthesizer 未按提示重读原始 evidence，严格工具步骤检查未通过；
其两份 finding 和 synthesis 确实已发布。因此区分“完成发布”与“完整协议遵循”，
并未删除这个样本。另有 6 个模型 preflight 会话，不计为任务 agent。
当前未做独立的答案正确性评分。episode 时间包含 preflight、server 启动与清理。

这组数据支持的 motivation 是：同一对象版本在多个消费者间复用，且状态需要跨越
远长于实际访问临界区的间隙。仅看到某个 agent 暂时没有访问，不能证明其状态可回收。
访问跨度是事后观测，不是对未来读者的预测，也不是可免费提前隐藏的传输时间。

- [正式汇总及逐 episode 数据](results/arvm-live-final-20261010/summary.json)
- [原始 episode 清单](results/arvm-live-final-20261010/episodes.jsonl)
- [完整性与协议审计](results/arvm-live-final-20261010/validation.json)
- [PNG 图](results/arvm-live-final-20261010/arvm-motivation.png) / [PDF 图](results/arvm-live-final-20261010/arvm-motivation.pdf)
- 示例：[arVM 内存约束](results/arvm-live-final-20261010/runtime-review-r2/arvm-memory-contract.json)、
  [观测数据流](results/arvm-live-final-20261010/runtime-review-r2/arvm-dataflow.json)、
  [OpenCode 原始工具调用](results/arvm-live-final-20261010/runtime-review-r2/analyst-0/opencode.jsonl)

图左侧的点是各 episode 比例、横线是中位数；上表是合并分子/分母的比例，两者定义不同。
图中时间轴使用对数坐标，显示全部 6 个 episode。

## Grant 性能及 NVIDIA Sentry 对照状态

目前将 Grant 解释为 arVM 内存访问的授权/撤销机制：
`grant → check/pin → access → release → revoke`，不是另一个已识别的外部 Grant 项目。
性能微实验使用相同本机 CXL 控制状态和 native CXL 锁，分别测试 1、2、4 个并发 worker，
每组 5 次，随机顺序。共 3,500 次 check/pin/release 和 1,500 对 issuance/revoke；
每次 revoke 后还验证新的访问被拒绝。

| 并发 worker | check + pin/release p50 | p95 | grant issue p50 | revoke p50 | throughput |
|---|---:|---:|---:|---:|---:|
| 1 | 59.791 µs | 71.639 µs | 50.747 µs | 29.735 µs | 14,704 ops/s |
| 2 | 64.578 µs | 77.688 µs | 52.750 µs | 31.177 µs | 27,657 ops/s |
| 4 | 107.423 µs | 173.003 µs | 55.604 µs | 33.952 µs | 31,852 ops/s |

表内每项是 5 次 trial 对应统计量的中位数；不是置信区间。
issue/revoke 单独在并发阶段结束后由一个 controller 测量。
这些是预热后的 Python API 实测，包含 CXL 锁、JSON 控制状态处理及日志开销，
不含模型推理、payload 访问或网络 RPC。控制表的串行临界区限制了扩展性。
吞吐包含 dispatch barrier 和结果队列；单次操作延迟不包含这些同步。

[Grant 原始数据和验证](results/grant-final-20261010/validation.json)、
[汇总](results/grant-final-20261010/summary.json)、
[逐 trial 样本](results/grant-final-20261010/runs.jsonl)。

**NVIDIA Sentry 尚未实测，不能报告相对加速比。** 本机 PCI inventory 没有 BlueField，
也没有配置 Sentry endpoint/SDK。
[NVIDIA 官方架构](https://www.nvidia.com/en-us/solutions/ai/agent-safety/)
把 Sentry 的硬件隔离路径放在 BlueField DPU 上；OpenShell 是另一层软件运行时。
这里没有用 OpenShell、gVisor Sentry、sleep 或模拟延迟代替它。

已导出全部 107 次真实请求的
[比较输入](results/arvm-live-final-20261010/sentry-comparison/requests.jsonl) 和
[比较协议](results/arvm-live-final-20261010/sentry-comparison/protocol.json)。
该 JSONL 是实验输入格式，不是假定的 NVIDIA API。
接入真实 baseline 时必须记录 Sentry/DOCA/firmware、硬件拓扑、实际策略与身份映射、
请求传输及计时边界，保留失败请求，并分别报告冷启动、warm path 和完整任务耗时。
本地 MCP grant 不具有 DPU 的隔离边界；指针撤销、带外监控等不等价能力须单列。

## 实现与数据语义

1. `dataflow.compile_memory_contract` 对可信任务 effects 做 DAG 检查，识别同一 backing、
   共享读者、发布依赖、并发写者与待完成消费者；输出 DSec 接受的权限声明。
   它不从模型自然语言推导权限，也不分析任意程序指针。
2. `DSec` 是本仓库自行实现的原型。它管理 scoped grants、依赖 admission、in-flight pins、
   generation、撤销、retirement 和物理回收。accepted effects 与项目 grants 分开；
   未开始的合法消费者、active pins 和 session roots 都会阻止提前回收。
3. payload、版本 metadata、DSec registry 和锁字都在 `MAP_SHARED` 的 tmpfs backing 中。
   在 first touch 前 `mbind`，随后检查 `/proc/self/numa_maps` 的真实驻留页；CXL 身份
   再与 sysfs decoder 地址范围核对。测量主机上 node 1 为 CXL。磁盘 JSON 是审计快照。
4. 上游 C++ SpinLock 通过小型 C ABI bridge 使用调用者提供的真实 CXL 地址。
   `-Dcxl` 选择 region-backed 实现；旧 emucxl allocator 如果意外调用会 abort，没有模拟器路径。
   所有控制与数据临界区都使用 CXL 锁。CAS 写和原子 append 保持版本连续性。
5. 每个 OpenCode 角色运行于独立 bwrap workspace/server；只允许 `sharing_*` MCP 工具。
   共享映射属于可信 MCP 工具进程，OpenCode/LLM 收到的是复制出的有限文本，
   因而不能宣称 LLM prompt 零复制或透明拦截任意 shell/内存访问。
   此原型不是针对恶意 native 程序的完整 sandbox/网络策略产品。
6. 实际访问记录导出 `(object, version, producer, reader/writer, time, range, alias_set)`。
   `RAW`、`RMW`、`WAR`、`WAW` 与发布边来自实际版本；同一 agent 较早读取对较晚输出的
   `may_influence` 边只表示保守上下文依赖，不证明模型语义上用了该数据。

`ledger` 是多写者对象；每个 append 的 RMW 边连接旧版本与新版本，因此保留传递贡献。
表中 cross-agent reads 只数直接版本生产者与读者不同的显式 read，避免将 RMW 算作协作读取。
可变版本复用同一 backing；它们不是同时保留的 immutable snapshot。
第一组 live 对象固定容量 256 KiB，多轮 profile 使用 64 KiB；每次校验会扫描 padding。
`bytes_scanned` 不是 CXL 总线流量，`bytes_copied=0` 也不代表 JSON/RPC/model prompt 无复制。

锁不跨模型思考阶段持有。撤销阻止新的工具 grant；已有 pin 必须 drain 后才能结束任务或
回收。没有任意 CPU 指针的强制撤销。持锁进程死亡时 timeout 并 fail closed，
不会强制解锁潜在不一致对象。CXL 内存和当前对象协议不提供崩溃持久化。

## 复现

依赖 Linux、真实 CXL NUMA 节点、libnuma、g++、bwrap、OpenCode v2；
制图另需 matplotlib。源码、模型与库 SHA-256 均随结果保存。
`build/` 的 native 库和 OpenCode 二进制副本、`third_party/` checkout 不纳入源码版本控制。

```bash
python3 workloads/agent_sharing/build_cxl_lock.py
python3 -m unittest discover -s workloads/agent_sharing -p 'test_*.py' -v

# 本机模型路径与参数已在脚本内配置；在另一个终端启动服务。
python3 workloads/agent_sharing/serve_model.py

# 每个 output 使用新目录。
python3 workloads/agent_sharing/run_live_suite.py \
  --output workloads/agent_sharing/results/my-live --repeats 3
python3 workloads/agent_sharing/bench_grant.py \
  --output workloads/agent_sharing/results/my-grant --repeats 5 --steps 100
python3 workloads/agent_sharing/summarize_live.py \
  workloads/agent_sharing/results/my-live \
  --grant workloads/agent_sharing/results/my-grant
python3 workloads/agent_sharing/export_sentry_workload.py \
  workloads/agent_sharing/results/my-live

# 单 episode 的 arVM 数据流分析入口：
python3 tools/arvm_shared_state.py \
  workloads/agent_sharing/results/my-live/runtime-review-r0
```

12 项单元/多进程检查已通过，包括 80 次并发 append 无丢失、CAS 冲突、校验失败、
CXL owner-death fail closed、权限/依赖/存活约束、撤销后拒绝访问、版本数据流、DAG 拒绝环、
scope/effects 分离、未来消费者与 root 保留、实际 backing 回收及搜索的共享别名验证。
另外完成 26 组当前 CXL 后端的控制实验 smoke check，共 150 次操作；
该检查用于回归验证，不作为真实 agent motivation 或 Sentry 性能结论。

## 历史目录

`controlled-20261010`、早期 `opencode-live-*`、`mcp-*` 等目录是上一版文件锁控制或
配置排错记录；`cxl-live-20261010-r0`、`arvm-review-20261010-r0`、`grant-20261010`
是本机保留的探索运行。它们均未合入正式结果，也不纳入这次 Git 提交。
正式 agent 数据来自 `arvm-live-final-20261010` 和 `arvm-profile-20261011`，
并发 PSS probe 来自 `arvm-sharing-probe-20261011`，Grant 性能来自 `grant-final-20261010`。
新套件的预实验位于 `profile-development`，不进入上述统计。所有 `.dsec-state` 运行缓存均不提交。
