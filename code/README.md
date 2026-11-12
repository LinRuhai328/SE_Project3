# 多人共享记账系统 — 实现

实验三的代码部分。按实验二的类图落地，Python 3.11 + SQLite，标准库实现，除测试外无第三方依赖。

## 运行

```bash
cd code
python demo.py          # 跑一遍完整流程，输出分节日志
python -m pytest -q     # 59 个单元测试
python benchmark.py     # 一万条记录下的查询与结算耗时
```

## 目录

```
code/
├── ledger/
│   ├── models.py          领域模型：用户、账本、成员、记录、结算、审计
│   ├── store.py           SQLite 建表、索引与仓储
│   ├── permissions.py     三个阶段 14 个动作的权限矩阵
│   ├── records.py         记录服务：新增、提交、审核、驳回、撤回、作废、更正
│   ├── ledger_service.py  账本服务：建账本、邀请、角色、退出、类别、预算
│   ├── settlement.py      结算与重开账期
│   ├── stats.py           分类与成员统计
│   ├── audit.py           审计日志
│   └── cli.py             命令行入口
├── tests/                 59 个用例，覆盖状态机、权限、幂等、结算锁
├── demo.py                完整流程演示
└── benchmark.py           性能基准
```

## 与实验二设计的对应

| 实验二的图 | 代码里的落点 |
|---|---|
| 类图 | `models.py` 的十三个 dataclass |
| 状态图 | `models.RecordState` 与 `ALLOWED_TRANSITIONS` |
| 组件图 | `cli → records/ledger_service/settlement → store` 四层 |
| 活动图的三个判断点 | 字段校验、`operation_id` 幂等、审核时的状态比对 |
| 时序图 | `records.submit` 与 `records.approve` 的调用顺序，审计先于通知 |

## 规模

核心代码 2,633 行，测试 736 行。不含任何界面生成工具产出的代码。
