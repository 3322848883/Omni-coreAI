# A/B 对比测试：价格行为人格 × 4周期图（Skill vs 无 Skill）

> 2026-10-01 · 同 bot / 同人格 `prompts/brooks_btc_pa.md` / 同 4 周期图 / 同 BTC_USDT
> **唯一变量：`skills: []` vs `skills: [price-action-trading]`**

## 0. 本轮发现并修复的 Bug（重要）

**现象**：第一次复跑时，`skills: []` 的 A 组竟然激活了 skill（journal 出现 `bot_id=""` 记录）。

**根因**：`run_tool` 的 skill 分支只从 `args` 取白名单（`args.get("enabled_skills")`），而 **LLM 不会传这个参数** → `enabled_ids=None` → 无限制放行。**bot yaml 的 `skills` 白名单从未被强制执行**。

**修复**：
- `run_tool(..., bot_id="", skill_ids=None)` 新增两个显式参数
- `loop.py` 两处调用点注入 `bot_id=self.cfg.bot_id, skill_ids=self.cfg.skills`
- 白名单以 runner 上下文为准，**不信任 LLM 传参**（LLM 在 args 里塞 `enabled_skills` 也无法绕过）

**回归测试**：`TestRunToolWhitelistEnforcement` 5 例（含「LLM 传参不能绕过」）
→ skillkit 角度测试 19 OK，全量 **778 OK**

## 1. 配置（两组仅 skills 不同）

```yaml
prompt_file: prompts/brooks_btc_pa.md        # 阿尔布鲁克斯价格行为人格
vision: true
vision_timeframes: [5m, 15m, 1h, 4h]         # 默认发 4 周期图
market: {extra_timeframes: [5m, 15m, 4h], extra_candles: 60}
tools: {enabled: true, native: true, max_rounds: 5}
skills: []                                    # A 组
skills: [price-action-trading]                # B 组
```

## 2. 载荷（两组同）

| 项 | 值 |
|----|-----|
| K 线图 | **4 张**（5m 86K / 15m 102K / 1h 99K / 4h 90K）≈ 368 KB base64 |
| 文本快照 | 50.6 KB（1h 60 根 + tf 子块各 60 根 = 240 根） |
| system prompt | A 3.1 KB / B 3.5 KB（多 skill_catalog 377 字符） |
| 工具面 | 21 个 |

## 3. 结果（修复后）

| 指标 | **A 无 skill** | **B 有 skill** |
|------|:---:|:---:|
| skill 工具激活 | ❌ **正确不激活** | ✅ **正确激活** |
| thinking 轮数 | 2 | 3 |
| thinking 字数 | 16,160 | 10,804 |
| Plan | hold (0.78) | hold (0.35) |

## 4. 方法论术语密度（每千字归一）

| Brooks 术语 | A 无 skill | **B 有 skill** | 密度比 |
|------------|:---:|:---:|:---:|
| **H2**（回调做多） | 1 (0.06‰) | **6 (0.56‰)** | **9×** |
| **H1** | 1 (0.06‰) | **3 (0.28‰)** | **4.5×** |
| **Brooks** | 0 | **4** | ∞ |
| signal bar | 1 | **2** | — |

B 组 thinking 更短但**术语密度显著更高**——不是靠堆字数，而是方法论结构化。

## 5. 结论

| 维度 | 结论 |
|------|------|
| **功能正确性** | ✅ 修复后 A 组不再误激活；白名单真实强制 |
| **方法论注入** | ✅ B 组 H2/H1/Brooks 术语密度 4.5–9× |
| **成本** | B 组仅多 ~5.4K token（catalog 377 字符 + body 2479 token） |
| **可复现性** | 两轮 A/B 都显示 B 组术语密度更高（结构性差异，非随机） |

**附带收益**：本轮测试暴露并修复了一个**权限绕过漏洞**——skill 白名单此前形同虚设。这是生产测试才能发现的问题，已加回归测试锁定。
