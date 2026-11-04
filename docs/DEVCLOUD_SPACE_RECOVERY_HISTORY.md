# 历次大幅回收与2026-11-03全天对账

文档中的方法确实产生过大幅回收。10月27日的大项是重复Git对象、失效临时pack和工具/构建缓存；10月29日较大的df下降还包含其他会话并行清理。11月3日上一条回复只汇总后续worktree批次，没有把当天较早的生成物/镜像批次一起说明。

## 上次主要清了什么

| 日期与对象 | 有直接回执支持的规模 | 处理与边界 |
| --- | ---: | --- |
| 10月27日，11份重放Git库的重复pack | 约34.6 GiB | Git自身保留对象的压缩，refs不变、连通性检查通过 |
| 10月27日，13个失效tmp_pack | 约14.82 GiB | Git确认为garbage、满3天、无引用，经Mole删除 |
| 10月27日，两份Cargo专属target | 工具自报5.4+4.9 GiB | Cargo owner命令；不含源码 |
| 10月27日，8份依赖/构建目录 | 约6.54 GiB | 满3天、完整年龄/引用/保护检查通过 |
| 10月27日，Go/npm/pnpm缓存 | npm verify自报7.94 GiB，另有cache clean及Go cache清理；pnpm自报1.33GB | 不将工具不同口径与df净量混加；Go模块缓存不计完整成功 |
| 10月29日，依赖/缓存、镜像、worktree | 14处生成物约6.90 GiB、11镜像，追加4个worktree | 镜像共享层不相加估回收；通用tmp仅约108MiB |

10月27日收尾df约663G已用；10月29日第一份回执为897G→818G，追加调查期间继续变到约737G。它们是不同测量区间，期间有其他管家清理/tmp与开发任务写入，不能把全部df下降归因一个脚本或本会话。

历史依据：[10月27日清理与修复回执](/root/.bb/thread-storage/cleanup-20261027/清理与修复最终回执.md)、[10月29日清理回执](/root/.bb/thread-storage/cleanup-20261029/清理回执.md)、[根因与追加清理](/root/.bb/thread-storage/cleanup-20261029/root-cause/根因与追加清理.md)。

## 今天实际完成了多少

对账时间：2026-11-03T11:27:16.129827+08:00。

| 批次 | 对象 | 大小口径 |
| --- | --- | --- |
| 较早的应急清理 | 50个生成目录 | 删除前实占28.49 GiB |
| 后续人工与10:15监督 | 27个worktree | 删除前实占15.77 GiB |
| 同日镜像清理 | 8个镜像 | 显示大小3.98 GiB，存在共享层，单列 |
| 目录合计 | 50个生成目录+27个worktree | 删除前实占44.25 GiB，不能直接当作df回收 |
| 数据盘全天净变化 | 已用959.84→927.03 GiB | 净下降32.81 GiB，含并发增删 |

该时点普通可用16.43 GiB。目标仍未达；77个目录的测量合计、8个镜像的共享层显示大小、df净变化各有口径，分别报告。

## 今天已清理目录的来源项目

| Git项目/来源 | 删除前实占GiB |
| --- | ---: |
| git.woa.com/wisp/mono2 | 25.828 |
| github.com/opencraft-app/opencraft | 16.607 |
| github.com/hxy91819/bb-plugins | 0.679 |
| github.com/raphaelluethy/cursor-acp | 0.496 |
| github.com/NousResearch/hermes-agent | 0.196 |
| github.com/hxy91819/bb-plugin-account-limits | 0.155 |
| git.woa.com/lightcloud-ai-infra/external-slides | 0.111 |
| github.com/hxy91819/bb-plugin-upgrade-resume | 0.107 |
| Lightspace（Git主checkout /data/code/lightspace） | 0.074 |

8个镜像能确认上游repository，但本地使用项目未定位，继续单列；不凭镜像名推断项目。

依据：[较早批次](/root/.local/state/space-recovery-20261103/report.json)、[较早批次逐项项目归属](/root/.local/state/worktree-batch-20261103/earlier-round-project-attribution.json)、[27个worktree逐项清单](/root/.local/state/worktree-cleanup-review/runs/20261103T101500/清理明细.csv)、[本次对账快照](/root/.local/state/worktree-cleanup-review/comparisons/20261103/summary.json)。

## 为什么上次的方法不能原样保证同样收益

- 重复pack属于特定历史积压；10月29日未再发现超过64MiB的同类重放pack堆积。后续需要重新测量，不能把34.6GiB当作每次固定收益。
- 共享缓存明显回长：10月29日Go缓存约36.6GiB，11月3日较早实测105.74GiB；pnpm全局store从10月27日之后也明显增长。当前池混有近期活动，整库重置未满足现有条件；按owner语义处理旧对象及生产者预算尚需补齐。
- 后续这些worktree很多只含约0.7GiB源码与小缓存，数量和旧批次的空间不能直接类比。缓存生成、完整checkout、制品复制需要与worktree本体分别统计。
- 文档早已记录ignored小缓存阻塞、维护刷新活跃时间、扫描超时和共享父目录漏扫。它们没有完整落实到执行器，是自动化尚未闭环的执行问题。已修reflog事件时间、正常锁竞争误诊断及完整引用检查；生成物分类/删除入口、吞吐、增长归属和生产者预算仍须继续推进。

后续监督应同时核验手册里的Docker、工具缓存、历史测试产物和Git对象维护分支，按当前收益和证据安排；不把worktree数量当作全盘清理效果。既有3天及保留规则保持。
