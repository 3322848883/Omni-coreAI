# 交易执行操作指南

> 版本: v1.0 | 基于 account-watcher quick_order.py v1.0 + kline_watcher.py v1.0
> 原则：先执行后写入 — 所有API调用成功后再写入日志文件

---

## 一、数据获取（SQLite 查询模板）

### 1.1 系统健康检查

```python
import urllib.request, json
resp = urllib.request.urlopen("http://localhost:18080/health")
health = json.loads(resp.read())
# health["status"] == "running" 表示数据可用
```

### 1.2 持仓状态查询

```python
import sqlite3
conn = sqlite3.connect("/path/to/account.db")
cursor = conn.cursor()
cursor.execute("SELECT * FROM position_current")
# 返回: contract, size, entry_price, mark_price, unrealised_pnl, leverage, liq_price
# size>0=多头, size<0=空头, 无记录=无持仓
```

### 1.3 未成交挂单查询

```python
cursor.execute("SELECT * FROM order_current")
# 返回: order_id, contract, size, price, left, status
# status='open'=未成交
```

### 1.4 账户余额查询

```python
cursor.execute("SELECT * FROM balance_current WHERE id=1")
# 返回: total, available, unrealised_pnl, position_margin, order_margin
```

### 1.5 持仓模式查询（下单前必须）

```python
# REST API
url = "https://api.gateio.ws/api/v4/futures/usdt/accounts"
headers = {"KEY": api_key, "SIGN": sign, "Timestamp": ts}
resp = urllib.request.urlopen(urllib.request.Request(url, headers=headers))
data = json.loads(resp.read())
position_mode = data[0]["position_mode"]  # "single" / "dual" / "dual_long_short"
is_dual = position_mode in ("dual", "dual_long_short")
```

### 1.6 K线数据查询

```python
conn_kline = sqlite3.connect("/path/to/kline.db")
cursor_kline = conn_kline.cursor()
cursor_kline.execute("SELECT * FROM kline WHERE symbol=? AND interval=? ORDER BY t DESC LIMIT ?",
                     ("BTC_USDT", "5m", 200))
# 返回: t, o, h, l, c, v, sum, ema20, atr14
```

---

## 二、下单操作（quick_order.py API 模板）

### 2.1 持仓模式差异总览

| 模式 | 开仓 | 平仓 | 止盈止损触发单 |
|------|------|------|--------------|
| single | size正=多,负=空 | size反向,无reduce_only | close=True |
| dual | 同single | size反向+reduce_only=True | reduce_only=True+auto_size |
| dual_long_short | 同dual+pos_margin_mode | 同dual | 同dual+pos_margin_mode |

### 2.2 开仓订单

```python
# === 开仓（三种模式通用） ===
order_size = abs(N)
if side == "short":
    order_size = -order_size

body = {"contract": "BTC_USDT", "size": int(order_size)}

# 止损入场（趋势回撤/突破）
body["price"] = str(入场价)
body["tif"] = "gtc"

# 限价入场（区间边界）
body["price"] = str(入场价)
body["tif"] = "gtc"

# 市价入场（错过原始入场但趋势明确）
body["price"] = "0"
body["tif"] = "ioc"

# dual_long_short 模式额外字段
if margin_mode and is_dual:
    body["pos_margin_mode"] = margin_mode
```

### 2.3 止盈触发单（需区分持仓模式）

```python
if is_dual:
    initial = {
        "contract": "BTC_USDT", "size": 1,
        "price": str(止盈价), "tif": "gtc",
        "reduce_only": True,
        "auto_size": "close_long" if side == "long" else "close_short",
        "close": True
    }
else:
    initial = {
        "contract": "BTC_USDT",
        "size": -abs(N),  # 平多=负, 平空=正
        "price": str(止盈价), "tif": "gtc",
        "close": True
    }

trigger = {
    "strategy_type": 0, "price_type": 0,
    "price": str(止盈触发价),
    "rule": 2 if side == "long" else 1
}
body = {"initial": initial, "trigger": trigger}
# POST /futures/usdt/price_orders
```

### 2.4 止损触发单（需区分持仓模式）

```python
if is_dual:
    initial = {
        "contract": "BTC_USDT", "size": 1,
        "price": "0", "tif": "ioc",
        "reduce_only": True,
        "auto_size": "close_long" if side == "long" else "close_short",
        "close": True
    }
else:
    initial = {
        "contract": "BTC_USDT",
        "size": -abs(N),
        "price": "0", "tif": "ioc",
        "close": True
    }

trigger = {
    "strategy_type": 0, "price_type": 0,
    "price": str(止损触发价),
    "rule": 1 if side == "long" else 2
}
body = {"initial": initial, "trigger": trigger}
# POST /futures/usdt/price_orders
```

### 2.5 指定数量平仓

```python
if is_dual:
    body = {
        "contract": "BTC_USDT", "size": order_size,
        "price": "0", "tif": "ioc",
        "reduce_only": True
    }
else:
    body = {
        "contract": "BTC_USDT", "size": order_size,
        "price": "0", "tif": "ioc"
    }
# POST /futures/usdt/orders
```

### 2.6 全部平仓

```python
if is_dual:
    for auto_sz in ("close_long", "close_short"):
        body = {
            "contract": "BTC_USDT",
            "size": -1 if auto_sz == "close_long" else 1,
            "price": "0", "tif": "ioc",
            "reduce_only": True,
            "auto_size": auto_sz, "close": True
        }
        # POST /futures/usdt/orders
else:
    positions = api_get_positions(api_key, api_secret)
    for p in positions:
        if p["contract"] == "BTC_USDT":
            sz = int(p["size"])
            body = {"contract": "BTC_USDT", "size": -sz, "price": "0", "tif": "ioc"}
            # POST /futures/usdt/orders
```

### 2.7 撤单操作

```python
# 撤销单个挂单
# DELETE /futures/usdt/orders/{order_id}

# 撤销某品种所有挂单
# DELETE /futures/usdt/orders?contract=BTC_USDT
```

---

## 三、三单提交顺序

1. 先提交入场订单 → 获取 order_id
2. 再提交止盈触发订单 → 获取 order_id
3. 最后提交止损触发订单 → 获取 order_id
4. 更新 memory/market_state.md（记录三单ID）
5. 写入 logs/orders/（先执行后写入）

---

## 四、价格精度与最小下单量

| 品种 | 价格精度 | 最小下单量 | 典型止损距离 |
|------|---------|-----------|-------------|
| BTC_USDT | 0.1 | 0.001 | $200-500 |
| ETH_USDT | 0.01 | 0.01 | $15-30 |
| SOL_USDT | 0.001 | 1 | $0.5-1.5 |
| XAU_USDT | 0.01 | 0.01 | $3-8 |
| XAG_USDT | 0.001 | 1 | $0.2-0.5 |

查询方法：GET /futures/usdt/contracts/{contract}

---

## 五、错误处理

| 错误码 | 含义 | 处理方式 |
|--------|------|---------|
| insufficient_balance | 余额不足 | 减仓或放弃 |
| price_precision | 价格精度错误 | 按精度四舍五入 |
| size_precision | 数量精度错误 | 按精度四舍五入 |
| order_min_size | 低于最小下单量 | 放弃或增加数量 |
| position_mode_mismatch | 持仓模式不匹配 | 检查reduce_only/auto_size |
