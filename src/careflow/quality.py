"""质量委员会汇总分析：不可变快照、小单元抑制与纳入规则留痕。

报表只输出项目类别（医美/体重管理）与对齐自然周期（月/季/年）的汇总，
不接受医生、患者或滑动窗口等任意筛选，从结构上消除相邻筛选相减的攻击面。
每个周期第一次生成时物化数据截止点（cutoff）：迟到更正与事后授权撤回都不
改变已导出结果；撤回质量汇总授权的患者不进入之后生成的快照。
"""

from __future__ import annotations

import calendar
import hashlib
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import __version__ as service_version, audit
from .db import decode_json, encode_json
from .errors import NotFound
from .ids import new_id
from .security import authorize, principal_for
from .validation import ValidationError, choice, integer, timestamp

RULES_VERSION = "quality-rules-v1"
FORMAT_VERSION = "quality-snapshot-v1"

MIN_THRESHOLD = 5
MAX_THRESHOLD = 50
DEFAULT_THRESHOLD = 5

GRANULARITIES = {"monthly", "quarterly", "yearly"}
CATEGORIES = (("aesthetic", "医美"), ("weight", "体重管理"))

# 预约：临时占位不进入履约统计；到达及以后视为履约。
APPOINTMENT_STATES_INCLUDED = ("booked", "arrived", "in_service", "completed", "cancelled", "no_show")
APPOINTMENT_STATES_KEPT = ("arrived", "in_service", "completed")
# 随访：已取消的任务不属于到期分母；done 为完成，deferred/pending/claimed 为未完成。
FOLLOWUP_STATES_INCLUDED = ("pending", "claimed", "done", "deferred")
FOLLOWUP_STATES_DONE = ("done",)

# 纳入规则代码同时对外返回，作为口径说明。
RULE_ACTIVE_PATIENT = "INCLUSION_ACTIVE_PATIENT_V1"
RULE_QUALITY_CONSENT = "INCLUSION_QUALITY_CONSENT_V1"
RULE_PARTICIPATION = "INCLUSION_CATEGORY_PARTICIPATION_V1"
RULE_APPOINTMENT_STATES = "INCLUSION_APPOINTMENT_STATES_V1"
RULE_FOLLOWUP_STATES = "INCLUSION_FOLLOWUP_STATES_V1"
RULE_TIMEZONE_WINDOW = "INCLUSION_CLINIC_TIMEZONE_WINDOW_V1"

REASON_EMPTY = "EMPTY_ELIGIBLE_PARTICIPANTS_V1"
REASON_SMALL_CELL = "SUPPRESSED_SMALL_CELL_V1"
REASON_COMPLEMENTARY = "SUPPRESSED_COMPLEMENTARY_RESIDUAL_V1"
REASON_SMALL_COUNT = "SUPPRESSED_SMALL_EVENT_COUNT_V1"
REASON_RATE_UNDEFINED = "METRIC_RATE_UNDEFINED_V1"

EXCLUSION_INACTIVE = "EXCLUDED_PATIENT_NOT_ACTIVE_V1"
EXCLUSION_CONSENT_MISSING = "EXCLUDED_QUALITY_CONSENT_MISSING_V1"
EXCLUSION_CONSENT_WITHDRAWN = "EXCLUDED_QUALITY_CONSENT_WITHDRAWN_V1"
EXCLUSION_CONSENT_EXPIRED = "EXCLUDED_QUALITY_CONSENT_EXPIRED_V1"
EXCLUSION_REASONS = (EXCLUSION_INACTIVE, EXCLUSION_CONSENT_MISSING,
                     EXCLUSION_CONSENT_WITHDRAWN, EXCLUSION_CONSENT_EXPIRED)


def _small(value: int, threshold: int) -> bool:
    """落在 1 至门槛-1 的精确计数都可能指向小群体，必须抑制。"""
    return 1 <= value < threshold


class QualitySnapshotService:
    """生成并读取质量委员会汇总快照。"""

    def __init__(self, database, clock):
        self.db = database
        self.clock = clock

    # ------------------------------------------------------------------ 入口

    def create_or_get(self, clinic_id: str, actor_id: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ValidationError("请求体必须为对象")
        granularity = choice(body.get("granularity", ""), "统计粒度", GRANULARITIES)
        year = integer(body.get("year"), "年份", minimum=2000, maximum=2100)
        threshold = integer(body.get("threshold", DEFAULT_THRESHOLD), "抑制门槛",
                            minimum=MIN_THRESHOLD, maximum=MAX_THRESHOLD)
        quarter = integer(body.get("quarter"), "季度", minimum=1, maximum=4) if granularity == "quarterly" else None
        month = integer(body.get("month"), "月份", minimum=1, maximum=12) if granularity == "monthly" else None
        periods = self._periods(granularity, year, quarter, month)
        period_start = periods[0][0]
        period_end = periods[-1][1]
        request_key = {
            "clinic_id": clinic_id, "granularity": granularity, "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(), "threshold": threshold, "rules_version": RULES_VERSION,
        }
        request_hash = hashlib.sha256(encode_json(request_key).encode("utf-8")).hexdigest()
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "quality:read", clinic_id=clinic_id)
            clinic = connection.execute("SELECT timezone,state FROM clinics WHERE id=?", (clinic_id,)).fetchone()
            if clinic is None:
                raise NotFound("诊所不存在")
            existing = connection.execute(
                "SELECT id,result_json FROM quality_snapshots WHERE clinic_id=? AND granularity=? "
                "AND period_start=? AND period_end=? AND threshold=? AND rules_version=?",
                (clinic_id, granularity, period_start.isoformat(), period_end.isoformat(),
                 threshold, RULES_VERSION)).fetchone()
            if existing:
                result = decode_json(existing["result_json"])
                result["replayed"] = True
                audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                                   aggregate_type="quality_snapshot", aggregate_id=existing["id"],
                                   action="quality.snapshot_replayed", occurred_at=now,
                                   payload={"request_hash": request_hash})
                return result
            snapshot = self._build(connection, clinic_id, clinic["timezone"], periods, granularity,
                                   threshold, now)
            snapshot_id = new_id("qsn")
            snapshot["snapshot_id"] = snapshot_id
            persisted = {key: value for key, value in snapshot.items() if key != "replayed"}
            connection.execute(
                "INSERT INTO quality_snapshots(id,clinic_id,granularity,period_start,period_end,threshold,"
                "rules_version,format_version,data_cutoff_at,source_digest,status,request_hash,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,'finalized',?,?,?,?)",
                (snapshot_id, clinic_id, granularity, period_start.isoformat(), period_end.isoformat(), threshold,
                 RULES_VERSION, FORMAT_VERSION, snapshot["data_cutoff_at"], snapshot["source_digest"],
                 request_hash, encode_json(persisted), actor_id, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="quality_snapshot", aggregate_id=snapshot_id,
                               action="quality.snapshot_generated", occurred_at=now,
                               payload={"granularity": granularity, "period_start": period_start.isoformat(),
                                        "period_end": period_end.isoformat(), "threshold": threshold,
                                        "data_cutoff_at": snapshot["data_cutoff_at"],
                                        "source_digest": snapshot["source_digest"], "grid_cells": len(snapshot["cells"])})
            return {**snapshot}

    def list_snapshots(self, clinic_id: str, actor_id: str, *, granularity: str | None = None,
                       year: int | None = None, limit: int = 100) -> dict:
        limit = max(1, min(limit, 200))
        clauses, params = ["clinic_id=?"], [clinic_id]
        if granularity:
            clauses.append("granularity=?")
            params.append(choice(granularity, "统计粒度", GRANULARITIES))
        if year is not None:
            year = integer(year, "年份", minimum=2000, maximum=2100)
            clauses.append("CAST(strftime('%Y',period_start) AS INTEGER)=?")
            params.append(year)
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "quality:read", clinic_id=clinic_id)
            rows = connection.execute(
                f"SELECT id,granularity,period_start,period_end,threshold,rules_version,format_version,"
                f"data_cutoff_at,source_digest,created_by,created_at FROM quality_snapshots "
                f"WHERE {' AND '.join(clauses)} ORDER BY period_start DESC,created_at DESC LIMIT ?",
                (*params, limit)).fetchall()
            return {"clinic_id": clinic_id, "items": [dict(row) for row in rows], "returned": len(rows)}

    def get_snapshot(self, clinic_id: str, actor_id: str, snapshot_id: str) -> dict:
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "quality:read", clinic_id=clinic_id)
            row = connection.execute("SELECT id,result_json FROM quality_snapshots WHERE id=? AND clinic_id=?",
                                     (snapshot_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("质量汇总快照不存在")
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="quality_snapshot", aggregate_id=row["id"],
                               action="quality.snapshot_viewed", occurred_at=now, payload={})
            result = decode_json(row["result_json"])
            result["replayed"] = True
            return result

    # ------------------------------------------------------------------ 周期

    @staticmethod
    def _periods(granularity: str, year: int, quarter: int | None, month: int | None):
        """返回 [(本地首日, 本地末日), ...]，周期严格对齐年/季/月网格。"""
        if granularity == "monthly":
            months = [month]
        elif granularity == "quarterly":
            months = list(range((quarter - 1) * 3 + 1, quarter * 3 + 1))
        else:
            months = list(range(1, 13))
        result = []
        for value in months:
            last_day = calendar.monthrange(year, value)[1]
            result.append((date(year, value, 1), date(year, value, last_day)))
        return result

    # ------------------------------------------------------------------ 取数

    def _build(self, connection, clinic_id: str, timezone_name: str, periods, granularity: str,
               threshold: int, now: str) -> dict:
        zone = ZoneInfo(timezone_name)
        window = []
        for first_day, last_day in periods:
            low = datetime.combine(first_day, datetime.min.time(), zone).astimezone(UTC)
            high = datetime.combine(last_day + timedelta(days=1), datetime.min.time(), zone).astimezone(UTC)
            starts_at, ends_at = timestamp(low), timestamp(high)
            window.append({"period": first_day.strftime("%Y-%m"), "start_date": first_day.isoformat(),
                           "end_date": last_day.isoformat(), "starts_at": starts_at, "ends_at": ends_at})
        period_keys = [item["period"] for item in window]
        span_low, span_high = window[0]["starts_at"], window[-1]["ends_at"]

        # 候选总体：在诊区内拥有医美/体重管理计划的全部患者（含已停用，供排除统计）。
        candidates = connection.execute(
            "SELECT p.id AS patient_id,p.state AS patient_state FROM patients p "
            "WHERE p.clinic_id=? AND EXISTS(SELECT 1 FROM plans pl WHERE pl.patient_id=p.id "
            "AND pl.kind IN ('aesthetic','weight'))", (clinic_id,)).fetchall()
        candidate_state = {row["patient_id"]: row["patient_state"] for row in candidates}

        consent_rows = connection.execute(
            "SELECT c.patient_id,c.state,c.expires_at FROM consents c JOIN patients p ON p.id=c.patient_id "
            "WHERE p.clinic_id=? AND c.purpose='quality_aggregation' AND c.revision=("
            "SELECT MAX(c2.revision) FROM consents c2 WHERE c2.patient_id=c.patient_id "
            "AND c2.purpose='quality_aggregation')", (clinic_id,)).fetchall()
        consent_state = {row["patient_id"]: (row["state"], row["expires_at"]) for row in consent_rows}

        plans = connection.execute(
            "SELECT id,patient_id,kind,state,start_date,updated_at FROM plans "
            "WHERE clinic_id=? AND kind IN ('aesthetic','weight')", (clinic_id,)).fetchall()
        appointments = connection.execute(
            "SELECT a.id,a.plan_id,a.starts_at,a.state FROM appointments a JOIN plans pl ON pl.id=a.plan_id "
            "WHERE a.clinic_id=? AND pl.kind IN ('aesthetic','weight') AND a.starts_at>=? AND a.starts_at<?",
            (clinic_id, span_low, span_high)).fetchall()
        followups = connection.execute(
            "SELECT f.id,f.plan_id,f.due_at,f.state FROM followups f JOIN plans pl ON pl.id=f.plan_id "
            "WHERE pl.clinic_id=? AND pl.kind IN ('aesthetic','weight') AND f.due_at>=? AND f.due_at<?",
            (clinic_id, span_low, span_high)).fetchall()
        source_digest = self._source_digest(candidate_state, consent_state, plans, appointments, followups)

        def eligible(patient_id: str) -> bool:
            return self._exclusion_code(patient_id, candidate_state, consent_state, now) is None

        raw = {code: {key: {"patients": set(), "apt_total": 0, "apt_kept": 0, "fol_due": 0,
                            "fol_done": 0, "completed": 0} for key in period_keys}
               for code, _label in CATEGORIES}

        plan_by_id = {row["id"]: row for row in plans}
        for plan in plans:
            patient_id = plan["patient_id"]
            if patient_id not in candidate_state or not eligible(patient_id):
                continue
            bucket = raw[plan["kind"]]
            start_period = plan["start_date"][:7]
            if start_period in bucket:
                bucket[start_period]["patients"].add(patient_id)
            if plan["state"] == "completed":
                completed_period = self._local_period(plan["updated_at"], zone)
                if completed_period in bucket:
                    bucket[completed_period]["completed"] += 1
                    bucket[completed_period]["patients"].add(patient_id)

        for row in appointments:
            plan = plan_by_id.get(row["plan_id"])
            if plan is None or not eligible(plan["patient_id"]):
                continue
            if row["state"] not in APPOINTMENT_STATES_INCLUDED:
                continue
            bucket = raw[plan["kind"]].get(self._local_period(row["starts_at"], zone))
            if bucket is None:
                continue
            bucket["apt_total"] += 1
            bucket["patients"].add(plan["patient_id"])
            if row["state"] in APPOINTMENT_STATES_KEPT:
                bucket["apt_kept"] += 1

        for row in followups:
            plan = plan_by_id.get(row["plan_id"])
            if plan is None or not eligible(plan["patient_id"]):
                continue
            if row["state"] not in FOLLOWUP_STATES_INCLUDED:
                continue
            bucket = raw[plan["kind"]].get(self._local_period(row["due_at"], zone))
            if bucket is None:
                continue
            bucket["fol_due"] += 1
            bucket["patients"].add(plan["patient_id"])
            if row["state"] in FOLLOWUP_STATES_DONE:
                bucket["fol_done"] += 1

        cells, leaf_views = self._release_leaves(raw, period_keys, threshold)
        period_totals, category_totals, grand_total = self._release_totals(
            raw, period_keys, threshold, leaf_views)
        exclusions = self._exclusion_summary(candidate_state, plans, consent_state, now, threshold)

        return {
            "format_version": FORMAT_VERSION,
            "replayed": False,
            "clinic_id": clinic_id,
            "status": "finalized",
            "generated_at": now,
            "data_cutoff_at": now,
            "source_digest": source_digest,
            "service_version": service_version,
            "timezone": timezone_name,
            "window": {"granularity": granularity, "periods": window},
            "categories": [{"code": code, "label": label} for code, label in CATEGORIES],
            "inclusion_rules": {
                "rules_version": RULES_VERSION,
                "rules": [
                    {"code": RULE_ACTIVE_PATIENT, "description": "仅纳入 cutoff 时在诊（active）患者。"},
                    {"code": RULE_QUALITY_CONSENT,
                     "description": "cutoff 时须存在有效的 quality_aggregation 质量汇总/研究授权。"},
                    {"code": RULE_PARTICIPATION,
                     "description": "患者在该周期存在对应类别计划开始、关联预约、关联随访或计划完成方计入人数。"},
                    {"code": RULE_APPOINTMENT_STATES,
                     "description": "预约分母排除 held 占位；arrived/in_service/completed 计为履约，no_show/cancelled 计为未履约。"},
                    {"code": RULE_FOLLOWUP_STATES,
                     "description": "随访分母排除 cancelled；done 计为完成，deferred/pending/claimed 为未完成。"},
                    {"code": RULE_TIMEZONE_WINDOW, "description": "周期边界按诊所时区的本地日界解释。"},
                ],
                "consent_purpose": "quality_aggregation",
                "category_attribution": "仅统计显式关联到医美/体重管理计划的预约与随访；无计划关联的事件不可归因，已排除。",
                "appointment_states": {"included": list(APPOINTMENT_STATES_INCLUDED),
                                       "kept": list(APPOINTMENT_STATES_KEPT)},
                "followup_states": {"included": list(FOLLOWUP_STATES_INCLUDED),
                                    "completed": list(FOLLOWUP_STATES_DONE)},
            },
            "suppression": {
                "threshold": threshold,
                "policies": [
                    {"code": REASON_SMALL_CELL, "description": "单元去重人数为 1 至门槛-1 时抑制整单元。"},
                    {"code": REASON_COMPLEMENTARY,
                     "description": "合计与其已发布子单元之差为 1 至门槛-1 时抑制合计，防止相减还原。"},
                    {"code": REASON_SMALL_COUNT,
                     "description": "事件计数、完成量或其互补差为 1 至门槛-1 时抑制该计数及派生比率。"},
                ],
            },
            "cells": cells,
            "period_totals": period_totals,
            "category_totals": category_totals,
            "grand_total": grand_total,
            "exclusions": exclusions,
        }

    # ------------------------------------------------------------- 叶子发布

    def _release_leaves(self, raw, period_keys, threshold):
        cells = []
        views = {}
        for code, _label in CATEGORIES:
            for period in period_keys:
                data = raw[code][period]
                patients = len(data["patients"])
                view = {"scope": "cell", "category": code, "period": period}
                if patients == 0:
                    view.update(status="empty", reasons=[REASON_EMPTY], patients=0,
                                appointments=self._empty_event_block("预约"),
                                followups=self._empty_event_block("随访"),
                                programs={"status": "empty", "completed": 0, "reasons": []})
                elif _small(patients, threshold):
                    view.update(status="suppressed", reasons=[REASON_SMALL_CELL], patients=None,
                                appointments=None, followups=None, programs=None)
                else:
                    view.update(status="reported", reasons=[], patients=patients,
                                appointments=self._event_block(data["apt_total"], data["apt_kept"], threshold, "预约"),
                                followups=self._event_block(data["fol_due"], data["fol_done"], threshold, "随访"),
                                programs=self._program_block(data["completed"], threshold))
                cells.append(view)
                views[(code, period)] = view
        return cells, views

    @staticmethod
    def _empty_event_block(label: str) -> dict:
        if label == "预约":
            return {"status": "empty", "scheduled": 0, "kept": 0, "fulfillment_rate": None,
                    "reasons": [REASON_RATE_UNDEFINED]}
        return {"status": "empty", "due": 0, "completed": 0, "completion_rate": None,
                "reasons": [REASON_RATE_UNDEFINED]}

    def _event_block(self, total: int, done: int, threshold: int, label: str) -> dict:
        if label == "预约":
            total_key, done_key, rate_key = "scheduled", "kept", "fulfillment_rate"
        else:
            total_key, done_key, rate_key = "due", "completed", "completion_rate"
        if total == 0:
            return self._empty_event_block(label)
        if _small(total, threshold):
            return {"status": "suppressed", total_key: None, done_key: None, rate_key: None,
                    "reasons": [REASON_SMALL_COUNT]}
        reasons = []
        published_done = done
        if _small(done, threshold) or _small(total - done, threshold):
            published_done = None
            rate = None
            reasons.append(REASON_SMALL_COUNT)
        else:
            rate = round(done / total, 4)
        return {"status": "reported", total_key: total, done_key: published_done, rate_key: rate,
                "reasons": reasons}

    @staticmethod
    def _program_block(count: int, threshold: int) -> dict:
        if count == 0:
            return {"status": "empty", "completed": 0, "reasons": []}
        if _small(count, threshold):
            return {"status": "suppressed", "completed": None, "reasons": [REASON_SMALL_COUNT]}
        return {"status": "reported", "completed": count, "reasons": []}

    # ------------------------------------------------------------- 合计发布

    def _release_totals(self, raw, period_keys, threshold, leaf_views):
        def group(keys):
            """聚合一组叶子的真值集合/计数与已发布部分。"""
            patients_true: set = set()
            patients_visible: set = set()
            sums = {"apt_total": 0, "apt_kept": 0, "fol_due": 0, "fol_done": 0, "completed": 0}
            visible = {"apt_total": 0, "apt_kept": 0, "apt_total_in_kept_visible": 0,
                       "fol_due": 0, "fol_done": 0, "fol_due_in_done_visible": 0, "completed": 0}
            for code, period in keys:
                data = raw[code][period]
                view = leaf_views[(code, period)]
                patients_true |= data["patients"]
                if view["status"] == "reported":
                    patients_visible |= data["patients"]
                    apt = view["appointments"]
                    if apt["status"] == "reported" and apt["scheduled"] is not None:
                        visible["apt_total"] += data["apt_total"]
                        if apt["kept"] is not None:
                            visible["apt_kept"] += data["apt_kept"]
                            visible["apt_total_in_kept_visible"] += data["apt_total"]
                    fol = view["followups"]
                    if fol["status"] == "reported" and fol["due"] is not None:
                        visible["fol_due"] += data["fol_due"]
                        if fol["completed"] is not None:
                            visible["fol_done"] += data["fol_done"]
                            visible["fol_due_in_done_visible"] += data["fol_due"]
                    programs = view["programs"]
                    if programs["status"] == "reported" and programs["completed"] is not None:
                        visible["completed"] += data["completed"]
                for name in sums:
                    sums[name] += data[name]
            return patients_true, patients_visible, sums, visible

        def total_view(scope, keys, *, category=None, period=None):
            patients_true, patients_visible, sums, visible = group(keys)
            n_true = len(patients_true)
            residual = n_true - len(patients_visible)
            view = {"scope": scope, "category": category, "period": period}
            if n_true == 0:
                view.update(status="empty", reasons=[REASON_EMPTY], patients=0,
                            appointments=self._empty_event_block("预约"),
                            followups=self._empty_event_block("随访"),
                            programs={"status": "empty", "completed": 0, "reasons": []})
                return view
            if _small(residual, threshold):
                view.update(status="suppressed", reasons=[REASON_COMPLEMENTARY], patients=None,
                            appointments=None, followups=None, programs=None)
                return view
            view.update(status="reported", reasons=[], patients=n_true,
                        appointments=self._total_event_block(
                            sums["apt_total"], sums["apt_kept"], visible["apt_total"],
                            visible["apt_kept"], visible["apt_total_in_kept_visible"], threshold, "预约"),
                        followups=self._total_event_block(
                            sums["fol_due"], sums["fol_done"], visible["fol_due"],
                            visible["fol_done"], visible["fol_due_in_done_visible"], threshold, "随访"),
                        programs=self._total_program_block(sums["completed"], visible["completed"], threshold))
            return view

        period_totals = []
        for key in period_keys:
            keys = [(code, key) for code, _ in CATEGORIES]
            period_totals.append(total_view("period_total", keys, period=key))
        category_totals = []
        for code, _label in CATEGORIES:
            keys = [(code, key) for key in period_keys]
            category_totals.append(total_view("category_total", keys, category=code))
        grand_total = total_view("grand_total", [(code, key) for code, _ in CATEGORIES for key in period_keys])
        return period_totals, category_totals, grand_total

    def _total_event_block(self, total_true, done_true, total_visible, done_visible,
                           total_in_done_visible, threshold, label):
        """合计事件块：总量残差与完成量残差任一落在 1..k-1 都抑制对应部分。"""
        if label == "预约":
            total_key, done_key, rate_key = "scheduled", "kept", "fulfillment_rate"
        else:
            total_key, done_key, rate_key = "due", "completed", "completion_rate"
        if total_true == 0:
            return self._empty_event_block(label)
        total_residual = total_true - total_visible
        if _small(total_true, threshold) or _small(total_residual, threshold):
            reason = REASON_COMPLEMENTARY if _small(total_residual, threshold) else REASON_SMALL_COUNT
            return {"status": "suppressed", total_key: None, done_key: None, rate_key: None,
                    "reasons": [reason]}
        hidden_done = done_true - done_visible
        hidden_not_done = (total_true - done_true) - (total_in_done_visible - done_visible)
        if _small(done_true, threshold) or _small(total_true - done_true, threshold) \
                or _small(hidden_done, threshold) or _small(hidden_not_done, threshold):
            reasons = []
            if _small(hidden_done, threshold) or _small(hidden_not_done, threshold):
                reasons.append(REASON_COMPLEMENTARY)
            if _small(done_true, threshold) or _small(total_true - done_true, threshold):
                reasons.append(REASON_SMALL_COUNT)
            return {"status": "reported", total_key: total_true, done_key: None, rate_key: None,
                    "reasons": reasons or [REASON_SMALL_COUNT]}
        return {"status": "reported", total_key: total_true, done_key: done_true,
                rate_key: round(done_true / total_true, 4), "reasons": []}

    @staticmethod
    def _total_program_block(count_true, count_visible, threshold):
        if count_true == 0:
            return {"status": "empty", "completed": 0, "reasons": []}
        residual = count_true - count_visible
        if _small(count_true, threshold) or _small(residual, threshold):
            reason = REASON_COMPLEMENTARY if _small(residual, threshold) else REASON_SMALL_COUNT
            return {"status": "suppressed", "completed": None, "reasons": [reason]}
        return {"status": "reported", "completed": count_true, "reasons": []}

    # ------------------------------------------------------------- 排除/指纹

    def _exclusion_code(self, patient_id, candidate_state, consent_state, cutoff):
        if candidate_state.get(patient_id) != "active":
            return EXCLUSION_INACTIVE
        state = consent_state.get(patient_id)
        if state is None:
            return EXCLUSION_CONSENT_MISSING
        consent_status, expires_at = state
        if consent_status == "withdrawn":
            return EXCLUSION_CONSENT_WITHDRAWN
        if consent_status == "expired" or (expires_at and expires_at <= cutoff):
            return EXCLUSION_CONSENT_EXPIRED
        return None

    def _exclusion_summary(self, candidate_state, plans, consent_state, cutoff, threshold):
        """报表级排除计数本身也按门槛抑制；各类别候选总体不对外发布，封闭互减。"""
        patient_categories: dict[str, set[str]] = {}
        for plan in plans:
            patient_categories.setdefault(plan["patient_id"], set()).add(plan["kind"])
        counts: dict[tuple[str, str], int] = {}
        for patient_id, categories in patient_categories.items():
            code = self._exclusion_code(patient_id, candidate_state, consent_state, cutoff)
            if code is None:
                continue
            for category in categories:
                counts[(category, code)] = counts.get((category, code), 0) + 1
        result = []
        for category, _label in CATEGORIES:
            for reason in EXCLUSION_REASONS:
                count = counts.get((category, reason), 0)
                if count == 0:
                    result.append({"category": category, "code": reason, "status": "empty", "count": 0})
                elif _small(count, threshold):
                    result.append({"category": category, "code": reason, "status": "suppressed", "count": None})
                else:
                    result.append({"category": category, "code": reason, "status": "reported", "count": count})
        return result

    @staticmethod
    def _local_period(value: str, zone: ZoneInfo) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(zone).strftime("%Y-%m")

    @staticmethod
    def _source_digest(candidate_state, consent_state, plans, appointments, followups) -> str:
        payload = {
            "patients": sorted(candidate_state.items()),
            "consents": sorted((patient_id, state, expires_at)
                               for patient_id, (state, expires_at) in consent_state.items()),
            "plans": sorted((row["id"], row["patient_id"], row["kind"], row["state"],
                             row["start_date"], row["updated_at"]) for row in plans),
            "appointments": sorted((row["id"], row["plan_id"], row["starts_at"], row["state"])
                                   for row in appointments),
            "followups": sorted((row["id"], row["plan_id"], row["due_at"], row["state"]) for row in followups),
        }
        return hashlib.sha256(encode_json(payload).encode("utf-8")).hexdigest()
