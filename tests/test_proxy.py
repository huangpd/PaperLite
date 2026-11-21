"""代理提取与体检（proxy.py）。"""
import time
import unittest
from unittest import mock

from paperlite.proxy import ProxySettings




class ProxyCheckTest(unittest.TestCase):
    def _src(self, **kw):
        from paperlite.proxy import IpSource
        return IpSource(ProxySettings(api_url="https://api/x", **kw))

    def _lease(self, ip="1.2.3.4"):
        from paperlite.proxy import Lease
        return Lease(server="1.2.3.4:8000", exit_ip=ip, area="", expires_at=time.time() + 600)

    def _ok(self, egress="1.2.3.4", total_ms=500.0, status=200):
        from paperlite.proxy import ProbeResult
        return ProbeResult(ok=True, status=status, tcp_ms=30, tls_ms=200, ttfb_ms=total_ms * 0.8,
                           total_ms=total_ms, egress=egress)

    def _fail(self, error="CurlError: connect"):
        from paperlite.proxy import ProbeResult
        return ProbeResult(ok=False, error=error)

    def test_extract_ip(self):
        from paperlite.proxy import _extract_ip
        self.assertEqual(_extract_ip("fl=1f\nip=203.0.113.7\nts=1.2"), "203.0.113.7")   # cloudflare trace
        self.assertEqual(_extract_ip('{\n  "origin": "203.0.113.7"\n}'), "203.0.113.7")  # httpbin/ip
        self.assertEqual(_extract_ip("no ip here"), "")

    def test_passes_on_first_good_judge(self):
        src = self._src()
        with mock.patch.object(src, "_probe", return_value=self._ok("1.2.3.4")) as probe:
            self.assertTrue(src._check(self._lease("1.2.3.4")))
            self.assertEqual(probe.call_count, 1)            # 首个目标就过，不再探第二个

    def test_connect_fail_rejected(self):
        src = self._src()
        with mock.patch.object(src, "_probe", return_value=self._fail()) as probe:
            self.assertFalse(src._check(self._lease()))
            self.assertEqual(probe.call_count, 2)            # 两个目标都探过才判死

    def test_bad_status_rejected(self):
        src = self._src()
        with mock.patch.object(src, "_probe", return_value=self._ok(status=403)):
            self.assertFalse(src._check(self._lease("1.2.3.4")))

    def test_exit_ip_mismatch_rejected(self):
        src = self._src()
        with mock.patch.object(src, "_probe", return_value=self._ok(egress="9.9.9.9")):
            self.assertFalse(src._check(self._lease("1.2.3.4")))   # 回显出口≠proxy_ip

    def test_latency_gate_rejects_slow(self):
        src = self._src(check_max_latency_ms=1000)
        with mock.patch.object(src, "_probe", return_value=self._ok(total_ms=2000)):
            self.assertFalse(src._check(self._lease("1.2.3.4")))   # 两目标都超阈值

    def test_second_judge_denoise(self):
        src = self._src()
        with mock.patch.object(src, "_probe", side_effect=[self._fail(), self._ok("1.2.3.4")]):
            self.assertTrue(src._check(self._lease("1.2.3.4")))    # 首个目标挂、备用好 → 整体通过

    def test_ip_verify_skipped_when_unparsed(self):
        src = self._src()                                    # verify_exit_ip 默认 True
        with mock.patch.object(src, "_probe", return_value=self._ok(egress="")):
            self.assertTrue(src._check(self._lease("1.2.3.4")))    # 抠不出回显 IP 时不因此误杀

    def test_extract_checked_retries_then_raises(self):
        from paperlite.proxy import ProxyUnavailable
        src = self._src(check_max_tries=3)
        with mock.patch.object(src, "_extract", return_value=self._lease()), \
             mock.patch.object(src, "_check", return_value=False) as chk:
            with self.assertRaises(ProxyUnavailable):
                src.extract_checked()
            self.assertEqual(chk.call_count, 3)              # 连续 3 个不过才放弃
