#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/inspect.py
──────────────────────
巡检：采集过程中，某个标题重复 >= 阈值次（默认 10）就认定它是栏目/封面类"坏标题"，
从这些样本的 doi / url 归纳规则（两个维度各取最长公共前缀，再加一个去掉末尾数字的更宽前缀），
用已采数据验证精确率（命中规则的已采条目里"坏标题"占比），只保留 precision/support 达标的最宽候选，
把命中规则的未采（pending）任务标 suspect_cover，之后不再领取。

只用 doi + url 泛化：未采任务也只有这两样，所以规则对它们可判。判"坏"的唯一依据是标题重复，不看作者
（作者要采了才知道，对未采任务没法泛化）。精确率护栏保证封面散落、只能按刊泛化时不会把真文章误标。
"""

import logging
import re
import threading
import unicodedata
from collections import Counter, defaultdict, deque

logger = logging.getLogger(__name__)


def norm_title(t: str) -> str:
    """标题归一化：NFKC、转小写、压空白、去标点。"""
    if not t:
        return ""
    t = unicodedata.normalize("NFKC", t).lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^\w\s]", "", t)
    return t.strip()


def _lcp(xs: list[str]) -> str:
    """最长公共前缀。"""
    if not xs:
        return ""
    s = xs[0]
    for x in xs[1:]:
        i = 0
        while i < min(len(s), len(x)) and s[i] == x[i]:
            i += 1
        s = s[:i]
        if not s:
            break
    return s


def _coarsen(prefix: str) -> str:
    """去掉前缀末尾的数字/校验位，得到更宽的候选（卷/刊级）。"""
    return re.sub(r"[0-9xX]+$", "", prefix)


def _candidates(samples: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """由样本的 (doi, url) 生成候选规则 (doi前缀, url前缀)，空串=该维度不限。"""
    dois = [d for d, _ in samples]
    urls = [u for _, u in samples]
    dps = {p for p in (_lcp(dois), _coarsen(_lcp(dois))) if p}
    ups = {p for p in (_lcp(urls), _coarsen(_lcp(urls))) if p}
    out = []
    for dp in list(dps) + [""]:
        for up in list(ups) + [""]:
            if dp or up:
                out.append((dp, up))
    return out


def _matches(doi: str, url: str, dp: str, up: str) -> bool:
    return (not dp or doi.startswith(dp)) and (not up or url.startswith(up))


class WindowInspector:
    """浏览器采集用的增量巡检器，只观察本次进程最近 ``window_size`` 条成功结果。

    每条结果只进入内存一次；标题达到阈值后，有新的同标题样本才重新评估。已经生成过的
    规则不会重复写入，但新样本确实跨越卷期时允许归纳出更宽的新规则。
    """

    def __init__(self, db, trigger: int = 10, min_precision: float = 0.98,
                 min_support: int = 10, window_size: int = 500) -> None:
        if trigger <= 0:
            raise ValueError("trigger 必须大于 0")
        if window_size < trigger:
            raise ValueError("window_size 不能小于 trigger")
        self.db = db
        self.trigger = trigger
        self.min_precision = min_precision
        self.min_support = min_support
        self.window_size = window_size
        self._rows: deque[tuple[str, str, str, str]] = deque()
        self._counts: Counter[str] = Counter()
        self._applied_rules: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def observe(self, doi: str, url: str, title: str) -> tuple[int, int]:
        """加入一条刚成功的结果；返回本次新增的 ``(规则数, 标记数)``。"""
        nt = norm_title(title)
        if not nt:
            return 0, 0
        with self._lock:
            if len(self._rows) >= self.window_size:
                _, _, old_nt, _ = self._rows.popleft()
                self._counts[old_nt] -= 1
                if self._counts[old_nt] <= 0:
                    del self._counts[old_nt]
            self._rows.append((doi, url, nt, title))
            self._counts[nt] += 1
            if self._counts[nt] < self.trigger:
                return 0, 0
            return self._trigger(nt)

    def _trigger(self, nt: str) -> tuple[int, int]:
        rows = list(self._rows)
        samples = [(d, u) for d, u, t, _ in rows if t == nt]
        bad_titles = {t for t, count in self._counts.items() if count >= self.trigger}
        original = next(title for _, _, t, title in rows if t == nt)
        best = None
        for dp, up in _candidates(samples):
            matched = [t for d, u, t, _ in rows if _matches(d, u, dp, up)]
            support = len(matched)
            if support < self.min_support:
                continue
            precision = sum(1 for t in matched if t in bad_titles) / support
            if precision < self.min_precision:
                continue
            pending = self.db.count_pending_matching(dp, up)
            if pending == 0:
                continue
            key = (pending, support)
            if best is None or key > best[0]:
                best = (key, support, precision, dp, up)
        if best is None:
            return 0, 0
        _, support, precision, dp, up = best
        if (dp, up) in self._applied_rules:
            return 0, 0
        flagged = self.db.flag_suspects(dp, up, original, support, precision)
        if not flagged:
            return 0, 0
        self._applied_rules.add((dp, up))
        logger.info("巡检窗口：标题重复 %d 次（%r）→ 规则 doi~%r url~%r"
                    "（precision %.3f，窗口命中 %d）→ 标记 %d 条疑似封面，不再采",
                    len(samples), original[:40], dp, up, precision, support, flagged)
        return 1, flagged


def inspect(db, trigger: int = 10, min_precision: float = 0.98, min_support: int = 10) -> tuple[int, int]:
    """
    跑一轮巡检，返回 (本次生效规则数, 本次新标记的 pending 数)。
    db 需提供 crawled_titles() / count_pending_matching() / flag_suspects()（见 tasks.TaskDB）。
    """
    crawled = db.crawled_titles()
    if len(crawled) < trigger:
        return 0, 0
    rows = [(d, u, norm_title(t)) for d, u, t in crawled]
    by_title: dict[str, list[tuple[str, str]]] = defaultdict(list)
    orig: dict[str, str] = {}
    for (d, u, nt), (_, _, t0) in zip(rows, crawled):
        if nt:
            by_title[nt].append((d, u))
            orig.setdefault(nt, t0)
    bad_titles = {nt for nt, s in by_title.items() if len(s) >= trigger}
    if not bad_titles:
        return 0, 0

    rules = flagged = 0
    for nt in bad_titles:
        best = None                      # ((pending, support), support, precision, dp, up)
        for dp, up in _candidates(by_title[nt]):
            matched = [t for d, u, t in rows if _matches(d, u, dp, up)]
            support = len(matched)
            if support < min_support:
                continue
            precision = sum(1 for t in matched if t in bad_titles) / support
            if precision < min_precision:
                continue
            pend = db.count_pending_matching(dp, up)
            if pend == 0:
                continue
            key = (pend, support)
            if best is None or key > best[0]:
                best = (key, support, precision, dp, up)
        if best:
            _, support, precision, dp, up = best
            n = db.flag_suspects(dp, up, orig[nt], support, precision)
            if n:
                rules += 1
                flagged += n
                logger.info("巡检：标题重复 %d 次（%r）→ 规则 doi~%r url~%r（precision %.3f，已采命中 %d）"
                            "→ 标记 %d 条疑似封面，不再采",
                            len(by_title[nt]), orig[nt][:40], dp, up, precision, support, n)
    return rules, flagged
