"""试错台账：观测 prompt/yaml 内容哈希变更，累计 per-bot / 全局 N。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _load_yaml_map(path: Path) -> dict:
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def scan_trials(
    root: Path,
    trials_path: Optional[Path] = None,
    state_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """扫描 prompts/*.md 与 config/bots/*.yaml，变更则追加 trial 记录。

    返回本次新增记录列表。
    """
    root = Path(root)
    trials_path = trials_path or (root / "data" / "trials.jsonl")
    state_path = state_path or (root / "data" / "trials_state.json")
    now = now if now is not None else time.time()

    state: dict[str, str] = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            state = {}

    # bot → prompt_file 映射
    bot_prompt: dict[str, str] = {}
    bot_files: list[tuple[str, Path, str]] = []  # (bot_id, path, kind)
    bots_dir = root / "config" / "bots"
    for yml in sorted(bots_dir.glob("*.yaml")):
        if yml.name.startswith("_"):
            continue
        data = _load_yaml_map(yml)
        bid = str(data.get("bot_id") or yml.stem)
        pf = str((data.get("strategist") or {}).get("prompt_file") or "")
        bot_prompt[bid] = pf
        bot_files.append((bid, yml, "config"))

    prompts_dir = root / "prompts"
    prompt_owners: dict[str, list[str]] = {}
    for bid, pf in bot_prompt.items():
        if pf:
            prompt_owners.setdefault(pf, []).append(bid)

    watched: list[tuple[str, Path, str]] = list(bot_files)
    if prompts_dir.exists():
        for md in sorted(prompts_dir.glob("*.md")):
            rel = f"prompts/{md.name}"
            owners = prompt_owners.get(rel) or prompt_owners.get(f"prompts\\{md.name}")
            if owners:
                # 共享 prompt：每个 owner bot 各记一笔，保证 per-bot N 公平
                for bid in owners:
                    watched.append((bid, md, "prompt"))
            else:
                watched.append(("_shared", md, "prompt"))

    new_records: list[dict[str, Any]] = []
    # 同一文件对多 bot：state 键需含 bot，否则只记第一次
    for bid, path, kind in watched:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        state_key = f"{bid}:{rel}"
        h = _sha8(text)
        prev = state.get(state_key)
        if prev == h:
            continue
        rec = {
            "ts": now,
            "bot_id": bid,
            "kind": kind,
            "path": rel,
            "sha256_8": h,
            "note": "init" if prev is None else "changed",
        }
        new_records.append(rec)
        state[state_key] = h

    if new_records:
        trials_path.parent.mkdir(parents=True, exist_ok=True)
        with open(trials_path, "a", encoding="utf-8") as fh:
            for rec in new_records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return new_records


def trial_counts(root: Path, trials_path: Optional[Path] = None) -> dict[str, int]:
    """bot_id → 试错次数 N（含 _shared 记入所有 bot 的分母基数由 scoreboard 处理）。"""
    trials_path = trials_path or (Path(root) / "data" / "trials.jsonl")
    counts: dict[str, int] = {}
    if not trials_path.exists():
        return counts
    for line in trials_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        bid = str(rec.get("bot_id") or "")
        if bid:
            counts[bid] = counts.get(bid, 0) + 1
    return counts


def global_trial_n(root: Path, trials_path: Optional[Path] = None) -> int:
    trials_path = trials_path or (Path(root) / "data" / "trials.jsonl")
    if not trials_path.exists():
        return 1
    n = 0
    for line in trials_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            n += 1
    return max(1, n)
