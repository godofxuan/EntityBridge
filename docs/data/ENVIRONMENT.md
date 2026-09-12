# 实测环境与故障记录

2026-09-12，在 Windows 11 的本地工作区完成验证。Python 3.12.13、12 个逻辑 CPU、33,947,549,696 字节 RAM；项目开始时 D 盘约 93.8 GB 可用。具体依赖以根目录 `requirements.lock` 为准，来自实际安装后的 pip freeze；pip check 通过。

系统默认 Python 3.14 不在项目支持范围，注册的 3.13 启动失败。使用机器上已有的 Python 3.12 创建项目内 `.venv`，没有更改全局 Python。随后重新创建 `.venv-repro`，从锁文件安装并安装本项目，执行最小演示和测试。此验证是同一机器上的干净虚拟环境，不宣称在第二台机器或 Linux 上实测。

Splink 4.0.17、DuckDB 1.5.5、pandas 3.0.5、PyArrow 25.0.1、SQLAlchemy 2.0.52、psycopg 3.3.5 均实际运行。固定模型与训练 TF 的保存、加载、额外输入不改变旧分数由 `scripts/check_compatibility.py` 和 `tests/test_matching.py` 检验。未知名称/未观察比较层可能仍使用 Splink 默认参数，模型分数未经校准；训练 manifest 明列缺失层。

PostgreSQL 使用 [官方 Windows 下载页](https://www.postgresql.org/download/windows/) 指向的 [EDB 二进制](https://www.enterprisedb.com/download-postgresql-binaries)，版本 17.11。下载文件 `postgresql-17.11-1-windows-x64-binaries.zip`，341,325,378 字节，SHA-256 `4b8db0930c38f6ef845db919551dedda3b6b845aeb0927b3d79a6e8e9e4537cf`。仅将 bin/lib/share 提取到项目 `.tools`；随机 SCRAM 密码只存于忽略的 `.tools/database.json`，监听 127.0.0.1:55432，没有安装系统服务。

两个真实阻塞及解决依据：

1. 中文项目路径导致 initdb 的模板 SQL 含本地编码路径，UTF-8 初始化失败。使用临时 `subst Z:` 指向本项目，脚本通过 samefile 校验不会错误指向其他目录，再以 ASCII 路径初始化。不是关闭编码校验。停止服务后可按 README 解除映射。
2. Windows 后台 PostgreSQL 继承 PIPE，使父进程 communicate 等不到 EOF。改为临时文件收集 pg_ctl 输出并设置 60 秒超时；真实启动、停止、重启与隔离 schema 测试通过。

PyPI 首次下载因沙箱网络限制失败，获工具执行授权后成功。另有一次空响应哈希错误，通过正常重新下载解决，没有关闭 TLS 或哈希校验。Docker 未安装，compose 配置仅提供可选路径，不标记为已验证。

页面录制的 CDP screencast 调用曾异常耗时。交付视频采用实际浏览器截图配合明确标注的五分钟字幕导览；它不是一镜到底的实时操作录屏。
