"""从 taxonomy 采样 Domain/Scenario，支持固定 seed 和随机采样。

本模块提供从 taxonomy JSON 中选取 Domain/Scenario 的功能，支持三种模式：
- 固定 audit case：同时指定 domain_id 和 scenario_id
- 固定 domain：只指定 domain_id，scenario 随机选取
- 均匀采样：domain 和 scenario 都随机选取

采样结果包含完整的场景信息和 taxonomy 版本指纹，确保可追溯。
"""
from __future__ import annotations

import hashlib
import json
import random


def taxonomy_sha256(taxonomy: dict) -> str:
    """计算 taxonomy 的 SHA-256 指纹，用于版本追溯。

    使用 sort_keys=True 和紧凑分隔符确保同一 taxonomy 总是产生相同的哈希值。

    Args:
        taxonomy: 完整的 taxonomy 数据

    Returns:
        64 字符的十六进制 SHA-256 哈希值
    """
    raw = json.dumps(taxonomy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def sample_scene(taxonomy: dict, seed: int, domain_id: str | None = None,
                 scenario_id: str | None = None, sampling_mode: str = "uniform_coverage") -> dict:
    """从 taxonomy 中采样一个 Domain/Scenario 组合。

    采样逻辑：
    1. 如果指定了 domain_id，直接定位该 domain
    2. 如果只指定了 scenario_id，从 taxonomy 中查找其所属 domain
       （不依赖命名约定，而是遍历查找，确保调用者无需同时提供 parent domain）
    3. 如果都未指定，使用 seed 驱动的随机选择

    Args:
        taxonomy: 完整的 taxonomy 数据
        seed: 随机种子，确保可复现
        domain_id: 可选的 domain ID，固定 domain
        scenario_id: 可选的 scenario ID，固定 scenario
        sampling_mode: 采样模式标识，记录在输出中供追溯

    Returns:
        包含采样结果和追溯信息的 dict，包括：
        - domain_id, domain, domain_definition
        - scenario_id, scenario, example_tasks, example_task_seeds
        - rng_seed, sampling_mode, sampling_reason
        - taxonomy_version, taxonomy_sha256

    Raises:
        ValueError: 如果指定的 domain_id 或 scenario_id 不存在，或 scenario_id 跨 domain 歧义
    """
    domains = taxonomy["domains"]
    rng = random.Random(seed)
    if domain_id:
        domain = next((d for d in domains if d["domain_id"] == domain_id), None)
    elif scenario_id:
        # A scenario ID encodes its domain, but do not rely on that naming
        # convention alone: resolve it from the taxonomy so callers may pin a
        # scenario without also supplying its parent domain.
        # 不依赖 scenario_id 的命名约定推断 domain，而是遍历 taxonomy 查找，
        # 这样调用者只需提供 scenario_id 即可，无需知道其所属 domain。
        matches = [
            domain for domain in domains
            if any(item.get("scenario_id") == scenario_id for item in domain.get("scenarios", []))
        ]
        if len(matches) > 1:
            raise ValueError(f"scenario_id is ambiguous across domains: {scenario_id}")
        domain = matches[0] if matches else None
    else:
        # 均匀采样：先随机选 domain，再随机选 scenario
        domain = rng.choice(domains)
    if domain is None:
        if scenario_id and not domain_id:
            raise ValueError(f"unknown scenario_id: {scenario_id}")
        raise ValueError(f"unknown domain_id: {domain_id}")
    # 如果指定了 scenario_id 则定位，否则随机选择
    scenario = next((s for s in domain["scenarios"] if s["scenario_id"] == scenario_id), None) if scenario_id else rng.choice(domain["scenarios"])
    if scenario is None:
        raise ValueError(f"unknown scenario_id: {scenario_id}")
    return {
        "case_id": None,
        "rng_seed": seed,
        "domain_id": domain["domain_id"],
        "domain": domain["domain"],
        "domain_definition": domain.get("definition", ""),
        "scenario_id": scenario["scenario_id"],
        "scenario": scenario["scenario"],
        "example_tasks": scenario.get("example_tasks", {}),
        "example_task_seeds": scenario.get("example_task_seeds", []),
        "sampling_mode": sampling_mode,
        "sampling_reason": (
            "fixed_audit_case" if domain_id and scenario_id
            else "fixed_domain" if domain_id
            else "fixed_scenario" if scenario_id
            else "uniform_domain_then_scenario"
        ),
        "taxonomy_version": taxonomy["taxonomy_version"],
        "taxonomy_sha256": taxonomy_sha256(taxonomy),
    }
