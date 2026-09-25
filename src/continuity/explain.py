"""教务解释视图：回答"这段时间学员为何如此安排、支持落实如何、
缺席如何归因、学习目标怎样跨班接续"。

输出为结构化字典，并附中文叙述 narrative，便于教务员直接说明。
"""

from __future__ import annotations

from datetime import datetime

from .events import iso
from .model import (
    ATTRIBUTION_LABELS,
    FORMAT_LABELS,
    NEED_LABELS,
    AbsenceAttribution,
    GoalStatus,
    NotFoundError,
    overlapping,
)

GOAL_STATUS_LABELS = {
    GoalStatus.OPEN: "进行中",
    GoalStatus.COMPLETED: "已完成",
    GoalStatus.CARRIED: "已接续",
}


def _fmt(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M") if moment else "至今"


def explain_enrollment(service, enrollment_id: str, start=None, end=None) -> dict:
    enrollment = service.enrollments.get(enrollment_id)
    if enrollment is None:
        raise NotFoundError(f"报名 {enrollment_id} 不存在")
    learner = service.learners[enrollment.learner_id]
    start = start or enrollment.review_anchor or service.clock.now()
    end = end or service.clock.now()

    # 一、授课形态安排时间线（含安排原因与来源方案）
    placements = []
    for p in overlapping(enrollment.placements, start, end):
        offering = service.offerings.get(p.offering_id)
        version = offering.version_at(p.valid_from) if offering else None
        entry = {
            "offering_id": p.offering_id,
            "offering_title": version.title if version else "",
            "format": p.format.value,
            "format_label": FORMAT_LABELS[p.format],
            "valid_from": iso(p.valid_from),
            "valid_to": iso(p.valid_to) if p.valid_to else None,
            "reason": p.reason,
            "proposal_id": p.proposal_id,
        }
        if p.proposal_id and p.proposal_id in service.proposals:
            entry["proposal_checks"] = service.proposals[p.proposal_id].checks
        placements.append(entry)

    # 二、逐场次：出勤、缺席归因、支持需求与落实
    sessions = []
    for p in overlapping(enrollment.placements, start, end):
        offering = service.offerings.get(p.offering_id)
        if offering is None:
            continue
        seg_start = max(start, p.valid_from)
        seg_end = min(end, p.valid_to or end)
        for session, version in offering.live_sessions():
            if session.format != p.format:
                continue
            if not (seg_start <= session.starts_at < seg_end):
                continue
            record = next(
                (
                    r
                    for r in service.records.values()
                    if r.enrollment_id == enrollment_id
                    and r.session_id == session.session_id
                    and r.valid_to is None
                ),
                None,
            )
            needed = learner.needs_at(session.starts_at)
            delivered = set(version.resources.get(session.format, set()))
            paused = session.format in learner.paused_formats_at(session.starts_at)
            sessions.append(
                {
                    "session_id": session.session_id,
                    "starts_at": iso(session.starts_at),
                    "format": session.format.value,
                    "paused_by_safety": paused,
                    "attendance": (
                        "UNRECORDED" if record is None else ("ATTENDED" if record.attended else "ABSENT")
                    ),
                    "attribution": record.attribution.value if record and record.attribution else None,
                    "counts_toward_withdrawal": bool(
                        record
                        and not record.attended
                        and record.attribution == AbsenceAttribution.VOLUNTARY
                    ),
                    "supports_needed": sorted(n.value for n in needed),
                    "supports_delivered": sorted(n.value for n in delivered & needed),
                    "support_gaps": sorted(n.value for n in needed - delivered),
                }
            )
    sessions.sort(key=lambda s: s["starts_at"])

    absences = [s for s in sessions if s["attendance"] == "ABSENT"]
    absence_summary: dict[str, int] = {}
    for item in absences:
        key = item["attribution"] or "UNATTRIBUTED"
        absence_summary[key] = absence_summary.get(key, 0) + 1

    # 三、学习目标与跨班接续链
    goals = []
    for goal in enrollment.goals.values():
        chain = []
        source_enrollment_id = goal.source_enrollment_id
        source_goal_id = goal.carried_from
        while source_enrollment_id and source_goal_id:
            source = service.enrollments.get(source_enrollment_id)
            if source is None or source_goal_id not in source.goals:
                break
            source_goal = source.goals[source_goal_id]
            chain.append(
                {
                    "enrollment_id": source_enrollment_id,
                    "goal_id": source_goal_id,
                    "title": source_goal.title,
                    "status": source_goal.status.value,
                }
            )
            source_enrollment_id = source_goal.source_enrollment_id
            source_goal_id = source_goal.carried_from
        goals.append(
            {
                "goal_id": goal.goal_id,
                "title": goal.title,
                "status": goal.status.value,
                "carried_from": goal.carried_from,
                "source_enrollment_id": goal.source_enrollment_id,
                "completed_at": iso(goal.completed_at) if goal.completed_at else None,
                "completed_in_offering": goal.completed_in_offering,
                "carry_chain": chain,
            }
        )

    result = {
        "enrollment_id": enrollment_id,
        "learner_id": learner.learner_id,
        "learner_name": learner.name,
        "period": {"start": iso(start), "end": iso(end)},
        "placements": placements,
        "sessions": sessions,
        "absences": absences,
        "absence_summary": absence_summary,
        "withdrawal_risk": service.withdrawal_risk(enrollment_id),
        "goals": goals,
        "reviews": list(enrollment.reviews),
    }
    result["narrative"] = _narrate(result)
    return result


def _narrate(report: dict) -> str:
    lines = [
        f"学员{report['learner_name']}（{report['enrollment_id']}）"
        f"在 {_fmt(datetime.fromisoformat(report['period']['start']))} 至 "
        f"{_fmt(datetime.fromisoformat(report['period']['end']))} 的连续学习档案："
    ]

    lines.append("一、授课形态安排")
    if not report["placements"]:
        lines.append("- 期间没有生效的授课安排。")
    for p in report["placements"]:
        since = _fmt(datetime.fromisoformat(p["valid_from"]))
        until = _fmt(datetime.fromisoformat(p["valid_to"])) if p["valid_to"] else "至今"
        lines.append(
            f"- {since} 至 {until}：{p['offering_title'] or p['offering_id']}"
            f"（{p['format_label']}），原因：{p['reason']}。"
        )

    lines.append("二、支持需求落实")
    needed: set[str] = set()
    gaps: set[str] = set()
    gap_sessions = 0
    for s in report["sessions"]:
        needed.update(s["supports_needed"])
        if s["support_gaps"]:
            gap_sessions += 1
            gaps.update(s["support_gaps"])
    if not report["sessions"]:
        lines.append("- 期间没有排定场次。")
    else:
        needed_text = "、".join(_need_labels(needed)) or "无"
        lines.append(
            f"- 期间共 {len(report['sessions'])} 场，需求为：{needed_text}；"
            f"其中 {len(report['sessions']) - gap_sessions} 场支持全部落实。"
        )
        if gaps:
            lines.append(f"- {gap_sessions} 场存在缺口：{'、'.join(_need_labels(gaps))}。")

    lines.append("三、缺席归因")
    if not report["absences"]:
        lines.append("- 期间无缺席。")
    else:
        parts = []
        for key, count in sorted(report["absence_summary"].items()):
            label = ATTRIBUTION_LABELS.get(AbsenceAttribution(key), key)
            mark = "（计入退学评估）" if key == AbsenceAttribution.VOLUNTARY.value else "（不计入退学评估）"
            parts.append(f"{label} {count} 次{mark}")
        lines.append(f"- 共缺席 {len(report['absences'])} 次：" + "；".join(parts) + "。")

    lines.append("四、学习目标接续")
    if not report["goals"]:
        lines.append("- 期间没有登记学习目标。")
    for g in report["goals"]:
        status = GOAL_STATUS_LABELS.get(GoalStatus(g["status"]), g["status"])
        text = f"- 「{g['title']}」{status}"
        if g["completed_at"]:
            done_at = _fmt(datetime.fromisoformat(g["completed_at"]))
            text += f"（{done_at} 于 {g['completed_in_offering']} 完成）"
        if g["carry_chain"]:
            origin = g["carry_chain"][-1]
            text += f"，最早来自 {origin['enrollment_id']} 的「{origin['title']}」，跨班接续未中断"
        lines.append(text + "。")
    return "\n".join(lines)


def _need_labels(values: set[str]) -> list[str]:
    from .model import SupportNeed

    return [NEED_LABELS[SupportNeed(v)] for v in sorted(values)]
