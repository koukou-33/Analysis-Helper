# 可复现随机测试数据

生成器只读取示例的表头，保留全部 4186 列及重复列的位置，不复制原始数据记录。未确认语义的字段（包括波束域）保留为空，避免随机值被误解成物理模型。

## 数据规则

- 默认种子 20261022；每个文件 50 个块，每块 4 条记录，默认两个文件，共 400 条记录。
- 正常增益：第 0 流随机取 14–20 dB，其后每流递减 1–3 dB，保证每条完整正常记录满足第 0 流 > 第 1 流 > 第 2 流 > 第 3 流。
- 增益范围和间隔只是测试分布，不是生产异常门限。用户提供的递减规律用于正常样本生成，不据此自动确认软件故障。
- RSRP 在 -115 至 -85 dBm、NI 在 -120 至 -95 dBm 内随机取值。含两个小区、四个合成 CRNTI、两个资源；时间从 2026-10-21 23:58:00 起每条增加 3 秒，覆盖午夜。
- normal：全部为正常完整块。mixed：按块插入缺流、增益反转、可疑 RSRP、非法/非有限增益、尾部 CRNTI 缺失、全空 CRNTI、标量缺测。各场景含随机数值，位置固定便于回归。
- synthetic_manifest.json 保存种子、表头哈希、生成器版本、场景及逐记录预期流控结果。该文件用于测试核对，不传入诊断引擎充当测量证据。
- 不覆盖已有输出目录；要生成另一个版本请指定新目录。CSV 和数据库存储在被 Git 忽略的 workspace_storage 下。

## 运行

从仓库根目录执行：

```powershell
python -m srs_helper.cli generate workspace_storage/synthetic_normal --profile normal
python -m srs_helper.cli generate workspace_storage/synthetic_mixed --profile mixed --seed 20261022
python -m srs_helper.cli load workspace_storage/synthetic_normal --contract config/sample_input_contract.json
python -m srs_helper.cli summary <加载输出的数据集目录> --cell 0 --resource 0 --start 2026-10-22T00:00:00 --end 2026-10-22T00:05:00
```

模板默认 docs/NR_SRS_temp.csv，也可用 --template 指定。--files 和 --blocks 控制规模。源码回归使用独立的小型合成表头，无需用户原始 CSV 即可运行。

summary 支持 --cell、--crnti、--ue-uid、--resource、--start、--end，时间范围左闭右开，采用与 CSV 一致的不带时区时间。查询绑定单数据集，值以参数传入，从流控有效明细重算；不平均预聚合均值，不重新划分观测会话。输出包含各流有效数、缺测/错误数、覆盖时间、流均值差，以及资源统计和当前范围 UE 记录数。空范围返回空结果。

目前为固定描述性查询 API，不接受自由 SQL。大规模结果分页、通用算子、执行时限和内存预算属于后续受控执行器开发，不视为已经完成。

## 本轮验证

18 项回归通过，包含独立 CSV 遍历计算与过滤结果对照、时间上界排除、单数据集身份校验、空范围、参数绑定、固定种子可复现和源记录不复制。

完整表头实跑结果：normal 400 条原始记录全部流控有效；mixed 400 条中 340 条有效，均与逐行预期一致。两套均产生 8 个 UE/资源统计组。小区 0、资源 0、2026-10-22 00:00:00 至 00:05:00 范围分别选中 26 / 22 条记录。

本地生成文件：workspace_storage/synthetic_v1/{normal,mixed}；统计与过滤统计写入同级 *_summary.json / *_scoped_summary.json；run_results.json 记录对应数据集路径。模拟数据仅用于开发回归，不代替真实数值校准或现场诊断验收。
