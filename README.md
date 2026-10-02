# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

## 研发组合管理

`civicflow.portfolio` 与 `civicflow.portfolio_funds` 在通用能力之上提供研发组合管理：

- **结构关系**：计划 → 专题 → 项目 → 课题 → 里程碑逐级引用，预算来源、设备资源、知识产权、技术合同挂在对应节点，创建时校验上级存在；合同收入按课题归集，累计归集不得超过合同金额。
- **版本冻结**：立项或重大调整（预算、指南）时冻结适用指南与预算版本，历史版本保留在实体版本表中。
- **资金分录**：拨款、配套资金、支出、支出退回进入不可改写分录，参考号唯一防重；支出不得超过当年可用资金，退回不得超过累计支出。
- **重大调整**：延期、负责人变更、任务合并经提交、审批、执行三步；项目负责人不得审批自己项目的调整，审批人与提交人不得相同；执行只影响未完成阶段（待办里程碑），每步幂等，中断后重入可继续。
- **资源占用**：受限样品与大型设备容量为一，重叠占用会被拒绝；每次占用记录责任人。
- **资料授权**：合作单位只能接触 `allowed_orgs` 覆盖的资料，审核角色可跨单位查看。
- **成果转化**：记录各项目贡献比例，比例之和必须等于一；转化收入按比例记入各项目账簿。
- **验收与结转**：验收回执经收件箱接入，重复回执不重复释放资金，内容冲突进入复核队列；验收结算与预算结转是可恢复任务，处理中断后凭分录参考号接续。
- **审核下钻**：`contract_drilldown` 从合同金额下钻到每个课题的归集额、每笔资金分录、每次设施使用和相应责任人；`project_budget_view` 给出分年度余额与未完成里程碑占用预算。

运行端到端演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-portfolio.sqlite3 portfolio-demo
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
