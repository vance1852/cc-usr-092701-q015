"""质量委员会聚合分析。

仅输出项目类别 × 时间段的去标识汇总，不返回任何患者级记录。单元人数低于
配置门槛时抑制整格；各指标再做互补抑制，避免用相邻筛选条件相减还原小数量。
导出（冻结）时固化数据快照版本、时间窗口与纳入规则，迟到更正不改变已导出结果。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import audit
from .db import Database, decode_json, encode_json
from .errors import NotFound, ValidationError
from .ids import new_id
from .security import authorize, principal_for
from .validation import calendar_date, choice, integer

FORMAT = "careflow-quality-aggregate-v1"
RULE_VERSION = 1
CATEGORIES = ("aesthetic", "weight")
GRANULARITIES = ("day", "week", "month", "quarter")
MAX_PERIODS = {"day": 31, "week": 13, "month": 12, "quarter": 4}
CONFIG_KEY = "default"

DEFAULT_CONFIG = {"min_cell_count": 5, "min_cell_delta": 2, "followup_window_days": 14}

# 纳入与计算规则随报表输出并进入冻结快照；修改语义须提升 RULE_VERSION。
INCLUSION_RULES: dict[str, Any] = {
    "rule_version": RULE_VERSION,
    "cohort": "诊所在诊患者，且在时间段内（按诊所时区解释的本地日期）存在已进入过生效状态"
              "（当前为 active/paused/completed）的对应类别计划，计划开始日期落在该时间段内。",
    "authorization": "患者须持有评估时仍有效（granted 且未到期）的 quality_aggregate 汇总分析授权；"
                     "撤回后不进入此后的任何快照，重新签署更高版本授权后方可再次纳入。",
    "headcount": "去重患者人数；同一患者在同一类别时间段内只计一次。",
    "appointment_window": "预约 starts_at 落在时间段界内，且必须关联对应类别计划；held 占位预约不计入。",
    "appointment_fulfilled": "履约数为状态 arrived/in_service/completed 的预约；scheduled 为除 held 外全部预约。",
    "followup_window": "随访 due_at 落在时间段界内且关联对应类别计划；cancelled 不计入分母，done 计为完成。",
    "followup_grace": "时间段结束后保留 followup_window_days 天宽限期；宽限期未结束时随访完成率为空。",
    "attribution": "项目类别一律取关联计划的 kind；无法归因到计划的预约与随访列入排除说明，不摊入任何类别。",
    "corrections": "观察值更正等迟到数据只影响此后的新计算；一旦冻结导出，数值与规则不再变化。",
    "granularity_note": "抑制门槛在日/周/月/季度各粒度一致生效；跨粒度相减时被抑制的细粒度格不返回数值，"
                        "但委员会仍应固定使用同一粒度（建议月）比较，不要混合粒度求和，以免把多个细粒度互补"
                        "拼成小样本估计。",
}

RULE_EXPLANATIONS = {
    "null_reasons": {
        "no_scheduled_appointments": "该时间段内没有可计入的预约，履约率分母为零。",
        "no_due_followups": "该时间段内没有到期随访，随访完成率分母为零。",
        "followup_grace_open": "随访宽限期尚未结束，完成率待宽限结束后再计算。",
        "period_not_closed": "时间段尚未结束，不输出指标。",
    },
    "suppression_reasons": {
        "cell_below_threshold": f"单元去重人数低于 min_cell_count，整格抑制以避免小样本重识别。",
        "complement_small": "指标某组成部分与其互补人数之差小于 min_cell_delta；"
                            "为防止相邻筛选相减还原被抑制数量，该指标整组不发布。",
        "excluded_count_small": "排除项数量过小可能间接识别患者，本次不发布该排除计数。",
        "config_conflict": "该时间段已按另一套规则冻结，不能用新规则改写。",
    },
    "exclusion_reasons": {
        "provisional_holds_excluded": "held 为临时占位，未确认的预约不计入履约统计。",
        "cancelled_followups_excluded": "已取消随访不代表随访义务，不计入完成率分母。",
        "appointments_without_plan": "未关联计划的预约无法归因到项目类别。",
        "followups_without_plan": "未关联计划的随访无法归因到项目类别。",
        "plan_not_activated": "计划从未进入生效状态（草稿/提议/未激活即取消）。",
        "authorization_missing": "患者缺少评估时有效的 quality_aggregate 汇总分析授权。",
        "patient_not_active": "患者档案已合并或关闭。",
    },
}


class QualityAnalyticsService:
    """质量委员会聚合分析；所有公开方法都只返回汇总值与规则解释。"""

    def __init__(self, database: Database, clock):
        self.db = database
        self.clock = clock

    # -- 配置 -----------------------------------------------------------

    def configure(self, clinic_id: str, actor_id: str, *, min_cell_count: int,
                  min_cell_delta: int, followup_window_days: int) -> dict[str, Any]:
        min_cell_count = integer(min_cell_count, "最小单元人数", minimum=2, maximum=50)
        min_cell_delta = integer(min_cell_delta, "最小互补差", minimum=1, maximum=25)
        followup_window_days = integer(followup_window_days, "随访宽限天数", minimum=1, maximum=90)
        if min_cell_count < 2 * min_cell_delta:
            raise ValidationError("最小单元人数必须至少为最小互补差的两倍，否则互补抑制无法成立")
        now = self._now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "quality:manage", clinic_id=clinic_id)
            self._clinic(connection, clinic_id)
            row = connection.execute("SELECT * FROM quality_aggregate_configs WHERE clinic_id=? AND key=?",
                                     (clinic_id, CONFIG_KEY)).fetchone()
            fingerprint = self._fingerprint(min_cell_count, min_cell_delta, followup_window_days)
            if row is None:
                config_id = new_id("qcfg")
                connection.execute(
                    "INSERT INTO quality_aggregate_configs(id,clinic_id,key,min_cell_count,min_cell_delta,"
                    "followup_window_days,active,created_by,created_at) VALUES(?,?,?,?,?,?,1,?,?)",
                    (config_id, clinic_id, CONFIG_KEY, min_cell_count, min_cell_delta,
                     followup_window_days, actor_id, now))
            else:
                config_id = row["id"]
                connection.execute(
                    "UPDATE quality_aggregate_configs SET min_cell_count=?,min_cell_delta=?,followup_window_days=?,"
                    "version=version+1 WHERE id=?",
                    (min_cell_count, min_cell_delta, followup_window_days, config_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="quality_config", aggregate_id=config_id,
                               action="quality.configured", occurred_at=now,
                               payload={"min_cell_count": min_cell_count, "min_cell_delta": min_cell_delta,
                                        "followup_window_days": followup_window_days, "fingerprint": fingerprint})
        return {"id": config_id, "key": CONFIG_KEY, "min_cell_count": min_cell_count,
                "min_cell_delta": min_cell_delta, "followup_window_days": followup_window_days,
                "fingerprint": fingerprint, "version": (row["version"] + 1) if row else 1}

    def get_config(self, clinic_id: str, actor_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "quality:read", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM quality_aggregate_configs WHERE clinic_id=? AND key=? AND active=1",
                                     (clinic_id, CONFIG_KEY)).fetchone()
            if row is None:
                return {"key": CONFIG_KEY, **DEFAULT_CONFIG, "fingerprint": self._fingerprint(**DEFAULT_CONFIG),
                        "default": True}
            return {"id": row["id"], "key": CONFIG_KEY, "min_cell_count": row["min_cell_count"],
                    "min_cell_delta": row["min_cell_delta"],
                    "followup_window_days": row["followup_window_days"],
                    "fingerprint": self._fingerprint(row["min_cell_count"], row["min_cell_delta"],
                                                      row["followup_window_days"]),
                    "version": row["version"], "default": False}

    def explain(self, clinic_id: str, actor_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "quality:read", clinic_id=clinic_id)
            self._clinic(connection, clinic_id)
        return {"format": FORMAT, "inclusion_rules": INCLUSION_RULES, **RULE_EXPLANATIONS}

    # -- 聚合快照 --------------------------------------------------------

    def report(self, clinic_id: str, actor_id: str, *, granularity: str, start_date: str,
               end_date: str, categories: list[str], freeze: bool = False) -> dict[str, Any]:
        granularity = choice(granularity, "时间段粒度", set(GRANULARITIES))
        first = date.fromisoformat(calendar_date(start_date, "起始日期"))
        last = date.fromisoformat(calendar_date(end_date, "结束日期"))
        if last < first:
            raise ValidationError("结束日期不能早于起始日期")
        if not isinstance(categories, list) or not categories:
            raise ValidationError("至少选择一个项目类别")
        selected = tuple(choice(item, "项目类别", set(CATEGORIES)) for item in categories)
        if len(set(selected)) != len(selected):
            raise ValidationError("项目类别不能重复")
        periods = self._periods(granularity, first, last)
        now = self._now()
        with self.db.transaction(write=freeze) as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "quality:manage" if freeze else "quality:read", clinic_id=clinic_id)
            clinic = self._clinic(connection, clinic_id)
            config = self._load_or_create_config(connection, clinic_id, actor_id, now, persist=freeze)
            eval_local_date = self.clock.now().astimezone(ZoneInfo(clinic["timezone"])).date()
            if freeze and periods[-1][1] + timedelta(days=config["followup_window_days"]) >= eval_local_date:
                raise ValidationError("冻结导出前，最晚时间段及其随访宽限期必须均已结束",
                                      details={"last_period_end": periods[-1][1].isoformat(),
                                               "grace_days": config["followup_window_days"],
                                               "today": eval_local_date.isoformat()})
            plans = connection.execute(
                "SELECT patient_id,kind,start_date FROM plans WHERE clinic_id=? AND kind IN ('aesthetic','weight') "
                "AND state IN ('active','paused','completed') AND start_date>=? AND start_date<=?",
                (clinic_id, periods[0][0].isoformat(), periods[-1][1].isoformat())).fetchall()
            authorized = {
                row["patient_id"] for row in connection.execute(
                    "SELECT patient_id FROM consents WHERE purpose='quality_aggregate' AND state='granted' "
                    "AND (expires_at IS NULL OR expires_at>?)", (now,))}
            active_patients = {
                row["id"] for row in connection.execute(
                    "SELECT id FROM patients WHERE clinic_id=? AND state='active'", (clinic_id,))}
            head = connection.execute(
                "SELECT sequence,digest FROM audit_events WHERE clinic_id=? ORDER BY sequence DESC LIMIT 1",
                (clinic_id,)).fetchone()
            head_sequence, audit_head = (head["sequence"], head["digest"]) if head else (0, "0" * 64)

            cells: list[dict[str, Any]] = []
            export_id = new_id("qexp") if freeze else None
            suppressed_cell_count = 0
            newly_frozen = 0
            for pstart, pend in periods:
                cell_window_start = self._bound(clinic, pstart, start=True)
                cell_window_end = self._bound(clinic, pend + timedelta(days=1), start=True)
                period_closed = pend < eval_local_date
                unattributed = self._unattributed_counts(connection, clinic_id, cell_window_start, cell_window_end)
                for category in selected:
                    row = connection.execute(
                        "SELECT * FROM quality_snapshots WHERE clinic_id=? AND period_start=? AND period_end=? "
                        "AND category=? AND config_id=?",
                        (clinic_id, pstart.isoformat(), pend.isoformat(), category, config["id"])).fetchone()
                    if row is not None and row["frozen_payload_json"] is not None:
                        # 已导出历史对任何后续规则都保持不变；指纹不同只在格内标注。
                        frozen_cell = decode_json(row["frozen_payload_json"])
                        if row["config_fingerprint"] != config["fingerprint"]:
                            frozen_cell["advisories"] = frozen_cell.get("advisories", []) + [
                                {"code": "frozen_under_other_config",
                                 "frozen_fingerprint": row["config_fingerprint"],
                                 "reason": "config_conflict"}]
                        cells.append(frozen_cell)
                        continue
                    base = {"period_start": pstart.isoformat(), "period_end": pend.isoformat(), "category": category,
                            "window": {"starts_at": cell_window_start, "ends_at": cell_window_end},
                            "config_fingerprint": config["fingerprint"], "inclusion_rules": INCLUSION_RULES,
                            "data_snapshot": {"audit_head_sequence": head_sequence, "audit_head_digest": audit_head,
                                              "snapshot_version": self._snapshot_version(
                                                  config["fingerprint"], head_sequence, audit_head)}}
                    if not period_closed:
                        cells.append({**base, "status": "not_closed",
                                      "nulls": [{"metric": "cell", "reason": "period_not_closed"}]})
                        continue
                    cohort = {
                        plan["patient_id"] for plan in plans
                        if plan["kind"] == category
                        and pstart.isoformat() <= plan["start_date"] <= pend.isoformat()
                        and plan["patient_id"] in authorized and plan["patient_id"] in active_patients}
                    cell = self._evaluate(connection, clinic_id, category, cohort, config,
                                          eval_local_date, pend, base, unattributed)
                    if cell["status"] == "suppressed":
                        suppressed_cell_count += 1
                    if freeze:
                        cells.append(self._persist_frozen(
                            connection, row, clinic_id, actor_id, now, granularity, category,
                            pstart, pend, config, cell, export_id))
                        newly_frozen += 1
                    else:
                        cells.append(cell)

            if freeze and newly_frozen == 0:
                # 整批时间段此前均已冻结：返回原结果，不生成新的导出编号或审计事件。
                export_id = cells[0].get("export_id") if cells else None
            elif freeze:
                audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                                   aggregate_type="quality_export", aggregate_id=export_id,
                                   action="quality.exported", occurred_at=now,
                                   payload={"export_id": export_id, "granularity": granularity,
                                            "periods": [[p[0].isoformat(), p[1].isoformat()] for p in periods],
                                            "categories": list(selected), "config_fingerprint": config["fingerprint"],
                                            "newly_frozen": newly_frozen, "suppressed_cells": suppressed_cell_count,
                                            "head_sequence": head_sequence})
        all_frozen = bool(cells) and all(cell.get("frozen") for cell in cells)
        return {"format": FORMAT, "clinic_id": clinic_id, "evaluated_at": now,
                "period_granularity": granularity,
                "start_date": first.isoformat(), "end_date": last.isoformat(),
                "categories": list(selected), "timezone": clinic["timezone"],
                "config": {"key": CONFIG_KEY, "min_cell_count": config["min_cell_count"],
                           "min_cell_delta": config["min_cell_delta"],
                           "followup_window_days": config["followup_window_days"],
                           "fingerprint": config["fingerprint"]},
                "inclusion_rules": INCLUSION_RULES,
                "export_id": export_id if freeze else None,
                "frozen": bool(freeze) and newly_frozen > 0,
                "replayed": (freeze and newly_frozen == 0) or (not freeze and all_frozen),
                "cells": cells}

    # -- 计算 ------------------------------------------------------------

    def _evaluate(self, connection, clinic_id: str, category: str, cohort: set[str], config: dict,
                  eval_local_date: date, pend: date, base: dict[str, Any], unattributed: dict) -> dict[str, Any]:
        headcount = len(cohort)
        if headcount < config["min_cell_count"]:
            return {**base, "status": "suppressed",
                    "suppressions": [{"metric": "cell", "reason": "cell_below_threshold",
                                      "threshold": config["min_cell_count"]}]}
        window_start = base["window"]["starts_at"]
        window_end = base["window"]["ends_at"]
        cell = {**base, "status": "included", "headcount": headcount,
                "appointments": None, "followups": None, "exclusions": []}
        for rule, count in (("appointments_without_plan", unattributed["appointments_without_plan"]),
                            ("followups_without_plan", unattributed["followups_without_plan"])):
            if count:
                cell["exclusions"].append({"rule": rule,
                                           "count": self._gated_count(count, config["min_cell_delta"])})
        self._evaluate_appointments(connection, clinic_id, category, window_start, window_end, cohort, config, cell)
        self._evaluate_followups(connection, clinic_id, category, pend, window_start, window_end,
                                 cohort, config, eval_local_date, cell)
        return cell

    def _unattributed_counts(self, connection, clinic_id: str, window_start: str, window_end: str) -> dict[str, int]:
        appointments = connection.execute(
            "SELECT COUNT(*) AS c FROM appointments WHERE clinic_id=? AND plan_id IS NULL "
            "AND starts_at>=? AND starts_at<? AND state!='held'",
            (clinic_id, window_start, window_end)).fetchone()["c"]
        followups = connection.execute(
            "SELECT COUNT(*) AS c FROM followups f JOIN patients p ON p.id=f.patient_id "
            "WHERE p.clinic_id=? AND f.plan_id IS NULL AND f.due_at>=? AND f.due_at<?",
            (clinic_id, window_start, window_end)).fetchone()["c"]
        return {"appointments_without_plan": appointments, "followups_without_plan": followups}

    def _evaluate_appointments(self, connection, clinic_id, category, window_start, window_end,
                               cohort, config, cell) -> None:
        marks = ",".join("?" for _ in cohort)
        rows = connection.execute(
            f"SELECT state,COUNT(*) AS c FROM appointments WHERE clinic_id=? AND plan_id IN "
            f"(SELECT id FROM plans WHERE clinic_id=? AND kind=?) AND starts_at>=? AND starts_at<? "
            f"AND patient_id IN ({marks}) GROUP BY state",
            (clinic_id, clinic_id, category, window_start, window_end, *cohort)).fetchall()
        counts = {row["state"]: row["c"] for row in rows}
        held = counts.get("held", 0)
        if held:
            cell["exclusions"].append({"rule": "provisional_holds_excluded",
                                       "count": self._gated_count(held, config["min_cell_delta"])})
        scheduled_states = ("booked", "arrived", "in_service", "completed", "cancelled", "no_show")
        components = {state: counts.get(state, 0) for state in scheduled_states}
        scheduled = sum(components.values())
        if self._complement_visible(scheduled, components, config["min_cell_delta"]):
            cell["appointments"] = None
            cell["suppressions"] = cell.get("suppressions", []) + [
                {"metric": "appointments", "reason": "complement_small", "threshold": config["min_cell_delta"]}]
            return
        fulfilled = sum(components[state] for state in ("arrived", "in_service", "completed"))
        cell["appointments"] = {
            "scheduled": scheduled, "fulfilled": fulfilled,
            "no_show": components["no_show"], "cancelled": components["cancelled"],
            "fulfillment_rate": round(fulfilled / scheduled, 4) if scheduled else None}
        if not scheduled:
            cell.setdefault("nulls", []).append({"metric": "appointments", "reason": "no_scheduled_appointments"})

    def _evaluate_followups(self, connection, clinic_id, category, pend, window_start, window_end,
                            cohort, config, eval_local_date, cell) -> None:
        marks = ",".join("?" for _ in cohort)
        rows = connection.execute(
            f"SELECT state,COUNT(*) AS c FROM followups WHERE plan_id IN (SELECT id FROM plans WHERE clinic_id=? AND kind=?) "
            f"AND due_at>=? AND due_at<? AND patient_id IN ({marks}) GROUP BY state",
            (clinic_id, category, window_start, window_end, *cohort)).fetchall()
        counts = {row["state"]: row["c"] for row in rows}
        cancelled = counts.get("cancelled", 0)
        if cancelled:
            cell["exclusions"].append({"rule": "cancelled_followups_excluded",
                                       "count": self._gated_count(cancelled, config["min_cell_delta"])})
        due = sum(counts.get(state, 0) for state in ("pending", "claimed", "done", "deferred"))
        grace_open = eval_local_date <= pend + timedelta(days=config["followup_window_days"])
        if due == 0:
            cell["followups"] = {"due": 0, "completed": 0, "completion_rate": None}
            cell.setdefault("nulls", []).append({"metric": "followups", "reason": "no_due_followups"})
            return
        if grace_open:
            cell["followups"] = {"due": due, "completed": None, "completion_rate": None}
            cell.setdefault("nulls", []).append({"metric": "followups", "reason": "followup_grace_open",
                                                  "grace_days": config["followup_window_days"]})
            return
        components = {"done": counts.get("done", 0),
                      "open": counts.get("pending", 0) + counts.get("claimed", 0),
                      "deferred": counts.get("deferred", 0)}
        if self._complement_visible(due, components, config["min_cell_delta"]):
            cell["followups"] = None
            cell["suppressions"] = cell.get("suppressions", []) + [
                {"metric": "followups", "reason": "complement_small", "threshold": config["min_cell_delta"]}]
            return
        cell["followups"] = {"due": due, "completed": components["done"], "deferred": components["deferred"],
                             "completion_rate": round(components["done"] / due, 4)}

    # -- 冻结与规则辅助 ---------------------------------------------------

    def _persist_frozen(self, connection, existing, clinic_id: str, actor_id: str, now: str,
                        granularity: str, category: str, pstart: date, pend: date,
                        config: dict, cell: dict, export_id: str) -> dict[str, Any]:
        window_start = cell["window"]["starts_at"]
        window_end = cell["window"]["ends_at"]
        payload = {**cell, "frozen": True, "frozen_at": now, "export_id": export_id,
                   "followup_grace_days": config["followup_window_days"]}
        snapshot = cell["data_snapshot"]["audit_head_sequence"]
        if existing is None:
            snapshot_id = new_id("qsn")
            connection.execute(
                "INSERT INTO quality_snapshots(id,clinic_id,period_start,period_end,period_granularity,category,"
                "config_id,config_fingerprint,window_start,window_end,followup_grace_days,head_sequence,audit_head,"
                "inclusion_rules_json,frozen_payload_json,frozen_at,exported_at,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (snapshot_id, clinic_id, pstart.isoformat(), pend.isoformat(), granularity, category,
                 config["id"], config["fingerprint"], window_start, window_end,
                 config["followup_window_days"], snapshot, cell["data_snapshot"]["audit_head_digest"],
                 encode_json(INCLUSION_RULES), encode_json(payload), now, now, actor_id, now))
        else:
            snapshot_id = existing["id"]
            connection.execute(
                "UPDATE quality_snapshots SET config_fingerprint=?,head_sequence=?,audit_head=?,"
                "inclusion_rules_json=?,frozen_payload_json=?,frozen_at=?,exported_at=?,version=version+1 WHERE id=?",
                (config["fingerprint"], snapshot, cell["data_snapshot"]["audit_head_digest"],
                 encode_json(INCLUSION_RULES), encode_json(payload), now, now, snapshot_id))
        payload["snapshot_id"] = snapshot_id
        return payload

    def _load_or_create_config(self, connection, clinic_id: str, actor_id: str, now: str,
                               *, persist: bool) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM quality_aggregate_configs WHERE clinic_id=? AND key=? AND active=1",
                                 (clinic_id, CONFIG_KEY)).fetchone()
        if row is not None:
            return {"id": row["id"], "min_cell_count": row["min_cell_count"],
                    "min_cell_delta": row["min_cell_delta"],
                    "followup_window_days": row["followup_window_days"],
                    "fingerprint": self._fingerprint(row["min_cell_count"], row["min_cell_delta"],
                                                      row["followup_window_days"])}
        fingerprint = self._fingerprint(**DEFAULT_CONFIG)
        if not persist:
            # 只读请求不隐式落库；冻结时才固化默认配置。
            return {"id": new_id("qcfg"), **DEFAULT_CONFIG, "fingerprint": fingerprint}
        config_id = new_id("qcfg")
        connection.execute(
            "INSERT INTO quality_aggregate_configs(id,clinic_id,key,min_cell_count,min_cell_delta,"
            "followup_window_days,active,created_by,created_at) VALUES(?,?,?,?,?,?,1,?,?)",
            (config_id, clinic_id, CONFIG_KEY, DEFAULT_CONFIG["min_cell_count"], DEFAULT_CONFIG["min_cell_delta"],
             DEFAULT_CONFIG["followup_window_days"], actor_id, now))
        audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                           aggregate_type="quality_config", aggregate_id=config_id,
                           action="quality.configured", occurred_at=now,
                           payload={**DEFAULT_CONFIG, "fingerprint": fingerprint, "default": True})
        return {"id": config_id, **DEFAULT_CONFIG, "fingerprint": fingerprint}

    @staticmethod
    def _complement_visible(total: int, components: dict[str, int], delta: int) -> bool:
        """某非零组成部分及其互补都非空且任一小到可还原个体时，整组指标不可发布。"""
        return any(0 < value < total and (value < delta or total - value < delta)
                   for value in components.values())

    @staticmethod
    def _gated_count(value: int, delta: int) -> int | None:
        return value if value >= delta else None

    @staticmethod
    def _fingerprint(min_cell_count: int, min_cell_delta: int, followup_window_days: int) -> str:
        body = {"format": FORMAT, "rule_version": RULE_VERSION, "min_cell_count": min_cell_count,
                "min_cell_delta": min_cell_delta, "followup_window_days": followup_window_days}
        return hashlib.sha256(encode_json(body).encode("utf-8")).hexdigest()[:16]

    def _snapshot_version(self, fingerprint: str, head_sequence: int, audit_head: str) -> str:
        body = {"fingerprint": fingerprint, "head_sequence": head_sequence, "audit_head": audit_head}
        return "snap-" + hashlib.sha256(encode_json(body).encode("utf-8")).hexdigest()[:16]

    def _periods(self, granularity: str, first: date, last: date) -> list[tuple[date, date]]:
        aligned = self._period_start(granularity, first)
        if aligned != first:
            raise ValidationError("起始日期必须与时间段粒度对齐",
                                  details={"expected": aligned.isoformat()})
        periods: list[tuple[date, date]] = []
        cursor = first
        while cursor <= last:
            nxt = self._next_period_start(granularity, cursor)
            pend = nxt - timedelta(days=1)
            periods.append((cursor, pend))
            cursor = nxt
        if periods[-1][1] != last:
            raise ValidationError("结束日期必须是某个完整时间段的最后一天",
                                  details={"expected_end": periods[-1][1].isoformat()})
        if len(periods) > MAX_PERIODS[granularity]:
            raise ValidationError(f"{granularity} 粒度一次最多请求 {MAX_PERIODS[granularity]} 个时间段")
        return periods

    @staticmethod
    def _period_start(granularity: str, day: date) -> date:
        if granularity == "day":
            return day
        if granularity == "week":
            return day - timedelta(days=day.weekday())
        if granularity == "month":
            return day.replace(day=1)
        quarter_month = ((day.month - 1) // 3) * 3 + 1
        return date(day.year, quarter_month, 1)

    @staticmethod
    def _next_period_start(granularity: str, day: date) -> date:
        if granularity == "day":
            return day + timedelta(days=1)
        if granularity == "week":
            return day + timedelta(days=7)
        months = 1 if granularity == "month" else 3
        month = day.month + months
        year = day.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        return date(year, month, 1)

    def _bound(self, clinic, day: date, *, start: bool) -> str:
        zone = ZoneInfo(clinic["timezone"])
        moment = datetime.combine(day, datetime.min.time(), zone).astimezone(UTC)
        return moment.isoformat(timespec="seconds").replace("+00:00", "Z")

    def _now(self) -> str:
        return self.clock.now().astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _clinic(connection, clinic_id: str):
        row = connection.execute("SELECT timezone,state FROM clinics WHERE id=?", (clinic_id,)).fetchone()
        if row is None:
            raise NotFound("诊所不存在")
        return row
