"""
paperlite-monitor：监控面板端命令。

  paperlite-monitor serve  [--config config.toml]   # 消费监控事件 → 汇总入库 → 起大屏 http://127.0.0.1:8700/
  paperlite-monitor test   [--config config.toml]   # 在终端机器上写一条测试心跳，验证能写进监控库、面板是否在消费
  paperlite-monitor demo   [--config config.toml]   # 灌一份模拟数据（schema paperlite_monitor_demo）并起大屏

配置读 config.toml 的 [monitor] 段（查找顺序同 paperlite：--config > PAPERLITE_CONFIG > ./config.toml）。
面板数据存 PostgreSQL：[monitor] dsn，留空则用 [pg] 的 dsn。
"""

import argparse
import dataclasses
import math
import logging
import random
import sys
import threading
import time

from paperlite import config as config_mod
from paperlite.monitor import schema
from paperlite.monitor.config import MonitorConfig, load_monitor
from paperlite.monitor.reporter import _redact

logger = logging.getLogger("paperlite.monitor")


def _cfg(args) -> MonitorConfig:
    try:
        cfg = load_monitor(config_mod.resolve_path(args.config))
    except (TypeError, ValueError) as e:
        raise SystemExit(f"配置错误: {e}")
    over = {k: v for k, v in (("dsn", getattr(args, "dsn", None)), ("schema", getattr(args, "schema", None)),
                               ("bind", getattr(args, "bind", None)),
                               ("port", getattr(args, "port", None)))
            if v is not None}
    return dataclasses.replace(cfg, **over) if over else cfg


def _open(cfg: MonitorConfig):
    from paperlite.monitor.db import MonitorDB
    if not cfg.dsn:
        raise SystemExit("面板数据存 PostgreSQL：请在 [monitor] 填 dsn，或在 [pg] 填 dsn（两者共用一个库）")
    try:
        return MonitorDB(cfg.dsn, cfg.schema, cfg.retention_days, cfg.raw_retention_hours, cfg.backlog_max)
    except Exception as e:
        raise SystemExit(f"连接 PostgreSQL 失败: {e}")


def _serve(cfg: MonitorConfig, consume: bool) -> None:
    from paperlite.monitor.dashboard import make_server

    if not cfg.loopback_only and not cfg.auth_enabled:
        raise SystemExit(f"面板监听 {cfg.bind}（非本机）却没设密码：任何人都能看。请在 [monitor] 设 password"
                         f"（或 token），或改回 bind = \"127.0.0.1\"。见 docs/guide/monitoring.md")
    db = _open(cfg)
    db.prune(force=True)
    stop = threading.Event()
    if consume:
        from paperlite.monitor.consumer import consume_forever
        threading.Thread(target=consume_forever, args=(cfg, db, stop), name="consumer", daemon=True).start()
    try:
        srv = make_server(cfg, db)
    except OSError as e:
        db.close()
        raise SystemExit(f"面板启动失败（端口被占用？）: {e}")
    host = "127.0.0.1" if cfg.bind in ("0.0.0.0", "") else cfg.bind
    auth = (f"登录用户 {cfg.username}" if cfg.password else "") + \
           ("，另可用 token" if cfg.token else "") or "未设密码（仅本机可访问）"
    logger.info("监控大屏 http://%s:%d/  %s  数据存 PostgreSQL schema %s（保留 %d 天）", host, cfg.port, auth,
                cfg.schema, cfg.retention_days)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
        db.close()


def _test(cfg: MonitorConfig, wait_s: float = 5.0) -> None:
    """写一条测试心跳到 events 表，再看面板有没有把它取走（取走 = 面板在消费）。"""
    import psycopg
    from psycopg.types.json import Jsonb
    from paperlite.monitor import events

    if not cfg.dsn:
        raise SystemExit("没有监控用的 PostgreSQL：填 [monitor] dsn，或 [pg] dsn")
    msg = schema.envelope(schema.HEARTBEAT, time.time(), cfg.node_name, cfg.instance, "monitor-test",
                          cfg.vendor_name, {"mode": "test", "browsers_online": 0, "workers": 0,
                                            "pending": 0, "running": 0, "success": 0, "failed": 0})
    try:
        with psycopg.connect(cfg.dsn, autocommit=True, connect_timeout=10) as conn:
            events.use_schema(conn, cfg.schema, create=True)
            event_id = conn.execute("INSERT INTO events (msg) VALUES (%s) RETURNING id", (Jsonb(msg),)).fetchone()[0]
            deadline = time.monotonic() + wait_s
            consumed = False
            while time.monotonic() < deadline:
                try:
                    consumed = conn.execute("SELECT NOT EXISTS (SELECT 1 FROM events WHERE id = %s)",
                                            (event_id,)).fetchone()[0]
                except psycopg.errors.InsufficientPrivilege:
                    break                                   # 只有写权限的终端账号：看不了，算写入成功
                if consumed:
                    break
                time.sleep(0.5)
    except Exception as e:
        raise SystemExit(f"写入监控库失败: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")
    print(f"OK：测试心跳已写入 {_redact(cfg.dsn)} schema={cfg.schema}，终端 {cfg.instance}")
    print("面板正在消费（「终端」表里会出现 batch=monitor-test）" if consumed
          else f"面板 {wait_s:.0f}s 内没有取走它：面板没启动或看不到这张表；起 paperlite-monitor serve 后会补上")


def demo_messages(now: float, days: int = 30, scale: int = 1) -> list[dict]:
    """模拟数据：3 个终端、2 个厂商、若干域名（不同 WAF、不同难度），分布在最近 days 天。"""
    rnd = random.Random(7)
    hosts = [("www.sciencedirect.com", "cloudflare", 0.86, 9000), ("dl.acm.org", "cloudflare", 0.93, 6000),
             ("ieeexplore.ieee.org", "aws_waf", 0.78, 7000), ("onlinelibrary.wiley.com", "cloudflare", 0.7, 11000),
             ("www.tandfonline.com", "datadome", 0.55, 14000), ("link.springer.com", "none", 0.97, 3000)]
    insts = [("gpu-01", "qingguo", "sciencedirect"), ("gpu-02", "qingguo", "acm-mixed"),
             ("hk-03", "kuaidaili", "wiley-tandf")]
    vendor_q = {"qingguo": 1.0, "kuaidaili": 0.88}
    out = []

    def env(t, ts, node, vendor, batch, data):
        return schema.envelope(t, ts, node, f"{node}:1000", batch, vendor, data)

    for _ in range(days * 220 * scale):
        ts = now - rnd.random() ** 1.6 * days * 86400          # 越近越密
        node, vendor, batch = rnd.choice(insts)
        r = rnd.random()
        if r < 0.25:
            ok = rnd.random() < 0.82 * vendor_q[vendor]
            out.append(env(schema.PROXY_CHECK, ts, node, vendor, batch, {
                "ok": ok, "total_ms": rnd.uniform(300, 2500) if ok else rnd.uniform(1500, 9000),
                "reason": "" if ok else rnd.choice(["slow_3400ms", "exit_ip_mismatch(1.2.3.4)", "status_403",
                                                    "ConnectionError: timeout"])}))
            continue
        host, waf, p, base_ms = rnd.choice(hosts)
        if r < 0.33:
            ok = rnd.random() < p * vendor_q[vendor] + 0.05
            exc = rnd.random() < 0.03
            out.append(env(schema.BROWSER, ts, node, vendor, batch, {
                "ok": ok and not exc, "kind": "exception" if exc else ("ok" if ok else "challenge_failed"),
                "host": host, "waf": waf, "seconds": rnd.uniform(8, 40), "error": "TimeoutError" if not ok else ""}))
            continue
        passed = rnd.random() < p * vendor_q[vendor]
        ok = passed and rnd.random() < 0.94
        status = (200 if ok else rnd.choice([404, 403, 500])) if passed else None
        out.append(env(schema.PAGE, ts, node, vendor, batch, {
            "host": host, "passed": passed, "ok": ok, "status": status, "waf": waf,
            "elapsed_ms": int(rnd.lognormvariate(0, 0.45) * base_ms) if passed else 90000,
            "error": "" if passed else "TimeoutError: 90s 内页面未稳定"}))
    for i, (node, vendor, batch) in enumerate(insts):                 # 历史心跳：每 15 分钟一次，在线数有起伏
        for k in range(days * 96, 0, -1):
            ts = now - k * 900
            online = max(0, min(20, round([17, 19, 8][i] + 3 * math.sin(ts / 20000 + i) + rnd.uniform(-2, 2))))
            out.append(env(schema.HEARTBEAT, ts, node, vendor, batch, {
                "mode": "browser", "browsers_online": online, "workers": 20, "pending": 0, "running": online,
                "success": 0, "failed": 0}))
        out.append(env(schema.HEARTBEAT, now - i * 5, node, vendor, batch, {
            "mode": "browser", "browsers_online": [18, 20, 7][i], "workers": 20,
            "pending": [12034, 4410, 960][i], "running": [18, 20, 7][i], "success": [8231, 5120, 2210][i],
            "failed": [512, 233, 801][i]}))
    out.append(env(schema.LOG, now - 40, "hk-03", "kuaidaili", "wiley-tandf",
                   {"level": "WARNING", "event": "log", "message": "提取 IP 失败: 代理提取失败: 接口返回异常"}))
    return out


def _demo(cfg: MonitorConfig, scale: int) -> None:
    db = _open(cfg)
    n = db.ingest(demo_messages(time.time(), scale=scale))
    db.close()
    logger.info("已写入 %d 条模拟数据到 schema %s", n, cfg.schema)
    _serve(cfg, consume=False)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="paperlite-monitor", description="PaperLite 监控面板",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--config", help="配置文件（读 [monitor] 段），默认同 paperlite")
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<命令>")
    for name, help_ in (("serve", "消费监控事件、汇总入库并起大屏"), ("demo", "灌模拟数据并起大屏")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--dsn", help="面板 PostgreSQL（覆盖 [monitor].dsn）")
        p.add_argument("--schema", help="面板表所在 schema（覆盖 [monitor].schema）")
        p.add_argument("--bind", help="监听地址（覆盖 [monitor].bind）")
        p.add_argument("--port", type=int, help="端口（覆盖 [monitor].port）")
        if name == "serve":
            p.add_argument("--no-consume", action="store_true", help="只起大屏，不消费（看已有数据）")
        else:
            p.add_argument("--scale", type=int, default=1, help="模拟数据量倍数")
    p = sub.add_parser("test", help="写一条测试心跳，验证终端能写进监控库、面板是否在消费")
    p.add_argument("--dsn", help="监控用的 PostgreSQL（覆盖 [monitor].dsn）")
    p.add_argument("--schema", help="监控表所在 schema（覆盖 [monitor].schema）")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    cfg = _cfg(args)
    if args.cmd == "serve":
        _serve(cfg, consume=not args.no_consume)
    elif args.cmd == "demo":
        if args.schema is None:
            cfg = dataclasses.replace(cfg, schema="paperlite_monitor_demo")
        _demo(cfg, args.scale)
    else:
        _test(cfg)


if __name__ == "__main__":
    main()
