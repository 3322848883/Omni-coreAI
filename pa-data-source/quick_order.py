#!/usr/bin/env python3
"""
quick_order.py - Gate.io 期货快速下单工具

功能:
  查询: --positions, --orders, --price-orders, --balance, --position-mode
  开仓: --side long/short, --type limit/market/post_only/ioc/fok, --price, --size
  平仓: --close, --close-long, --close-short, --close-all, --close-long-all, --close-short-all
  价格触发订单(计划委托/止盈止损): --trigger-price, --trigger-type market/limit, --trigger-limit-price
  撤单: --cancel-order, --cancel-all, --cancel-price-order, --cancel-all-price-orders
  确认: --confirm 跳过确认
  分仓: --margin-mode cross/isolated
  标签: --order-label eval/drive 区分评测订单和开车订单

复用 kline_watcher.py 中的签名函数
配置从 watchlist.yaml 读取

持仓模式说明:
  单向持仓(single): 每个合约只能持有一个方向的仓位, 开反向单会自动平仓并反向开仓
  双向持仓(dual):   可同时持有多仓和空仓, 开平仓需明确指定方向
  使用 --position-mode 查询当前模式

单向持仓常用命令:

  1. 查询
     python quick_order.py --positions              # 查询持仓
     python quick_order.py --orders                 # 查询普通挂单
     python quick_order.py --price-orders           # 查询计划委托(止盈止损)
     python quick_order.py --balance                # 查询账户余额

  2. 限价开仓
     python quick_order.py -s BTC_USDT --type limit --price 61000 --side long --size 1 --confirm

  3. 市价平仓
     python quick_order.py -s BTC_USDT --close --confirm

  4. 止盈单 (价格>=触发价时市价平仓)
     python quick_order.py -s BTC_USDT --trigger-price 62000 --trigger-type market --side short --size 1 --close-trigger --trigger-rule 1 --confirm

  5. 止损单 (价格<=触发价时市价平仓)
     python quick_order.py -s BTC_USDT --trigger-price 60000 --trigger-type market --side short --size 1 --close-trigger --trigger-rule 2 --confirm

  6. 撤单
     python quick_order.py --cancel-order ORDER_ID              # 撤销普通挂单
     python quick_order.py --cancel-all -s BTC_USDT             # 撤销全部普通挂单
     python quick_order.py --cancel-price-order ORDER_ID        # 撤销止盈/止损单
     python quick_order.py --cancel-all-price-orders -s BTC_USDT # 撤销全部止盈/止损单

双向持仓差异说明:
  - 平仓需指定方向: --close-long 或 --close-short
  - 止盈止损的 --side 需与持仓方向相反
  - 例: 持有多仓时, 止盈止损 --side 应为 short

参数说明:
  -s, --symbol          合约名称, 如 BTC_USDT
  --side long/short     方向: long=做多, short=做空
  --type limit/market   订单类型: limit=限价, market=市价
  --price               限价单价格
  --size                数量(正数)
  --trigger-price       触发价格(用于止盈止损)
  --trigger-type        触发后类型: market=市价, limit=限价
  --trigger-rule 1/2    触发规则: 1=价格>=触发价, 2=价格<=触发价
  --close-trigger       表示平仓触发单(止盈止损必须加)
  --order-label          订单标签: eval=评测订单, drive=开车订单
  --confirm             跳过确认直接执行
"""

import os
import sys
import json
import time
import hmac
import hashlib
import urllib.request
import urllib.error
import argparse

try:
    import yaml
except ImportError:
    print("错误: 需要安装 PyYAML: pip install pyyaml")
    sys.exit(1)

# ── 常量 ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(SCRIPT_DIR, "watchlist.yaml")

GATE_REST = os.environ.get("GATE_API_BASE", "https://api.gateio.ws")
FUTURES_API = "/api/v4/futures/usdt"

SYMBOL_MAP = {
    "BTC": "BTC_USDT",
    "ETH": "ETH_USDT",
    "SOL": "SOL_USDT",
    "XAU": "XAU_USDT",
    "XAG": "XAG_USDT",
    "AU": "XAU_USDT",
    "AG": "XAG_USDT",
}

ORDER_TYPE_DESC = {
    "market": "市价",
    "limit": "限价",
    "post_only": "仅Maker(POST_ONLY)",
    "ioc": "立即成交或取消(IOC)",
    "fok": "全部成交或取消(FOK)",
}

ORDER_TIF = {
    "limit": "gtc",
    "post_only": "poc",
    "ioc": "ioc",
    "fok": "fok",
}


# ── 符号解析 (复用自 kline_watcher.py) ────────────────────
def resolve_symbol(name):
    """解析交易对名称: BTC -> BTC_USDT"""
    upper = name.upper()
    if upper in SYMBOL_MAP:
        return SYMBOL_MAP[upper]
    if "_USDT" in upper:
        base = upper.split("_")[0]
        if base in SYMBOL_MAP:
            return SYMBOL_MAP[base]
        return upper
    return upper + "_USDT"


def normalize_contract_size(value, label="size"):
    """Gate futures size is an integer contract count."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        print(f"错误: {label} 必须是正整数")
        sys.exit(1)

    if num <= 0 or not num.is_integer():
        print(f"错误: {label} 必须是正整数合约张数")
        sys.exit(1)
    return int(num)


def apply_order_type(body, order_type, price_str):
    """Fill price/tif fields for normal futures orders."""
    if order_type == "market":
        body["price"] = "0"
        body["tif"] = "ioc"
        return

    if not price_str:
        label = ORDER_TYPE_DESC.get(order_type, order_type)
        print(f"错误: {label} 单必须指定 --price")
        sys.exit(1)

    body["price"] = str(price_str)
    body["tif"] = ORDER_TIF[order_type]


def apply_order_label(body, order_label):
    if order_label:
        body["text"] = f"t-{order_label}"


def is_dual_position_mode(position_mode):
    return position_mode in ("dual", "dual_long_short", "dual_plus")


def active_contract_positions(positions, contract):
    return [
        p for p in positions
        if p.get("contract") == contract and int(p.get("size", "0") or 0) != 0
    ]


def is_long_position(position):
    mode = str(position.get("mode", "")).lower()
    size = int(position.get("size", "0") or 0)
    return size > 0 or mode.endswith("long")


def is_short_position(position):
    mode = str(position.get("mode", "")).lower()
    size = int(position.get("size", "0") or 0)
    return size < 0 or mode.endswith("short")


def total_position_size(positions):
    return sum(abs(int(p.get("size", "0") or 0)) for p in positions)


# ── Gate.io API 签名 (复用并增强自 kline_watcher.py) ──────
def gate_sign(channel, event, timestamp, secret):
    """WebSocket 鉴权签名: HMAC-SHA512"""
    message = f"channel={channel}&event={event}&time={timestamp}"
    return hmac.new(
        secret.encode("utf8"), message.encode("utf8"), hashlib.sha512
    ).hexdigest()


def rest_signed_request(method, path, query_string="", body=None,
                        api_key="", api_secret=""):
    """
    Gate APIv4 REST 签名请求 (增强版, 支持 POST body)

    复用自 kline_watcher.py 的签名逻辑, 增加 body 参数:
      - body=None  => GET/DELETE 无 body (与原函数行为一致)
      - body=dict  => POST/PUT 带 JSON body
    """
    url = f"{GATE_REST}{path}"
    if query_string:
        url += f"?{query_string}"

    if body is not None:
        body_str = json.dumps(body, separators=(",", ":"))
    else:
        body_str = ""

    body_hash = hashlib.sha512(body_str.encode("utf8")).hexdigest()
    timestamp = str(int(time.time()))
    sign_str = f"{method}\n{path}\n{query_string}\n{body_hash}\n{timestamp}"
    sign = hmac.new(
        api_secret.encode("utf8"), sign_str.encode("utf8"), hashlib.sha512
    ).hexdigest()

    headers = {
        "KEY": api_key,
        "SIGN": sign,
        "Timestamp": timestamp,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "quick-order/1.0",
    }

    data = body_str.encode("utf8") if body_str else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf8")
            if not raw.strip():
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf8")
        except Exception:
            pass
        # 尝试解析错误 JSON
        try:
            err_json = json.loads(err_body)
            msg = err_json.get("label", "") or err_body
        except (json.JSONDecodeError, AttributeError):
            msg = err_body or str(e)
        print(f"  [API 错误] {e.code} {path}: {msg}")
        return None
    except Exception as e:
        print(f"  [请求异常] {path}: {e}")
        return None


# ── 配置读取 ──────────────────────────────────────────────
def load_config(env="live", config_path=None):
    """按环境读取 api_key / api_secret（实盘/模拟盘独立配置，不混用）。

    live    → watchlist.yaml（密钥：GATE_API_KEY/GATE_API_SECRET 或文件 api_key/api_secret）
    testnet → watchlist_testnet.yaml（密钥：GATE_TESTNET_API_KEY/GATE_TESTNET_API_SECRET 或文件 api_key/api_secret）
    """
    global GATE_REST
    if config_path is None:
        config_path = "watchlist_testnet.yaml" if env == "testnet" else "watchlist.yaml"
    path = os.path.join(SCRIPT_DIR, config_path)
    if not os.path.exists(path):
        print(f"错误: 找不到配置文件 {path}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # REST 地址按环境选择（不依赖全局 GATE_API_BASE，避免双实例环境变量混用）
    GATE_REST = "https://api-testnet.gateapi.io" if env == "testnet" else "https://api.gateio.ws"

    # 密钥按环境完全独立：实盘/模拟盘密钥分离，防误下单
    if env == "testnet":
        api_key = str(os.environ.get("GATE_TESTNET_API_KEY", config.get("api_key", "") or "")).strip()
        api_secret = str(os.environ.get("GATE_TESTNET_API_SECRET", config.get("api_secret", "") or "")).strip()
        if not api_key or not api_secret:
            print("错误: 模拟盘（testnet）未配置 api_key 或 api_secret")
            print("请设置环境变量 GATE_TESTNET_API_KEY / GATE_TESTNET_API_SECRET")
            print(f"或在 {config_path} 中添加:")
            print('  api_key: "your_testnet_api_key"')
            print('  api_secret: "your_testnet_api_secret"')
            print("注意: 模拟盘必须使用测试网密钥，不要复用实盘密钥")
            sys.exit(1)
    else:
        api_key = str(os.environ.get("GATE_API_KEY", config.get("api_key", "") or "")).strip()
        api_secret = str(os.environ.get("GATE_API_SECRET", config.get("api_secret", "") or "")).strip()
        if not api_key or not api_secret:
            print("错误: 实盘（live）未配置 api_key 或 api_secret")
            print("请设置环境变量 GATE_API_KEY / GATE_API_SECRET")
            print(f"或在 {config_path} 中添加:")
            print('  api_key: "your_api_key"')
            print('  api_secret: "your_api_secret"')
            sys.exit(1)
    return api_key, api_secret


def env_label(env):
    """环境显示标识：模拟盘/实盘"""
    return "模拟盘" if env == "testnet" else "实盘"


# ── API 查询函数 ──────────────────────────────────────────
def api_get_account(api_key, api_secret):
    """GET /api/v4/futures/usdt/accounts"""
    return rest_signed_request(
        "GET", f"{FUTURES_API}/accounts", "", None, api_key, api_secret
    )


def api_get_positions(api_key, api_secret):
    """GET /api/v4/futures/usdt/positions"""
    result = rest_signed_request(
        "GET", f"{FUTURES_API}/positions", "", None, api_key, api_secret
    )
    return result if isinstance(result, list) else []


def api_get_open_orders(api_key, api_secret):
    """GET /api/v4/futures/usdt/orders?status=open"""
    result = rest_signed_request(
        "GET", f"{FUTURES_API}/orders", "status=open", None, api_key, api_secret
    )
    return result if isinstance(result, list) else []


def api_get_position_mode(api_key, api_secret):
    """获取持仓模式: single / dual / dual_long_short"""
    account = api_get_account(api_key, api_secret)
    if account and isinstance(account, dict):
        return account.get("position_mode", "single")
    return "single"


# ── 查询命令 ──────────────────────────────────────────────
def cmd_positions(api_key, api_secret):
    """--positions: 查询持仓"""
    mode = api_get_position_mode(api_key, api_secret)
    positions = api_get_positions(api_key, api_secret)
    active = [p for p in positions if str(p.get("size", "0")) != "0"]

    print()
    print("=" * 72)
    print(f"  持仓模式: {mode}")
    print("=" * 72)

    if not active:
        print("  (无持仓)")
    else:
        header = (f"  {'合约':<12} {'方向':<6} {'数量':>8} {'入场价':>12}"
                  f" {'标记价':>12} {'浮盈':>12} {'杠杆':>5} {'保证金模式':<10}")
        print(header)
        print("  " + "-" * 68)
        for pos in active:
            contract = pos.get("contract", "")
            size = int(pos.get("size", "0"))
            direction = "多" if size > 0 else "空"
            entry = pos.get("entry_price", "0")
            mark = pos.get("mark_price", "0")
            pnl = pos.get("unrealised_pnl", "0")
            lever = pos.get("leverage", "0")
            margin_mode = pos.get("pos_margin_mode", "-")
            print(f"  {contract:<12} {direction:<6} {size:>8} {entry:>12}"
                  f" {mark:>12} {pnl:>12} {lever:>4}x {margin_mode:<10}")

    print("=" * 72)
    print()


def cmd_orders(api_key, api_secret):
    """--orders: 查询挂单"""
    orders = api_get_open_orders(api_key, api_secret)

    print()
    print("=" * 72)
    if not orders:
        print("  (无挂单)")
    else:
        print(f"  共 {len(orders)} 个挂单:")
        header = (f"  {'订单ID':<14} {'合约':<12} {'方向':<6}"
                  f" {'价格':>12} {'数量':>8} {'已成交':>8} {'类型':<8} {'标签':<10}")
        print(header)
        print("  " + "-" * 80)
        for o in orders:
            oid = str(o.get("id", ""))[:12]
            contract = o.get("contract", "")
            size = int(o.get("size", "0"))
            direction = "多" if size > 0 else "空"
            price = o.get("price", "0")
            amount = abs(size)
            left = abs(float(o.get("left", "0")))
            filled = amount - left if left <= amount else 0
            tif = o.get("tif", "")
            tif_label = {
                "gtc": "限价", "ioc": "IOC",
                "fok": "FOK", "poc": "仅Maker",
            }.get(tif, tif)
            text = o.get("text", "")
            print(f"  {oid:<14} {contract:<12} {direction:<6}"
                  f" {price:>12} {amount:>8} {filled:>8.0f} {tif_label:<8} {text:<10}")

    print("=" * 72)
    print()


def cmd_price_orders(api_key, api_secret):
    """--price-orders: 查询计划委托 (止盈止损)"""
    path = "/api/v4/futures/usdt/price_orders"
    qs = "status=open"
    raw = rest_signed_request("GET", path, qs, None, api_key, api_secret)
    if raw is None:
        raw = []
    orders = raw if isinstance(raw, list) else []

    print()
    print("=" * 72)
    if not orders:
        print("  (无计划委托)")
    else:
        print(f"  共 {len(orders)} 个计划委托:")
        for o in orders:
            oid = str(o.get("id", ""))[:16]
            initial = o.get("initial", {})
            trigger = o.get("trigger", {})
            contract = initial.get("contract", "")
            size = int(initial.get("size", "0"))
            direction = "多" if size > 0 else "空" if size < 0 else "全平"
            price = initial.get("price", "0")
            tif = initial.get("tif", "")
            trigger_price = trigger.get("price", "")
            rule = trigger.get("rule", 0)
            rule_label = ">=" if rule == 1 else "<="
            price_type = trigger.get("price_type", 0)
            pt_label = ["最新价", "标记价", "指数价"][price_type] if price_type in (0, 1, 2) else str(price_type)
            reduce_only = initial.get(
                "reduce_only", initial.get("is_reduce_only", False)
            )
            auto_size = initial.get("auto_size", "")
            close_tag = " [平仓]" if reduce_only else ""
            text = initial.get("text", "")
            print(f"  ID: {oid}")
            print(f"    合约: {contract}  方向: {direction}  数量: {abs(size)}{close_tag}")
            if text:
                print(f"    标签: {text}")
            print(f"    触发条件: {pt_label} {rule_label} {trigger_price}")
            print(f"    触发后: {'市价' if price == '0' else f'限价({price})'}")
            print()
    print("=" * 72)
    print()


def cmd_balance(api_key, api_secret):
    """--balance: 查询余额"""
    account = api_get_account(api_key, api_secret)

    print()
    print("=" * 72)
    if not account or not isinstance(account, dict):
        print("  获取账户信息失败")
    else:
        print(f"  持仓模式:   {account.get('position_mode', 'N/A')}")
        print(f"  总资产:     {account.get('total', 'N/A')} USDT")
        print(f"  可用余额:   {account.get('available', 'N/A')} USDT")
        print(f"  持仓保证金: {account.get('position_margin', 'N/A')} USDT")
        print(f"  挂单保证金: {account.get('order_margin', 'N/A')} USDT")
        print(f"  未实现盈亏: {account.get('unrealised_pnl', 'N/A')} USDT")
        print(f"  杠杆倍数:   {account.get('leverage', 'N/A')}x")

    print("=" * 72)
    print()


def cmd_position_mode(api_key, api_secret):
    """--position-mode: 查询持仓模式"""
    mode = api_get_position_mode(api_key, api_secret)
    desc_map = {
        "single": "单向持仓 -- 每个合约只能持有一个方向的仓位",
        "dual": "双向持仓 -- 可同时持有多头和空头仓位",
        "dual_long_short": "双向持仓 -- 可同时持有多头和空头仓位",
        "dual_plus": "简易分仓模式 -- 可区分全仓/逐仓的双向持仓",
    }
    desc = desc_map.get(mode, f"未知模式: {mode}")

    print()
    print("=" * 72)
    print(f"  当前持仓模式: {mode}")
    print(f"  说明:         {desc}")
    print("=" * 72)
    print()


# ── 订单确认 ──────────────────────────────────────────────
def confirm_order(description_lines, skip_confirm):
    """显示订单详情, 要求用户确认"""
    print()
    print("  === 订单详情 ===")
    for line in description_lines:
        print(f"  {line}")
    print()

    if skip_confirm:
        print("  [--confirm 已跳过确认]")
        return True

    try:
        ans = input("  确认执行？(y/N): ").strip().lower()
        if ans in ("y", "yes"):
            return True
        print("  已取消操作")
        return False
    except (EOFError, KeyboardInterrupt):
        print("\n  已取消操作")
        return False


# ── 开仓 ──────────────────────────────────────────────────
def cmd_open(api_key, api_secret, symbol, side, order_type, price_str,
             size, margin_mode, skip_confirm, dry_run=False,
             order_label=None):
    """
    开仓操作

    持仓模式差异:
      - single: 买入做多 / 卖出做空, 无需额外字段
      - dual:   买入做多 / 卖出做空, 无需额外字段 (close=false)
      - dual+:  同 dual, 额外可指定 pos_margin_mode
    """
    contract = resolve_symbol(symbol)
    position_mode = api_get_position_mode(api_key, api_secret)

    # 构建 body
    order_qty = normalize_contract_size(size)
    order_size = -order_qty if side == "short" else order_qty

    body = {"contract": contract, "size": order_size}
    apply_order_label(body, order_label)

    # 订单类型 -> price + tif
    apply_order_type(body, order_type, price_str)

    # 分仓模式: 指定 pos_margin_mode
    if margin_mode and is_dual_position_mode(position_mode):
        body["pos_margin_mode"] = margin_mode

    # 确认描述
    side_label = "做多 (买入)" if side == "long" else "做空 (卖出)"
    desc = [
        f"操作:     开仓 - {side_label}",
        f"合约:     {contract}",
        f"类型:     {ORDER_TYPE_DESC.get(order_type, order_type)}",
        f"数量:     {order_qty}",
    ]
    if price_str:
        desc.append(f"价格:     {price_str}")
    if margin_mode:
        desc.append(f"保证金:   {margin_mode}")
    if order_label:
        desc.append(f"标签:     {order_label}")
    desc.append(f"持仓模式: {position_mode}")

    if not confirm_order(desc, skip_confirm):
        return

    if dry_run:
        print("\n  [DRY RUN] 不发送订单，请求体:")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return

    print("\n  提交订单中...")
    submit_order(api_key, api_secret, body, contract, "开仓", order_label)


# ── 平仓 ──────────────────────────────────────────────────
def cmd_close(api_key, api_secret, symbol, close_type, size,
              order_type, price_str, margin_mode, skip_confirm,
              dry_run=False, order_label=None):
    """
    平仓操作

    close_type 映射:
      auto       -- 单向模式, 自动识别方向平仓
      long       -- 平多仓 (双向/分仓)
      short      -- 平空仓 (双向/分仓)
      all        -- 全平
      long_all   -- 全平多仓 (双向/分仓)
      short_all  -- 全平空仓 (双向/分仓)
    """
    contract = resolve_symbol(symbol)
    position_mode = api_get_position_mode(api_key, api_secret)
    is_dual = is_dual_position_mode(position_mode)

    # 获取当前持仓
    positions = api_get_positions(api_key, api_secret)
    active_positions = active_contract_positions(positions, contract)

    if not active_positions:
        print(f"\n  {contract} 当前无持仓\n")
        return

    long_positions = [p for p in active_positions if is_long_position(p)]
    short_positions = [p for p in active_positions if is_short_position(p)]
    target = active_positions[0]
    pos_size = int(target.get("size", "0") or 0)

    # ── close-type: auto ──
    if close_type == "auto":
        if is_dual:
            # 双向模式: auto 等同于 all
            _close_all(api_key, api_secret, contract, position_mode,
                       margin_mode, skip_confirm, dry_run, order_label)
            return
        # 单向模式: 根据持仓方向决定卖出/买入
        if pos_size > 0:
            _close_direct(api_key, api_secret, contract, position_mode,
                          "long", normalize_contract_size(size) if size else pos_size,
                          order_type, price_str, margin_mode, skip_confirm,
                          dry_run, order_label)
        elif pos_size < 0:
            _close_direct(api_key, api_secret, contract, position_mode,
                          "short", normalize_contract_size(size) if size else abs(pos_size),
                          order_type, price_str, margin_mode, skip_confirm,
                          dry_run, order_label)
        else:
            print(f"\n  {contract} 无持仓\n")
        return

    # ── close-type: all ──
    if close_type == "all":
        _close_all(api_key, api_secret, contract, position_mode,
                   margin_mode, skip_confirm, dry_run, order_label)
        return

    # ── close-type: long_all ──
    if close_type == "long_all":
        if not long_positions:
            print(f"\n  {contract} 无多头持仓\n")
            return
        _close_all_one_side(api_key, api_secret, contract, position_mode,
                            "close_long", margin_mode, skip_confirm,
                            dry_run, order_label)
        return

    # ── close-type: short_all ──
    if close_type == "short_all":
        if not short_positions:
            print(f"\n  {contract} 无空头持仓\n")
            return
        _close_all_one_side(api_key, api_secret, contract, position_mode,
                            "close_short", margin_mode, skip_confirm,
                            dry_run, order_label)
        return

    # ── close-type: long / short (指定数量) ──
    if close_type == "long":
        close_size = normalize_contract_size(size) if size else total_position_size(long_positions)
        _close_direct(api_key, api_secret, contract, position_mode,
                      "long", close_size, order_type, price_str,
                      margin_mode, skip_confirm, dry_run, order_label)
    elif close_type == "short":
        close_size = normalize_contract_size(size) if size else total_position_size(short_positions)
        _close_direct(api_key, api_secret, contract, position_mode,
                      "short", close_size, order_type, price_str,
                      margin_mode, skip_confirm, dry_run, order_label)


def _close_direct(api_key, api_secret, contract, position_mode,
                  side, close_size, order_type, price_str,
                  margin_mode, skip_confirm, dry_run=False,
                  order_label=None):
    """
    指定数量平仓

    单向模式:  平多 -> 卖出 (size<0), 平空 -> 买入 (size>0)
    双向模式:  同上 + reduce_only=true
    """
    is_dual = is_dual_position_mode(position_mode)

    if close_size <= 0:
        print(f"\n  {contract} 无仓位可平\n")
        return

    # 构建 size: 平多=卖出(负数), 平空=买入(正数)
    if side == "long":
        order_size = -abs(close_size)
        label = "平多"
    else:
        order_size = abs(close_size)
        label = "平空"

    body = {"contract": contract, "size": order_size}
    apply_order_label(body, order_label)
    apply_order_type(body, order_type, price_str)

    body["reduce_only"] = True

    if margin_mode and is_dual:
        body["pos_margin_mode"] = margin_mode

    type_label = "市价" if order_type == "market" else f"限价({price_str})"
    desc = [
        f"操作:     {label}",
        f"合约:     {contract}",
        f"类型:     {type_label}",
        f"数量:     {close_size}",
        f"持仓模式: {position_mode}",
    ]
    if margin_mode:
        desc.append(f"保证金:   {margin_mode}")
    if order_label:
        desc.append(f"标签:     {order_label}")

    if not confirm_order(desc, skip_confirm):
        return

    if dry_run:
        print(f"\n  [DRY RUN] 不发送{label}订单，请求体:")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return

    print(f"\n  提交 {label} 订单中...")
    submit_order(api_key, api_secret, body, contract, label, order_label)


def _close_all(api_key, api_secret, contract, position_mode,
               margin_mode, skip_confirm, dry_run=False,
               order_label=None):
    """全平: 单向模式直接市价平; 双向模式分多空两次平"""
    is_dual = is_dual_position_mode(position_mode)
    close_sides = []
    if is_dual:
        positions = active_contract_positions(
            api_get_positions(api_key, api_secret), contract
        )
        if any(is_long_position(p) for p in positions):
            close_sides.append("close_long")
        if any(is_short_position(p) for p in positions):
            close_sides.append("close_short")

        if not close_sides:
            print(f"\n  {contract} 当前无持仓\n")
            return

    desc = [
        f"操作:     市价全平",
        f"合约:     {contract}",
        f"持仓模式: {position_mode}",
    ]
    if close_sides:
        side_label = "、".join(
            "多仓" if side == "close_long" else "空仓"
            for side in close_sides
        )
        desc.append(f"方向:     {side_label}")
    if margin_mode:
        desc.append(f"保证金:   {margin_mode}")
    if order_label:
        desc.append(f"标签:     {order_label}")

    if not confirm_order(desc, skip_confirm):
        return

    if is_dual:
        # 双向: 只平当前确实存在的方向
        for auto_sz in close_sides:
            _submit_auto_close(api_key, api_secret, contract, auto_sz,
                               margin_mode, dry_run, order_label)
    else:
        # 单向: 查询方向后直接平
        positions = api_get_positions(api_key, api_secret)
        for p in positions:
            if p.get("contract") == contract:
                sz = int(p.get("size", "0"))
                if sz == 0:
                    continue
                body = {
                    "contract": contract,
                    "size": 0,
                    "price": "0",
                    "tif": "ioc",
                    "close": True,
                }
                apply_order_label(body, order_label)
                print("\n  提交单向全平订单 (size=0, close=true)...")
                if dry_run:
                    print("  [DRY RUN] 不发送全平订单，请求体:")
                    print(json.dumps(body, ensure_ascii=False, indent=2))
                    continue
                submit_order(api_key, api_secret, body, contract,
                             "全平", order_label)
        print("  全平订单已提交")


def _close_all_one_side(api_key, api_secret, contract, position_mode,
                         auto_size_val, margin_mode, skip_confirm,
                         dry_run=False, order_label=None):
    """全平单边 (双向模式): 使用 auto_size 字段"""
    desc = [
        f"操作:     市价全平{'多' if auto_size_val == 'close_long' else '空'}仓",
        f"合约:     {contract}",
        f"持仓模式: {position_mode}",
    ]
    if margin_mode:
        desc.append(f"保证金:   {margin_mode}")
    if order_label:
        desc.append(f"标签:     {order_label}")

    if not confirm_order(desc, skip_confirm):
        return

    _submit_auto_close(api_key, api_secret, contract, auto_size_val,
                       margin_mode, dry_run, order_label)


def _submit_auto_close(api_key, api_secret, contract, auto_size_val,
                       margin_mode, dry_run=False, order_label=None):
    """提交 auto_size 平仓订单"""
    body = {
        "contract": contract,
        "size": 0,
        "price": "0",
        "tif": "ioc",
        "reduce_only": True,
        "auto_size": auto_size_val,
    }
    apply_order_label(body, order_label)
    if margin_mode:
        body["pos_margin_mode"] = margin_mode

    label = "平多" if auto_size_val == "close_long" else "平空"
    print(f"\n  提交{label}全平订单 (auto_size={auto_size_val})...")
    if dry_run:
        print("  [DRY RUN] 不发送全平订单，请求体:")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return
    submit_order(api_key, api_secret, body, contract, label, order_label)


# ── 价格触发订单 ──────────────────────────────────────────
def cmd_trigger_order(api_key, api_secret, symbol, side, size,
                      trigger_price, trigger_type, trigger_limit_price,
                      trigger_price_type, trigger_expiration,
                      trigger_rule_override, is_close, margin_mode, skip_confirm,
                      dry_run=False, order_label=None, env="live"):
    """
    价格触发订单 (计划委托 / 止盈止损)

    API: POST /api/v4/futures/usdt/price_orders
    Body 结构:
      {
        "initial": { contract, size, price, tif, ... },
        "trigger": { strategy_type, price_type, price, rule, expiration }
      }
    """
    contract = resolve_symbol(symbol)
    position_mode = api_get_position_mode(api_key, api_secret)
    is_dual = is_dual_position_mode(position_mode)

    order_qty = normalize_contract_size(size)
    order_size = -order_qty if side == "short" else order_qty

    if is_close and trigger_type == "market" and not dry_run:
        print("错误: 真实平仓触发单禁止使用 --trigger-type market")
        print("      请使用 --trigger-type limit 并显式指定 --trigger-limit-price。")
        sys.exit(1)

    initial = {"contract": contract, "size": order_size}
    apply_order_label(initial, order_label)
    if trigger_type == "market":
        initial["price"] = "0"
        initial["tif"] = "ioc"
    elif trigger_type == "limit":
        if not trigger_limit_price:
            print("错误: 触发类型 limit 必须指定 --trigger-limit-price")
            sys.exit(1)
        initial["price"] = str(trigger_limit_price)
        initial["tif"] = "gtc"

    if is_close:
        initial["reduce_only"] = True

    if is_close and trigger_rule_override is None:
        print("错误: 平仓触发单必须显式指定 --trigger-rule 1 或 2")
        print("      止盈通常按价格相对触发价判断，止损也一样，不能只根据 --side 自动推导。")
        sys.exit(1)

    trigger_rule = trigger_rule_override if trigger_rule_override else (1 if side == "long" else 2)

    price_type_map = {"latest": 0, "mark": 1, "index": 2}
    price_type_int = price_type_map.get(trigger_price_type, 0)

    trigger = {
        "strategy_type": 0,
        "price_type": price_type_int,
        "price": str(trigger_price),
        "rule": trigger_rule,
    }
    if trigger_expiration:
        if env == "testnet":
            print("提示: 测试网(模拟盘)不支持价格触发订单的过期时间参数 (--trigger-expiration)，")
            print("      已忽略该参数。实盘支持该参数。")
        else:
            trigger["expiration"] = trigger_expiration

    body = {"initial": initial, "trigger": trigger}
    if margin_mode:
        body["pos_margin_mode"] = margin_mode

    side_label = "做多" if side == "long" else "做空"
    type_label = "市价" if trigger_type == "market" else f"限价({trigger_limit_price})"
    close_label = " (平仓)" if is_close else ""
    desc = [
        f"操作:     价格触发订单{close_label}",
        f"合约:     {contract}",
        f"方向:     {side_label}",
        f"触发价:   {trigger_price}",
        f"触发类型: {type_label}",
        f"数量:     {order_qty}",
        f"持仓模式: {position_mode}",
    ]
    if margin_mode:
        desc.append(f"保证金:   {margin_mode}")
    if order_label:
        desc.append(f"标签:     {order_label}")

    if not confirm_order(desc, skip_confirm):
        return

    if dry_run:
        print("\n  [DRY RUN] 不发送价格触发订单，请求体:")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return

    print("\n  提交价格触发订单中...")
    submit_order(api_key, api_secret, body, contract, "价格触发订单",
                 order_label, price_order=True)


# ── 撤单 ──────────────────────────────────────────────────
def cmd_cancel_order(api_key, api_secret, order_id, skip_confirm, dry_run=False):
    """--cancel-order ORDER_ID: 撤销指定挂单"""
    path = f"{FUTURES_API}/orders/{order_id}"
    desc = [
        "操作:     撤销普通挂单",
        f"订单ID:   {order_id}",
        f"请求:     DELETE {path}",
    ]
    if not confirm_order(desc, skip_confirm):
        return

    if dry_run:
        print("\n  [DRY RUN] 不发送撤单请求:")
        print(f"  DELETE {path}")
        return

    result = rest_signed_request("DELETE", path, "", None, api_key, api_secret)
    if result is not None:
        # Gate.io 撤单成功返回被撤订单信息
        label = result.get("id", order_id) if isinstance(result, dict) else order_id
        print(f"\n  订单 {label} 已撤销\n")
    else:
        print(f"\n  订单 {order_id} 撤销失败\n")


def cmd_cancel_all(api_key, api_secret, symbol=None,
                   skip_confirm=False, dry_run=False):
    """--cancel-all [--symbol]: 撤销所有挂单"""
    contract = resolve_symbol(symbol) if symbol else None

    if contract:
        path = f"{FUTURES_API}/orders"
        qs = f"contract={contract}"
    else:
        path = f"{FUTURES_API}/orders"
        qs = ""

    target = contract or "全部合约"
    req = f"DELETE {path}" + (f"?{qs}" if qs else "")
    desc = [
        "操作:     批量撤销普通挂单",
        f"范围:     {target}",
        f"请求:     {req}",
    ]
    if not confirm_order(desc, skip_confirm):
        return

    if dry_run:
        print("\n  [DRY RUN] 不发送批量撤单请求:")
        print(f"  {req}")
        return

    result = rest_signed_request("DELETE", path, qs, None, api_key, api_secret)
    if result is not None:
        print(f"\n  {target} 所有挂单已撤销\n")
    else:
        print(f"\n  {target} 撤销所有挂单失败\n")


def cmd_cancel_price_order(api_key, api_secret, order_id,
                           skip_confirm, dry_run=False):
    """--cancel-price-order ORDER_ID: 撤销指定计划委托 (止盈止损)"""
    path = f"{FUTURES_API}/price_orders/{order_id}"
    desc = [
        "操作:     撤销计划委托",
        f"订单ID:   {order_id}",
        f"请求:     DELETE {path}",
    ]
    if not confirm_order(desc, skip_confirm):
        return

    if dry_run:
        print("\n  [DRY RUN] 不发送计划委托撤单请求:")
        print(f"  DELETE {path}")
        return

    result = rest_signed_request("DELETE", path, "", None, api_key, api_secret)
    if result is not None:
        label = result.get("id", order_id) if isinstance(result, dict) else order_id
        print(f"\n  计划委托 {label} 已撤销\n")
    else:
        print(f"\n  计划委托 {order_id} 撤销失败\n")


def cmd_cancel_all_price_orders(api_key, api_secret, symbol=None,
                                skip_confirm=False, dry_run=False):
    """--cancel-all-price-orders [--symbol]: 撤销所有计划委托 (止盈止损)"""
    contract = resolve_symbol(symbol) if symbol else None

    if contract:
        path = f"{FUTURES_API}/price_orders"
        qs = f"contract={contract}"
    else:
        path = f"{FUTURES_API}/price_orders"
        qs = ""

    target = contract or "全部合约"
    req = f"DELETE {path}" + (f"?{qs}" if qs else "")
    desc = [
        "操作:     批量撤销计划委托",
        f"范围:     {target}",
        f"请求:     {req}",
    ]
    if not confirm_order(desc, skip_confirm):
        return

    if dry_run:
        print("\n  [DRY RUN] 不发送批量计划委托撤单请求:")
        print(f"  {req}")
        return

    result = rest_signed_request("DELETE", path, qs, None, api_key, api_secret)
    if result is not None:
        print(f"\n  {target} 所有计划委托已撤销\n")
    else:
        print(f"\n  {target} 撤销所有计划委托失败\n")


# ── 订单结果输出 ──────────────────────────────────────────
def _print_order_result(result, action_label):
    """打印下单结果"""
    if result and isinstance(result, dict):
        oid = result.get("id", "N/A")
        status = result.get("status", "N/A")
        initial = result.get("initial", {})
        contract = result.get("contract", "") or initial.get("contract", "")
        size = result.get("size", "0")
        if size == "0" and initial:
            size = initial.get("size", initial.get("amount", "0"))
        price = result.get("price", "0")
        if price == "0" and initial:
            price = initial.get("price", "0")
        text = result.get("text", "") or initial.get("text", "")
        label = f" 标签={text}" if text else ""
        print(f"  {action_label}成功: ID={oid} 合约={contract}"
              f" 数量={size} 价格={price} 状态={status}{label}")
    else:
        print(f"  {action_label}失败或结果未知 (请检查上方错误与对账信息)")


def reconcile_order_by_label(api_key, api_secret, contract, order_label,
                             price_order=False):
    """下单请求结果未知（网络超时等）时，按订单标签对账：
    查询该合约挂单与最近成交，确认订单是否实际已提交，防止重复下单。
    依赖下单时 --order-label 写入的 text 字段 (t-<label>)。
    返回匹配到的订单列表。"""
    if not order_label:
        print("  [对账] 未指定 --order-label，无法自动对账；")
        print("         重试前请先手动查询挂单/持仓，确认订单未实际成交，避免重复下单")
        return []
    marker = f"t-{order_label}"
    prefix = "price_orders" if price_order else "orders"
    print(f"  [对账] 请求结果未知，按标签 '{order_label}' 查询 {contract} 实际订单...")
    time.sleep(1)  # 给撮合与订单查询留一点时间
    found = []
    try:
        for status in ("open", "finished"):
            qs = f"contract={contract}&status={status}&limit=100"
            result = rest_signed_request(
                "GET", f"{FUTURES_API}/{prefix}", qs, None, api_key, api_secret
            )
            if isinstance(result, list):
                for o in result:
                    text = o.get("text", "") or o.get("initial", {}).get("text", "")
                    if text == marker:
                        found.append(o)
            if found:
                break
    except Exception as e:
        print(f"  [对账] 查询异常: {e}")
        return []
    if found:
        for o in found:
            oid = o.get("id", "N/A")
            status = o.get("status", "N/A")
            size = o.get("size", o.get("initial", {}).get("size", "?"))
            price = o.get("price", o.get("initial", {}).get("price", "?"))
            print(f"  [对账] 订单已实际提交: ID={oid} 状态={status} "
                  f"数量={size} 价格={price} — 请勿重复下单")
    else:
        print(f"  [对账] 挂单与最近成交中未找到标签 '{order_label}'，大概率未提交成功")
    return found


def submit_order(api_key, api_secret, body, contract, action_label,
                 order_label=None, price_order=False):
    """统一下单入口：提交订单并打印结果；结果未知时按标签对账。"""
    path = f"{FUTURES_API}/price_orders" if price_order else f"{FUTURES_API}/orders"
    result = rest_signed_request("POST", path, "", body, api_key, api_secret)
    if isinstance(result, dict):
        _print_order_result(result, action_label)
        return result
    reconciled = reconcile_order_by_label(
        api_key, api_secret, contract, order_label, price_order
    )
    if reconciled:
        return reconciled[0]
    _print_order_result(result, action_label)
    return result


# ── CLI 参数解析 ──────────────────────────────────────────
def build_parser():
    parser = argparse.ArgumentParser(
        prog="quick_order",
        description="Gate.io 期货快速下单工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  查询:
    python quick_order.py --positions
    python quick_order.py --orders
    python quick_order.py --balance
    python quick_order.py --position-mode

  模拟盘 (测试网, 独立密钥 GATE_TESTNET_API_KEY):
    python quick_order.py --env testnet --balance
    python quick_order.py --env testnet -s BTC_USDT --side long --type market --size 1 --confirm

  开仓:
    python quick_order.py -s BTC_USDT --side long --type market --size 1
    python quick_order.py -s BTC_USDT --side short --type limit --price 73500 --size 1
    python quick_order.py -s BTC_USDT --side long --type market --size 1 -m isolated

  平仓 (单向):
    python quick_order.py -s BTC_USDT --close --size 1
    python quick_order.py -s BTC_USDT --close-all

  平仓 (双向):
    python quick_order.py -s BTC_USDT --close-long --size 1
    python quick_order.py -s BTC_USDT --close-short --size 1
    python quick_order.py -s BTC_USDT --close-long-all
    python quick_order.py -s BTC_USDT --close-short-all

  价格触发 (开仓):
    python quick_order.py -s BTC_USDT --trigger-price 72000 --trigger-type market --side long --size 1
    python quick_order.py -s BTC_USDT --trigger-price 75000 --trigger-type limit --trigger-limit-price 74900 --side short --size 1

  止盈止损 (平仓):
    python quick_order.py -s BTC_USDT --close-trigger --trigger-price 72000 --trigger-type market --side short --size 1 --trigger-rule 2
    python quick_order.py -s BTC_USDT --close-trigger --trigger-price 75000 --trigger-type limit --trigger-limit-price 75100 --side short --size 1 --trigger-rule 1

  撤单:
    python quick_order.py --cancel-order 123456789
    python quick_order.py --cancel-all -s BTC_USDT

  撤销计划委托 (止盈止损):
    python quick_order.py --cancel-price-order 2062928387801874
    python quick_order.py --cancel-all-price-orders -s BTC_USDT

  安全核对请求体 (不真实下单/撤单):
    python quick_order.py -s BTC_USDT --side long --type market --size 1 --dry-run --confirm
    python quick_order.py --cancel-all -s BTC_USDT --dry-run --confirm

  跳过确认:
    python quick_order.py -s BTC_USDT --side long --type market --size 1 --confirm
""",
    )

    # ── 环境选择 (实盘/模拟盘独立配置，不混用) ──
    parser.add_argument("--env", choices=["live", "testnet"], default="live",
                        help="环境: live=实盘(默认) / testnet=模拟盘(测试网, 独立密钥)")
    parser.add_argument("--config", default=None,
                        help="配置文件路径 (默认: live→watchlist.yaml, testnet→watchlist_testnet.yaml)")

    # ── 查询 ──
    g_query = parser.add_argument_group("查询命令")
    g_query.add_argument("--positions", action="store_true",
                         help="查询持仓")
    g_query.add_argument("--orders", action="store_true",
                         help="查询挂单")
    g_query.add_argument("--price-orders", action="store_true",
                         help="查询计划委托 (止盈止损)")
    g_query.add_argument("--balance", action="store_true",
                         help="查询余额")
    g_query.add_argument("--position-mode", action="store_true",
                         help="查询持仓模式")

    # ── 通用选项 ──
    parser.add_argument("-s", "--symbol", type=str, metavar="SYMBOL",
                        help="交易对, 如 BTC_USDT")
    parser.add_argument("--side", choices=["long", "short"],
                        help="方向: long(做多) / short(做空)")
    parser.add_argument("--type", choices=["limit", "market", "post_only", "ioc", "fok"],
                        default="market", dest="order_type",
                        help="订单类型 (默认: market)")
    parser.add_argument("--price", type=str, default=None,
                        help="委托价格 (限价单必填)")
    parser.add_argument("--size", type=float, default=None,
                        help="合约数量")

    # ── 平仓 ──
    g_close = parser.add_argument_group("平仓命令")
    g_close.add_argument("--close", action="store_true",
                         help="平仓 (单向模式自动识别方向)")
    g_close.add_argument("--close-long", action="store_true",
                         help="平多仓 (双向模式)")
    g_close.add_argument("--close-short", action="store_true",
                         help="平空仓 (双向模式)")
    g_close.add_argument("--close-all", action="store_true",
                         help="市价全平所有仓位")
    g_close.add_argument("--close-long-all", action="store_true",
                         help="市价全平多仓 (双向模式)")
    g_close.add_argument("--close-short-all", action="store_true",
                         help="市价全平空仓 (双向模式)")

    # ── 价格触发 ──
    g_trigger = parser.add_argument_group("价格触发订单")
    g_trigger.add_argument("--trigger-price", type=str, default=None,
                           help="触发价格")
    g_trigger.add_argument("--trigger-type", choices=["market", "limit"],
                           default=None,
                           help="触发后订单类型: market / limit")
    g_trigger.add_argument("--trigger-limit-price", type=str, default=None,
                           help="触发后限价价格 (trigger-type=limit 时必填)")
    g_trigger.add_argument("--trigger-price-type",
                           choices=["latest", "mark", "index"],
                           default="latest",
                           help="触发价格类型: latest(最新价)/mark(标记价)/index(指数价)")
    g_trigger.add_argument("--trigger-expiration", type=int, default=None,
                           help="触发订单过期秒数 (如 86400=24h)")
    g_trigger.add_argument("--close-trigger", action="store_true",
                           help="触发后平仓 (止盈止损)")
    g_trigger.add_argument("--trigger-rule", type=int, choices=[1, 2],
                           default=None,
                           help="触发规则: 1=价格>=触发价, 2=价格<=触发价 (平仓触发单必填)")

    # ── 撤单 ──
    g_cancel = parser.add_argument_group("撤单命令")
    g_cancel.add_argument("--cancel-order", type=str, metavar="ORDER_ID",
                          default=None,
                          help="撤销指定挂单 (传入订单ID)")
    g_cancel.add_argument("--cancel-all", action="store_true",
                          help="撤销指定合约的所有普通挂单 (必须配合 --symbol)")
    g_cancel.add_argument("--cancel-price-order", type=str, metavar="ORDER_ID",
                          default=None,
                          help="撤销指定计划委托/止盈止损单 (传入订单ID, 可用 --price-orders 查询)")
    g_cancel.add_argument("--cancel-all-price-orders", action="store_true",
                          help="撤销指定合约的所有计划委托/止盈止损单 (必须配合 --symbol)")

    # ── 其他 ──
    parser.add_argument("-m", "--margin-mode", choices=["cross", "isolated"],
                        default=None,
                        help="保证金模式: cross(全仓) / isolated(逐仓)")
    parser.add_argument("--confirm", action="store_true",
                        help="跳过确认直接执行")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将发送的请求, 不真实下单/撤单")
    parser.add_argument("--order-label", choices=["eval", "drive"],
                        default="eval",
                        help="订单标签: eval=评测订单, drive=开车订单 (默认: eval)")

    return parser


# ── 主入口 ────────────────────────────────────────────────
def main():
    parser = build_parser()
    args = parser.parse_args()

    # ── 加载配置（按环境独立：live=实盘 / testnet=模拟盘，密钥与 REST 地址均独立）──
    api_key, api_secret = load_config(args.env, args.config)

    # ── 环境标识横幅：所有操作醒目提示当前环境，防误下单 ──
    print("=" * 72)
    print("  环境: {} ({})".format(env_label(args.env), args.env))
    print("  密钥: {}".format("GATE_TESTNET_API_KEY" if args.env == "testnet" else "GATE_API_KEY"))
    print("=" * 72)

    # ── 查询命令 (不需要 symbol) ──
    if args.positions:
        cmd_positions(api_key, api_secret)
        return

    if args.orders:
        cmd_orders(api_key, api_secret)
        return

    if args.price_orders:
        cmd_price_orders(api_key, api_secret)
        return

    if args.balance:
        cmd_balance(api_key, api_secret)
        return

    if args.position_mode:
        cmd_position_mode(api_key, api_secret)
        return

    # ── 撤单命令 ──
    if args.cancel_order:
        cmd_cancel_order(
            api_key, api_secret, args.cancel_order,
            args.confirm, args.dry_run,
        )
        return

    if args.cancel_all:
        if not args.symbol:
            parser.error("--cancel-all requires --symbol (-s)")
        cmd_cancel_all(
            api_key, api_secret, args.symbol,
            args.confirm, args.dry_run,
        )
        return

    if args.cancel_price_order:
        cmd_cancel_price_order(
            api_key, api_secret, args.cancel_price_order,
            args.confirm, args.dry_run,
        )
        return

    if args.cancel_all_price_orders:
        if not args.symbol:
            parser.error("--cancel-all-price-orders requires --symbol (-s)")
        cmd_cancel_all_price_orders(
            api_key, api_secret, args.symbol,
            args.confirm, args.dry_run,
        )
        return

    # ── 以下命令需要 --symbol ──
    has_close = any([
        args.close, args.close_long, args.close_short,
        args.close_all, args.close_long_all, args.close_short_all,
    ])
    has_trigger = args.trigger_price is not None
    has_open = args.side is not None

    if has_close or has_trigger or has_open:
        if not args.symbol:
            parser.error("此操作需要指定 --symbol (-s)")

    # ── 价格触发订单 ──
    if has_trigger:
        if not args.side:
            parser.error("价格触发订单需要指定 --side")
        if not args.size:
            parser.error("价格触发订单需要指定 --size")
        if not args.trigger_type:
            parser.error("价格触发订单需要指定 --trigger-type")

        cmd_trigger_order(
            api_key, api_secret, args.symbol, args.side, args.size,
            args.trigger_price, args.trigger_type, args.trigger_limit_price,
            args.trigger_price_type, args.trigger_expiration,
            args.trigger_rule, args.close_trigger, args.margin_mode, args.confirm,
            args.dry_run, args.order_label, args.env,
        )
        return

    # ── 平仓命令 ──
    if has_close:
        if args.close:
            close_type = "auto"
        elif args.close_long:
            close_type = "long"
        elif args.close_short:
            close_type = "short"
        elif args.close_all:
            close_type = "all"
        elif args.close_long_all:
            close_type = "long_all"
        elif args.close_short_all:
            close_type = "short_all"
        else:
            close_type = "auto"

        cmd_close(
            api_key, api_secret, args.symbol, close_type, args.size,
            args.order_type, args.price, args.margin_mode, args.confirm,
            args.dry_run, args.order_label,
        )
        return

    # ── 开仓命令 ──
    if has_open:
        if not args.size:
            parser.error("开仓需要指定 --size")

        cmd_open(
            api_key, api_secret, args.symbol, args.side, args.order_type,
            args.price, args.size, args.margin_mode, args.confirm,
            args.dry_run, args.order_label,
        )
        return

    # ── 无有效操作 ──
    parser.print_help()
    print("\n提示: 请指定操作命令, 例如 --positions / --side long 等")


if __name__ == "__main__":
    main()
