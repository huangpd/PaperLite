# Turnstile token

PaperLite 可以为任意页面求解 Cloudflare Turnstile，不依赖任务队列和身份池。

## 命令行

```bash
paperlite turnstile --url https://example.com/login --sitekey 0x4AAAA... --json
# {"ok": true, "token": "1.xxxx…", "user_agent": "...", "seconds": 15.3, ...}
```

| 参数 | 说明 |
|---|---|
| `--url` | 目标页 URL，token 绑定其 hostname |
| `--sitekey` | 目标站的 Turnstile sitekey（`0x4AAAA…`） |
| `--action`、`--cdata` | 可选，控件的 action / cData |
| `--proxy` | `scheme://[user:pass@]host:port`；提交 token 时须用同一出口 |
| `--timeout` | 单次最长等待秒数，默认 60 |
| `--json` | 输出 JSON（含 `user_agent`、耗时），而不是只输出 token |
| `--headed` | 显示浏览器窗口（调试） |

## HTTP 服务

```bash
paperlite turnstile --serve 8191 --concurrency 4          # 默认只监听 127.0.0.1，--bind 可改
curl localhost:8191/solve -d '{"url":"...","sitekey":"0x4AAAA...","proxy":"http://u:p@1.2.3.4:8000"}'
```

## 原理

1. 用 CloakBrowser 打开目标 URL，但拦截该文档，替换成只渲染指定 sitekey 的最小页面。页面的 origin 仍是目标站点，因此能通过 hostname 校验。
2. 点击 Turnstile iframe 中的复选框，从回调里取得 token。

## 使用注意

- token 一次性有效，有效期约 300 秒。
- 部分站点会校验提交时的 IP 和 UA：提交时请用同一代理和返回的 `user_agent`。
