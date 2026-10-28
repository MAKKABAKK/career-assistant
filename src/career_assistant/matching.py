"""Deterministic evidence checks and coverage, independent from model scores.

Each unique condition has equal weight within its required/bonus group.
Unknown evidence is never failure. When unknowns remain, coverage is a range;
``percent`` is null. No score represents likelihood of being hired.
"""
from __future__ import annotations

import re
import unicodedata


class MatchingError(ValueError):
    """The AI output does not have trustworthy references or structure."""


def normalize(text: str) -> str:
    """Normalize Unicode, case and whitespace without inventing skill aliases."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def fact_map(facts: dict) -> dict[str, dict]:
    if not isinstance(facts, dict) or not isinstance(facts.get("facts"), list):
        raise MatchingError("事实必须包含 facts 列表。")
    result = {}
    for fact in facts["facts"]:
        if not isinstance(fact, dict):
            raise MatchingError("事实条目格式错误。")
        for key in ("id", "text", "evidence"):
            if not isinstance(fact.get(key), str) or not fact[key].strip():
                raise MatchingError(f"事实缺少有效的 {key}。")
        if fact["text"] != fact["evidence"]:
            raise MatchingError("事实文字必须与原文或用户确认的依据一致。")
        if fact["id"] in result:
            raise MatchingError("事实 ID 重复。")
        result[fact["id"]] = fact
    if not result:
        raise MatchingError("没有可用的简历事实。")
    return result


def calculate_coverage(requirements: list[dict]) -> dict:
    """Count validated rows. Deduplication belongs to validate_requirements."""
    if not isinstance(requirements, list):
        raise MatchingError("条件必须是列表。")
    groups = {
        kind: {"total": 0, "matched": 0, "unmet": 0, "unknown": 0}
        for kind in ("required", "bonus")
    }
    for row in requirements:
        if not isinstance(row, dict) or row.get("kind") not in groups:
            raise MatchingError("岗位条件类型必须是 required 或 bonus。")
        if row.get("status") not in ("matched", "unmet", "unknown"):
            raise MatchingError("岗位条件判断无效。")
        group = groups[row["kind"]]
        group["total"] += 1
        group[row["status"]] += 1
    for group in groups.values():
        total = group["total"]
        minimum = round(100 * group["matched"] / total, 1) if total else None
        maximum = (
            round(100 * (group["matched"] + group["unknown"]) / total, 1)
            if total else None
        )
        group.update(
            percent=minimum if not group["unknown"] else None,
            minimum_percent=minimum,
            maximum_percent=maximum,
        )
    return groups


# Absence from the resume is not negative evidence. Only explicit negative
# statements can justify "unmet"; numerical comparisons need human confirmation.
_NEGATIVE = re.compile(
    r"\b(?:no|not|never|lack|lacks|without|cannot|can't|don't|doesn't)\b"
    r"|没有|沒有|未曾|尚未|不具备|不具備|不会|不會|不能|不熟悉|无经验|無經驗",
    re.IGNORECASE,
)


def preserves_negation(quote: str, source: str) -> bool:
    """Reject a cropped quote that removes the surrounding sentence's negation.

    This deliberately conservative check does not establish semantic support.
    Keeping the full sentence lets the reviewer see both positive and negative
    statements, including "no Java experience, but I know Python".
    """
    if quote not in source:
        return False
    offset = 0
    while (start := source.find(quote, offset)) != -1:
        end = start + len(quote)
        left = re.split(r"[.!?。！？;\n]", source[:start])[-1]
        right = re.split(r"[.!?。！？;\n]", source[end:])[0]
        context = left + quote + right
        if not _NEGATIVE.search(context) or _NEGATIVE.search(quote):
            return True
        offset = start + 1
    return False


def validate_requirements(requirements: list, facts: dict, jd: str) -> list[dict]:
    """Validate quotes and IDs, downgrade unsupported decisions, dedupe rows."""
    known = fact_map(facts)
    if not isinstance(requirements, list) or not requirements:
        raise MatchingError("AI 未返回可验证的岗位条件。")
    result = []
    seen_ids = set()
    seen_conditions = {}
    for source in requirements:
        if not isinstance(source, dict):
            raise MatchingError("岗位条件格式错误。")
        row = dict(source)
        for key in ("id", "text", "jd_evidence", "reason"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise MatchingError(f"岗位条件缺少有效的 {key}。")
        if row["id"] in seen_ids:
            raise MatchingError("岗位条件 ID 重复。")
        seen_ids.add(row["id"])
        if row.get("kind") not in ("required", "bonus"):
            raise MatchingError("条件必须区分必须项与加分项。")
        if row.get("status") not in ("matched", "unmet", "unknown"):
            raise MatchingError("条件判断无效。")
        if row["jd_evidence"] not in jd:
            raise MatchingError("岗位条件引用不在 JD 原文中。")
        if not preserves_negation(row["jd_evidence"], jd):
            raise MatchingError("岗位条件引用省略了原文的否定语境。")
        ids, evidence = row.get("fact_ids"), row.get("evidence")
        if not isinstance(ids, list) or not all(isinstance(i, str) and i in known for i in ids):
            raise MatchingError("匹配引用了不存在的简历事实。")
        if not isinstance(evidence, list) or not all(isinstance(e, str) and e.strip() for e in evidence):
            raise MatchingError("匹配证据必须是原文列表。")
        if any(not any(e in known[i]["text"] or e in known[i]["evidence"] for i in ids) for e in evidence):
            raise MatchingError("匹配证据不在引用的简历事实中。")
        if any(not any(
            preserves_negation(e, known[i]["text"]) for i in ids
        ) for e in evidence):
            raise MatchingError("匹配引用省略了简历事实的否定语境。")
        if row["status"] != "unknown" and (not ids or not evidence):
            row["status"] = "unknown"
            row["reason"] = "缺少可引用的简历证据，待本人确认。"
        if (row["status"] != "unknown" and any(_NEGATIVE.search(e) for e in evidence)
                and any(not _NEGATIVE.search(e) for e in evidence)):
            row["status"] = "unknown"
            row["reason"] = "同一条件的引用包含正面与否定表述，可能存在矛盾；保留双方原文，按未知处理。"
        if row["status"] == "matched" and any(_NEGATIVE.search(e) for e in evidence):
            row["status"] = "unknown"
            row["reason"] = "引用含有否定表述，不能自动认定已满足，待本人确认。"
        if row["status"] == "unmet" and not any(_NEGATIVE.search(e) for e in evidence):
            row["status"] = "unknown"
            row["reason"] = "没有明确否定的原文证据，待本人确认。"
        # Same quote and normalized requirement must not increase its weight.
        condition = normalize(row["text"])
        if condition in seen_conditions:
            previous = seen_conditions[condition]
            if previous["kind"] != row["kind"] or previous["status"] != row["status"]:
                raise MatchingError("AI 对重复岗位条件给出冲突判断。")
            continue
        seen_conditions[condition] = row
        result.append(row)
    return result
