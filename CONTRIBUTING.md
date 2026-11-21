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

测试是 `unittest.TestCase` 子类，用 pytest 运行。依赖外部服务的用例由环境变量控制，没有设置时自动跳过。任务队列和监控面板都在 PostgreSQL 里，**大部分用例需要 PG**（每个用例建一个临时 schema，用完删掉）：

```bash
docker run -d --name paperlite-pg -e POSTGRES_PASSWORD=pl -e POSTGRES_DB=paperlite -p 5439:5432 postgres:16-alpine
export PAPERLITE_TEST_PG=postgresql://postgres:pl@127.0.0.1:5439/paperlite

uv run pytest                                                   # 全部测试（任务队列、身份池、监控都在 PG）
uv run pytest tests/test_paperlite.py::ConfigTest::test_from_dict   # 单个用例
```

不设 `PAPERLITE_TEST_PG` 时只跑不需要数据库的用例（配置、分类、参数解析等），不能代替完整测试。GitHub Actions 发布前会起一个 PG 服务跑全量。

身份池（`store.py`）的测试也跑在 PG 上，跟着全量一起跑。PG 相关的测试辅助在 `tests/pgutil.py`（`make_source`、`make_store`、`needs_pg`、`pg_config`）。

## 改代码之前

- 先读[架构与设计说明](docs/development/architecture.md)和[事故复盘](docs/development/postmortems.md)。过盾判定、熔断、巡检、按块领取的很多"看起来可以简化"的地方，都是被事故否决过的方案。
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
