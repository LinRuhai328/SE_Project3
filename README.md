# 多人共享记账系统 —— 软件实现

实验三的代码部分。按实验二的类图实现：三类角色、六种记录状态、按月结算与审计留痕。
语言为 Python 3.11，存储用标准库自带的 SQLite，不依赖第三方库（测试用 pytest）。

## 目录

```
code/
├── ledger/
│   ├── models.py         实体与枚举，含状态转换表
│   ├── store.py          SQLite 表结构与仓储
│   ├── permissions.py    角色到动作的权限矩阵
│   ├── records.py        状态机、审核、驳回、撤回、作废、更正
│   ├── stats.py          多条件查询与统计汇总
│   ├── settlement.py     预算、结算、重开与快照
│   ├── ledger_service.py 账本、成员、邀请与角色
│   ├── audit.py          审计日志与站内通知
│   └── cli.py            命令行入口
├── tests/                pytest 用例
├── demo.py               端到端演示脚本
└── 运行结果.txt          demo.py 的实际输出
```

## 运行

```bash
# 建库并注册用户
python -m ledger.cli --db ledger.db user add --email lrh@smail.nju.edu.cn --name 李睿豪

# 建账本
python -m ledger.cli --db ledger.db ledger create --owner U-xxx --name 家庭账本 --type shared

# 记一笔、提交、审核
python -m ledger.cli --db ledger.db record add --ledger LG-xxx --user U-yyy \
    --direction expense --amount 40.00 --date 2026-10-05 --category CAT-zzz
python -m ledger.cli --db ledger.db record submit --ledger LG-xxx --user U-yyy \
    --record R-xxx --operation-id OP-0001
python -m ledger.cli --db ledger.db record approve --ledger LG-xxx --user U-zzz --record R-xxx

# 统计与结算
python -m ledger.cli --db ledger.db query summary --ledger LG-xxx --period 2026-10
python -m ledger.cli --db ledger.db settle run --ledger LG-xxx --user U-zzz \
    --period 2026-10 --today 2026-11-04
```

也可以直接跑演示脚本，十步走完主流程并把结果打到标准输出：

```bash
cd code && python demo.py
```

## 测试

```bash
cd code && python -m pytest tests -q
```

44 个用例，覆盖状态迁移、禁止自审、幂等重试、旧页面冲突、金额与日期边界、
结算阻断与锁月、重开后版本保留、跨月更正、统计口径与审计可见范围。

## 规模

| 部分 | 行数 |
|---|---|
| 领域代码（`ledger/*.py`） | 约 2550 |
| 测试（`tests/*.py`） | 约 380 |
| 演示脚本 | 约 120 |

行数按 `wc -l` 口径统计，不含空行以外的生成代码；本项目没有使用任何 GUI 代码生成工具。

## 与设计的对应

| 实验二的图 | 代码位置 |
|---|---|
| 类图（用户/账本/记录/结算/审计四个包） | `models.py` + `store.py` |
| 用例图（三类角色边界） | `permissions.py` |
| 活动图（六状态与三个判断点） | `records.py` |
| 时序图（提交—审核链路） | `records.py` 的 `submit` / `approve` |
