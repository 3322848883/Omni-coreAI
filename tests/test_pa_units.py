# -*- coding: utf-8 -*-
"""`pa-data-source/systemd/*.service` 的不变量。

这些单元只在服务器上生效，本地跑不到 systemd —— 所以**用测试把关键约定钉住**，
否则改动时很容易悄悄破坏掉（下面每一条都对应一次真实踩过的坑）。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT_DIR = ROOT / "pa-data-source" / "systemd"
PA = ROOT / "pa-data-source"

UNITS = ("omnialpha-kline-live.service",
         "omnialpha-kline-testnet.service",
         "omnialpha-aux.service")


def _read(name: str) -> str:
    return (UNIT_DIR / name).read_text(encoding="utf-8")


def _exec_start(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            return line[len("ExecStart="):].strip()
    return ""


# kline_watcher.py parse_args() 里的默认值 —— 判断冲突时必须用「生效值」而不是「显式值」
KLINE_DEFAULTS = {
    "--db": "data/kline.db",
    "--health-port": "18080",
    "--lock": "kline_watcher.lock",
}


def _effective(cmd: str, flag: str) -> str:
    """显式给了就用给的，没给就用 kline_watcher.py 的默认值。"""
    m = re.search(rf"{flag}\s+(\S+)", cmd)
    return m.group(1) if m else KLINE_DEFAULTS[flag]


class TestPaSystemdUnits(unittest.TestCase):
    def test_all_units_present_with_required_sections(self):
        for name in UNITS:
            p = UNIT_DIR / name
            self.assertTrue(p.is_file(), f"缺单元文件 {name}")
            t = _read(name)
            for section in ("[Unit]", "[Service]", "[Install]"):
                self.assertIn(section, t, f"{name} 缺 {section}")
            self.assertTrue(_exec_start(t), f"{name} 缺 ExecStart")
            self.assertIn("WantedBy=multi-user.target", t, f"{name} 没有开机自启目标")

    def test_no_unrotated_append_logging(self):
        """**不能**用 append: 文件 —— 应用自己已写带轮转的文件日志。

        实测：append 那份永不轮转，涨到 133MB + 83MB，且内容与文件日志逐字相同。
        """
        for name in UNITS:
            t = _read(name)
            self.assertNotIn("append:", t,
                             f"{name} 用了 append:（未轮转的重复日志），应改 journal")
            self.assertIn("StandardOutput=journal", t, f"{name} 应交给 journald")
            self.assertIn("StandardError=journal", t, f"{name} 应交给 journald")

    def test_testnet_unit_has_no_credentials(self):
        """testnet 实例不挂 EnvironmentFile。

        它只用公开行情接口；测试网账户推送要 GATE_TESTNET_API_KEY/SECRET，
        塞实盘密钥没用，而且会让健康端点**恒 503**（push_mismatch）。
        """
        t = _read("omnialpha-kline-testnet.service")
        self.assertNotIn("EnvironmentFile=", t,
                         "testnet 单元不该挂 EnvironmentFile（会引入实盘密钥）")

    def test_testnet_unit_separates_its_log(self):
        """两个 kline 实例必须各写各的日志，否则消息交错（踩过：误判实盘采错品种）。"""
        t = _read("omnialpha-kline-testnet.service")
        self.assertIn("PA_KLINE_LOG=", t, "testnet 单元应设 PA_KLINE_LOG 分开日志")

    def test_kline_units_do_not_collide(self):
        """两个 kline 实例的 --db / --health-port / --lock 必须互不相同。

        **必须比「生效值」而不是「显式值」** —— 实盘单元就没写 `--health-port`
        （用 `kline_watcher.py` 的默认 18080）。只比显式值的话
        `None != "18080"` 会通过，漏掉「测试网显式写成同一个值」这种真冲突
        （反向验证时正是这里漏了，才补上 KLINE_DEFAULTS）。
        """
        live = _exec_start(_read("omnialpha-kline-live.service"))
        test = _exec_start(_read("omnialpha-kline-testnet.service"))
        for flag, default in KLINE_DEFAULTS.items():
            a, b = _effective(live, flag), _effective(test, flag)
            self.assertNotEqual(a, b, f"{flag} 生效值相同（{a}），两个实例会互抢")

    def test_execstart_scripts_and_configs_exist(self):
        """ExecStart 里的脚本与 --config 指向的文件必须真实存在。"""
        for name in UNITS:
            cmd = _exec_start(_read(name))
            m = re.search(r"python\s+(\S+\.py)", cmd)
            self.assertIsNotNone(m, f"{name} 的 ExecStart 里找不到 .py 脚本")
            self.assertTrue((PA / m.group(1)).is_file(), f"{name}: 脚本 {m.group(1)} 不存在")
            cfg = re.search(r"--config\s+(\S+)", cmd)
            if cfg:
                self.assertTrue((PA / cfg.group(1)).is_file(),
                                f"{name}: 配置 {cfg.group(1)} 不存在")

    def test_aux_unit_runs_fetch_aux_loop(self):
        cmd = _exec_start(_read("omnialpha-aux.service"))
        self.assertIn("fetch_aux.py", cmd)
        self.assertIn("--loop", cmd, "aux 应常驻循环（--loop），否则只采一次就退出")


if __name__ == "__main__":
    unittest.main()
