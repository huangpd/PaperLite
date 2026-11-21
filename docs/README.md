# PaperLite 文档

按"要做什么"分成四类：上手、使用指南、参考、开发。除快速上手外均为中文。

## 上手

- [快速上手](getting-started.zh-CN.md)（[English](getting-started.md)）：安装、写配置、跑第一个批次、看进度。

## 使用指南（怎么做）

- [运行模式](guide/modes.md)：浏览器模式与 curl 模式的区别和选择，输出目录规划。
- [部署 PostgreSQL](guide/postgres.md)：任务队列和监控数据所在的数据库：部署、访问控制、备份、清理。
- [多机部署](guide/multi-machine.md)：多台机器跑同一个批次：安装、配置、启动、看进度。
- [日常运维](guide/operations.md)：后台运行、中断续跑、重试、导出、巡检、新增站点、日志。
- [监控](guide/monitoring.md)：RabbitMQ + 监控面板的部署、终端接入、安全与容量。
- [Turnstile](guide/turnstile.md)：单独获取 Cloudflare Turnstile token（命令行或 HTTP 服务）。
- [故障排查](guide/troubleshooting.md)：常见现象、原因和处理办法。

## 参考（查什么）

- [配置参考](reference/configuration.md)：`config.toml` 每一段、每个字段的默认值和含义。
- [命令参考](reference/cli.md)：`paperlite` 与 `paperlite-monitor` 的全部子命令和参数。
- [任务库](reference/task-database.md)：`pl_tasks` / `pl_chunks` / `pl_skip_rules` 表结构、状态流转、错误分类、常用查询。

## 开发（为什么）

- [架构与设计说明](development/architecture.md)：身份模型、过盾、熔断、巡检、按块领取等行为背后的取舍。
- [PG 按块领取设计](development/pg-chunk-lease.md)：多机队列为什么按块、压测数据、表结构与生命周期。
- [项目结构](development/project-layout.md)：源码模块分工。
- [事故复盘](development/postmortems.md)：踩过的坑，改核心行为前先读。
- [参与开发](../CONTRIBUTING.md)：环境、测试、提交与发布。
- [更新日志](../CHANGELOG.md)
