"""
paperlite-monitor：监控面板端命令。

  paperlite-monitor serve  [--config config.toml]   # 消费 RabbitMQ → 入库 → 起大屏 http://127.0.0.1:8700/
  paperlite-monitor test   [--config config.toml]   # 在终端机器上发一条测试心跳，验证到 RabbitMQ 的连通性
  paperlite-monitor demo   --db demo.db             # 灌一份模拟数据并起大屏（不需要 RabbitMQ），看效果用

配置读 config.toml 的 [monitor] 段（查找顺序同 paperlite：--config > PAPERLITE_CONFIG > ./config.toml）。
"""

import argparse
import dataclasses
import logging
import random
import sys
import threading
import time

from paperlite import config as config_mod
from paperlite.monitor import schema
from paperlite.monitor.config import MonitorConfig, load_monitor

logger = logging.getLogger("paperlite.monitor")


def _cfg(args) -> MonitorConfig:
    try:
        cfg = load_monitor(config_mod.resolve_path(args.config))
    except (TypeError, ValueError) as e:
        raise SystemExit(f"配置错误: {e}")
    over = {k: v for k, v in (("db", getattr(args, "db", None)), ("bind", getattr(args, "bind", None)),
                               ("port", getattr(args, "port", None)), ("amqp_url", getattr(args, "amqp", None)))
            if v is not None}
    return dataclasses.replace(cfg, **over) if over else cfg


def _need_pika() -> None:
    try:
        import pika  # noqa: F401
    except ImportError:
        raise SystemExit("需要 pika：pip install 'paperlite[monitor]'（或 uv pip install pika）")


def _serve(cfg: MonitorConfig, consume: bool) -> None:
    from paperlite.monitor.dashboard import make_server
    from paperlite.monitor.db import MonitorDB

    db = MonitorDB(cfg.db_path, cfg.retention_days)
    db.prune(force=True)
    stop = threading.Event()
    if consume:
        _need_pika()
        from paperlite.monitor.consumer import consume_forever
        threading.Thread(target=consume_forever, args=(cfg, db, stop), name="consumer", daemon=True).start()
    srv = make_server(cfg, db)
    host = "127.0.0.1" if cfg.bind in ("0.0.0.0", "") else cfg.bind
    logger.info("监控大屏 http://%s:%d/%s  数据库 %s（保留 %d 天）", host, cfg.port,
                f"?token={cfg.token}" if cfg.token else "", cfg.db_path.resolve(), cfg.retention_days)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
        db.close()


def _test(cfg: MonitorConfig) -> None:
    _need_pika()
    import pika

    msg = schema.envelope(schema.HEARTBEAT, time.time(), cfg.node_name, cfg.instance, "monitor-test",
                          cfg.vendor_name, {"mode": "test", "browsers_online": 0, "workers": 0,
                                            "pending": 0, "running": 0, "success": 0, "failed": 0})
    try:
        conn = pika.BlockingConnection(pika.URLParameters(cfg.amqp_url))
        ch = conn.channel()
        ch.exchange_declare(cfg.exchange, exchange_type="topic", durable=True)
        ch.confirm_delivery()
        ch.basic_publish(cfg.exchange, msg["type"], schema.encode(msg),
                         pika.BasicProperties(content_type="application/json", delivery_mode=2), mandatory=True)
        conn.close()
    except pika.exceptions.UnroutableError:
        raise SystemExit("已连上 RabbitMQ，但还没有面板队列绑定到该 exchange（先起 paperlite-monitor serve）")
    except Exception as e:
        raise SystemExit(f"连接/发送失败: {type(e).__name__}: {e}")
    print(f"OK：测试心跳已送达 exchange={cfg.exchange}，终端 {cfg.instance}（面板「终端」表里会出现 batch=monitor-test）")


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
    for i, (node, vendor, batch) in enumerate(insts):
        out.append(env(schema.HEARTBEAT, now - i * 5, node, vendor, batch, {
            "mode": "browser", "browsers_online": [18, 20, 7][i], "workers": 20,
            "pending": [12034, 4410, 960][i], "running": [18, 20, 7][i], "success": [8231, 5120, 2210][i],
            "failed": [512, 233, 801][i]}))
    out.append(env(schema.LOG, now - 40, "hk-03", "kuaidaili", "wiley-tandf",
                   {"level": "WARNING", "event": "log", "message": "提取 IP 失败: 代理提取失败: 接口返回异常"}))
    return out


def _demo(cfg: MonitorConfig, scale: int) -> None:
    from paperlite.monitor.db import MonitorDB

    db = MonitorDB(cfg.db_path, cfg.retention_days)
    n = db.ingest(demo_messages(time.time(), scale=scale))
    db.close()
    logger.info("已写入 %d 条模拟数据到 %s", n, cfg.db_path)
    _serve(cfg, consume=False)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="paperlite-monitor", description="PaperLite 监控面板",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--config", help="配置文件（读 [monitor] 段），默认同 paperlite")
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<命令>")
    for name, help_ in (("serve", "消费 RabbitMQ、入库并起大屏"), ("demo", "灌模拟数据并起大屏（不连 RabbitMQ）")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--db", help="面板 SQLite（覆盖 [monitor].db）")
        p.add_argument("--bind", help="监听地址（覆盖 [monitor].bind）")
        p.add_argument("--port", type=int, help="端口（覆盖 [monitor].port）")
        if name == "serve":
            p.add_argument("--amqp", help="RabbitMQ 地址（覆盖 [monitor].amqp_url）")
            p.add_argument("--no-consume", action="store_true", help="只起大屏，不消费（看已有数据）")
        else:
            p.add_argument("--scale", type=int, default=1, help="模拟数据量倍数")
    p = sub.add_parser("test", help="发一条测试心跳，验证终端到 RabbitMQ 的连通性")
    p.add_argument("--amqp", help="RabbitMQ 地址（覆盖 [monitor].amqp_url）")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    for noisy in ("pika",):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = _cfg(args)
    if args.cmd == "serve":
        _serve(cfg, consume=not args.no_consume)
    elif args.cmd == "demo":
        if args.db is None:
            cfg = dataclasses.replace(cfg, db="monitor-demo.db")
        _demo(cfg, args.scale)
    else:
        _test(cfg)


if __name__ == "__main__":
    main()
