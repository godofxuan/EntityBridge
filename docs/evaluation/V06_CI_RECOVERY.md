# 首次公开 CI 的崩溃测试启动问题

2026-09-15，首次公开源码提交 `3c8b3b43e855600a2aac772a792121763e54ae5f` 的 [Actions](https://github.com/godofxuan/EntityBridge/actions/runs/34941688693) 在 Windows、Ubuntu 与 PostgreSQL 作业失败，optional-neural 通过。失败全部位于真实杀进程复核恢复检查：子进程在进入故障注入点前以 code 1 退出。因此不能将该次 CI 写成跨平台通过。

在没有安装 EntityBridge 的新虚拟环境中，仅提供第三方依赖路径，复现两个 SQLite 失败；直接运行检查脚本得到 `ModuleNotFoundError: No module named 'entitybridge'`。pytest 配置的 `pythonpath=src` 只修改父解释器的模块路径，不能传给新 Python 子进程。此前本地验证的 PYTHONPATH/可编辑安装掩盖了这个启动依赖。

修复位于 `scripts/check_review_recovery.py`：从父进程实际加载的 schema 模块定位包根目录，通过子进程 PYTHONPATH 明确传递，并设置 UTF-8。这样源码检查使用同一源码，安装包检查也继续使用父进程加载的同一安装包，不通过跳过崩溃检查来绕开失败。

同一干净环境中，SQLite/PostgreSQL × saved_intent/after_rename 的四个真实进程终止与恢复用例通过。它们检查单一事件、候选回执恢复、显式发布、历史查询与孤立文件不登记。匹配模型、实验阈值、数据和应用 wheel 均未改动；该修复不需要重新训练或重算效果。

最终远端状态以修复后的提交及对应 Actions 为准；不能借用上一版本的 CI 结果。本地复现、修复后 JUnit 与首轮失败日志保存在本地研究目录，原始 CI 失败记录不覆盖。
