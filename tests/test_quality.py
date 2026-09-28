from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.api import create_handler
from careflow.clock import FrozenClock
from careflow.db import Database
from careflow.errors import Forbidden, ValidationError
from careflow.service import Careflow


class QualityCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        # 构造数据时停在 8 月 1 日：预约只能预订未来时刻；评估前再推进时钟。
        self.clock = FrozenClock(datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "诊所负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        self.clinician = self.app.create_staff(self.clinic, "临床医生", "clinician", actor_id=self.owner)["id"]
        self.coordinator = self.app.create_staff(self.clinic, "运营协调员", "coordinator", actor_id=self.owner)["id"]
        self.qofficer = self.app.create_staff(self.clinic, "质量专员", "quality_officer", actor_id=self.owner)["id"]
        self.app.quality.configure(self.clinic, self.owner, min_cell_count=5, min_cell_delta=2,
                                   followup_window_days=14)
        self.patients = []
        for i in range(7):
            patient = self.app.create_patient(self.clinic, self.coordinator, f"q-{i:02d}", f"患者{i}")
            self.patients.append(patient["id"])

    def tearDown(self):
        self.temp.cleanup()

    def _digest(self, value):
        return hashlib.sha256(value.encode()).hexdigest()

    def _consent(self, patient, purpose, revision=1):
        return self.app.grant_consent(self.clinic, self.clinician, patient, purpose, revision,
                                      self._digest(f"{purpose}-{patient}-{revision}"))

    def _program(self, patient, kind="weight", *, day=1, complete=False):
        purpose = "weight_program" if kind == "weight" else "aesthetic_procedure"
        consent = self._consent(patient, purpose)
        self._consent(patient, "quality_aggregate")
        plan = self.app.create_plan(
            self.clinic, self.clinician, patient, kind, self.clinician,
            {"description": "门诊项目"}, {}, f"2026-08-{day:02d}", consent_id=consent["id"])
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 1, "propose")
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 2, "activate")
        if complete:
            self.app.transition_plan(self.clinic, self.clinician, plan["id"], 3, "complete")
        return plan

    def _appointment(self, patient, plan_id, key, *, day=5, hour=10, fulfilled=True):
        created = self.app.create_appointment(
            self.clinic, self.coordinator, patient, "复诊",
            f"2026-08-{day:02d}T{hour:02d}:00:00+08:00", f"2026-08-{day:02d}T{hour:02d}:30:00+08:00",
            key, staff_id=self.clinician, plan_id=plan_id)
        version = 1
        if fulfilled:
            actions = ["book", "arrive", "start"]
        else:
            actions = ["book", "no_show"]
        for index, action in enumerate(actions):
            result = self.app.transition_appointment(
                self.clinic, self.coordinator, created["id"], version, action,
                reason=None if action != "no_show" else "患者未到诊")
            version = result["version"]
        return created

    def _schedule_followup(self, patient, plan_id, key, *, day=6):
        return self.app.schedule_followup(
            self.clinic, self.clinician, patient, f"2026-08-{day:02d}T03:00:00Z",
            "月度随访", key, plan_id=plan_id)

    def _complete_due_followups(self):
        self.clock.set(datetime(2026, 8, 10, 0, 0, tzinfo=UTC))
        claimed = self.app.claim_followups(self.clinic, self.clinician, limit=100, lease_minutes=60)
        for item in claimed:
            self.app.complete_followup(self.clinic, self.clinician, item["id"], item["claim_token"],
                                       "已完成随访", item["version"])

    def _at_evaluation(self):
        # 8 月时间段与 14 天随访宽限期（至 9 月 14 日）均已结束。
        self.clock.set(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))

    def _report(self, actor=None, categories=("weight", "aesthetic"), **kwargs):
        return self.app.quality.report(
            self.clinic, actor or (self.owner if kwargs.get("freeze") else self.qofficer),
            granularity="month", start_date="2026-08-01", end_date="2026-08-31",
            categories=list(categories), **kwargs)

    def _cell(self, report, category="weight"):
        return next(c for c in report["cells"] if c["category"] == category)

    # -- 角色与访问控制 ---------------------------------------------------

    def test_only_quality_roles_can_access_and_no_patient_fields(self):
        report = self._report()
        blob = json.dumps(report, ensure_ascii=False)
        for patient in self.patients:
            self.assertNotIn(patient, blob)
        self.assertNotIn("phone_ciphertext", blob)
        with self.assertRaises(Forbidden):
            self._report(actor=self.coordinator)
        # 质量岗位不能触碰任何患者级接口。
        with self.assertRaises(Forbidden):
            self.app.get_patient(self.clinic, self.qofficer, self.patients[0])
        with self.assertRaises(Forbidden):
            self.app.reports.daily_operations(self.clinic, self.qofficer)
        with self.assertRaises(Forbidden):
            self.app.quality.configure(self.clinic, self.qofficer, min_cell_count=4,
                                       min_cell_delta=2, followup_window_days=14)

    def test_explains_rules_without_data(self):
        explanation = self.app.quality.explain(self.clinic, self.qofficer)
        self.assertIn("cell_below_threshold", explanation["suppression_reasons"])
        self.assertIn("complement_small", explanation["suppression_reasons"])
        self.assertIn("followup_grace_open", explanation["null_reasons"])
        self.assertEqual(explanation["inclusion_rules"]["rule_version"], 1)

    # -- 抑制与差分防御 ---------------------------------------------------

    def test_cell_below_threshold_is_suppressed_without_counts(self):
        for patient in self.patients[:3]:
            self._program(patient)
        self._at_evaluation()
        report = self._report()
        weight = self._cell(report, "weight")
        aesthetic = self._cell(report, "aesthetic")
        self.assertEqual(weight["status"], "suppressed")
        self.assertNotIn("headcount", weight)
        self.assertEqual(weight["suppressions"][0]["reason"], "cell_below_threshold")
        # 两个类别均抑制，无法相减还原任一类别人数。
        self.assertEqual(aesthetic["status"], "suppressed")

    def test_complement_suppression_blocks_subtraction_of_single_no_show(self):
        for i, patient in enumerate(self.patients[:6]):
            plan = self._program(patient)
            self._appointment(patient, plan["id"], f"apt-{i}", hour=9 + i, fulfilled=(i != 0))
        self._at_evaluation()
        cell = self._cell(self._report())
        self.assertEqual(cell["status"], "included")
        self.assertIsNone(cell["appointments"])
        self.assertTrue(any(s["metric"] == "appointments" and s["reason"] == "complement_small"
                            for s in cell.get("suppressions", [])))

    def test_healthy_density_publishes_rates(self):
        for i, patient in enumerate(self.patients[:6]):
            plan = self._program(patient)
            self._appointment(patient, plan["id"], f"apt-{i}", hour=8+i, fulfilled=True)
            self._schedule_followup(patient, plan["id"], f"fup-{i}")
        self._complete_due_followups()
        self._at_evaluation()
        cell = self._cell(self._report())
        self.assertEqual(cell["appointments"]["scheduled"], 6)
        self.assertEqual(cell["appointments"]["fulfilled"], 6)
        self.assertEqual(cell["appointments"]["fulfillment_rate"], 1.0)
        self.assertEqual(cell["followups"]["due"], 6)
        self.assertEqual(cell["followups"]["completed"], 6)
        self.assertEqual(cell["followups"]["completion_rate"], 1.0)

    def test_partial_outcomes_publish_when_complement_is_large_enough(self):
        for i, patient in enumerate(self.patients[:6]):
            plan = self._program(patient)
            self._appointment(patient, plan["id"], f"apt-{i}", hour=8+i, fulfilled=(i >= 2))
            # 4 人随访在 8/6 到期可完成；2 人在 8/15 到期，8/10 领取时尚未到期。
            self._schedule_followup(patient, plan["id"], f"fup-{i}", day=6 if i >= 2 else 15)
        self._complete_due_followups()  # 仅完成 i=2..5 共 4 人
        self._at_evaluation()
        cell = self._cell(self._report())
        self.assertEqual(cell["appointments"]["scheduled"], 6)
        self.assertEqual(cell["appointments"]["fulfilled"], 4)
        self.assertEqual(cell["appointments"]["no_show"], 2)
        self.assertEqual(cell["appointments"]["fulfillment_rate"], 0.6667)
        self.assertEqual(cell["followups"]["completed"], 4)
        self.assertEqual(cell["followups"]["due"], 6)
        self.assertEqual(cell["followups"]["completion_rate"], 0.6667)

    # -- 授权撤回、队列与排除 ---------------------------------------------

    def test_withdrawn_authorization_excludes_patient_from_later_snapshot(self):
        for patient in self.patients[:6]:
            self._program(patient)
        self._at_evaluation()
        self.assertEqual(self._cell(self._report())["headcount"], 6)
        consents = self.app.consent_history(self.clinic, self.clinician, self.patients[0],
                                            purpose="quality_aggregate")
        self.app.withdraw_consent(self.clinic, self.clinician, consents[-1]["id"], "不再参加汇总")
        self.assertEqual(self._cell(self._report())["headcount"], 5)

    def test_never_activated_plan_does_not_enter_cohort(self):
        for patient in self.patients[:6]:
            self._consent(patient, "weight_program")
            self._consent(patient, "quality_aggregate")
            weight_consent = self.app.consent_history(
                self.clinic, self.clinician, patient, purpose="weight_program")[-1]["id"]
            plan = self.app.create_plan(
                self.clinic, self.clinician, patient, "weight", self.clinician,
                {"description": "体重管理"}, {}, "2026-08-02", consent_id=weight_consent)
            self.app.transition_plan(self.clinic, self.clinician, plan["id"], 1, "propose")
            # 停留在提议、从未进入生效状态。
        self._at_evaluation()
        cell = self._cell(self._report(), "weight")
        self.assertEqual(cell["status"], "suppressed")
        self.assertEqual(cell["suppressions"][0]["reason"], "cell_below_threshold")

    def test_planless_records_are_listed_as_exclusions_with_small_count_gated(self):
        for i, patient in enumerate(self.patients[:6]):
            plan = self._program(patient)
            self._appointment(patient, plan["id"], f"apt-{i}", hour=8+i)
        # 一条无法归因到计划的预约。
        loose = self.app.create_appointment(
            self.clinic, self.coordinator, self.patients[6], "散客预约",
            "2026-08-08T10:00:00+08:00", "2026-08-08T10:30:00+08:00", "loose-1")
        self.app.transition_appointment(self.clinic, self.coordinator, loose["id"], 1, "book")
        self._at_evaluation()
        cell = self._cell(self._report(categories=("weight",)))
        rules = {item["rule"]: item["count"] for item in cell["exclusions"]}
        # 仅 1 条，低于互补差 2：计数被门控为 null，但规则解释保留。
        self.assertIsNone(rules["appointments_without_plan"])

    # -- 空值、宽限期与时间窗口 --------------------------------------------

    def test_followup_grace_keeps_rate_open_then_zero_after_grace(self):
        for i, patient in enumerate(self.patients[:6]):
            plan = self._program(patient)
            self._schedule_followup(patient, plan["id"], f"fup-{i}", day=20)
        # 宽限期内（8 月 31 日 +14 天 = 9 月 14 日之前）。
        self.clock.set(datetime(2026, 9, 10, 0, 0, tzinfo=UTC))
        cell = self._cell(self._report())
        self.assertIsNone(cell["followups"]["completion_rate"])
        self.assertIn("followup_grace_open", [n["reason"] for n in cell["nulls"]])
        # 宽限期结束仍无人完成：完成率为 0（可发布，零互补不构成还原风险）。
        self._at_evaluation()
        cell = self._cell(self._report())
        self.assertEqual(cell["followups"]["completion_rate"], 0.0)
        self.assertEqual(cell["followups"]["due"], 6)

    def test_no_appointments_yields_explained_null_rate(self):
        for patient in self.patients[:6]:
            self._program(patient)
        self._at_evaluation()
        cell = self._cell(self._report())
        self.assertIsNone(cell["appointments"]["fulfillment_rate"])
        self.assertIn("no_scheduled_appointments", [n["reason"] for n in cell["nulls"]])

    def test_current_period_is_reported_as_not_closed(self):
        for i, patient in enumerate(self.patients[:6]):
            plan = self._program(patient, day=1)
            self._appointment(patient, plan["id"], f"apt-{i}", day=5, hour=8 + i)
        self._at_evaluation()  # 9 月 27 日，9 月时间段尚未结束
        report = self.app.quality.report(
            self.clinic, self.qofficer, granularity="month",
            start_date="2026-09-01", end_date="2026-09-30", categories=["weight"])
        cell = self._cell(report, "weight")
        self.assertEqual(cell["status"], "not_closed")
        self.assertEqual(cell["nulls"][0]["reason"], "period_not_closed")

    def test_period_alignment_is_validated(self):
        with self.assertRaises(ValidationError):
            self.app.quality.report(self.clinic, self.qofficer, granularity="month",
                                    start_date="2026-08-02", end_date="2026-08-31", categories=["weight"])
        with self.assertRaises(ValidationError):
            self.app.quality.report(self.clinic, self.qofficer, granularity="month",
                                    start_date="2026-08-01", end_date="2026-08-30", categories=["weight"])

    # -- 冻结、复算与迟到更正 ----------------------------------------------

    def test_freeze_is_immutable_to_late_changes_and_replays_identically(self):
        for i, patient in enumerate(self.patients[:6]):
            plan = self._program(patient)
            self._appointment(patient, plan["id"], f"apt-{i}", hour=8+i)
            self._schedule_followup(patient, plan["id"], f"fup-{i}")
        self._complete_due_followups()
        self._at_evaluation()
        first = self._report(freeze=True)
        self.assertTrue(first["frozen"])
        self.assertFalse(first["replayed"])
        first_cell = self._cell(first)
        # 迟到数据：新增患者进入同期队列、观察值更正。
        late_patient = self.patients[6]
        late_plan = self._program(late_patient, day=2)
        original = self.app.record_observation(
            self.clinic, self.clinician, late_patient, "weight_kg", 80.0,
            "2026-08-10T08:00:00+08:00", plan_id=late_plan["id"])
        self.app.record_observation(
            self.clinic, self.clinician, late_patient, "weight_kg", 79.0,
            "2026-08-10T08:00:00+08:00", plan_id=late_plan["id"], correction_of=original["id"])
        replay = self._report(freeze=True)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["export_id"], first["export_id"])
        replay_cell = self._cell(replay)
        self.assertEqual(replay_cell["headcount"], 6)
        self.assertEqual(replay_cell["appointments"], first_cell["appointments"])
        self.assertEqual(replay_cell["data_snapshot"], first_cell["data_snapshot"])
        # 只读复算同样返回冻结版本。
        self.assertTrue(self._report()["replayed"])

    def test_second_clinic_recomputes_same_period_independently(self):
        for patient in self.patients[:6]:
            self._program(patient)
        self._at_evaluation()
        self._report(freeze=True)
        other = self.app.create_clinic("城东分院", "Asia/Shanghai")
        other_owner = self.app.create_staff(other["id"], "分院负责人", "owner")["id"]
        other_quality = self.app.create_staff(other["id"], "分院质量专员", "quality_officer",
                                              actor_id=other_owner)["id"]
        report = self.app.quality.report(
            other["id"], other_quality, granularity="month",
            start_date="2026-08-01", end_date="2026-08-31", categories=["weight"])
        self.assertEqual(report["clinic_id"], other["id"])
        self.assertEqual(self._cell(report, "weight")["status"], "suppressed")

    def test_cannot_freeze_before_grace_period_ends(self):
        self._at_evaluation()  # 9 月 27 日，9 月时间段及宽限期均未结束
        with self.assertRaises(ValidationError):
            self.app.quality.report(self.clinic, self.owner, granularity="month",
                                    start_date="2026-09-01", end_date="2026-09-30",
                                    categories=["weight"], freeze=True)

    def test_reconfiguring_thresholds_does_not_rewrite_frozen_history(self):
        for patient in self.patients[:6]:
            self._program(patient)
        self._at_evaluation()
        first = self._report(freeze=True)
        self.assertTrue(first["frozen"])
        self.app.quality.configure(self.clinic, self.owner, min_cell_count=10, min_cell_delta=3,
                                   followup_window_days=14)
        # 已冻结格按旧规则返回，不允许按新规则重冻。
        replay = self._report(freeze=True)
        self.assertTrue(replay["replayed"])
        self.assertEqual(self._cell(replay)["headcount"], 6)
        self.assertEqual(self._cell(replay)["config_fingerprint"],
                         self._cell(first)["config_fingerprint"])

    def test_thresholds_must_keep_complement_rule_feasible(self):
        with self.assertRaises(ValidationError):
            self.app.quality.configure(self.clinic, self.owner, min_cell_count=3, min_cell_delta=3,
                                       followup_window_days=14)

    # -- 快照元数据与审计 -------------------------------------------------

    def test_snapshot_records_window_rules_and_version(self):
        for patient in self.patients[:6]:
            self._program(patient)
        self._at_evaluation()
        cell = self._cell(self._report())
        self.assertEqual(cell["window"]["starts_at"], "2026-07-31T16:00:00Z")
        self.assertEqual(cell["window"]["ends_at"], "2026-08-31T16:00:00Z")
        self.assertTrue(cell["data_snapshot"]["snapshot_version"].startswith("snap-"))
        self.assertGreaterEqual(cell["data_snapshot"]["audit_head_sequence"], 0)
        self.assertIn("cohort", cell["inclusion_rules"])
        self.assertTrue(self.app.verify_audit(self.clinic, self.owner)["ok"])

    # -- HTTP 验收 --------------------------------------------------------

    def test_http_quality_endpoints_enforce_role(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            self.app.set_password(self.clinic, self.owner, self.qofficer, "QualityPass!2026")
            qtoken = self.app.login(self.clinic, self.qofficer, "QualityPass!2026")["access_token"]
            query = urlencode({"granularity": "month", "start": "2026-08-01", "end": "2026-08-31",
                               "category": ["weight", "aesthetic"]}, doseq=True)
            request = Request(base + "/quality/aggregates?" + query,
                              headers={"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {qtoken}"})
            with urlopen(request, timeout=3) as response:
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(len(payload["cells"]), 2)
            request = Request(base + "/quality/rules",
                              headers={"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {qtoken}"})
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
            self.app.set_password(self.clinic, self.owner, self.coordinator, "CoordPass!2026")
            ctoken = self.app.login(self.clinic, self.coordinator, "CoordPass!2026")["access_token"]
            request = Request(base + "/quality/aggregates?" + query,
                              headers={"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {ctoken}"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 403)
            self._at_evaluation()
            for patient in self.patients[:6]:
                self._program(patient)
            # 质量专员不能冻结导出（需要 quality:manage）。
            qtoken = self.app.login(self.clinic, self.qofficer, "QualityPass!2026")["access_token"]
            request = Request(base + "/quality/exports",
                              data=json.dumps({"granularity": "month", "start": "2026-08-01",
                                               "end": "2026-08-31", "categories": ["weight"]}).encode(),
                              method="POST", headers={"X-Clinic-ID": self.clinic,
                                                       "Authorization": f"Bearer {qtoken}",
                                                       "Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 403)
            # 负责人可以冻结。
            otoken = self.app.login(self.clinic, self.owner, "LongPassphrase!2026")["access_token"]
            request = Request(base + "/quality/exports",
                              data=json.dumps({"granularity": "month", "start": "2026-08-01",
                                               "end": "2026-08-31", "categories": ["weight"]}).encode(),
                              method="POST", headers={"X-Clinic-ID": self.clinic,
                                                       "Authorization": f"Bearer {otoken}",
                                                       "Content-Type": "application/json"})
            with urlopen(request, timeout=3) as response:
                payload = json.loads(response.read())
                self.assertEqual(response.status, 201)
                self.assertTrue(payload["frozen"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
