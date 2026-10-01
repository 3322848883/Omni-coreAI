#!/usr/bin/env python3
"""本地一键「同步 + 重启服务器」。

在**本地**跑：推送代码到 origin → SSH 到服务器跑 scripts/deploy.sh（拉取/装 skill/测试/重启）。

用法：
    python scripts/deploy_remote.py                # 完整：push + 远程部署（含重启）
    python scripts/deploy_remote.py --no-push      # 跳过 push（只远程部署）
    python scripts/deploy_remote.py --no-restart   # 远程只同步不重启
    python scripts/deploy_remote.py --dry-run      # 预览将执行的动作

凭据（按优先级）：
    1) 环境变量 GATE_DEPLOY_HOST / PORT / USER / PASSWORD / KEY
    2) scripts/.deploy.env（gitignore，KEY=VALUE 每行一条）
    3) ~/.gate-deploy.env

示例 scripts/.deploy.env：
    GATE_DEPLOY_HOST=69.12.85.185
    GATE_DEPLOY_PORT=2222
    GATE_DEPLOY_USER=root
    GATE_DEPLOY_PASSWORD=...
    GATE_DEPLOY_ROOT=/opt/gate-signal-bot
    GATE_DEPLOY_HOSTKEY=SHA256:...
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULTS = {
    "GATE_DEPLOY_PORT": "22",
    "GATE_DEPLOY_USER": "root",
    "GATE_DEPLOY_ROOT": "/opt/gate-signal-bot",
    "GATE_DEPLOY_HOSTKEY": "",
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    for p in (ROOT / "scripts" / ".deploy.env", Path.home() / ".gate-deploy.env"):
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    # 环境变量覆盖
    for k in list(cfg) + ["GATE_DEPLOY_HOST", "GATE_DEPLOY_PASSWORD", "GATE_DEPLOY_KEY"]:
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def ssh_exec(cfg: dict, remote_cmd: str, timeout: int = 900) -> tuple[int, str]:
    """在服务器执行命令（自动重试网络抖动）。"""
    host = cfg.get("GATE_DEPLOY_HOST", "")
    if not host:
        return 2, "缺少 GATE_DEPLOY_HOST（见脚本 docstring 的凭据配置）"
    keyfile = cfg.get("GATE_DEPLOY_KEY", "")
    base = ["ssh", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout=20", "-p", cfg["GATE_DEPLOY_PORT"]]
    if keyfile:
        base += ["-i", keyfile]
    target = f"{cfg['GATE_DEPLOY_USER']}@{host}"

    for i in range(4):
        if keyfile:
            r = _run(base + [target, remote_cmd], timeout=timeout)
        else:
            # 无 key 时用 plink（支持 -pw 密码）
            plink = r"C:\Program Files\PuTTY\plink.exe"
            if not Path(plink).is_file():
                return 2, "无 SSH key 且找不到 plink.exe；请配 GATE_DEPLOY_KEY 或装 PuTTY"
            cmd = [plink, "-batch", "-ssh"]
            if cfg.get("GATE_DEPLOY_HOSTKEY"):
                cmd += ["-hostkey", cfg["GATE_DEPLOY_HOSTKEY"]]
            cmd += ["-pw", cfg.get("GATE_DEPLOY_PASSWORD", ""),
                    "-P", cfg["GATE_DEPLOY_PORT"], target, remote_cmd]
            r = _run(cmd, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        if "Connection timed out" in out or "Network error" in out:
            print(f"    (网络抖动，重试 {i + 1}/4)")
            time.sleep(3 * (i + 1))
            continue
        return r.returncode, out
    return 1, "SSH 多次重试仍失败"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="跳过本地 push")
    ap.add_argument("--no-restart", action="store_true", help="远程只同步不重启")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="本地有未提交改动时中止（默认仅警告并继续）")
    ap.add_argument("--branch", default="master")
    args = ap.parse_args()

    cfg = load_config()
    host = cfg.get("GATE_DEPLOY_HOST", "")
    print("=" * 56)
    print(f"远程部署 → {cfg['GATE_DEPLOY_USER']}@{host or '<未配置>'}:{cfg['GATE_DEPLOY_PORT']}")
    print(f"远程目录: {cfg['GATE_DEPLOY_ROOT']}")
    print("=" * 56)

    # 1) 本地检查 + push
    if not args.no_push:
        print("\n[1/2] 本地推送")
        st = _run(["git", "status", "--porcelain"], cwd=str(ROOT)).stdout.strip()
        real = [ln for ln in st.splitlines()
                if not ln.startswith("?? config/bots.local/")
                and not ln.startswith("?? data/")
                and not ln.startswith("?? skills/")
                and not ln.startswith("?? verify_data/")
                and not ln.startswith("?? tmp")
                and "config/bots.local/" not in ln]
        if real:
            # 不阻断：未提交的改动本就不会被 push，服务器拿到的是已提交内容。
            # 但必须让用户知道「本次部署不含这些改动」。
            print(f"  ⚠️  本地有 {len(real)} 项未提交改动（本次部署【不含】它们）：")
            for ln in real[:10]:
                print("     ", ln)
            if len(real) > 10:
                print(f"      ... 另有 {len(real) - 10} 项")
            if args.strict:
                print("  ⛔ --strict：中止（请先提交或 --no-push）")
                return 1
            print("  （继续；如需中止加 --strict）")
        else:
            print("  OK 工作区干净")
        if args.dry_run:
            print(f"  [dry-run] git push origin {args.branch}")
        else:
            r = _run(["git", "push", "origin", args.branch], cwd=str(ROOT), timeout=300)
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            print("  " + (out.splitlines()[-1] if out else "pushed"))
            if r.returncode != 0 and "Everything up-to-date" not in out:
                print("  ⛔ push 失败")
                return 1

    # 2) 远程部署
    print("\n[2/2] 远程部署（pull → skill → 编码自检 → 测试 → 重启 → 验证）")
    flags = ""
    if args.no_restart:
        flags += " --no-restart"
    if args.dry_run:
        flags += " --dry-run"
    remote = f"cd {cfg['GATE_DEPLOY_ROOT']} && bash scripts/deploy.sh{flags}"
    code, out = ssh_exec(cfg, remote)
    print(out.rstrip())
    if code != 0:
        print(f"\n⛔ 远程部署失败（exit {code}）")
        return code
    print("\n✅ 远程部署完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
