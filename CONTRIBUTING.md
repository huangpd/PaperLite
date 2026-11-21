# 参与开发

## 环境

```bash
git clone https://github.com/huangpd/PaperLite.git
cd PaperLite
uv sync
uv run paperlite --version
```

受限网络下用镜像：`uv sync --index-url https://pypi.tuna.tsinghua.edu.cn/simple`（uv 不读 `pip.conf`）。

## 测试

测试是 `unittest.TestCase` 子类，用 pytest 运行。依赖外部服务的用例由环境变量控制，没有设置时自动跳过。

```bash
uv run pytest                                                   # 跳过依赖 Redis / RabbitMQ 的用例
PAPERLITE_TEST_REDIS=redis://127.0.0.1:6379/0 uv run pytest     # 包含 Redis 用例
PAPERLITE_TEST_AMQP=amqp://guest:guest@127.0.0.1:5672/%2F uv run pytest tests/test_monitor.py

# PostgreSQL 后端
docker run -d --name paperlite-pg -e POSTGRES_PASSWORD=pl -e POSTGRES_DB=paperlite -p 5439:5432 postgres:16-alpine
PAPERLITE_TEST_PG=postgresql://postgres:pl@127.0.0.1:5439/paperlite uv run --extra pg pytest
uv run pytest tests/test_paperlite.py::ConfigTest::test_from_dict   # 单个用例
```

改动 `store.py`、`producer.py`、`consumer.py` 的 Redis 逻辑后，提交前务必起本机 Redis 全量跑一遍。改动 `pgqueue.py`，或改了 worker 调用任务队列的方式（两个后端都要兼容）时，用 `PAPERLITE_TEST_PG` 跑一遍。

## 改代码之前

- 先读[架构与设计说明](docs/development/architecture.md)和[事故复盘](docs/development/postmortems.md)。过盾判定、熔断、卷期停止、巡检的很多"看起来可以简化"的地方，都是被事故否决过的方案。
- 模块分工见[项目结构](docs/development/project-layout.md)。
- 监控是旁路：不能让监控阻塞下载或抛错，新增埋点只加一行调用。

## 文档

- 用户可见的行为变化同步更新 `docs/` 下对应文档；新增或改名配置字段时更新 `src/paperlite/config.example.toml` 和[配置参考](docs/reference/configuration.md)。
- `README.md` 与 `README.zh-CN.md`、`docs/getting-started.md` 与 `docs/getting-started.zh-CN.md` 中英两份同步维护；`docs/` 其余文档只写中文。
- 在 [CHANGELOG.md](CHANGELOG.md) 的 `Unreleased` 下记一笔。

## 提交

- 提交信息用 `type(scope): 摘要`，type 取 `feat`、`fix`、`docs`、`refactor`、`test`、`chore`、`release`。
- 不要提交 `config.toml` 等含代理密码的文件。`pyproject.toml` 的 sdist `only-include` 白名单保证它们不会进包，新增需要打包的路径时改那里。

## 发布

1. 更新 `pyproject.toml` 中的版本号，把 `CHANGELOG.md` 的 `Unreleased` 改成版本号和日期。
2. 提交 `release: version X.Y.Z`，打 tag `vX.Y.Z` 并推送。
3. GitHub Actions（`.github/workflows/publish.yml`）跑测试并发布到 PyPI。
