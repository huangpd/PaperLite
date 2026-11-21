"""包之间的依赖方向（见 docs/development/project-layout.md）。扫描 src/paperlite 下所有 import（包括函数里的延迟 import）。

  core/（tasks page pdf）                       纯规则：只依赖 core 自己（不碰网络、数据库、配置）
  根目录（config proxy logutil errors）          core、根目录，外加遥测出口 monitor.reporter
  taskq/                                         core、根目录
  browser/                                       core、根目录、taskq、monitor.reporter
  curl/                                          core、根目录、taskq、monitor.reporter，以及 browser.session / browser.env（过盾、渲染）
  monitor/                                       自己，外加 config（读 [pg] / [monitor]）
  cli、__main__、包的 __init__                    不限
任何模块都不许 import cli。
"""
import ast
import unittest
from pathlib import Path

import paperlite

PKG = Path(paperlite.__file__).parent
ROOT_MODULES = {p.stem for p in PKG.glob("*.py")} - {"__init__", "__main__", "cli"}
REPORTER = "paperlite.monitor.reporter"


def module_name(path: Path) -> str:
    parts = path.relative_to(PKG.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def is_module(dotted: str) -> bool:
    rel = Path(*dotted.split(".")[1:]) if "." in dotted else Path()
    return (PKG / rel).with_suffix(".py").is_file() or (PKG / rel / "__init__.py").is_file()


def imports(path: Path) -> set[str]:
    """该文件 import 的 paperlite 内部模块（from X import y 时 y 是子模块就记 X.y）。"""
    out = set()
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names if a.name.split(".")[0] == "paperlite"}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0 \
                and node.module.split(".")[0] == "paperlite":
            for a in node.names:
                sub = f"{node.module}.{a.name}"
                out.add(sub if is_module(sub) else node.module)
    return out


def allowed(src: str, dst: str) -> bool:
    if dst == "paperlite.cli":
        return src == "paperlite.__main__"
    parts = src.split(".")
    if src in ("paperlite", "paperlite.__main__", "paperlite.cli") or (len(parts) == 2 and is_package(src)):
        return True                                       # 入口和包的 __init__ 不限
    core = dst == "paperlite.core" or dst.startswith("paperlite.core.")
    root = core or dst == "paperlite" or (dst.count(".") == 1 and dst.split(".")[1] in ROOT_MODULES)
    group = parts[1] if len(parts) > 2 else "root"
    same = dst.startswith(f"paperlite.{group}.") or dst == f"paperlite.{group}"
    if group == "core":
        return core
    if group == "root":
        return root or dst == REPORTER
    if group == "taskq":
        return root or same
    if group == "browser":
        return root or same or dst.startswith("paperlite.taskq") or dst == REPORTER
    if group == "curl":
        return root or same or dst.startswith("paperlite.taskq") or dst == REPORTER \
            or dst in ("paperlite.browser.session", "paperlite.browser.env")
    if group == "monitor":
        return same or dst == "paperlite.config"
    return False


def is_package(name: str) -> bool:
    return (PKG / name.split(".", 1)[1] / "__init__.py").is_file()


class LayeringTest(unittest.TestCase):
    def test_dependency_direction(self):
        bad = []
        for path in sorted(PKG.rglob("*.py")):
            src = module_name(path)
            if path.name == "__init__.py" and path.parent != PKG:
                continue                                  # 子包 __init__ 只做导出
            for dst in sorted(imports(path)):
                if dst != src and not allowed(src, dst):
                    bad.append(f"{src} → {dst}")
        self.assertEqual(bad, [], "违反依赖方向：\n" + "\n".join(bad))

    def test_checker_catches_violations(self):
        """规则本身要能拦住反方向依赖（防止规则写成恒真）。"""
        self.assertFalse(allowed("paperlite.config", "paperlite.curl.identity"))
        self.assertFalse(allowed("paperlite.core.tasks", "paperlite.taskq.queue"))
        self.assertFalse(allowed("paperlite.core.page", "paperlite.config"))          # core 不读配置
        self.assertFalse(allowed("paperlite.core.pdf", REPORTER))                     # core 不上报监控
        self.assertTrue(allowed("paperlite.core.page", "paperlite.core.tasks"))
        self.assertTrue(allowed("paperlite.taskq.queue", "paperlite.core.tasks"))
        self.assertFalse(allowed("paperlite.taskq.queue", "paperlite.browser.pool"))
        self.assertFalse(allowed("paperlite.browser.pool", "paperlite.curl.identity"))
        self.assertFalse(allowed("paperlite.curl.producer", "paperlite.browser.pool"))
        self.assertFalse(allowed("paperlite.monitor.reporter", "paperlite.taskq"))
        self.assertFalse(allowed("paperlite.browser.pool", "paperlite.cli"))
        self.assertTrue(allowed("paperlite.curl.producer", "paperlite.browser.session"))
        self.assertTrue(allowed("paperlite.proxy", REPORTER))

    def test_scanner_sees_imports(self):
        """扫描器能看到顶层和函数里的 import，from 包 import 子模块 也认得出。"""
        self.assertIn("paperlite.browser.session", imports(PKG / "curl" / "producer.py"))
        self.assertIn("paperlite.browser.env", imports(PKG / "browser" / "turnstile.py"))     # 函数内延迟 import
        self.assertIn(REPORTER, imports(PKG / "proxy.py"))                                    # from paperlite.monitor import reporter


if __name__ == "__main__":
    unittest.main()
