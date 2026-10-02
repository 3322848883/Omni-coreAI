---
name: sentinel-risk
description: "哨兵风控检查 skill，仅用于 SkillKit 多 skill 可见性测试。Use when testing skill visibility filtering and whitelist behavior."
model-invocation: false
---

# Sentinel Risk (test fixture)

这是一个**用户专属** skill（model-invocation=false），用于验证：
- 模型不可自主触发（catalog 不列、skill 工具拒绝）
- CLI `omnialpha skill run sentinel-risk` 仍可执行

内容仅测试用。
