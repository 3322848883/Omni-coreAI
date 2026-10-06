> 本文件是 `plan-schema_part1.md` 的第 5/6 片（按 `##` 小节切分，内容未改动）。

### 缺 params 后果（硬契约）
- 形态类条件（signal_bar / sequence / momentum_check / risk_reward_check）**缺 params 或 params 为空 → 节点3 判 manual，转监控员 AI**（fail-safe）；**无 AI（监控员）在场时计划过期**（PENDING_CONFIRM → EXPIRED）。
- 未登记的 pattern/sequence 名 → 同样 manual（reason 注明未知形态）。
- 语境类形态程序不判，条件必须标注 `ai_judged: true` 归口监控员 AI：**#20 强趋势尖峰期暂停 / #21 通道中的棒线·二次入场 / #22 ii最终旗形·区间陷阱·信号K×市场状态语境 / #24 尖峰态专属信号 SPS·尖峰旗形顺向突破（禁 SCS 追高潮）**。此类条件若仍落在形态类 4 类型上，须携带最小几何基线 params（如 `{"direction": "bull", "timeframe": "15m"}`，程序判 manual 后转 AI），或把可量化部分改用机械类条件表达——**绝不写无 params 的形态类条件**。
- 交付门禁第 34 项对以上三条程序化把关：缺 params / 未登记形态名 → violation（阻断交付）；语境类形态缺 `ai_judged: true` → warning。

