"""
paperlite/errors.py
───────────────────
UserError：给人看的错误（配置不对、文件读不了、批次不满足运行条件）。库代码抛它，命令行打印信息后以退出码 1 结束。
库代码不直接 SystemExit：被别的程序调用时，不该让对方整个进程退出。
"""


class UserError(Exception):
    pass
