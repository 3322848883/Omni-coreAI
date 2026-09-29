# 飞书告警配置指南（防踩坑）

## 踩过的坑（勿再犯）

### 坑 1：混淆「应用机器人」vs「群机器人」

| | 应用机器人 | 群机器人 |
|---|---|---|
| 凭据 | App ID (`cli_` 开头) + App Secret | Webhook URL (`https://open.feishu.cn/...`) |
| 发消息 | API `im/v1/messages` + 用户 open_id | POST webhook |
| 需要 | 用户 open_id（从 contact API 获取） | 不需要 |
| 我们的用法 | **用这个** | 备选 |

### 坑 2：拿错 open_id

- **bot 的 open_id**（`ou_` 开头，bot info 里拿）→ **不能用来发消息给自己**
- **用户的 open_id**（从 `contact/v3/users` 获取）→ 发消息给这个人

### 坑 3：im/v1/chats 不返回私聊

`GET /open-apis/im/v1/chats` 只返回**群聊**，不含 p2p 私聊。
要找用户 open_id 用 `GET /open-apis/contact/v3/users`。

## 正确配置方法

### 方式 A：config/alerts.yaml（推荐）

```yaml
# config/alerts.yaml（此文件含密钥，已加 .gitignore）
feishu:
  app_id: cli_aa3154528bf8dbcb
  app_secret: xxx
  user_open_id: ou_xxx
```

### 方式 B：环境变量

```powershell
$env:FEISHU_APP_ID = "cli_xxx"
$env:FEISHU_APP_SECRET = "xxx"
$env:FEISHU_USER_OPEN_ID = "ou_xxx"
```

### 发消息流程（应用机器人）

```
1. App ID + Secret → POST auth/v3/tenant_access_token/internal → token
2. token + 用户 open_id → POST im/v1/messages?receive_id_type=open_id
```

### 成交告警自动触发

设好凭证后，开仓/平仓/TP/SL 自动推送到飞书（通过 `TradeLogger.log_execution`）。

### 验证连通

```python
from gate_bot.monitoring.notify import build_notifier
n = build_notifier(root=Path('.'))
print(n.has_channel, n.channel_names)
n.send('测试消息')
```
