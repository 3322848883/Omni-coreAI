# pa-data-source 的 systemd 单元（按组件起）

这三个单元是「**只跑其中几个组件**」的方式。与 `watchdog.py`（一键全上）**只能选一种** ——
理由与切换顺序见 `../SERVER.md §5.1`。

| 单元 | 起什么 | 产出 |
|---|---|---|
| `omnialpha-kline-live.service` | Gate 实盘 K 线 + 账户推送 | `data/kline.db`、`data/account.db` |
| `omnialpha-kline-testnet.service` | Gate 测试网 K 线 | `data/kline_testnet.db` |
| `omnialpha-aux.service` | aux 辅助信息流 | `aux-data/aux_cache.db` |

（**不含** 5 个非 Gate 交易所 —— 那些在 `watchdog.py` 的 `kline-multi` 目标里。
要单独起它们，仿照这里写一个单元，`ExecStart` 用
`kline_watcher_multi.py --exchanges ...`，注意 `--lock`/`--health-port` 要错开。）

## 安装

```bash
# 前置：先按 ../SERVER.md 跑过 server_setup.sh（.venv + gate-cli + data/logs 目录）
cd /opt/omnialpha/pa-data-source

install -m 644 systemd/omnialpha-kline-live.service    /etc/systemd/system/
install -m 644 systemd/omnialpha-kline-testnet.service /etc/systemd/system/
install -m 644 systemd/omnialpha-aux.service           /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now omnialpha-kline-live omnialpha-kline-testnet omnialpha-aux
```

## 验证

```bash
systemctl is-active omnialpha-{aux,kline-live,kline-testnet}     # 都应为 active
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18080/health   # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18081/health   # 200
ps aux | grep -E 'kline_watcher|fetch_aux' | grep -v grep | wc -l        # 3
```

**`18081` 返回 503** 通常不是 K 线的问题，而是账户推送失配 ——
见 `watchlist_testnet.yaml` 里 `account_push` 的注释。

## 两个单元里**故意**这么写的地方（改动前请先读）

- **`StandardOutput=journal` 而不是 `append:文件`** ——
  这些组件自己已经写了带轮转的文件日志（`logs/kline_watcher*.log`、
  `aux-data/logs/aux_info_feed.log`）。再 append 到文件 = 同一份日志落两处，
  而且 **append 文件永不轮转**：实测 `pa-kline-live.err` 涨到 133MB、
  `pa-kline-testnet.err` 83MB（内容与文件日志逐字相同）。
- **testnet 单元不挂 `EnvironmentFile`** —— 它只用公开行情接口；
  测试网账户推送要的是 `GATE_TESTNET_API_KEY/SECRET`（与实盘 `GATE_API_KEY` 隔离），
  塞实盘密钥进去没用，而且会让健康端点恒 503。
- **testnet 单元设了 `PA_KLINE_LOG`** —— 两个 kline 实例默认都写
  `logs/kline_watcher.log`，消息会交错（实测因此误判过「实盘采错品种」）。

这些不变量由 `tests/test_pa_units.py` 守着 —— 改单元文件时它会先报错。
