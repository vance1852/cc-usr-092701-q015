"""质量委员会汇总快照的权限、抑制、不可变性与排除规则测试。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.api import create_handler
from careflow.clock import FrozenClock
from careflow.db import Database, decode_json
from careflow.errors import Forbidden, NotFound, Unauthorized, ValidationError
from careflow.service import Careflow


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class QualityCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "诊所负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        self.clinician = self.app.create_staff(self.clinic, "临床医生", "clinician", actor_id=self.owner)["id"]
        self.nurse = self.app.create_staff(self.clinic, "护理人员", "nurse", actor_id=self.owner)["id"]
        self.coordinator = self.app.create_staff(self.clinic, "运营协调员", "coordinator", actor_id=self.owner)["id"]
        self.auditor = self.app.create_staff(self.clinic, "内审员", "auditor", actor_id=self.owner)["id"]
        self.officer = self.app.create_staff(self.clinic, "质量专员", "quality_officer",
                                             actor_id=self.owner)["id"]
        self.refs = {}

    def tearDown(self):
        self.temp.cleanup()

    # ------------------------------------------------------------ 构造辅助

    def make_patient(self, ref: str, kind: str = "weight", *, quality: bool = True,
                     start: str = "2026-09-20", quality_state: str = "granted"):
        patient = self.app.create_patient(self.clinic, self.coordinator, ref, f"患者{ref}")
        if quality:
            consent = self.app.grant_consent(self.clinic, self.clinician, patient["id"],
                                             "quality_aggregation", 1, digest(f"quality-{ref}"))
            if quality_state == "withdrawn":
                self.app.withdraw_consent(self.clinic, self.clinician, consent["id"], "患者撤回汇总授权")
        purpose = "weight_program" if kind == "weight" else "aesthetic_procedure"
        program = self.app.grant_consent(self.clinic, self.clinician, patient["id"], purpose, 1,
                                         digest(f"{purpose}-{ref}"))
        plan = self.app.create_plan(
            self.clinic, self.clinician, patient["id"], kind, self.clinician,
            {"description": "质量统计测试计划"}, {}, start, target_date="2026-12-31", consent_id=program["id"])
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 1, "propose")
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 2, "activate")
        self.refs[ref] = patient
        return patient, plan

    def appointment(self, patient_id: str, plan_id: str, index: int, *, end_state: str = "arrive",
                    month: int = 9, day: int = 29):
        """08:00 起每名患者 30 分钟错峰，避免同一医生时段重叠。"""
        start_hour = 8 + index // 2
        start_minute = (index % 2) * 30
        end_hour, end_minute = (start_hour, 30) if start_minute == 0 else (start_hour + 1, 0)
        starts = f"2026-{month:02d}-{day:02d}T{start_hour:02d}:{start_minute:02d}:00+08:00"
        ends = f"2026-{month:02d}-{day:02d}T{end_hour:02d}:{end_minute:02d}:00+08:00"
        apt = self.app.create_appointment(self.clinic, self.coordinator, patient_id, "复诊",
                                          starts, ends, f"apt-{patient_id}-{index}-{month}{day}",
                                          staff_id=self.clinician, plan_id=plan_id)
        self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 1, "book")
        if end_state != "book":
            reason = "未到诊" if end_state == "no_show" else None
            self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 2, end_state,
                                            reason=reason)
        return apt

    def done_followup(self, patient_id: str, plan_id: str, tag: str, *, outcome: str = "done"):
        fup = self.app.schedule_followup(self.clinic, self.nurse, patient_id,
                                         "2026-09-27T11:00:00Z", "周期随访", f"fup-{tag}", plan_id=plan_id)
        claimed_items = self.app.claim_followups(self.clinic, self.nurse, limit=50)
        claimed = next(item for item in claimed_items if item["id"] == fup["id"])
        if outcome == "done":
            self.app.complete_followup(self.clinic, self.nurse, fup["id"], claimed["claim_token"], "已完成",
                                       claimed["version"])
        return fup

    def cell(self, snapshot, category: str, period: str = "2026-09"):
        return next(c for c in snapshot["cells"] if c["category"] == category and c["period"] == period)

    def snapshot(self, *, granularity="monthly", year=2026, month=9, threshold=5, actor=None):
        body = {"granularity": granularity, "year": year, "threshold": threshold}
        if granularity == "monthly":
            body["month"] = month
        if granularity == "quarterly":
            body["quarter"] = (month - 1) // 3 + 1
        return self.app.quality.create_or_get(self.clinic, actor or self.officer, body)

    # ------------------------------------------------------------ 权限

    def test_quality_role_is_separate_and_cannot_read_patient_records(self):
        self.assertTrue(self.snapshot()["snapshot_id"])
        for actor in (self.clinician, self.nurse, self.coordinator, self.auditor):
            with self.assertRaises(Forbidden):
                self.app.quality.create_or_get(self.clinic, actor,
                                               {"granularity": "monthly", "year": 2026, "month": 9})
        patient = self.app.create_patient(self.clinic, self.coordinator, "qx-1", "某患者")
        with self.assertRaises(Forbidden):
            self.app.get_patient(self.clinic, self.officer, patient["id"])
        with self.assertRaises(Forbidden):
            self.app.reports.daily_operations(self.clinic, self.officer)

    def test_clinic_boundary_is_enforced_for_snapshots(self):
        other = self.app.create_clinic("协作城市门诊", "UTC")
        other_owner = self.app.create_staff(other["id"], "外院负责人", "owner")
        outsider = self.app.create_staff(other["id"], "外院质量员", "quality_officer",
                                         actor_id=other_owner["id"])["id"]
        with self.assertRaises((Unauthorized, NotFound)):
            self.app.quality.create_or_get(self.clinic, outsider,
                                           {"granularity": "monthly", "year": 2026, "month": 9})
        own = self.app.quality.create_or_get(other["id"], outsider,
                                             {"granularity": "monthly", "year": 2026, "month": 9})
        with self.assertRaises((Unauthorized, NotFound)):
            self.app.quality.get_snapshot(self.clinic, outsider, own["snapshot_id"])

    def test_invalid_granularity_and_threshold_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.app.quality.create_or_get(self.clinic, self.officer,
                                           {"granularity": "weekly", "year": 2026})
        with self.assertRaises(ValidationError):
            self.app.quality.create_or_get(self.clinic, self.officer,
                                           {"granularity": "monthly", "year": 2026, "month": 9, "threshold": 4})
        with self.assertRaises(ValidationError):
            self.app.quality.create_or_get(self.clinic, self.officer,
                                           {"granularity": "quarterly", "year": 2026})

    # ------------------------------------------------------------ 汇总口径

    def test_monthly_snapshot_counts_people_appointments_and_followups(self):
        for i in range(5):
            patient, plan = self.make_patient(f"w-{i}")
            self.appointment(patient["id"], plan["id"], i)
            self.done_followup(patient["id"], plan["id"], f"w-{i}")
        snap = self.snapshot()
        weight = self.cell(snap, "weight")
        aesthetic = self.cell(snap, "aesthetic")
        self.assertEqual(weight["status"], "reported")
        self.assertEqual(weight["patients"], 5)
        self.assertEqual(weight["appointments"]["scheduled"], 5)
        self.assertEqual(weight["appointments"]["kept"], 5)
        self.assertEqual(weight["appointments"]["fulfillment_rate"], 1.0)
        self.assertEqual(weight["followups"]["due"], 5)
        self.assertEqual(weight["followups"]["completed"], 5)
        self.assertEqual(weight["followups"]["completion_rate"], 1.0)
        self.assertEqual(aesthetic["status"], "empty")
        self.assertEqual(next(t for t in snap["category_totals"] if t["category"] == "weight")["patients"], 5)
        self.assertEqual(next(t for t in snap["category_totals"] if t["category"] == "aesthetic")["status"], "empty")
        self.assertEqual(snap["period_totals"][0]["patients"], 5)
        self.assertEqual(snap["grand_total"]["patients"], 5)
        # 时间窗口按诊所时区解释，记录 UTC 边界。
        window = snap["window"]["periods"][0]
        self.assertEqual(window["starts_at"], "2026-08-31T16:00:00Z")
        self.assertEqual(window["ends_at"], "2026-09-30T16:00:00Z")
        # 任何层级都不出现患者或员工标识。
        raw = json.dumps(snap, ensure_ascii=False)
        self.assertNotIn("patient_id", raw)
        self.assertNotIn("staff_id", raw)
        self.assertNotIn("pat_", raw)
        self.assertNotIn("stf_", raw)

    def test_held_appointments_are_excluded_from_denominator(self):
        patient, plan = self.make_patient("hold-1")
        for i in range(5):
            p, pl = self.make_patient(f"hold-{i + 2}")
            self.appointment(p["id"], pl["id"], i)
        # 再多建一个停留于 held 的占位，不应计入分母。
        self.app.create_appointment(self.clinic, self.coordinator, patient["id"], "占位",
                                    "2026-09-29T17:00:00+08:00", "2026-09-29T17:30:00+08:00",
                                    "held-only", staff_id=self.clinician)
        weight = self.cell(self.snapshot(), "weight")
        self.assertEqual(weight["appointments"]["scheduled"], 5)

    def test_small_event_counts_suppress_component_and_rate_but_keep_total(self):
        for i in range(5):
            patient, plan = self.make_patient(f"evt-{i}")
            self.appointment(patient["id"], plan["id"], i, end_state="arrive" if i == 0 else "no_show")
        weight = self.cell(self.snapshot(), "weight")
        self.assertEqual(weight["appointments"]["scheduled"], 5)
        self.assertIsNone(weight["appointments"]["kept"])
        self.assertIsNone(weight["appointments"]["fulfillment_rate"])
        self.assertIn("SUPPRESSED_SMALL_EVENT_COUNT_V1", weight["appointments"]["reasons"])

    # ------------------------------------------------------------ 抑制

    def test_small_cell_is_suppressed_and_cannot_be_reconstructed_from_totals(self):
        for i in range(3):
            patient, plan = self.make_patient(f"small-{i}")
            self.appointment(patient["id"], plan["id"], i)
        snap = self.snapshot()
        weight = self.cell(snap, "weight")
        self.assertEqual(weight["status"], "suppressed")
        self.assertIsNone(weight["patients"])
        self.assertIn("SUPPRESSED_SMALL_CELL_V1", weight["reasons"])
        # 周期合计与总计全部抑制，防止 总计 − 已知部分 还原小单元。
        self.assertEqual(snap["period_totals"][0]["status"], "suppressed")
        self.assertIsNone(snap["period_totals"][0]["patients"])
        self.assertIn("SUPPRESSED_COMPLEMENTARY_RESIDUAL_V1", snap["period_totals"][0]["reasons"])
        self.assertEqual(snap["grand_total"]["status"], "suppressed")
        self.assertEqual(next(t for t in snap["category_totals"] if t["category"] == "weight")["status"], "suppressed")

    def test_complement_residual_suppresses_period_and_grand_totals(self):
        for i in range(5):
            patient, plan = self.make_patient(f"big-{i}", "weight")
            self.appointment(patient["id"], plan["id"], i)
        for i in range(3):
            # 医美只排 3 人，使用下午错峰时段。
            patient, plan = self.make_patient(f"tiny-{i}", "aesthetic")
            self.appointment(patient["id"], plan["id"], 8 + i)
        snap = self.snapshot()
        self.assertEqual(self.cell(snap, "weight")["status"], "reported")
        self.assertEqual(self.cell(snap, "aesthetic")["status"], "suppressed")
        period_total = snap["period_totals"][0]
        self.assertEqual(period_total["status"], "suppressed")
        self.assertIn("SUPPRESSED_COMPLEMENTARY_RESIDUAL_V1", period_total["reasons"])
        self.assertEqual(snap["grand_total"]["status"], "suppressed")
        weight_total = next(t for t in snap["category_totals"] if t["category"] == "weight")
        self.assertEqual(weight_total["status"], "reported")
        self.assertEqual(weight_total["patients"], 5)

    def test_exclusion_counts_below_threshold_are_never_reported(self):
        for i in range(5):
            self.make_patient(f"ok-{i}")
        self.make_patient("no-consent", quality=False)
        self.make_patient("withdrawn", quality_state="withdrawn")
        target, _ = self.make_patient("merge-target")
        source, _ = self.make_patient("merged-one")
        self.app.merge_patients(self.clinic, self.coordinator, source["id"], target["id"],
                                expected_source=1, expected_target=1, reason="重复建档")
        snap = self.snapshot()
        by_code = {(e["category"], e["code"]): e for e in snap["exclusions"]}
        missing = by_code[("weight", "EXCLUDED_QUALITY_CONSENT_MISSING_V1")]
        withdrawn = by_code[("weight", "EXCLUDED_QUALITY_CONSENT_WITHDRAWN_V1")]
        inactive = by_code[("weight", "EXCLUDED_PATIENT_NOT_ACTIVE_V1")]
        self.assertEqual(missing["status"], "suppressed")
        self.assertIsNone(missing["count"])
        self.assertEqual(withdrawn["status"], "suppressed")
        self.assertEqual(inactive["status"], "suppressed")
        # 达到门槛的排除数量可以发布。
        for i in range(5):
            self.make_patient(f"missing-bulk-{i}", quality=False)
        snap2 = self.snapshot(threshold=6)
        missing2 = next(e for e in snap2["exclusions"]
                        if e["code"] == "EXCLUDED_QUALITY_CONSENT_MISSING_V1" and e["category"] == "weight")
        self.assertEqual(missing2["status"], "reported")
        self.assertEqual(missing2["count"], 6)

    # ------------------------------------------------------------ 不可变性

    def test_snapshot_is_immutable_against_late_changes_and_replays(self):
        for i in range(6):
            patient, plan = self.make_patient(f"imm-{i}")
            self.appointment(patient["id"], plan["id"], i)
        first = self.snapshot(threshold=6)
        first_stored = {k: v for k, v in first.items() if k != "replayed"}
        # 迟到数据：新增 9 月预约并改状态、补观察值。
        late_patient, late_plan = self.make_patient("late")
        self.appointment(late_patient["id"], late_plan["id"], 11)
        self.app.record_observation(self.clinic, self.clinician, late_patient["id"], "weight_kg", 80.1,
                                    "2026-09-20T08:00:00+08:00")
        replay = self.snapshot(threshold=6)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["snapshot_id"], first["snapshot_id"])
        self.assertEqual(replay["source_digest"], first["source_digest"])
        self.assertEqual(replay["data_cutoff_at"], first["data_cutoff_at"])
        replay_stored = {k: v for k, v in replay.items() if k != "replayed"}
        self.assertEqual(replay_stored, first_stored)
        # 详情接口取回同一结果。
        fetched = self.app.quality.get_snapshot(self.clinic, self.officer, first["snapshot_id"])
        self.assertEqual(fetched["source_digest"], first["source_digest"])
        self.assertEqual(self.cell(fetched, "weight")["patients"], 6)

    def test_consent_withdrawal_excludes_from_later_snapshot_but_not_exported_history(self):
        for i in range(6):
            self.make_patient(f"wd-{i}")
        historical = self.snapshot(threshold=6)
        self.assertEqual(self.cell(historical, "weight")["patients"], 6)
        target_consent = next(c for c in self.app.consent_history(
            self.clinic, self.clinician, self.refs["wd-0"]["id"])
            if c["purpose"] == "quality_aggregation")
        self.app.withdraw_consent(self.clinic, self.clinician, target_consent["id"], "退出研究")
        # 已导出快照不变。
        replay = self.snapshot(threshold=6)
        self.assertEqual(self.cell(replay, "weight")["patients"], 6)
        # 不同门槛生成新快照（新 cutoff），撤回者不再纳入。
        later = self.snapshot(threshold=5)
        self.assertNotEqual(later["snapshot_id"], historical["snapshot_id"])
        self.assertEqual(self.cell(later, "weight")["patients"], 5)
        withdrawn = next(e for e in later["exclusions"]
                         if e["code"] == "EXCLUDED_QUALITY_CONSENT_WITHDRAWN_V1" and e["category"] == "weight")
        self.assertEqual(withdrawn["status"], "suppressed")  # 仅 1 人，数量抑制

    def test_branches_recompute_same_period_independently(self):
        for i in range(5):
            patient, plan = self.make_patient(f"branch-{i}")
            self.appointment(patient["id"], plan["id"], i)
        ours = self.snapshot()
        other = self.app.create_clinic("同城另一分院", "Asia/Shanghai")
        other_owner = self.app.create_staff(other["id"], "分院负责人", "owner")
        officer2 = self.app.create_staff(other["id"], "分院质量员", "quality_officer",
                                         actor_id=other_owner["id"])["id"]
        theirs = self.app.quality.create_or_get(other["id"], officer2,
                                                {"granularity": "monthly", "year": 2026, "month": 9})
        self.assertNotEqual(ours["snapshot_id"], theirs["snapshot_id"])
        self.assertEqual(self.cell(theirs, "weight")["status"], "empty")
        listing = self.app.quality.list_snapshots(self.clinic, self.officer, granularity="monthly", year=2026)
        self.assertEqual({item["id"] for item in listing["items"]}, {ours["snapshot_id"]})

    def test_quarterly_report_cannot_be_differenced_to_reveal_small_month(self):
        # 大组 5 人计划自 7 月开始，仅 7、8 月有预约；9 月只有 3 人（小单元）。
        for i in range(5):
            patient, plan = self.make_patient(f"q-{i}", start="2026-07-01")
            self.clock.set(datetime(2026, 7, 2, 2, 0, tzinfo=UTC))
            self.appointment(patient["id"], plan["id"], i, month=7, day=3)
            self.clock.set(datetime(2026, 8, 2, 2, 0, tzinfo=UTC))
            self.appointment(patient["id"], plan["id"], i, month=8, day=3)
        for i in range(3):
            patient, plan = self.make_patient(f"q-small-{i}")
            self.clock.set(datetime(2026, 9, 2, 2, 0, tzinfo=UTC))
            self.appointment(patient["id"], plan["id"], i, month=9, day=4)
        self.clock.set(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))

        july = self.snapshot(month=7)
        august = self.snapshot(month=8)
        self.assertEqual(self.cell(july, "weight", "2026-07")["patients"], 5)
        self.assertEqual(self.cell(august, "weight", "2026-08")["patients"], 5)
        quarter = self.snapshot(granularity="quarterly", month=9)
        qjuly = self.cell(quarter, "weight", "2026-07")
        qaugust = self.cell(quarter, "weight", "2026-08")
        qsept = self.cell(quarter, "weight", "2026-09")
        self.assertEqual(qjuly["patients"], 5)
        self.assertEqual(qaugust["patients"], 5)
        # 与独立月报一致，且 9 月小单元在季度报表里同样不可发布。
        self.assertEqual(qjuly["appointments"]["scheduled"], 5)
        self.assertEqual(qsept["status"], "suppressed")
        self.assertIsNone(qsept["patients"])
        weight_total = next(t for t in quarter["category_totals"] if t["category"] == "weight")
        self.assertEqual(weight_total["status"], "suppressed")
        self.assertIn("SUPPRESSED_COMPLEMENTARY_RESIDUAL_V1", weight_total["reasons"])
        sept_period = next(t for t in quarter["period_totals"] if t["period"] == "2026-09")
        self.assertEqual(sept_period["status"], "suppressed")
        self.assertEqual(quarter["grand_total"]["status"], "suppressed")
        # 季度 JSON 中不允许出现 9 月的精确人数（8=两月之和、5 可出现，3 不可出现）。
        released_numbers = [cell.get("patients") for cell in quarter["cells"] if cell["status"] == "reported"]
        released_numbers += [t.get("patients") for t in quarter["period_totals"] + quarter["category_totals"]
                             if t["status"] == "reported"]
        self.assertNotIn(3, released_numbers)

    # ------------------------------------------------------------ 周期与留痕

    def test_quarterly_and_yearly_windows_align_to_natural_grid(self):
        q3 = self.snapshot(granularity="quarterly", month=9)
        self.assertEqual([p["period"] for p in q3["window"]["periods"]], ["2026-07", "2026-08", "2026-09"])
        self.assertEqual(len(q3["cells"]), 6)
        yearly = self.snapshot(granularity="yearly", year=2026)
        self.assertEqual(len(yearly["window"]["periods"]), 12)
        self.assertEqual(yearly["window"]["periods"][0]["start_date"], "2026-01-01")
        self.assertEqual(yearly["window"]["periods"][-1]["end_date"], "2026-12-31")

    def test_snapshot_records_rules_version_cutoff_and_audit_trail(self):
        snap = self.snapshot()
        self.assertEqual(snap["format_version"], "quality-snapshot-v1")
        self.assertEqual(snap["inclusion_rules"]["rules_version"], "quality-rules-v1")
        self.assertEqual(snap["inclusion_rules"]["consent_purpose"], "quality_aggregation")
        self.assertGreaterEqual(len(snap["inclusion_rules"]["rules"]), 6)
        self.assertEqual(snap["suppression"]["threshold"], 5)
        self.assertEqual(snap["data_cutoff_at"], "2026-09-27T12:00:00Z")
        events = self.app.audit_history(self.clinic, self.owner, limit=10)
        self.assertIn("quality.snapshot_generated", [e["action"] for e in events])

    def test_http_endpoint_requires_quality_permission(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            # 质量专员需要先设置密码才能登录。
            self.app.set_password(self.clinic, self.owner, self.officer, "OfficerPass!2026")
            token = self.app.login(self.clinic, self.officer, "OfficerPass!2026")["access_token"]
            body = json.dumps({"granularity": "monthly", "year": 2026, "month": 9}).encode()
            request = Request(base + "/reports/quality-snapshots", data=body, method="POST",
                              headers={"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {token}",
                                       "Content-Type": "application/json"})
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 201)
                snapshot_id = json.loads(response.read())["snapshot_id"]
            request = Request(base + f"/reports/quality-snapshots/{snapshot_id}",
                              headers={"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {token}"})
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
            # 临床岗位无权访问。
            self.app.set_password(self.clinic, self.owner, self.clinician, "ClinicianPass!2026")
            clinical_token = self.app.login(self.clinic, self.clinician, "ClinicianPass!2026")["access_token"]
            request = Request(base + "/reports/quality-snapshots", data=body, method="POST",
                              headers={"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {clinical_token}",
                                       "Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
