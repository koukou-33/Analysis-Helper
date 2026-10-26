---
feature_id: SPF
revision: 1
title: SPF 预期生效与实际生效分析
flag_field: codeUsrSpfFlag
enabled_value: 1
disabled_value: 0
conditions:
- condition_id: c1
  role: NECESSARY
  expression:
    op: eq
    field: srsSpfSwitch
    value: 1
  source:
    document_id: features/SPF特性相关分析指南.md
    revision: '1'
    section: 预期生效与实际生效
    excerpt: 预期生效需要以下条件同时成立：srsSpfSwitch=1、isAiranActInd=1、preSinr<=spfPreSinrThd、codeUsrSpfSw=1、preSinrUeCurr<=spfPreSinrThd。
- condition_id: c2
  role: NECESSARY
  expression:
    op: eq
    field: isAiranActInd
    value: 1
  source:
    document_id: features/SPF特性相关分析指南.md
    revision: '1'
    section: 预期生效与实际生效
    excerpt: 预期生效需要以下条件同时成立：srsSpfSwitch=1、isAiranActInd=1、preSinr<=spfPreSinrThd、codeUsrSpfSw=1、preSinrUeCurr<=spfPreSinrThd。
- condition_id: c3
  role: NECESSARY
  expression:
    op: le
    field: preSinr
    value_field: spfPreSinrThd
  source:
    document_id: features/SPF特性相关分析指南.md
    revision: '1'
    section: 预期生效与实际生效
    excerpt: 预期生效需要以下条件同时成立：srsSpfSwitch=1、isAiranActInd=1、preSinr<=spfPreSinrThd、codeUsrSpfSw=1、preSinrUeCurr<=spfPreSinrThd。
- condition_id: c4
  role: NECESSARY
  expression:
    op: eq
    field: codeUsrSpfSw
    value: 1
  source:
    document_id: features/SPF特性相关分析指南.md
    revision: '1'
    section: 预期生效与实际生效
    excerpt: 预期生效需要以下条件同时成立：srsSpfSwitch=1、isAiranActInd=1、preSinr<=spfPreSinrThd、codeUsrSpfSw=1、preSinrUeCurr<=spfPreSinrThd。
- condition_id: c5
  role: NECESSARY
  expression:
    op: le
    field: preSinrUeCurr
    value_field: spfPreSinrThd
  source:
    document_id: features/SPF特性相关分析指南.md
    revision: '1'
    section: 预期生效与实际生效
    excerpt: 预期生效需要以下条件同时成立：srsSpfSwitch=1、isAiranActInd=1、preSinr<=spfPreSinrThd、codeUsrSpfSw=1、preSinrUeCurr<=spfPreSinrThd。
activation_logic:
  op: and
  args:
  - op: eq
    field: c1
    value: true
  - op: eq
    field: c2
    value: true
  - op: eq
    field: c3
    value: true
  - op: eq
    field: c4
    value: true
  - op: eq
    field: c5
    value: true
activation_source:
  document_id: features/SPF特性相关分析指南.md
  revision: '1'
  section: 预期生效与实际生效
  excerpt: 预期生效需要以下条件同时成立：srsSpfSwitch=1、isAiranActInd=1、preSinr<=spfPreSinrThd、codeUsrSpfSw=1、preSinrUeCurr<=spfPreSinrThd。
---

SPF是一个基于模型的降噪降干扰特性，在高干扰的场景下能够拿到较为明显的增益，但是在弱覆盖的场景下增益较小。

# 关键维测信息

| 字段名称              | 字段含义            | 设计预期                                                                                                                                                                           |
| ----------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| srsSpfSwitch      | SPF小区级开关        | 由L3指示L1，MML对应开关是XXX，预期来说除了专门说明ABAB测试的场景下，一份数据内每个小区的开关状态保持一致。1表示开关打开，0表示开关关闭                                                                                                           |
| isAiranActInd | SPF模型是否激活 | 1 表示模型已激活，是预期生效的必要条件；0 表示未激活 |
| spfPreSinrThd     | SPF特性生效门限       | 历史频分preSinr小于等于特性生效门限(preSinr <= spfPreSinrThd)，频分上所有用户能够进入瞬时preSinr生效判决。                                                                                                      |
| spfPreSinr3D4DThd | SPF模型切换门限       | 在下文完整生效条件满足的前提下，瞬时preSinr决定模型类型：<br>1. preSinrUeCurr <=spfPreSinrThd && preSinrUeCurr > spfPreSinr3D4DThd -> 生效3D<br>2. preSinrUeCurr <= spfPreSinr3D4DThd -> 生效4D<br>3. else: 不生效SPF |
| spfFreqEnFlag     | SPF频分用户筛选阶段标志     | 0： spf频分未生效<br>1： spf频分生效                                                                                                                                                      |
| codeUsrSpfSw      | SPF码分用户形态是否满足要求 | 0：不满足要求，可能因为不是1T4R/2T4R(selLayerNum=1&ueLayerNum=4 or selLayerNum = 2 & ueLayerNum=4)终端，或者不是服务用户（compFlag!=0)或者特性互斥；<br>1：满足要求                                                 |
| codeUsrSpfFlag    | SPF码分用户生效标志     | 0：码分用户未生效SPF<br>1：码分用户生效SPF                                                                                                                                                    |
| spfFreqCdtEnFlag  | SPF频分用户CDT最终记录标志  | 0： spf频分cdt未生效<br>1： spf频分cdt生效                                                                                                                                                |
| spfModelType      | 生效模型标志          | 仅 codeUsrSpfFlag=1 时有效：0 为3D模型，1 为4D模型；未生效时的0不代表3D生效                                                                                                                                                               |
| userLevelRsrp     | 推理后信号的rsrp      | 推理后 RSRP 预期比 usrRsrp 低；预期范围仅作排查参考，当前先展示数值，不据此自动排除记录                                                                                                                                             |
| userLevelNi       | 推理后信号的ni        | 推理后的ni，一般预期会比srsUeNiDb低                                                                                                                                                        |
# 一般定位思路

1. 确认数据中是否存在流控的影响，如果存在流控的影响，先剔除流控影响在继续后续分析
2. 使用codeUsrSpfFlag和spfFreqEnFlag分析特性生效比例，根据配置的门限情况得到未生效特性的原因和占比；
3. 如果是特性开关对比测试的情况（A为特性关闭数据，B为特性打开数据），可以根据B打开特性是的条件，筛选A中按预期能够生效的用户的数据，对比4流的bfgain情况。预期在有干扰的情况下，打开特性后的第一流bfgain会有抬升。

# 已确认的执行口径（2026-10-27）

## 预期生效与实际生效

预期生效需要以下条件同时成立：srsSpfSwitch=1、isAiranActInd=1、preSinr<=spfPreSinrThd、codeUsrSpfSw=1、preSinrUeCurr<=spfPreSinrThd。实际码分用户生效由 codeUsrSpfFlag=1 判断；必要输入缺失或冲突时对应条件为未知，不能当作条件不满足或特性关闭；完整规则按三值逻辑合成：有明确不满足条件则为假，全部满足才为真，其余为未知。

在上述前提满足时，preSinrUeCurr>spfPreSinr3D4DThd 对应3D；preSinrUeCurr<=spfPreSinr3D4DThd 对应4D。spfModelType 只在 codeUsrSpfFlag=1 时解释。模型切换门限缺失时，不能确认预期模型类型。

spfFreqEnFlag 与 spfFreqCdtEnFlag 属于筛选阶段，codeUsrSpfFlag 属于生效阶段。CDT 标志是最终记录标志，不单凭 CDT 标志断言算法实际生效；分别展示筛选标志和实际生效标志及其不一致情况。

## 数据与统计

- CSV 同名重复列逐条记录合并，采用有值的列，不固定选第一列。多个相同非空值合并为一个；不同非空值记录冲突，保留原始列位置和值，不静默选取。
- 所有 SINR、门限及推理后 RSRP/NI 都无需缩放。compFlag 为实际列名。
- 沿用统一流控及业务准入规则；推理后 RSRP/NI 的预期范围只是排查参考，暂不新增整行排除门限。
- 按条目及用户统计，不按频分任务去重。分别统计两个筛选标志及实际生效标志的1/0/未知数量；有效标志比例以0/1条目为分母，另列全部条目数及未知数，避免将未知当作0。
- 未生效原因可并存，分别展示各条件不满足数量和重叠情况，不推断各原因互斥或因果责任。无法从 codeUsrSpfSw=0 单独拆分终端形态、服务用户、特性互斥三种具体原因。
- 流增益继续按 ueLayerNum 筛选，selLayerNum 仅用于这里的终端形态条件，不替代流数口径。

## 对比范围

A 为特性关闭数据，B 为特性打开数据。本期分别分析两个数据集，不执行自动跨数据集 A/B 比较；后续再支持显式配置两个数据集及匹配条件。高干扰时第一流增益抬升、弱覆盖增益较小为定性预期，不作为自动故障门限。

## 当前程序支持边界

本文包含可直接执行的 FeatureSpec，无需模型先提取规则。value_field 表示同一条记录上的右侧字段；每条记录使用自己的门限，不将字段名作为常量，也不取单条门限替代整个数据集。当前结构化规则判断预期生效与实际 codeUsrSpfFlag 的一致性；3D/4D 解释仍须满足正文的生效前提。MML 开关名称 XXX 仍是待补充项，不用于自动推理。
