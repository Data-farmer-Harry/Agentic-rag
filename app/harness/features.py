from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from app.domain.models import RunTrajectory
from app.harness.models import CaseFeatures, HarnessToolSummary

_URL = re.compile(r"https?://[^\s<>()]+", flags=re.IGNORECASE)
_TOKEN = re.compile(r"[\w#+.-]+", flags=re.UNICODE)
_CJK = re.compile(r"[\u3400-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")

_INTENT_TERMS = {
    "compare": ("比较", "区别", "对比", "vs", "versus", "difference"),
    "research": ("调研", "研究", "论文", "综述", "research", "survey", "state of the art"),
    "debug": ("报错", "错误", "修复", "调试", "debug", "traceback", "exception"),
    "summarize": ("总结", "概括", "摘要", "summarize", "summary"),
    "lookup": ("什么", "怎么", "如何", "why", "what", "how", "explain", "查"),
    "social": ("你好", "谢谢", "再见", "hello", "hi", "thanks", "bye"),
}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256_text(encoded)


def build_case_features(trajectory: RunTrajectory) -> CaseFeatures:
    query = trajectory.user_input[:100_000]
    normalized = query.casefold()
    tokens = [
        token.casefold()
        for token in _TOKEN.findall(query)
        if 1 < len(token) <= 100
    ][:512]
    token_hashes = sorted({sha256_text(token)[:16] for token in tokens})[:128]
    intents = [
        intent
        for intent, terms in _INTENT_TERMS.items()
        if any(term in normalized for term in terms)
    ]
    if not intents:
        intents = ["lookup"]

    snapshot = trajectory.snapshot
    capability_names = sorted({event.tool_name for event in trajectory.tool_events})
    baseline_payload = {
        "config_hash": snapshot.config_hash if snapshot is not None else "unavailable",
        "policy_versions": snapshot.policy_versions if snapshot is not None else {},
        "component_versions": snapshot.component_versions if snapshot is not None else {},
    }
    return CaseFeatures(
        query_token_hashes=token_hashes,
        language=_language(query),
        character_count=len(query),
        code_block_count=min(query.count("```") // 2, 100),
        url_count=min(len(_URL.findall(query)), 100),
        intents=intents,
        personal_knowledge=any(
            term in normalized
            for term in ("我的", "个人", "之前说过", "记得", "my ", "personal", "remember")
        ),
        visual=any(
            term in normalized
            for term in ("图片", "图像", "截图", "视觉", "image", "screenshot", "vision")
        ),
        graph_relations=any(
            term in normalized
            for term in ("关系", "知识图谱", "实体", "路径", "relation", "graph", "entity")
        ),
        temporal=any(
            term in normalized
            for term in ("现在", "最新", "之前", "历史", "today", "latest", "before", "history")
        ),
        code=(
            "```" in query
            or any(
                term in normalized
                for term in (
                    "代码",
                    "函数",
                    "class ",
                    "def ",
                    "import ",
                    "typescript",
                    "python",
                )
            )
        ),
        tenant_id=trajectory.context.tenant_id,
        project_id=trajectory.context.project_id,
        domain_pack=trajectory.context.domain_pack,
        corpus_snapshot=(
            snapshot.corpus_snapshot if snapshot is not None else "unavailable"
        ),
        active_skill_versions=(
            dict(sorted(snapshot.skill_versions.items())[:100])
            if snapshot is not None
            else dict(sorted(trajectory.context.skill_versions.items())[:100])
        ),
        policy_versions=(
            dict(sorted(snapshot.policy_versions.items())[:100])
            if snapshot is not None
            else {}
        ),
        capability_allowlist_hash=canonical_json_hash(capability_names),
        baseline_harness_hash=canonical_json_hash(baseline_payload),
    )


def task_fingerprint(features: CaseFeatures) -> str:
    payload = {
        "query_token_hashes": features.query_token_hashes,
        "language": features.language,
        "intents": features.intents,
        "personal_knowledge": features.personal_knowledge,
        "visual": features.visual,
        "graph_relations": features.graph_relations,
        "temporal": features.temporal,
        "code": features.code,
        "domain_pack": features.domain_pack,
        "capability_allowlist_hash": features.capability_allowlist_hash,
    }
    return canonical_json_hash(payload)


def summarize_tools(trajectory: RunTrajectory) -> list[HarnessToolSummary]:
    names = Counter(event.tool_name for event in trajectory.tool_events)
    summaries: list[HarnessToolSummary] = []
    for name in sorted(names):
        events = [event for event in trajectory.tool_events if event.tool_name == name]
        summaries.append(
            HarnessToolSummary(
                tool_name_hash=sha256_text(name),
                call_count=len(events),
                success_count=sum(event.success for event in events),
                total_duration_ms=sum(event.duration_ms for event in events),
            )
        )
    return summaries[:50]


def _language(value: str) -> str:
    cjk = bool(_CJK.search(value))
    latin = bool(_LATIN.search(value))
    if cjk and latin:
        return "mixed"
    if cjk:
        return "zh"
    if latin:
        return "en"
    return "unknown"
