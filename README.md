# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

## 研发组合管理

在协同能力之上新增研发组合登记册（`civicflow.portfolio.PortfolioBook`）与可恢复工作流（`civicflow.portfolio_jobs.PortfolioWorkflows`），覆盖科技项目结转审议场景：

- **明确关系**：计划 → 专题 → 项目 → 课题四级层级，承担单位、预算来源、里程碑、知识产权、技术合同均可回溯；每个单元保留上级链与批准链。
- **版本冻结**：指南按版本冻结，立项时记录适用版本；预算只能冻结成版本，再版自动将旧版置为 superseded，资金口径只统计现行版本。
- **不可改写资金分录**：拨款、配套、支出、退回、验收释放均为只追加分录（`pf_fund_entries`），按“课题+类型+凭证号”防重；拨款/配套必须落在冻结来源额度内，支出受可用预算约束，退回恢复额度。
- **里程碑预算**：未完成里程碑持续占用预算，状态可查（预算/预留/净支出/可用）。
- **调整受限**：延期、负责人变更、任务合并经批准链执行，且只影响未完成阶段；申请人及新任负责人不得审批自己的重大调整。
- **资产专享**：受限样品与大型设备按时间窗互斥占用，边界相接允许，释放后可再约。
- **设施防重复计费**：共享平台每次使用独立登记，同一计费参考号拒绝重复登记，一条使用记录最多生成一笔支出分录。
- **资料授权**：合作单位仅能查看获授权且在有效期内的资料字段。
- **成果转化**：技术合同保存国家任务/省专项/企业成果引用与课题分成（合计 100%）；知识产权/合同的各项目贡献比例只允许冻结保存一次。
- **验收去重**：重复验收回执不会再次释放资金，差异进入复核队列（`pf_conflicts`），业务事务回滚但复核记录独立留存。
- **中断接续**：预算结转按请求键（`carried_from` 部分唯一索引）幂等，验收处理按回执幂等；任务队列会回收租约过期的 running 任务，崩溃后由新进程接续原任务。
- **合同下钻**：`drill_down(contract_id)` 从合同金额展开到各课题的每笔资金、每次设施使用、里程碑、责任人与完整上级链。

### 演示

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-pf.sqlite3 portfolio-demo
```

## 目录

- `src/civicflow/`：领域服务、SQLite 持久化、权限和命令行入口。
- `tests/`：核心流程、边界条件和异常路径测试。
- `examples/`：本地演示输入。

## 配置

通过 `CIVICFLOW_DB` 指定 SQLite 文件路径；不设置时命令行使用当前目录下的 `civicflow.sqlite3`。所有时间使用带时区的 ISO 8601 字符串。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译或构建

```bash
PYTHONPATH=src python3 -m compileall -q src
```

## 使用

初始化数据库并运行离线演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 demo
```

查看当前案件：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-cases
```
