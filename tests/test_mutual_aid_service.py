"""互助预付 / 公平分摊 / 退回清算的规则测试（只用标准库）。"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mutual_aid import (  # noqa: E402
    ConflictError,
    MutualAidService,
    PermissionDeniedError,
    ValidationError,
    apportion,
    bp_part,
    tenure_member,
)
from mutual_aid.server import create_server  # noqa: E402

DATA = json.loads((ROOT / "data" / "source_records.json").read_text(encoding="utf-8"))
AT = "2026-11-26T10:00:00+08:00"


def seed_service() -> MutualAidService:
    return MutualAidService.from_records(json.loads(json.dumps(DATA)))


def custom_records(**agree):
    ag = {
        "season": "T1", "emergency_bp": 7000,
        "household_limit_cents": 130000, "opening_pool_cents": 1_000_000,
        "member_order": ["P01", "P02"],
    }
    ag.update(agree)
    return {
        "agreement": ag,
        "tenure": [
            {"orchard": "O1", "member": "P01", "from": "2026-01-01", "to": "2027-01-01"},
            {"orchard": "O2", "member": "P02", "from": "2026-01-01", "to": "2027-01-01"},
        ],
        "trips": [
            {"shipment": "S1", "leg": 1, "party_ref": "P01", "orchard": "O1",
             "at": "2026-09-01", "unit": 10, "cost_cents": 100000, "resale_cents": 10000},
            {"shipment": "S1", "leg": 1, "party_ref": "P02", "orchard": "O2",
             "at": "2026-09-01", "unit": 10, "cost_cents": 100000, "resale_cents": 0},
        ],
        "claims": [],
        "pool_transfers": [],
        "compensation": [],
    }


def make_verified_claim(svc, member="P01", shipment="S1", leg=1, round="B1",
                        at=AT, with_cost=True):
    c = svc.submit_claim(member=member, shipment=shipment, leg=leg, round=round, at=at)
    svc.submit_material(c["id"], "cost_voucher", ref="INV-" + c["id"], status="accepted")
    svc.verify_claim(c["id"], at=at)
    return c


class CoreArithmeticTest(unittest.TestCase):
    def test_bp_floor(self):
        self.assertEqual(bp_part(173000, 7000), 121100)
        self.assertEqual(bp_part(1, 7000), 0)  # 向下取整，保守给付

    def test_apportion_seed_exact(self):
        # 150000 按 121100:90300 分摊 -> 85927/64072 取整，余 1 给最早核实 E1。
        w = {"E1": 121100, "E2": 90300}
        got = apportion(150000, w, {"E1": ("2026-11-25T09:00", "P01"),
                                    "E2": ("2026-11-25T09:10", "P02")})
        self.assertEqual(got, {"E1": 85928, "E2": 64072})

    def test_apportion_tie_uses_member_code(self):
        # 构造余 1 且核实时间相同：成员代码在前的得 1 分。
        got = apportion(100, {"P03": 1, "P01": 1, "P02": 1},
                        {k: ("same", k) for k in ("P01", "P02", "P03")})
        self.assertEqual(sum(got.values()), 100)
        # 100/3 地板 33，余 1 -> P01
        self.assertEqual(got, {"P01": 34, "P02": 33, "P03": 33})

    def test_tenure_half_open_boundary(self):
        t = DATA["tenure"]
        self.assertEqual(tenure_member(t, "O3", "2026-11-25"), "P01")
        # [from,to)：2026-11-26 当日由 P03 经营
        self.assertEqual(tenure_member(t, "O3", "2026-11-26"), "P03")


class SeedScenarioTest(unittest.TestCase):
    def setUp(self):
        self.svc = seed_service()

    def test_seed_balance_fully_encumbered(self):
        bal = self.svc.balance_summary()
        self.assertEqual(bal["pool_cash_cents"], 64072)       # 150000 - P810
        self.assertEqual(bal["reserved_pending_cents"], 64072)  # P814
        self.assertEqual(bal["available_cents"], 0)

    def test_seed_b4_amounts_match_proration(self):
        by_claim = {c["claim_id"]: c for c in self.svc.state["commitments"].values()}
        self.assertEqual(by_claim["E1"]["cents"], 85928)
        self.assertEqual(by_claim["E2"]["cents"], 64072)
        self.assertEqual(by_claim["E1"]["state"], "paid")
        self.assertEqual(by_claim["E2"]["state"], "pending")
        self.assertEqual(by_claim["E1"]["basis"]["allocated_cents"], 85928)

    def test_preview_b4_from_raw_records(self):
        raw = json.loads(json.dumps(DATA))
        raw["pool_transfers"] = []
        svc = MutualAidService.from_records(raw)
        pv = svc.preview_batch("B4")
        alloc = {a["claim_id"]: a["allocated_cents"] for a in pv["allocations"]}
        self.assertEqual(alloc, {"E1": 85928, "E2": 64072})
        self.assertEqual(pv["mode"], "prorata")

    def test_compensation_split_suspense(self):
        rec = self.svc.state["compensation"]["Y501"]
        self.assertEqual(rec["split"]["explicit_cents"], 182000)
        self.assertEqual(rec["split"]["unallocated_cents"], 17000)
        self.assertEqual(len(rec["split"]["unconfirmed_parts"]), 1)
        self.assertEqual(self.svc.state["share_compensation"]["TR-51|1|P01"], 100000)
        self.assertEqual(self.svc.state["share_compensation"]["TR-51|1|P02"], 82000)
        self.assertNotIn("TR-62|2|P03", self.svc.state["share_compensation"])

    def test_next_batch_cannot_crowd_out_pending(self):
        view = self.svc.next_batch_view("B5")
        self.assertEqual(view["balance"]["approvable_cents"], 0)
        self.assertEqual(len(view["prior_unfinished_commitments"]), 1)
        pv = self.svc.preview_batch("B5")
        self.assertEqual(pv["member_allocations"]["P03"], 0)


class EligibilityAndIdentityTest(unittest.TestCase):
    def setUp(self):
        self.svc = seed_service()

    def test_party_ref_and_tenure_required(self):
        # TR-51/1 份额属于 P01/P02；P03 不能申领
        with self.assertRaises(Exception):
            self.svc.submit_claim(member="P03", shipment="TR-51", leg=1, claim_id="Z1")
            self.svc.verify_claim("Z1")

    def test_successor_cannot_claim_pre_boundary_expense(self):
        # O3 在 11-24 属 P01：即使 P03 是现经营人也不能拿 TR-51 P01 份额
        c = self.svc.submit_claim(member="P03", shipment="TR-51", leg=1, claim_id="Z2")
        d = self.svc.deficiencies(c["id"])
        self.assertFalse(d["eligible"])

    def test_predecessor_refund_never_becomes_successor_debt(self):
        self.svc.settle_claim("E1")  # P01 待退 12928
        debts = {r["member"] for r in self.svc.state["receivables"].values()
                 if r["kind"] == "refund"}
        self.assertEqual(debts, {"P01"})
        p03 = self.svc.member_view("P03")
        self.assertEqual([r for r in p03["receivables"] if r["kind"] == "refund"], [])

    def test_alias_ambiguity_requires_code(self):
        self.svc.register_member("P01", aliases=["阿柑"])
        self.svc.register_member("P03", aliases=["阿柑"])
        with self.assertRaises(PermissionDeniedError):
            self.svc.resolve_member("阿柑")
        self.assertEqual(self.svc.as_member("P01").code, "P01")

    def test_alias_or_correction_not_a_new_loss(self):
        before = len(self.svc.state["claims"])
        got = self.svc.submit_claim(member="P01", shipment="TR-51", leg=1,
                                    note="更正金额备注")
        self.assertEqual(got["id"], "E1")
        self.assertEqual(len(self.svc.state["claims"]), before)
        self.assertTrue(got["revisions"])

    def test_cannot_revise_other_members_claim_by_id(self):
        with self.assertRaises(PermissionDeniedError):
            self.svc.submit_claim(member="P02", claim_id="E1",
                                  shipment="TR-51", leg=1)


class MaterialDeficiencyTest(unittest.TestCase):
    def test_new_claim_reports_missing_then_complete(self):
        svc = MutualAidService(custom_records())
        c = svc.submit_claim(member="P01", shipment="S1", leg=1, round="B1")
        d = svc.deficiencies(c["id"])
        codes = {i["code"] for i in d["missing"]}
        self.assertIn("cost_voucher", codes)
        self.assertFalse(d["compliant"])
        with self.assertRaises(ValidationError):
            svc.verify_claim(c["id"])
        svc.submit_material(c["id"], "cost_voucher", ref="I1", status="accepted")
        v = svc.verify_claim(c["id"], at=AT)
        self.assertEqual(v["status"], "verified")

    def test_rejected_material_needs_correction(self):
        svc = MutualAidService(custom_records())
        c = svc.submit_claim(member="P01", shipment="S1", leg=1, round="B1")
        m = svc.submit_material(c["id"], "cost_voucher", ref="bad.jpg")
        svc.reject_material(m["id"], reason="金额无法辨认")
        d = svc.deficiencies(c["id"])
        self.assertTrue(d["rejected"])
        # 补正：重新交清晰件并受理
        m2 = svc.submit_material(c["id"], "cost_voucher", ref="good.jpg", status="accepted")
        svc.accept_material(m["id"])  # 旧件维持 rejected 也不影响，因为有 accepted
        d2 = svc.deficiencies(c["id"])
        self.assertTrue(any(i["state"] == "satisfied" and i.get("material_id") == m2["id"]
                            for i in d2["items"]))

    def test_member_deficiencies_lists_expense(self):
        svc = MutualAidService(custom_records())
        c = svc.submit_claim(member="P01", shipment="S1", leg=1, round="B1")
        lst = svc.member_deficiencies("P01")
        self.assertEqual([x["claim_id"] for x in lst], [c["id"]])
        self.assertIn("cost_voucher", {x["code"] for x in lst[0]["missing"]})


class BatchQuotaTest(unittest.TestCase):
    def test_full_allocation_when_pool_enough(self):
        svc = MutualAidService(custom_records(opening_pool_cents=10_000_000))
        make_verified_claim(svc, "P01", round="B1")
        b = svc.approve_batch("B1", at=AT)
        # (100000-10000)*0.7 = 63000
        self.assertEqual(b["total_allocated_cents"], 63000)
        self.assertEqual(svc.balance_summary()["reserved_pending_cents"], 63000)

    def test_prorata_multi_household_and_remainder_order(self):
        rec = custom_records(opening_pool_cents=100000)
        svc = MutualAidService(rec)
        make_verified_claim(svc, "P01", at="2026-09-02T09:00:00+08:00")
        make_verified_claim(svc, "P02", at="2026-09-02T09:05:00+08:00")
        pv = svc.preview_batch("B1")
        self.assertEqual(pv["member_requests"], {"P01": 63000, "P02": 70000})
        b = svc.approve_batch("B1")
        alloc = {c["member"]: c["cents"] for c in
                 (svc.state["commitments"][cid] for cid in b["commitment_ids"])}
        # 地板 47368/52631 合计 99999，余 1 分给更早核实的 P01。
        self.assertEqual(alloc, {"P01": 47369, "P02": 52631})
        self.assertEqual(sum(alloc.values()), 100000)

    def test_pending_commitment_occupies_only_explicit_events_release(self):
        svc = seed_service()
        before = svc.balance_summary()["available_cents"]
        self.assertEqual(before, 0)
        # 单纯查询/试算不释放
        svc.preview_batch("B5")
        self.assertEqual(svc.balance_summary()["available_cents"], 0)
        # 撤销 P814 对应承诺 C2
        svc.cancel_commitment("C2", reason="成员放弃")
        self.assertEqual(svc.balance_summary()["available_cents"], 64072)

    def test_returned_receipt_frees_quota_and_next_batch_partial(self):
        svc = seed_service()
        svc.record_receipt("P814", "returned")
        self.assertEqual(svc.balance_summary()["available_cents"], 64072)
        b5 = svc.approve_batch("B5", at=AT)
        self.assertEqual(b5["total_allocated_cents"], 64072)  # 请求 72800，只能部分
        self.assertEqual(svc.balance_summary()["available_cents"], 0)

    def test_weekly_budget_cap(self):
        svc = MutualAidService(custom_records(opening_pool_cents=10_000_000))
        make_verified_claim(svc, "P01", round="B1")
        pv = svc.preview_batch("B1", budget_cents=10000)
        self.assertEqual(pv["budget_applied_cents"], 10000)
        self.assertEqual(pv["allocations"][0]["allocated_cents"], 10000)

    def test_household_cap_constrains_request(self):
        rec = custom_records(household_limit_cents=50000, opening_pool_cents=10_000_000)
        svc = MutualAidService(rec)
        make_verified_claim(svc, "P01")
        pv = svc.preview_batch("B1")
        row = pv["allocations"][0]
        self.assertEqual(row["bp_request_cents"], 63000)
        self.assertEqual(row["request_cents"], 50000)

    def test_top_up_preserves_fair_order(self):
        rec = custom_records(opening_pool_cents=63000)  # 恰好等于单户请求
        rec["trips"][1]["resale_cents"] = 10000        # 两户同损：请求均为 63000
        svc = MutualAidService(rec)
        make_verified_claim(svc, "P01", at="2026-09-02T09:00:00+08:00")
        make_verified_claim(svc, "P02", at="2026-09-02T09:05:00+08:00")
        b = svc.approve_batch("B1")
        first = {svc.state["commitments"][i]["member"]:
                 svc.state["commitments"][i]["cents"] for i in b["commitment_ids"]}
        # 多户批不足 -> 比例分摊，各 31500（不是先到先得）
        self.assertEqual(first, {"P01": 31500, "P02": 31500})
        # 新资金 63000 到位后，按原批公平顺序补足剩余 31500/户
        svc.state["cash_cents"] += 63000
        out = svc.top_up_batch_shortfall("B1")
        self.assertEqual(out["allocated_cents"], 63000)
        totals = {"P01": 0, "P02": 0}
        for c in svc.state["commitments"].values():
            totals[c["member"]] += c["cents"]
        self.assertEqual(totals, {"P01": 63000, "P02": 63000})


class ReceiptAndExcessTest(unittest.TestCase):
    def test_undisbursed_excess_is_commitment_release(self):
        svc = seed_service()
        svc.settle_claim("E2")
        c2 = svc.state["commitments"]["C2"]
        self.assertEqual(c2["cents"], 47000)
        self.assertEqual(c2["state"], "pending")
        self.assertEqual(svc.balance_summary()["available_cents"], 17072)
        # 可执行：剩余 47000 按回执支付
        svc.record_receipt("P814", "paid")
        self.assertEqual(c2["state"], "paid")
        self.assertEqual(svc.state["cash_cents"], 64072 - 47000)

    def test_paid_overpayment_is_refund_path_not_release(self):
        svc = seed_service()
        svc.record_receipt("P814", "paid")
        self.assertEqual(svc.balance_summary()["reserved_pending_cents"], 0)
        s = svc.settle_claim("E2")
        self.assertEqual(s["preview"]["paid_excess_refund_cents"], 17072)
        rid = s["results"]["receivables"][0]
        r = svc.record_recovery(rid, cents=17072, ref="R1")
        self.assertEqual(r["state"], "refunded")
        self.assertEqual(svc.balance_summary()["pool_cash_cents"], 17072)
        self.assertEqual(svc.balance_summary()["available_cents"], 17072)

    def test_cancel_paid_commitment_rejected_for_paid_part(self):
        svc = seed_service()
        svc.record_receipt("P810", "paid")  # already paid seed; idempotent-ish? -> remaining 0
        with self.assertRaises(ConflictError):
            svc.cancel_commitment("C1", reason="x")

    def test_partial_receipt_keeps_remaining_reserved(self):
        svc = MutualAidService(custom_records())
        c = make_verified_claim(svc, "P01")
        b = svc.approve_batch("B1")
        ref = "PFUND1"
        cid = b["commitment_ids"][0]
        svc.state["transfers"].append({"ref": ref, "commitment_id": cid,
                                      "claim_id": c["id"], "kind": "advance",
                                      "state": "pending", "cents": 63000})
        svc.state["commitments"][cid]["ref"] = ref
        svc.record_receipt(ref, "paid", cents=30000)
        cm = svc.state["commitments"][cid]
        self.assertEqual(cm["paid_cents"], 30000)
        self.assertEqual(svc.balance_summary()["reserved_pending_cents"], 33000)


class CompensationAndSettlementTest(unittest.TestCase):
    def test_promised_compensation_has_no_effect(self):
        svc = MutualAidService(custom_records())
        v = svc.register_compensation({
            "issuer": "buyer", "ref": "Y1", "state": "promised", "cents": 50000,
            "parts": [{"shipment": "S1", "leg": 1, "party_ref": "P01"}]})
        self.assertFalse(v["effects_applied"])
        c = make_verified_claim(svc, "P01")
        self.assertEqual(svc.claim_loss(c["id"])["confirmed_compensation_cents"], 0)

    def test_promised_compensation_confirmation_applies_later(self):
        svc = MutualAidService(custom_records())
        make_verified_claim(svc, "P01")
        svc.register_compensation({
            "issuer": "buyer", "ref": "Y9", "state": "promised", "cents": 20000,
            "parts": [{"shipment": "S1", "leg": 1, "party_ref": "P01",
                       "part_cents": 20000}]})
        cid = svc.state["claim_by_key"]["P01|S1|1"]
        self.assertEqual(svc.claim_loss(cid)["confirmed_compensation_cents"], 0)
        with self.assertRaises(ValidationError):
            svc.confirm_compensation("Y9")  # 缺 paid_on 外部凭据日期
        rec = svc.confirm_compensation("Y9", paid_on="2026-09-10")
        self.assertEqual(rec["state"], "paid")
        self.assertEqual(svc.claim_loss(cid)["confirmed_compensation_cents"], 20000)
        # 重复确认幂等
        svc.confirm_compensation("Y9", paid_on="2026-09-10")
        self.assertEqual(int(svc.state["share_compensation"]["S1|1|P01"]), 20000)

    def test_suspense_attribution_only_to_clear_share(self):
        svc = seed_service()
        sp_id = svc.state["suspense"][0]["id"]
        # 不能分给不存在的份额
        with self.assertRaises(ValidationError):
            svc.attribute_suspense(sp_id, [
                {"shipment": "NOPE", "leg": 9, "party_ref": "P99", "part_cents": 1}])
        svc.attribute_suspense(sp_id, [
            {"shipment": "TR-62", "leg": 2, "party_ref": "P03", "part_cents": 17000}])
        p3 = svc.preview_settlement("E3")
        self.assertEqual(p3["final_loss_cents"], 87000)

    def test_cannot_over_allocate_suspense(self):
        svc = seed_service()
        sp_id = svc.state["suspense"][0]["id"]
        with self.assertRaises(ValidationError):
            svc.attribute_suspense(sp_id, [
                {"shipment": "TR-62", "leg": 2, "party_ref": "P03", "part_cents": 17001}])

    def test_e1_refund_12928_and_recovery_changes_pool(self):
        svc = seed_service()
        s = svc.settle_claim("E1")
        self.assertEqual(s["preview"]["final_loss_cents"], 73000)
        rid = s["results"]["receivables"][0]
        self.assertEqual(svc.state["receivables"][rid]["state"], "refund_pending")
        avail0 = svc.balance_summary()["available_cents"]
        svc.record_recovery(rid, cents=12928, ref="R9")
        self.assertEqual(svc.balance_summary()["available_cents"], avail0 + 12928)

    def test_pool_routed_compensation_nets_payable(self):
        svc = seed_service()
        svc.receive_compensation_proceeds("Y501", proceeds_to="pool")
        bal = svc.balance_summary()
        self.assertEqual(bal["pool_cash_cents"], 263072)
        self.assertEqual(bal["comp_hold_cents"], 199000)
        s = svc.settle_claim("E1")
        p = s["preview"]
        self.assertEqual(p["retained_recovery_cents"], 12928)
        self.assertEqual(p["comp_remit_payable_cents"], 87072)
        self.assertEqual(p["total_payable_cents"], 87072)
        pid = s["results"]["receivables"][0]
        svc.pay_receivable(pid, ref="PAY1")
        self.assertEqual(svc.state["receivables"][pid]["state"], "paid")

    def test_payable_queues_without_cash(self):
        svc = MutualAidService(custom_records())
        c = make_verified_claim(svc, "P01")
        svc.approve_batch("B1")
        # 人为把池现金清零，再立一笔应付：支付必须因现金不足而排队失败。
        svc.state["cash_cents"] = 0
        svc.state["receivables"]["D99"] = {
            "id": "D99", "kind": "payable", "claim_id": c["id"], "member": "P01",
            "cents": 100, "paid_cents": 0, "state": "payable_pending", "payments": []}
        with self.assertRaises(ConflictError):
            svc.pay_receivable("D99", ref="X")
        self.assertEqual(svc.state["receivables"]["D99"]["state"], "payable_pending")

    def test_settlement_idempotent(self):
        svc = seed_service()
        s1 = svc.settle_claim("E1")
        s2 = svc.settle_claim("E1")
        self.assertEqual(s1["results"]["receivables"], s2["results"]["receivables"])
        self.assertEqual(len([r for r in svc.state["receivables"].values()
                             if r["claim_id"] == "E1"]), 1)


class PortalPrivacyTest(unittest.TestCase):
    def test_member_portal_scoped_and_scrubbed(self):
        svc = seed_service()
        p1 = svc.as_member("P01")
        view = p1.view()
        self.assertTrue(all(c["claim_id"] == "E1" for c in view["claims"]))
        text = json.dumps(view, ensure_ascii=False)
        # P02 的分摊数字不应出现在 P01 视图里
        self.assertNotIn("64072", text)
        with self.assertRaises(PermissionDeniedError):
            svc.as_member("P01")._own("P02")

    def test_funder_audit_has_basis(self):
        svc = seed_service()
        f = svc.as_funder("FR-1")
        audit = f.quota_audit("P814")
        self.assertEqual(audit["commitment"]["cents"], 64072)
        self.assertTrue(audit["occupation_and_release_entries"])
        stmt = f.quota_statement()
        self.assertIn("ledger", stmt)


class PersistenceTest(unittest.TestCase):
    def test_roundtrip(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            svc = seed_service()
            svc.settle_claim("E1")
            svc.save(p)
            loaded = MutualAidService.load(p)
            self.assertEqual(loaded.balance_summary(), svc.balance_summary())
            self.assertIn("E1", loaded.state["settlements"])


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = seed_service()
        cls.httpd = create_server(cls.svc, host="127.0.0.1", port=0)
        cls.port = cls.httpd.server_address[1]
        cls.t = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _req(self, path, method="GET", body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_health_and_balance(self):
        st, body = self._req("/api/health")
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        st, body = self._req("/api/available")
        self.assertEqual(body["available_cents"], 0)

    def test_member_cannot_read_other(self):
        st, _ = self._req("/api/members/P02/view",
                          headers={"X-Actor-Role": "member", "X-Actor-Code": "P01"})
        self.assertEqual(st, 403)
        st, body = self._req("/api/members/P01/view",
                             headers={"X-Actor-Role": "member", "X-Actor-Code": "P01"})
        self.assertEqual(st, 200)
        self.assertEqual(body["viewer"], "P01")

    def test_funder_quota_audit(self):
        st, body = self._req("/api/quota/audit?ref=P810",
                             headers={"X-Actor-Role": "funder"})
        self.assertEqual(st, 200)
        self.assertEqual(body["commitment"]["member"], "P01")

    def test_rpc_dispatch(self):
        st, body = self._req("/api/call", method="POST",
                             body={"method": "household_position", "args": {"member": "P03"}})
        self.assertEqual(st, 200)
        self.assertEqual(body["result"]["remaining_cents"], 130000)

    def test_rpc_role_boundaries(self):
        # 出资代表不能通过 RPC 办理审批
        st, _ = self._req("/api/call", method="POST",
                          headers={"X-Actor-Role": "funder"},
                          body={"method": "approve_batch", "args": {"round": "BX"}})
        self.assertEqual(st, 403)
        # 成员不能调用合作社方法
        st, _ = self._req("/api/call", method="POST",
                          headers={"X-Actor-Role": "member", "X-Actor-Code": "P01"},
                          body={"method": "settle_all", "args": {}})
        self.assertEqual(st, 403)

    def test_index_served(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertIn("text/html", r.headers["Content-Type"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
