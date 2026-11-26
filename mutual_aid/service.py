"""办理互助预付与退回清算的有状态服务（仅标准库）。

设计要点
========
* 金额一律整数分；所有状态变更都写 ``pool_ledger``，可向出资代表解释
  某次拨付为何占用 / 释放共同额度。
* 可用额度 = 池现金 - 待拨付承诺剩余 - 在途/待清分补偿 hold - 未付应付。
  已核准待回执的承诺通过 reserved 占额；只有明确撤销或 returned 回执
  才释放；成员还款(recovery)增加现金。
* 成员 / 合作社 / 出资代表三种视角，成员视角限定本人。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .core import (
    CST,
    ConflictError,
    MutualAidError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
    apportion,
    bp_part,
    check_claim_eligibility,
    eligible_shares,
    iso_now,
    loss_share,
    parse_instant,
    split_compensation_voucher,
    tenure_member,
)

DEFAULT_REQUIREMENTS = [
    ("application", "互助申请（花名/别名须核对到成员代码）"),
    ("identity", "成员身份与收款账户核对"),
    ("transport_receipt", "运输/改送凭据（shipment、leg、party_ref）"),
    ("tenure_proof", "约定期间的经营与费用承担核对"),
    ("cost_voucher", "临时处置费用凭证"),
]
RESALE_REQUIREMENT = ("resale_proof", "补送/残值收入凭证")


class MutualAidService:
    def __init__(
        self,
        records: "dict[str, Any] | str | Path | None" = None,
        *,
        clock: Callable[[], datetime] | None = None,
        data_path: "str | Path | None" = None,
    ):
        self._clock = clock or (lambda: datetime.now(CST))
        if isinstance(records, (str, Path)):
            data_path = records
            records = None
        if data_path is not None:
            records = json.loads(Path(data_path).read_text(encoding="utf-8"))
        self.state: dict[str, Any] = {
            "version": 1,
            "agreement": {},
            "members": {},
            "tenure": [],
            "trips": [],
            "claims": {},
            "claim_by_key": {},
            "materials": {},
            "batches": {},
            "commitments": {},
            "transfers": [],
            "compensation": {},
            "suspense": [],
            "share_compensation": {},
            "share_pool_held": {},
            "settlements": {},
            "receivables": {},
            "pool_ledger": [],
            "audit": [],
            "counters": {},
            "cash_cents": 0,
            "comp_hold_cents": 0,
            "bootstrapped": False,
        }
        if records is not None:
            self.load_source_records(records)

    # ------------------------------------------------------------------ utils
    def now_iso(self, at: str | datetime | None = None) -> str:
        if at is None:
            return iso_now(self._clock())
        if isinstance(at, datetime):
            return iso_now(at)
        # 校验可解析
        parse_instant(at)
        return str(at)

    def _new_id(self, kind: str, prefix: str) -> str:
        n = int(self.state["counters"].get(kind, 0)) + 1
        self.state["counters"][kind] = n
        return f"{prefix}{n}"

    def _audit(self, action: str, *, actor: str | None, details: Any = None) -> None:
        self.state["audit"].append(
            {"seq": len(self.state["audit"]) + 1, "at": self.now_iso(),
             "action": action, "actor": actor, "details": details}
        )

    def _ledger(
        self,
        kind: str,
        reason: str,
        *,
        cents: int = 0,
        cash_delta: int = 0,
        hold_delta: int = 0,
        commitment_id: str | None = None,
        receivable_id: str | None = None,
        claim_id: str | None = None,
        member: str | None = None,
        round: str | None = None,
        ref: str | None = None,
        actor: str | None = None,
        basis: dict[str, Any] | None = None,
        at: str | datetime | None = None,
        historical: bool = False,
    ) -> dict[str, Any]:
        """追加一条池额度/余额流水，并记录变动后快照。"""
        s = self.state
        s["cash_cents"] += int(cash_delta)
        s["comp_hold_cents"] += int(hold_delta)
        entry = {
            "seq": len(s["pool_ledger"]) + 1,
            "at": self.now_iso(at),
            "kind": kind,
            "reason": reason,
            "cents": int(cents),
            "cash_delta": int(cash_delta),
            "hold_delta": int(hold_delta),
            "commitment_id": commitment_id,
            "receivable_id": receivable_id,
            "claim_id": claim_id,
            "member": member,
            "round": round,
            "ref": ref,
            "actor": actor,
            "basis": basis or {},
            "historical": historical,
            "snapshot": self.balance_summary(),
        }
        s["pool_ledger"].append(entry)
        return entry

    # ============================================================== bootstrap
    @classmethod
    def from_records(cls, records: dict[str, Any]) -> "MutualAidService":
        return cls(records)

    @classmethod
    def from_data_file(cls, path: str | Path) -> "MutualAidService":
        records = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(records)

    def load_source_records(self, data: dict[str, Any]) -> "MutualAidService":
        if self.state["bootstrapped"]:
            raise ConflictError("服务已经载入过基础资料")
        s = self.state
        s["agreement"] = copy.deepcopy(data.get("agreement", {}))
        s["tenure"] = copy.deepcopy(data.get("tenure", []))
        s["trips"] = copy.deepcopy(data.get("trips", []))

        codes: list[str] = list(s["agreement"].get("member_order", []))
        for t in s["trips"]:
            codes.append(str(t["party_ref"]))
        for c in data.get("claims", []):
            codes.append(str(c["member"]))
        for c in codes:
            self.register_member(c)

        opening = int(s["agreement"].get("opening_pool_cents", 0))
        self._ledger(
            "opening", "agreement_opening_pool",
            cents=opening, cash_delta=opening,
            basis={"agreement": s["agreement"]}, historical=True,
        )

        # 先载入 claims（已核实的历史申请），再重建拨付承诺。
        for c in data.get("claims", []):
            self._bootstrap_claim(c)

        # 计算各给付批规则结果，作为历史拨付的分摊依据。
        allocation_basis_by_claim: dict[str, Any] = {}
        for rnd in sorted({c.get("round") for c in data.get("claims", []) if c.get("round")}):
            try:
                preview = self.preview_batch(rnd)
                for a in preview["allocations"]:
                    allocation_basis_by_claim[a["claim_id"]] = {
                        "batch_mode": preview["mode"],
                        "pool_available_before_cents": preview["pool_available_before_cents"],
                        "total_batch_request_cents": preview["total_request_cents"],
                        "member_requests": preview["member_requests"],
                        "member_allocations": preview["member_allocations"],
                        "fair_order": preview["fair_order"],
                        **{k: v for k, v in a.items() if k != "noncompliant_reason"},
                    }
            except MutualAidError:
                continue

        for tr in data.get("pool_transfers", []):
            self._bootstrap_transfer(tr, allocation_basis_by_claim.get(tr["claim"], {}))

        for v in data.get("compensation", []):
            # 交换资料里的 paid 补偿凭证默认不经过池账户（pool_transfers
            # 没有对应入账），因此只确认归属，不移动池现金；实际入账时
            # 用 receive_compensation_proceeds 补记。
            self.register_compensation(v, proceeds_to="member", _bootstrap=True)

        s["bootstrapped"] = True
        return self

    def register_member(
        self, code: str, aliases: Iterable[str] | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        s = self.state
        code = str(code)
        m = s["members"].setdefault(code, {"code": code, "aliases": [], "display_name": None})
        if display_name:
            m["display_name"] = display_name
        for a in aliases or []:
            a = str(a)
            if a not in m["aliases"]:
                m["aliases"].append(a)
        return m

    def resolve_member(self, identifier: str) -> str:
        """成员代码精确匹配；花名可能对应多个经营者，歧义时拒绝猜测。"""
        ident = str(identifier)
        if ident in self.state["members"]:
            return ident
        hits = [code for code, m in self.state["members"].items() if ident in m["aliases"]]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise PermissionDeniedError(
                "花名对应多个经营者，必须使用成员代码办理",
                details={"alias": ident, "member_codes": sorted(hits)},
            )
        raise NotFoundError("未找到成员", details={"identifier": ident})

    def _bootstrap_claim(self, c: dict[str, Any]) -> None:
        cid = str(c["id"])
        member = self.resolve_member(c["member"]) if c["member"] not in self.state["members"] else str(c["member"])
        key = f"{member}|{c['shipment']}|{c['leg']}"
        claim = {
            "id": cid,
            "member": member,
            "shipment": str(c["shipment"]),
            "leg": c["leg"],
            "round": c.get("round"),
            "status": "verified",
            "submitted_at": c.get("verified"),
            "verified_at": c.get("verified"),
            "idempotency_key": c.get("idempotency_key"),
            "natural_key": key,
            "revisions": [],
            "historical": True,
            "extra": {k: v for k, v in c.items()
                      if k not in {"id", "member", "shipment", "leg", "round", "verified"}},
        }
        self.state["claims"][cid] = claim
        self.state["claim_by_key"][key] = cid
        self.state["materials"][cid] = []
        # 已核实的历史申请：把默认要件标记为经资料/记录核实。
        for code, label in self._required_codes(claim):
            self.state["materials"][cid].append({
                "id": self._new_id("material", "M"),
                "claim_id": cid, "kind": code, "label": label,
                "status": "verified", "ref": "source_records",
                "submitted_by": "cooperative", "submitted_at": c.get("verified"),
                "reviewed_at": c.get("verified"), "reject_reason": None,
                "basis": "历史已核实申请，随交换资料导入",
            })

    def _required_codes(self, claim: dict[str, Any]) -> list[tuple[str, str]]:
        reqs = list(DEFAULT_REQUIREMENTS)
        share = loss_share(self.state["trips"], claim["shipment"], claim["leg"], claim["member"])
        if share["resale_cents"] > 0 or claim.get("extra", {}).get("resale_cents"):
            reqs.append(RESALE_REQUIREMENT)
        return reqs

    def _bootstrap_transfer(self, tr: dict[str, Any], basis: dict[str, Any]) -> None:
        claim_id = str(tr["claim"])
        if claim_id not in self.state["claims"]:
            raise ValidationError("资金凭证指向未知申请", details=tr)
        cents = int(tr["cents"])
        ref = str(tr["ref"])
        state = str(tr["state"])
        cid = self._new_id("commitment", "C")
        commitment = {
            "id": cid,
            "claim_id": claim_id,
            "member": self.state["claims"][claim_id]["member"],
            "round": self.state["claims"][claim_id].get("round"),
            "cents": cents,
            "paid_cents": cents if state == "paid" else 0,
            "state": "paid" if state == "paid" else ("returned" if state == "returned" else "pending"),
            "ref": ref,
            "source": "pool_transfers",
            "historical": True,
            "basis": basis or {"note": "来自 source_records.pool_transfers 的历史拨付"},
            "created_at": self.now_iso(),
        }
        self.state["commitments"][cid] = commitment
        self.state["transfers"].append({
            "ref": ref, "commitment_id": cid, "claim_id": claim_id,
            "kind": "advance", "state": state, "cents": cents,
            "at": self.now_iso(), "historical": True,
        })
        if state == "paid":
            self._ledger(
                "receipt_paid", "seed_paid_transfer",
                cents=cents, cash_delta=-cents,
                commitment_id=cid, claim_id=claim_id, member=commitment["member"],
                round=commitment["round"], ref=ref, basis=basis, historical=True,
            )
        elif state == "pending":
            self._ledger(
                "commitment_created", "approved_awaiting_funder_receipt",
                cents=cents, commitment_id=cid, claim_id=claim_id,
                member=commitment["member"], round=commitment["round"], ref=ref,
                basis=basis, historical=True,
            )
        elif state == "returned":
            # 历史 returned：从未形成现金占用，也不占额度。
            commitment["paid_cents"] = 0
            commitment["state"] = "returned"
        elif state == "recovery":
            self._ledger(
                "recovery_received", "seed_member_recovery",
                cents=cents, cash_delta=cents, claim_id=claim_id,
                member=commitment["member"], ref=ref, historical=True,
            )

    # ================================================================ balance
    def balance_summary(self) -> dict[str, Any]:
        s = self.state
        reserved = 0
        for c in s["commitments"].values():
            if c["state"] == "pending":
                reserved += max(0, int(c["cents"]) - int(c.get("paid_cents", 0)))
        payable_due = 0
        for r in s["receivables"].values():
            if r["kind"] == "payable" and r["state"] in ("payable_pending", "partially_paid"):
                payable_due += max(0, int(r["cents"]) - int(r.get("paid_cents", 0)))
        cash = int(s["cash_cents"])
        hold = int(s["comp_hold_cents"])
        available = cash - reserved - hold - payable_due
        return {
            "pool_cash_cents": cash,
            "reserved_pending_cents": reserved,
            "comp_hold_cents": hold,
            "payable_reserved_cents": payable_due,
            "available_cents": available,
            "approvable_cents": max(0, available),
            "funding_gap_cents": max(0, -available),
            "opening_pool_cents": int(s["agreement"].get("opening_pool_cents", 0)),
        }

    def available_balance(self) -> dict[str, Any]:
        return self.balance_summary()

    def next_batch_view(self, round: str | None = None) -> dict[str, Any]:
        bal = self.balance_summary()
        prior_unfinished = [
            {
                "commitment_id": c["id"], "claim_id": c["claim_id"],
                "member": c["member"], "round": c["round"], "ref": c.get("ref"),
                "remaining_cents": c["cents"] - c.get("paid_cents", 0),
                "state": c["state"],
            }
            for c in self.state["commitments"].values()
            if c["state"] == "pending"
        ]
        return {
            "next_round": round,
            "balance": bal,
            "prior_unfinished_commitments": prior_unfinished,
            "note": "上一批未完承诺优先占额，下一批不得挤占",
        }

    def household_used_cents(self, member: str, season_only: bool = True) -> int:
        season = self.state["agreement"].get("season")
        total = 0
        for c in self.state["commitments"].values():
            if c["member"] != member:
                continue
            if c["state"] in ("cancelled", "returned"):
                continue
            if season_only and season and c.get("season", season) != season:
                continue
            total += int(c["cents"])
        return total

    def household_position(self, member: str) -> dict[str, Any]:
        limit = int(self.state["agreement"].get("household_limit_cents", 0))
        used = self.household_used_cents(member)
        return {
            "member": member,
            "season": self.state["agreement"].get("season"),
            "limit_cents": limit,
            "approved_used_cents": used,
            "remaining_cents": max(0, limit - used),
        }

    # ============================================================ claims API
    def submit_claim(
        self,
        member: str | None = None,
        shipment: str | None = None,
        leg: Any = None,
        round: str | None = None,
        claim_id: str | None = None,
        id: str | None = None,  # 兼容命名
        idempotency_key: str | None = None,
        at: str | None = None,
        actor: str | None = None,
        *,
        alias: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """交申请/材料。同一 (成员代码, shipment, leg) 或同一 id/幂等键
        永远是同一项损失：重复提交按更正处理，不产生新损失。"""
        if member is None and alias is None:
            raise ValidationError("必须提供 member 代码或 alias 花名")
        if shipment is None or leg is None:
            raise ValidationError("必须提供 shipment 与 leg 才能定位费用份额")
        code = self.resolve_member(member) if member else self.resolve_member(alias)  # type: ignore[arg-type]
        cid_arg = claim_id or id
        key = f"{code}|{shipment}|{leg}"

        # 幂等：显式 id / 幂等键 / 自然键 任一命中都按更新返回。
        existing_id = None
        if cid_arg and cid_arg in self.state["claims"]:
            existing_id = cid_arg
        elif idempotency_key:
            for cid, c in self.state["claims"].items():
                if c.get("idempotency_key") == idempotency_key:
                    existing_id = cid
                    break
        elif key in self.state["claim_by_key"]:
            existing_id = self.state["claim_by_key"][key]

        if existing_id:
            claim = self.state["claims"][existing_id]
            if claim["member"] != code:
                raise PermissionDeniedError(
                    "申请编号属于其他成员，不能用该编号提交/更正",
                    details={"claim_id": existing_id, "owner": claim["member"]})
            changes = {"round": round, **fields}
            self._revise_claim(claim, {k: v for k, v in changes.items() if v is not None},
                               at=at, actor=actor, reason="resubmission_or_correction")
            if idempotency_key and not claim.get("idempotency_key"):
                claim["idempotency_key"] = idempotency_key
            return claim

        cid = cid_arg or self._new_id("claim", "E")
        if cid in self.state["claims"]:
            raise ConflictError("申请编号已存在", details={"claim_id": cid})
        claim = {
            "id": cid,
            "member": code,
            "shipment": str(shipment),
            "leg": leg,
            "round": round,
            "status": "draft",
            "submitted_at": self.now_iso(at),
            "verified_at": None,
            "idempotency_key": idempotency_key,
            "natural_key": key,
            "revisions": [],
            "historical": False,
            "extra": fields,
        }
        self.state["claims"][cid] = claim
        self.state["claim_by_key"][key] = cid
        self.state["materials"][cid] = []
        # 申请表本身算一件已收到的材料。
        self.submit_material(cid, "application", ref=fields.get("application_ref"),
                             by=code, at=at, status="accepted",
                             note="成员在线提交即受理")
        self._audit("claim_submitted", actor=actor or code,
                    details={"claim_id": cid, "key": key})
        return claim

    def _revise_claim(self, claim: dict[str, Any], changes: dict[str, Any], *,
                      at: str | None, actor: str | None, reason: str) -> None:
        if not changes:
            return
        snapshot = {k: claim.get(k) for k in ("round", "leg", "shipment")}
        extra_changes = {}
        for k, v in changes.items():
            if k in ("round",):
                claim[k] = v
            elif k in ("shipment", "leg"):
                claim[k] = v
                claim["natural_key"] = f"{claim['member']}|{claim['shipment']}|{claim['leg']}"
                self.state["claim_by_key"][claim["natural_key"]] = claim["id"]
            else:
                claim.setdefault("extra", {})[k] = v
                extra_changes[k] = v
        claim["revisions"].append({
            "at": self.now_iso(at), "actor": actor or claim["member"],
            "reason": reason, "before": snapshot, "changes": changes,
        })
        self._audit("claim_corrected", actor=actor or claim["member"],
                    details={"claim_id": claim["id"], "changes": list(changes)})

    def correct_claim(self, claim_id: str, *, at: str | None = None,
                      actor: str | None = None, **changes: Any) -> dict[str, Any]:
        claim = self._claim(claim_id)
        if claim["status"] in ("settled",):
            raise ConflictError("已结清申请不能直接更正，请走调整/冲正流程")
        self._revise_claim(claim, changes, at=at, actor=actor, reason="member_correction")
        return claim

    def _claim(self, claim_id: str) -> dict[str, Any]:
        c = self.state["claims"].get(str(claim_id))
        if not c:
            raise NotFoundError("申请不存在", details={"claim_id": claim_id})
        return c

    # -------------------------------------------------------------- materials
    def submit_material(
        self, claim_id: str, kind: str, ref: str | None = None,
        payload: dict[str, Any] | None = None, by: str | None = None,
        at: str | None = None, status: str = "received", note: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        claim = self._claim(claim_id)
        mid = self._new_id("material", "M")
        mat = {
            "id": mid, "claim_id": claim_id, "kind": kind,
            "label": label or dict(DEFAULT_REQUIREMENTS + [RESALE_REQUIREMENT]).get(kind, kind),
            "status": status, "ref": ref, "payload": payload or {},
            "submitted_by": by or claim["member"], "submitted_at": self.now_iso(at),
            "reviewed_at": self.now_iso(at) if status in ("accepted", "verified") else None,
            "reject_reason": None, "note": note,
        }
        self.state["materials"].setdefault(claim_id, []).append(mat)
        # 若提交的是新要件类型，纳入清单。
        return mat

    def review_material(self, material_id: str, *, accept: bool,
                        reason: str | None = None, by: str = "cooperative",
                        at: str | None = None) -> dict[str, Any]:
        for mats in self.state["materials"].values():
            for m in mats:
                if m["id"] == material_id:
                    m["status"] = "accepted" if accept else "rejected"
                    m["reviewed_at"] = self.now_iso(at)
                    m["reviewed_by"] = by
                    m["reject_reason"] = None if accept else (reason or "材料不符合要求，请补正")
                    self._audit("material_reviewed", actor=by,
                                details={"material_id": material_id, "accept": accept})
                    return m
        raise NotFoundError("材料不存在", details={"material_id": material_id})

    def accept_material(self, material_id: str, **kw: Any) -> dict[str, Any]:
        return self.review_material(material_id, accept=True, **kw)

    def reject_material(self, material_id: str, reason: str, **kw: Any) -> dict[str, Any]:
        return self.review_material(material_id, accept=False, reason=reason, **kw)

    def _material_status(self, claim: dict[str, Any], code: str) -> dict[str, Any]:
        mats = [m for m in self.state["materials"].get(claim["id"], []) if m["kind"] == code]
        accepted = [m for m in mats if m["status"] in ("accepted", "verified")]
        rejected = [m for m in mats if m["status"] == "rejected"]
        pending = [m for m in mats if m["status"] == "received"]
        if accepted:
            return {"code": code, "state": "satisfied", "material_id": accepted[-1]["id"],
                    "ref": accepted[-1].get("ref"), "basis": accepted[-1].get("basis")}
        if pending:
            return {"code": code, "state": "pending_review", "material_id": pending[-1]["id"]}
        if rejected:
            return {"code": code, "state": "rejected", "material_id": rejected[-1]["id"],
                    "reason": rejected[-1].get("reject_reason")}
        return {"code": code, "state": "missing"}

    def deficiencies(self, claim_id: str) -> dict[str, Any]:
        claim = self._claim(claim_id)
        required = self._required_codes(claim)
        items = []
        for code, label in required:
            item = self._material_status(claim, code)
            item["label"] = label
            items.append(item)

        # 交换记录可自动核实运输份额与经营期间。
        elig = None
        try:
            elig = check_claim_eligibility(
                self.state["trips"], self.state["tenure"],
                claim["member"], claim["shipment"], claim["leg"],
            )
        except MutualAidError as e:
            elig = {"eligible": False, "error": e.message, "details": e.details}

        auto: dict[str, dict[str, Any]] = {}
        # 成员代码本身就是归属身份；花名/别名只用于解析到代码。
        auto["identity"] = {
            "state": "satisfied", "auto": True,
            "basis": f"已核对到成员代码 {claim['member']}；花名/别名不单独确定主体",
        }
        if isinstance(elig, dict) and elig.get("eligible"):
            share = elig["share"]
            auto["transport_receipt"] = {
                "state": "satisfied", "auto": True,
                "basis": f"交换资料已核对 {share['trip_count']} 条运输凭据 "
                         f"{share['trip_refs']}",
            }
            auto["tenure_proof"] = {
                "state": "satisfied", "auto": True,
                "basis": f"经营边界 [from,to) 核对通过：{elig['tenure_checks']}",
            }
            if share["resale_cents"] > 0:
                auto["resale_proof"] = {
                    "state": "satisfied", "auto": True,
                    "basis": f"凭据载有补送/残值收入 {share['resale_cents']} 分",
                }
        for it in items:
            if it["code"] in auto and it["state"] == "missing":
                it.update(auto[it["code"]])
            elif it["code"] == "transport_receipt" and not (isinstance(elig, dict) and elig.get("eligible")):
                it["state"] = "missing"
                it["eligibility_problem"] = elig

        outstanding = [i for i in items if i["state"] in ("missing", "rejected", "pending_review")]
        return {
            "claim_id": claim_id,
            "member": claim["member"],
            "eligible": bool(isinstance(elig, dict) and elig.get("eligible")),
            "eligibility": elig,
            "items": items,
            "missing": [i for i in items if i["state"] == "missing"],
            "rejected": [i for i in items if i["state"] == "rejected"],
            "pending_review": [i for i in items if i["state"] == "pending_review"],
            "compliant": not [i for i in items if i["state"] in ("missing", "rejected", "pending_review")]
                          and bool(isinstance(elig, dict) and elig.get("eligible")),
        }

    def member_deficiencies(self, member: str) -> list[dict[str, Any]]:
        code = self.resolve_member(member)
        out = []
        for c in self.state["claims"].values():
            if c["member"] != code:
                continue
            d = self.deficiencies(c["id"])
            if d["missing"] or d["rejected"] or d["pending_review"]:
                out.append({"claim_id": c["id"], "shipment": c["shipment"],
                            "leg": c["leg"], "round": c["round"], **d})
        return out

    def verify_claim(self, claim_id: str, *, by: str = "cooperative",
                     at: str | None = None, accept_pending: bool = True) -> dict[str, Any]:
        claim = self._claim(claim_id)
        d = self.deficiencies(claim_id)
        if accept_pending:
            for i in d["pending_review"]:
                if i.get("material_id"):
                    self.accept_material(i["material_id"], by=by, at=at)
            d = self.deficiencies(claim_id)
        if not d["eligible"]:
            raise EligibilityError_from(d.get("eligibility"))
        if d["missing"] or d["rejected"]:
            raise ValidationError(
                "申请材料不齐或被退回，不能核实",
                details={"claim_id": claim_id,
                         "missing": d["missing"], "rejected": d["rejected"]},
            )
        claim["status"] = "verified"
        claim["verified_at"] = self.now_iso(at)
        claim["verified_by"] = by
        self._audit("claim_verified", actor=by, details={"claim_id": claim_id})
        return claim

    # ------------------------------------------------------------- loss views
    def claim_loss(self, claim_id: str) -> dict[str, Any]:
        c = self._claim(claim_id)
        share = loss_share(self.state["trips"], c["shipment"], c["leg"], c["member"])
        comp = int(self.state["share_compensation"].get(self._share_sig(c), 0))
        share["confirmed_compensation_cents"] = comp
        share["final_loss_cents"] = max(0, share["tentative_loss_cents"] - comp)
        return share

    def _share_sig(self, claim: dict[str, Any]) -> str:
        return f"{claim['shipment']}|{claim['leg']}|{claim['member']}"

    def claim_commitments(self, claim_id: str) -> list[dict[str, Any]]:
        return [c for c in self.state["commitments"].values() if c["claim_id"] == claim_id]

    # ---------------------------------------------------------- batch / alloc
    def _batch_claims(self, round: str) -> list[dict[str, Any]]:
        return [c for c in self.state["claims"].values() if c.get("round") == round]

    def preview_batch(self, round: str, cutoff: str | None = None,
                      budget_cents: int | None = None) -> dict[str, Any]:
        """按规则试算某给付批（不写状态），返回每户请求与分摊依据。

        budget_cents 可显式给出“当周预算”；默认使用池真实可用余额。
        """
        bp = int(self.state["agreement"].get("emergency_bp", 0))
        cap = int(self.state["agreement"].get("household_limit_cents", 10**18))
        bal = self.balance_summary()
        pool_available = bal["approvable_cents"]
        if budget_cents is None:
            available = pool_available
            budget_source = "pool_available"
        else:
            available = max(0, min(pool_available, int(budget_cents)))
            budget_source = "explicit_weekly_budget"
        budget_limited = budget_cents is not None and int(budget_cents) < pool_available

        claims = self._batch_claims(round)
        if cutoff is not None:
            cut = parse_instant(cutoff)
            claims = [c for c in claims if c.get("verified_at") and
                      parse_instant(c["verified_at"]) <= cut]  # type: ignore[operator]
        verified = [c for c in claims if c.get("verified_at")]

        # 已存在有效承诺的申请不再重复安排（其未完部分已在余额中占额）。
        covered = {cid for cid, cs in
                   ((cid, self.claim_commitments(cid)) for cid in [c["id"] for c in verified])
                   if any(x["state"] in ("pending", "paid") for x in cs)}
        candidates = [c for c in verified if c["id"] not in covered]

        rows = []
        by_member: dict[str, list[dict[str, Any]]] = {}
        for c in candidates:
            d = self.deficiencies(c["id"])
            share = loss_share(self.state["trips"], c["shipment"], c["leg"], c["member"])
            gross_req = bp_part(share["tentative_loss_cents"], bp)
            hp = self.household_position(c["member"])
            # 试算时若该户本批已有其它行，需合并占用；这里按户聚合处理。
            req = max(0, min(gross_req, hp["remaining_cents"]))
            row = {
                "claim_id": c["id"], "member": c["member"],
                "verified_at": c["verified_at"],
                "compliant": d["compliant"],
                "tentative_loss_cents": share["tentative_loss_cents"],
                "bp": bp, "bp_request_cents": gross_req,
                "household_limit_cents": cap,
                "household_used_cents": hp["approved_used_cents"],
                "household_remaining_cents": hp["remaining_cents"],
                "request_cents": req if d["compliant"] else 0,
                "noncompliant_reason": None if d["compliant"] else d,
            }
            rows.append(row)
            by_member.setdefault(c["member"], []).append(row)

        # 户级请求 = 各申请合规请求之和（受户上限在每行已保守取 min，
        # 多行时下面再做一次户上限收口）。
        member_req: dict[str, int] = {}
        member_priority: dict[str, tuple[Any, ...]] = {}
        for m, rs in by_member.items():
            total = sum(r["request_cents"] for r in rs)
            hp = self.household_position(m)
            total = min(total, hp["remaining_cents"])
            member_req[m] = total
            earliest = min(parse_instant(r["verified_at"]) for r in rs)  # type: ignore[arg-type]
            member_priority[m] = (earliest.isoformat() if earliest else "", m)

        total_request = sum(member_req.values())
        multi = sum(1 for v in member_req.values() if v > 0) >= 2

        if total_request == 0:
            member_alloc = {m: 0 for m in member_req}
            mode = "no_compliant_request"
        elif available >= total_request:
            member_alloc = dict(member_req)
            mode = "full"
        elif sum(1 for v in member_req.values() if v > 0) == 1:
            only = next(m for m, v in member_req.items() if v > 0)
            member_alloc = {m: 0 for m in member_req}
            member_alloc[only] = min(member_req[only], available)
            mode = "single_household_limited"
        else:
            positive = {m: v for m, v in member_req.items() if v > 0}
            member_alloc = apportion(min(available, sum(positive.values())),
                                     positive, member_priority)
            mode = "prorata"

        # 户内分配到申请（同样的取整规则）。
        allocations = []
        for row in rows:
            m = row["member"]
            mtotal = member_alloc.get(m, 0)
            rs = by_member[m]
            if len(rs) == 1:
                row["allocated_cents"] = min(row["request_cents"], mtotal)
            else:
                w = {r["claim_id"]: r["request_cents"] for r in rs}
                prio = {r["claim_id"]: (parse_instant(r["verified_at"]).isoformat(), r["claim_id"])
                        for r in rs}
                split = apportion(mtotal, w, prio) if mtotal else {k: 0 for k in w}
                row["allocated_cents"] = split[row["claim_id"]]
            row["batch_allocations"] = member_alloc
            allocations.append(row)

        allocated_total = sum(r["allocated_cents"] for r in allocations)
        return {
            "round": round,
            "mode": mode,
            "pool_available_before_cents": pool_available,
            "budget_applied_cents": available,
            "budget_source": budget_source,
            "weekly_budget_limited": budget_limited,
            "balance": bal,
            "total_request_cents": total_request,
            "total_allocated_cents": allocated_total,
            "shortfall_cents": max(0, total_request - allocated_total),
            "multi_household_prorata": multi and mode == "prorata",
            "member_requests": member_req,
            "member_allocations": member_alloc,
            "allocations": allocations,
            "fair_order": sorted(member_req, key=lambda m: member_priority.get(m, ("", m))),
        }

    def approve_batch(self, round: str, at: str | None = None,
                      cutoff: str | None = None, actor: str = "cooperative",
                      idempotent: bool = True,
                      budget_cents: int | None = None) -> dict[str, Any]:
        existing = self.state["batches"].get(round)
        if existing and idempotent and existing.get("status") != "draft":
            return existing
        preview = self.preview_batch(round, cutoff=cutoff, budget_cents=budget_cents)
        commitments: list[str] = []
        for row in preview["allocations"]:
            amt = int(row["allocated_cents"])
            if amt <= 0:
                continue
            cid = self._new_id("commitment", "C")
            commitment = {
                "id": cid, "claim_id": row["claim_id"], "member": row["member"],
                "round": round, "season": self.state["agreement"].get("season"),
                "cents": amt, "paid_cents": 0, "state": "pending",
                "ref": None, "source": "batch_approval", "historical": False,
                "created_at": self.now_iso(at),
                "basis": {
                    "rule": "emergency_advance",
                    "tentative_loss_cents": row["tentative_loss_cents"],
                    "emergency_bp": row["bp"],
                    "bp_request_cents": row["bp_request_cents"],
                    "household_remaining_cents": row["household_remaining_cents"],
                    "request_cents": row["request_cents"],
                    "batch_mode": preview["mode"],
                    "pool_available_before_cents": preview["pool_available_before_cents"],
                    "total_batch_request_cents": preview["total_request_cents"],
                    "member_requests": preview["member_requests"],
                    "member_allocations": preview["member_allocations"],
                    "fair_order": preview["fair_order"],
                },
            }
            self.state["commitments"][cid] = commitment
            self._ledger(
                "commitment_created", "batch_approved_awaiting_receipt",
                cents=amt, commitment_id=cid, claim_id=row["claim_id"],
                member=row["member"], round=round, actor=actor, at=at,
                basis=commitment["basis"],
            )
            commitments.append(cid)

        batch = {
            "round": round, "status": "approved", "approved_at": self.now_iso(at),
            "approved_by": actor, "preview": preview,
            "commitment_ids": commitments,
            "total_allocated_cents": preview["total_allocated_cents"],
        }
        self.state["batches"][round] = batch
        self._audit("batch_approved", actor=actor,
                    details={"round": round, "commitments": commitments,
                             "allocated": preview["total_allocated_cents"]})
        return batch

    def top_up_batch_shortfall(self, round: str, *, at: str | None = None,
                               actor: str = "cooperative",
                               budget_cents: int | None = None) -> dict[str, Any]:
        """预算/回款到位后，按原批公平顺序补足未满足的合规请求。"""
        batch = self.state["batches"].get(round)
        if not batch:
            raise NotFoundError("拨付批尚未审批", details={"round": round})
        original_rows = {a["claim_id"]: a for a in batch["preview"]["allocations"]}
        need_by_claim: dict[str, int] = {}
        member_of: dict[str, str] = {}
        prio_claim: dict[str, tuple[Any, ...]] = {}
        for cid, row in original_rows.items():
            if not row.get("compliant"):
                continue
            active = sum(c["cents"] for c in self.claim_commitments(cid)
                         if c["state"] in ("pending", "paid"))
            need = max(0, int(row["request_cents"]) - int(active))
            if need > 0:
                need_by_claim[cid] = need
                member_of[cid] = row["member"]
                dt = parse_instant(row["verified_at"])
                prio_claim[cid] = (dt.isoformat() if dt else "", row["member"], cid)
        if not need_by_claim:
            return {"round": round, "topped_up": [], "allocated_cents": 0,
                    "note": "该批已无未满足请求"}

        bal = self.balance_summary()
        available = bal["approvable_cents"] if budget_cents is None else \
            max(0, min(bal["approvable_cents"], int(budget_cents)))

        need_by_member: dict[str, int] = {}
        member_prio: dict[str, tuple[Any, ...]] = {}
        for cid, need in need_by_claim.items():
            m = member_of[cid]
            need_by_member[m] = need_by_member.get(m, 0) + need
            p = prio_claim[cid][:2]
            if m not in member_prio or p < member_prio[m]:
                member_prio[m] = p
        member_alloc = apportion(min(available, sum(need_by_member.values())),
                                 {m: n for m, n in need_by_member.items() if n > 0},
                                 member_prio) if available else {m: 0 for m in need_by_member}

        new_commitments = []
        allocated_total = 0
        for m, mtotal in member_alloc.items():
            cids = [cid for cid in need_by_claim if member_of[cid] == m]
            if len(cids) == 1:
                splits = {cids[0]: min(need_by_claim[cids[0]], mtotal)}
            else:
                splits = apportion(mtotal, {c: need_by_claim[c] for c in cids},
                                   {c: prio_claim[c] for c in cids}) if mtotal else \
                         {c: 0 for c in cids}
            for cid, amt in splits.items():
                if amt <= 0:
                    continue
                c = self._new_id("commitment", "C")
                cm = {
                    "id": c, "claim_id": cid, "member": m, "round": round,
                    "season": self.state["agreement"].get("season"),
                    "cents": int(amt), "paid_cents": 0, "state": "pending",
                    "ref": None, "source": "batch_shortfall_top_up",
                    "created_at": self.now_iso(at),
                    "basis": {"rule": "preserved_fair_order_top_up",
                              "remaining_need_cents": need_by_claim[cid],
                              "member_allocations": member_alloc},
                }
                self.state["commitments"][c] = cm
                new_commitments.append(c)
                allocated_total += int(amt)
                self._ledger(
                    "commitment_created", "batch_shortfall_top_up",
                    cents=int(amt), commitment_id=c, claim_id=cid, member=m,
                    round=round, actor=actor, at=at, basis=cm["basis"])
        batch.setdefault("top_ups", []).append(
            {"at": self.now_iso(at), "commitment_ids": new_commitments,
             "allocated_cents": allocated_total,
             "available_before_cents": available})
        return {"round": round, "topped_up": new_commitments,
                "allocated_cents": allocated_total,
                "member_allocations": member_alloc,
                "balance": self.balance_summary()}

    # ----------------------------------------------------- receipts / refunds
    def find_transfer(self, ref: str) -> dict[str, Any]:
        for tr in self.state["transfers"]:
            if tr["ref"] == ref:
                return tr
        raise NotFoundError("资金凭证号不存在", details={"ref": ref})

    def record_receipt(
        self, ref: str, state: str, cents: int | None = None,
        at: str | None = None, actor: str = "cooperative", note: str | None = None,
    ) -> dict[str, Any]:
        """根据资金方回执结清承诺。

        state=paid：到账/拨付成功，可给实际到账零头（部分拨付）。
        state=returned：拨付未完成退回，释放剩余承诺额度。
        """
        tr = self.find_transfer(ref)
        cid = tr["commitment_id"]
        cm = self.state["commitments"][cid]
        # 资金回执可能重投：同一终态回执幂等返回，绝不重复出/入账。
        if cents is None and tr.get("state") == state and state in ("paid", "returned"):
            return cm
        if cm["state"] in ("cancelled", "returned"):
            raise ConflictError("承诺已撤销/退回，不能再登记回执",
                                details={"commitment": cm})
        amount = int(cm["cents"] if cents is None else cents)
        already = int(cm.get("paid_cents", 0))

        if state == "paid":
            remaining = int(cm["cents"]) - already
            if amount <= 0 or amount > remaining:
                raise ValidationError(
                    "到账金额必须为正且不超过承诺未付余额",
                    details={"amount": amount, "remaining": remaining})
            cm["paid_cents"] = already + amount
            tr["state"] = "paid" if cm["paid_cents"] == cm["cents"] else "partially_paid"
            tr["receipt_at"] = self.now_iso(at)
            tr["receipt_note"] = note
            self._ledger(
                "receipt_paid", "funder_receipt_paid",
                cents=amount, cash_delta=-amount,
                commitment_id=cid, claim_id=cm["claim_id"], member=cm["member"],
                round=cm["round"], ref=ref, actor=actor, at=at,
                basis={"paid_total_cents": cm["paid_cents"],
                       "commitment_cents": cm["cents"]},
            )
            if cm["paid_cents"] == cm["cents"]:
                cm["state"] = "paid"
            return cm

        if state == "returned":
            unpaid = int(cm["cents"]) - already
            if cents is not None:
                # 部分退回：调减承诺额。
                if not 0 < int(cents) <= unpaid:
                    raise ValidationError("退回金额超过未付余额",
                                          details={"cents": cents, "unpaid": unpaid})
                self._release_commitment(
                    cm, int(cents), "receipt_returned_partial", ref=ref,
                    actor=actor, at=at)
                tr["state"] = "partially_returned"
                tr["receipt_at"] = self.now_iso(at)
                return cm
            cm["state"] = "returned"
            self._ledger(
                "receipt_returned", "funder_receipt_returned_uncompleted",
                cents=unpaid, commitment_id=cid, claim_id=cm["claim_id"],
                member=cm["member"], round=cm["round"], ref=ref, actor=actor, at=at,
                basis={"unpaid_released_cents": unpaid},
            )
            tr["state"] = "returned"
            tr["receipt_at"] = self.now_iso(at)
            return cm

        raise ValidationError("回执状态只能是 paid / returned", details={"state": state})

    def _release_commitment(self, cm: dict[str, Any], amount: int, reason: str, *,
                            ref: str | None = None, actor: str | None = None,
                            at: str | None = None, basis: dict[str, Any] | None = None
                            ) -> None:
        unpaid = int(cm["cents"]) - int(cm.get("paid_cents", 0))
        amount = int(amount)
        if amount <= 0 or amount > unpaid:
            raise ValidationError("可释放金额必须为正且不超过未拨付余额",
                                  details={"amount": amount, "unpaid": unpaid})
        cm["cents"] = int(cm["cents"]) - amount
        if cm["cents"] == cm.get("paid_cents", 0) and cm["state"] == "pending":
            # 金额降到已付水平：仍可能保持 pending? 已全额拨付则 paid。
            if cm["cents"] == cm["paid_cents"] and cm["paid_cents"] > 0:
                cm["state"] = "paid"
        self._ledger(
            "commitment_released", reason, cents=amount,
            commitment_id=cm["id"], claim_id=cm["claim_id"], member=cm["member"],
            round=cm.get("round"), ref=ref, actor=actor, at=at,
            basis=basis or {"released_cents": amount,
                            "remaining_commitment_cents": cm["cents"]},
        )

    def cancel_commitment(self, commitment_id: str, reason: str, *,
                          actor: str = "cooperative", at: str | None = None
                          ) -> dict[str, Any]:
        """明确撤销尚未拨付的承诺，释放剩余额度。"""
        cm = self.state["commitments"].get(commitment_id)
        if not cm:
            raise NotFoundError("承诺不存在", details={"commitment_id": commitment_id})
        if cm["state"] in ("cancelled", "returned"):
            raise ConflictError("承诺已终结", details={"state": cm["state"]})
        unpaid = int(cm["cents"]) - int(cm.get("paid_cents", 0))
        if unpaid <= 0:
            raise ConflictError("承诺已全额拨付，不能撤销；多付款应走退回/还款流程",
                                details={"commitment_id": commitment_id})
        if int(cm.get("paid_cents", 0)) > 0:
            # 部分已付：只撤销未付部分。
            self._release_commitment(cm, unpaid, "commitment_partial_cancelled",
                                     actor=actor, at=at, basis={"reason": reason})
        cm["state"] = "cancelled" if cm.get("paid_cents", 0) == 0 else cm["state"]
        if cm["paid_cents"] > 0:
            cm["state"] = "paid"  # 已付部分保留，差额应走应收退回
        cm["cancel_reason"] = reason
        self._audit("commitment_cancelled", actor=actor,
                    details={"commitment_id": commitment_id, "unpaid_released": unpaid})
        return cm

    def release_commitment_excess(self, commitment_id: str, cents: int, reason: str, *,
                                  actor: str = "cooperative", at: str | None = None
                                  ) -> dict[str, Any]:
        """已承诺但尚未实际拨付的多余部分：调减承诺、释放共同额度。"""
        cm = self.state["commitments"].get(commitment_id)
        if not cm:
            raise NotFoundError("承诺不存在")
        self._release_commitment(cm, cents, "committed_excess_released",
                                 actor=actor, at=at, basis={"reason": reason})
        return cm

    # ---------------------------------------------------------- compensation
    def register_compensation(
        self, voucher: dict[str, Any], *, proceeds_to: str = "member",
        at: str | None = None, actor: str = "cooperative",
        _bootstrap: bool = False,
    ) -> dict[str, Any]:
        ref = str(voucher["ref"])
        if ref in self.state["compensation"]:
            existing = self.state["compensation"][ref]
            if existing.get("raw") == voucher:
                return existing
            raise ConflictError("补偿凭证号已存在且内容不同（如需更正请走补正）",
                                details={"ref": ref})

        known = {(t["shipment"], t["leg"], t["party_ref"]) for t in self.state["trips"]}
        split = split_compensation_voucher(voucher, known)
        paid = str(voucher.get("state", "pending")) == "paid"

        rec = {
            "ref": ref,
            "issuer": voucher.get("issuer"),
            "state": "paid" if paid else str(voucher.get("state", "promised")),
            "cents": int(voucher["cents"]),
            "paid_on": voucher.get("paid_on"),
            "proceeds_to": proceeds_to,
            "raw": copy.deepcopy(voucher),
            "split": split.to_dict(),
            "effects_applied": False,
            "registered_at": self.now_iso(at),
        }
        self.state["compensation"][ref] = rec

        if not paid:
            rec["note"] = "仅为事先承诺/未兑现凭据，不冲减损失、不影响额度"
            self._audit("compensation_promised", actor=actor, details={"ref": ref})
            return rec

        self._apply_paid_compensation(rec, proceeds_to, at, actor)
        self._audit("compensation_registered", actor=actor,
                    details={"ref": ref, "booked": len(split.booked),
                             "suspense": split.unallocated_cents})
        return rec

    def _apply_paid_compensation(self, rec: dict[str, Any], proceeds_to: str,
                                 at: str | None, actor: str) -> None:
        """把已兑现凭证的明确份额入账，并为分不清的金额建 suspense。"""
        if rec.get("effects_applied"):
            return
        split = rec["split"]
        for b in split["booked"]:
            sig = f"{b['shipment']}|{b['leg']}|{b['party_ref']}"
            self.state["share_compensation"][sig] = \
                int(self.state["share_compensation"].get(sig, 0)) + int(b["part_cents"])
            if proceeds_to == "pool" or rec.get("proceeds_to") == "pool":
                self.state["share_pool_held"][sig] = \
                    int(self.state["share_pool_held"].get(sig, 0)) + int(b["part_cents"])

        if int(split["unallocated_cents"]) > 0 and not rec.get("suspense_id"):
            sp_id = self._new_id("suspense", "S")
            rec["suspense_id"] = sp_id
            self.state["suspense"].append({
                "id": sp_id, "voucher_ref": rec["ref"],
                "cents": int(split["unallocated_cents"]),
                "status": "open",
                "reason": "part_without_amount_or_excess_over_specified_parts",
                "unconfirmed_parts": split.get("unconfirmed_parts", []),
                "unknown_parts": split.get("unknown_parts", []),
                "created_at": self.now_iso(at),
                "resolutions": [],
            })

        if proceeds_to == "pool":
            rec["proceeds_to"] = "pool"
            self._ledger(
                "compensation_in_pool", "paid_compensation_received_by_pool",
                cents=int(rec["cents"]), cash_delta=int(rec["cents"]),
                hold_delta=int(rec["cents"]), ref=rec["ref"], actor=actor, at=at,
                basis={"booked": split["booked"],
                       "unallocated_cents": split["unallocated_cents"]},
            )
        rec["state"] = "paid"
        rec["effects_applied"] = True

    def confirm_compensation(
        self, ref: str, *, paid_on: str | None = None,
        proceeds_to: str | None = None, at: str | None = None,
        actor: str = "cooperative",
    ) -> dict[str, Any]:
        """外部凭据确认事先承诺的补偿已兑现：此刻才冲减损失/影响额度。"""
        rec = self.state["compensation"].get(ref)
        if not rec:
            raise NotFoundError("补偿凭证不存在", details={"ref": ref})
        if rec.get("effects_applied"):
            return rec  # 幂等确认
        if paid_on:
            rec["paid_on"] = paid_on
        if not rec.get("paid_on"):
            raise ValidationError("确认兑现必须提供可核对的 paid_on/外部到账日期")
        dest = proceeds_to or rec.get("proceeds_to") or "member"
        self._apply_paid_compensation(rec, dest, at, actor)
        self._audit("compensation_confirmed", actor=actor,
                    details={"ref": ref, "paid_on": rec["paid_on"]})
        return rec

    def receive_compensation_proceeds(
        self, ref: str, *, proceeds_to: str = "pool", at: str | None = None,
        actor: str = "cooperative",
    ) -> dict[str, Any]:
        """补记已确认补偿的资金实际进入池账户（此前只确认了归属）。"""
        rec = self.state["compensation"].get(ref)
        if not rec:
            raise NotFoundError("补偿凭证不存在", details={"ref": ref})
        if rec["state"] != "paid":
            raise ConflictError("凭证尚无外部到账凭据，不能记为池收款")
        if rec["proceeds_to"] == "pool":
            raise ConflictError("该凭证已按入池登记")
        rec["proceeds_to"] = "pool"
        split = rec["split"]
        for b in split["booked"]:
            sig = f"{b['shipment']}|{b['leg']}|{b['party_ref']}"
            self.state["share_pool_held"][sig] = \
                int(self.state["share_pool_held"].get(sig, 0)) + b["part_cents"]
        self._ledger(
            "compensation_in_pool", "paid_compensation_received_by_pool",
            cents=rec["cents"], cash_delta=rec["cents"], hold_delta=rec["cents"],
            ref=ref, actor=actor, at=at,
            basis={"booked": split["booked"],
                   "unallocated_cents": split["unallocated_cents"]},
        )
        return rec

    def attribute_suspense(
        self, suspense_id: str, allocations: list[dict[str, Any]], *,
        at: str | None = None, actor: str = "cooperative",
    ) -> dict[str, Any]:
        """事后核对：把待核对金额划分到明确份额。

        allocations: [{shipment, leg, party_ref, part_cents}]，
        分配合计不得超过 suspense 余额；仍分不完的继续留待核对。
        """
        sp = self._suspense(suspense_id)
        if sp["status"] == "closed":
            raise ConflictError("该待核对项已结清")
        known = {(t["shipment"], t["leg"], t["party_ref"]) for t in self.state["trips"]}
        total = 0
        applied = []
        for a in allocations:
            key = (str(a["shipment"]), a["leg"], str(a["party_ref"]))
            amt = int(a["part_cents"])
            if key not in known:
                raise ValidationError("目标份额无法核对，不能分配", details={"part": a})
            if amt < 0:
                raise ValidationError("分配金额不能为负")
            total += amt
            applied.append((key, amt))
        if total > sp["cents"]:
            raise ValidationError("分配合计超过待核对余额",
                                  details={"total": total, "balance": sp["cents"]})
        for (shipment, leg, party), amt in applied:
            sig = f"{shipment}|{leg}|{party}"
            self.state["share_compensation"][sig] = \
                int(self.state["share_compensation"].get(sig, 0)) + amt
            rec = self.state["compensation"][sp["voucher_ref"]]
            if rec["proceeds_to"] == "pool":
                self.state["share_pool_held"][sig] = \
                    int(self.state["share_pool_held"].get(sig, 0)) + amt
            sp["resolutions"].append(
                {"at": self.now_iso(at), "shipment": shipment, "leg": leg,
                 "party_ref": party, "part_cents": amt, "by": actor})
        sp["cents"] -= total
        if sp["cents"] == 0:
            sp["status"] = "closed"
        self._audit("suspense_attributed", actor=actor,
                    details={"suspense_id": suspense_id, "applied": total,
                             "remaining": sp["cents"]})
        return sp

    def _suspense(self, suspense_id: str) -> dict[str, Any]:
        for sp in self.state["suspense"]:
            if sp["id"] == suspense_id:
                return sp
        raise NotFoundError("待核对项不存在", details={"suspense_id": suspense_id})

    # -------------------------------------------------------------- settlement
    def preview_settlement(self, claim_id: str,
                           final_relief_bp: int | None = None) -> dict[str, Any]:
        claim = self._claim(claim_id)
        share = self.claim_loss(claim_id)
        comp = share["confirmed_compensation_cents"]
        sig = self._share_sig(claim)
        held = int(self.state["share_pool_held"].get(sig, 0))
        cms = self.claim_commitments(claim_id)
        paid = sum(int(c.get("paid_cents", 0)) for c in cms if c["state"] != "cancelled")
        pending = sum(max(0, int(c["cents"]) - int(c.get("paid_cents", 0)))
                      for c in cms if c["state"] == "pending")
        bp = int(final_relief_bp if final_relief_bp is not None
                 else self.state["agreement"].get("final_relief_bp", 10000))
        plan = settlement_plan_safe(
            cost=share["cost_cents"], resale=share["resale_cents"], comp=comp,
            paid=paid, pending=pending, held=held, final_relief_bp=bp,
        )
        return {"claim_id": claim_id, "member": claim["member"],
                "shipment": claim["shipment"], "leg": claim["leg"],
                "final_relief_bp": bp,
                "share": share, "pool_held_compensation_cents": held,
                "commitments": [c["id"] for c in cms], **plan}

    def settle_claim(self, claim_id: str, apply: bool = True,
                    actor: str = "cooperative", at: str | None = None,
                    final_relief_bp: int | None = None,
                    ) -> dict[str, Any]:
        """终局结清：计算并（默认）执行承诺调减、应付/待退认定。"""
        claim = self._claim(claim_id)
        preview = self.preview_settlement(claim_id, final_relief_bp=final_relief_bp)
        if not apply:
            return preview

        # 幂等：已有生效结清且关键数字未变 -> 直接返回。
        prior = self.state["settlements"].get(claim_id)
        if prior and prior.get("status") == "settled" and \
                prior.get("plan_signature") == _sig(preview):
            return prior

        # 1) 先把未拨付承诺调减到 desired_pending。
        releasable = preview["pending_releasable_cents"]
        released = 0
        for cm in self.claim_commitments(claim_id):
            if releasable <= 0:
                break
            if cm["state"] != "pending":
                continue
            unpaid = int(cm["cents"]) - int(cm.get("paid_cents", 0))
            take = min(unpaid, releasable)
            if take > 0:
                self._release_commitment(
                    cm, take, "settlement_undisbursed_excess",
                    actor=actor, at=at,
                    basis={"settlement": {k: preview[k] for k in
                                         ("final_loss_cents", "recognized_aid_cents",
                                          "pending_releasable_cents")},
                           "rule": "已承诺未拨付多余部分直接调减承诺，释放额度"})
                released += take
                releasable -= take

        # 2) 池持有的补偿款按份额解除 hold（全额 H 都有归属处理）。
        held = preview["comp_held_cents"]
        if held > 0:
            self._ledger(
                "comp_hold_clear", "settlement_reclassify_pool_held_comp",
                cents=held, hold_delta=-held, claim_id=claim_id,
                member=claim["member"], actor=actor, at=at,
                basis={"retained_recovery_cents": preview["retained_recovery_cents"],
                       "remit_payable_cents": preview["comp_remit_payable_cents"]},
            )
            self.state["share_pool_held"][self._share_sig(claim)] = 0

        results: dict[str, Any] = {"released_commitment_cents": released,
                                   "receivables": []}

        # 3) 已拨付多余部分 -> 待退（若池持有补偿，保留部分已在 hold 对冲）。
        refund_due = preview["refund_due_cents"]
        if refund_due > 0:
            r = self._upsert_receivable(
                claim, kind="refund", cents=refund_due,
                reason="advance_exceeds_final_uncompensated_loss",
                basis=preview, at=at)
            results["receivables"].append(r["id"])

        # 4) 池保留的补偿对冲，记一条已实现追回（无现金移动，只是 hold 释放）。
        retained = preview["retained_recovery_cents"]
        if retained > 0:
            self._ledger(
                "recovery_offset", "pool_held_compensation_retained",
                cents=retained, claim_id=claim_id, member=claim["member"],
                actor=actor, at=at,
                basis={"note": "池收采购补偿与成员应退对冲", **preview},
            )

        # 5) 应付（互助社补付 + 补偿转付）。
        payable = preview["total_payable_cents"]
        if payable > 0:
            r = self._upsert_receivable(
                claim, kind="payable", cents=payable,
                reason="final_entitlement_and_compensation_remittance",
                basis=preview, at=at)
            results["receivables"].append(r["id"])

        record = {
            "claim_id": claim_id, "member": claim["member"],
            "settled_at": self.now_iso(at), "by": actor,
            "status": "settled", "preview": preview, "results": results,
            "plan_signature": _sig(preview),
        }
        self.state["settlements"][claim_id] = record
        claim["status"] = "settled"
        self._audit("claim_settled", actor=actor,
                    details={"claim_id": claim_id, "results": results})
        return record

    def settle_all(self, **kw: Any) -> list[dict[str, Any]]:
        out = []
        for cid in list(self.state["claims"]):
            try:
                out.append(self.settle_claim(cid, **kw))
            except MutualAidError as e:
                out.append({"claim_id": cid, "skipped": True, "reason": e.message})
        return out

    def _upsert_receivable(self, claim: dict[str, Any], *, kind: str, cents: int,
                           reason: str, basis: dict[str, Any], at: str | None
                           ) -> dict[str, Any]:
        # 同一 claim+kind 存在未终结应收/应付时更新而不是重复开立。
        for r in self.state["receivables"].values():
            if r["claim_id"] == claim["id"] and r["kind"] == kind and \
                    r["state"] in ("refund_pending", "payable_pending",
                                   "partially_refunded", "partially_paid"):
                r["cents"] = cents
                r["basis"] = basis
                r["reason"] = reason
                return r
        rid = self._new_id("receivable", "D")
        state = "refund_pending" if kind == "refund" else "payable_pending"
        r = {
            "id": rid, "kind": kind, "claim_id": claim["id"],
            "member": claim["member"], "cents": cents, "paid_cents": 0,
            "state": state, "reason": reason, "basis": basis,
            "created_at": self.now_iso(at), "payments": [],
        }
        self.state["receivables"][rid] = r
        return r

    def record_recovery(
        self, receivable_id: str | None = None, cents: int | None = None,
        ref: str | None = None, member: str | None = None,
        at: str | None = None, actor: str = "cooperative",
        claim_id: str | None = None, note: str | None = None,
    ) -> dict[str, Any]:
        """成员实际归还多付款（recovery 回执）：现金入池，待退 -> 已退。"""
        if cents is None or ref is None:
            raise ValidationError("登记还款必须提供 cents 与 ref 还款凭据号")
        r = self._find_receivable(receivable_id, member=member, claim_id=claim_id,
                                  kind="refund") if receivable_id or member or claim_id else None
        amount = int(cents)
        if amount <= 0:
            raise ValidationError("还款金额必须为正")
        if r is None:
            # 无明确应收的还款也允许入账为待核对，绝不任选一户扣回。
            self._ledger(
                "recovery_unapplied", "unmatched_member_recovery",
                cents=amount, cash_delta=amount, ref=ref, actor=actor, at=at,
                member=member, claim_id=claim_id,
                basis={"note": "未匹配到待退记录，暂记待核对"},
            )
            return {"unapplied": True, "cents": amount, "ref": ref}
        due = int(r["cents"]) - int(r.get("paid_cents", 0))
        if amount > due:
            raise ValidationError("还款超过待退余额",
                                  details={"amount": amount, "due": due})
        r["paid_cents"] = int(r.get("paid_cents", 0)) + amount
        r["payments"].append({"cents": amount, "ref": ref, "at": self.now_iso(at), "note": note})
        r["state"] = "refunded" if r["paid_cents"] == r["cents"] else "partially_refunded"
        self.state["transfers"].append({
            "ref": ref, "receivable_id": r["id"], "claim_id": r["claim_id"],
            "member": r["member"], "kind": "recovery", "state": "paid",
            "cents": amount, "at": self.now_iso(at),
        })
        self._ledger(
            "recovery_received", "member_refund_recovered",
            cents=amount, cash_delta=amount, receivable_id=r["id"],
            claim_id=r["claim_id"], member=r["member"], ref=ref,
            actor=actor, at=at,
            basis={"receivable_total_cents": r["cents"],
                   "cumulative_recovered_cents": r["paid_cents"]},
        )
        return r

    def pay_receivable(self, receivable_id: str, *, ref: str,
                       cents: int | None = None, at: str | None = None,
                       actor: str = "cooperative") -> dict[str, Any]:
        """执行 pool -> 成员的应付（要求有真实可用资金）。"""
        r = self.state["receivables"].get(receivable_id)
        if not r or r["kind"] != "payable":
            raise NotFoundError("应付记录不存在", details={"receivable_id": receivable_id})
        due = int(r["cents"]) - int(r.get("paid_cents", 0))
        amount = due if cents is None else int(cents)
        if not 0 < amount <= due:
            raise ValidationError("支付金额非法", details={"amount": amount, "due": due})
        bal = self.balance_summary()
        # 应付本身已在 payable_reserved 中占额；支付时现金与应付准备同降，
        # 因此这里校验现金足以覆盖本次支付即可（不能透支）。
        if self.state["cash_cents"] < amount:
            raise ConflictError("池现金不足，应付继续排队",
                                details={"cash": self.state["cash_cents"], "need": amount})
        r["paid_cents"] = int(r.get("paid_cents", 0)) + amount
        r["payments"].append({"cents": amount, "ref": ref, "at": self.now_iso(at)})
        r["state"] = "paid" if r["paid_cents"] == r["cents"] else "partially_paid"
        self.state["transfers"].append({
            "ref": ref, "receivable_id": r["id"], "claim_id": r["claim_id"],
            "member": r["member"], "kind": "payable_payment", "state": "paid",
            "cents": amount, "at": self.now_iso(at),
        })
        self._ledger(
            "payable_paid", "pool_paid_settlement_due",
            cents=amount, cash_delta=-amount, receivable_id=r["id"],
            claim_id=r["claim_id"], member=r["member"], ref=ref,
            actor=actor, at=at,
        )
        return r

    def _find_receivable(self, receivable_id: str | None, *, member: str | None,
                         claim_id: str | None, kind: str
                         ) -> dict[str, Any] | None:
        if receivable_id:
            r = self.state["receivables"].get(receivable_id)
            if not r:
                raise NotFoundError("应收/应付记录不存在")
            return r
        candidates = [r for r in self.state["receivables"].values()
                      if r["kind"] == kind and r["state"] in
                      ("refund_pending", "partially_refunded")]
        if claim_id:
            candidates = [r for r in candidates if r["claim_id"] == claim_id]
        if member:
            code = self.resolve_member(member)
            candidates = [r for r in candidates if r["member"] == code]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            return None
        raise ConflictError("存在多笔待退记录，请指定 receivable_id",
                            details={"ids": [r["id"] for r in candidates]})

    # ------------------------------------------------------------ quota audit
    def quota_audit(self, ref_or_commitment_id: str) -> dict[str, Any]:
        """出资代表核对：某次拨付为什么占用/释放共同额度。"""
        cid = ref_or_commitment_id
        cm = self.state["commitments"].get(cid)
        if cm is None:
            tr = self.find_transfer(ref_or_commitment_id)
            cid = tr["commitment_id"]
            cm = self.state["commitments"][cid]
        entries = [e for e in self.state["pool_ledger"]
                   if e.get("commitment_id") == cid]
        # 关联申请级流水（结清/recovery 等）也附上。
        claim_entries = [e for e in self.state["pool_ledger"]
                         if e.get("claim_id") == cm["claim_id"]
                         and e.get("commitment_id") != cid]
        return {
            "commitment": cm,
            "occupation_and_release_entries": entries,
            "related_claim_entries": claim_entries,
            "current_balance": self.balance_summary(),
        }

    def quota_statement(self) -> dict[str, Any]:
        return {
            "balance": self.balance_summary(),
            "ledger": list(self.state["pool_ledger"]),
            "open_suspense": [s for s in self.state["suspense"] if s["status"] == "open"],
        }

    # ------------------------------------------------------------- member view
    def member_view(self, member: str) -> dict[str, Any]:
        code = self.resolve_member(member)
        claims_view = []
        adjustments = []
        for c in self.state["claims"].values():
            if c["member"] != code:
                continue
            loss = self.claim_loss(c["id"])
            d = self.deficiencies(c["id"])
            cms = self.claim_commitments(c["id"])
            recs = [r for r in self.state["receivables"].values() if r["claim_id"] == c["id"]]
            stl = self.state["settlements"].get(c["id"])
            claims_view.append({
                "claim_id": c["id"], "shipment": c["shipment"], "leg": c["leg"],
                "round": c["round"], "status": c["status"],
                "verified_at": c.get("verified_at"),
                "submitted_at": c.get("submitted_at"),
                "loss_share": loss,
                "deficiencies": self._scrub(d, code),
                "commitments": [self._scrub(cm, code) for cm in cms],
                "settlement": self._scrub(stl, code),
                "receivables": [self._scrub(r, code) for r in recs],
            })
            for e in self.state["pool_ledger"]:
                if e.get("claim_id") == c["id"] and e.get("member") == code:
                    adjustments.append({
                        "at": e["at"], "kind": e["kind"], "reason": e["reason"],
                        "cents": e["cents"], "ref": e.get("ref"),
                        "basis": self._scrub(e.get("basis", {}), code),
                    })
        return {
            "viewer": code,
            "member": self.state["members"].get(code),
            "season": self.state["agreement"].get("season"),
            "household": self.household_position(code),
            "claims": claims_view,
            "receivables": [r for r in self.state["receivables"].values()
                            if r["member"] == code],
            "adjustments": adjustments,
            "privacy": "本视图仅含本成员的权益与调整依据；批次内其他户的身份/金额已隐去",
        }

    _SCRUB_KEYS = {"member_requests", "member_allocations", "batch_allocations",
                   "fair_order", "member_order"}

    def _scrub(self, obj: Any, code: str) -> Any:
        """递归隐去其他成员的批次分摊身份/金额，只保留本人与汇总口径。"""
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            memberish = all(
                isinstance(k, str) and (k in self.state["members"] or k.startswith("P"))
                for k in obj.keys()
            ) and bool(obj)
            if memberish and code in obj:
                return {code: self._scrub(obj[code], code)}
            for k, v in obj.items():
                if k in self._SCRUB_KEYS:
                    out[k] = {"disclosed": "aggregate_only",
                              "total_cents": self._sum_scrub_values(v)}
                elif k == "allocations" and isinstance(v, list):
                    out[k] = [self._scrub(x, code) for x in v
                              if isinstance(x, dict) and x.get("member", code) == code]
                elif k == "noncompliant_reason":
                    continue
                else:
                    out[k] = self._scrub(v, code)
            return out
        if isinstance(obj, list):
            return [self._scrub(x, code) for x in obj]
        return obj

    @staticmethod
    def _sum_scrub_values(v: Any) -> Any:
        if isinstance(v, dict):
            nums = [x for x in v.values() if isinstance(x, int)]
            return sum(nums) if nums else None
        return None

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.state)

    @classmethod
    def from_dict(cls, state: dict[str, Any]) -> "MutualAidService":
        obj = cls.__new__(cls)
        obj._clock = lambda: datetime.now(CST)
        obj.state = copy.deepcopy(state)
        return obj

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    @classmethod
    def load(cls, path: str | Path) -> "MutualAidService":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------- portals
    def as_member(self, code: str) -> "MemberPortal":
        return MemberPortal(self, code)

    def as_funder(self, rep_id: str = "funder-rep") -> "FunderPortal":
        return FunderPortal(self, rep_id)


def EligibilityError_from(elig: Any) -> MutualAidError:  # noqa: N802
    if isinstance(elig, MutualAidError):
        return elig
    from .core import EligibilityError
    return EligibilityError("申领资格核对未通过", details=elig)


def settlement_plan_safe(*, cost: int, resale: int, comp: int, paid: int,
                         pending: int, held: int,
                         final_relief_bp: int = 10000) -> dict[str, int]:
    from .core import settlement_plan
    return settlement_plan(
        cost_cents=cost, resale_cents=resale, compensation_cents=comp,
        advance_paid_cents=paid, advance_pending_cents=pending,
        compensation_held_by_pool_cents=held,
        final_relief_bp=final_relief_bp,
    )


def _sig(preview: dict[str, Any]) -> tuple[Any, ...]:
    keys = ("final_loss_cents", "recognized_aid_cents", "advance_paid_cents",
            "advance_pending_cents", "comp_held_cents")
    return tuple(preview.get(k) for k in keys)


# ---------------------------------------------------------------------------
# 角色门户（强制可见范围）
# ---------------------------------------------------------------------------
class MemberPortal:
    def __init__(self, svc: MutualAidService, code: str):
        self.svc = svc
        self.code = svc.resolve_member(code)

    def _own(self, member: str | None) -> None:
        if member is not None and self.svc.resolve_member(member) != self.code:
            raise PermissionDeniedError("成员只能查询/办理本人的权益")

    def view(self) -> dict[str, Any]:
        return self.svc.member_view(self.code)

    def deficiencies(self) -> list[dict[str, Any]]:
        return self.svc.member_deficiencies(self.code)

    def submit_claim(self, **kw: Any) -> dict[str, Any]:
        kw["member"] = self.code
        kw.pop("actor", None)
        return self.svc.submit_claim(actor=self.code, **kw)

    def submit_material(self, claim_id: str, *a: Any, **kw: Any) -> dict[str, Any]:
        c = self.svc._claim(claim_id)
        self._own(c["member"])
        return self.svc.submit_material(claim_id, *a, by=self.code, **kw)

    def correct_claim(self, claim_id: str, **kw: Any) -> dict[str, Any]:
        c = self.svc._claim(claim_id)
        self._own(c["member"])
        return self.svc.correct_claim(claim_id, actor=self.code, **kw)

    def record_recovery(self, cents: int, ref: str, **kw: Any) -> dict[str, Any]:
        # 成员端只能还自己的待退；不允许指定他人单据。
        rid = kw.pop("receivable_id", None)
        if rid:
            r = self.svc.state["receivables"].get(rid)
            if not r or r["member"] != self.code:
                raise PermissionDeniedError("只能归还本人的待退款项")
        return self.svc.record_recovery(rid, member=self.code, cents=cents,
                                        ref=ref, actor=self.code, **kw)


class FunderPortal:
    """出资代表：核对共同额度占用/释放，不办理成员材料。"""

    def __init__(self, svc: MutualAidService, rep_id: str):
        self.svc = svc
        self.rep_id = rep_id

    def quota_statement(self) -> dict[str, Any]:
        return self.svc.quota_statement()

    def quota_audit(self, ref_or_id: str) -> dict[str, Any]:
        return self.svc.quota_audit(ref_or_id)

    def next_batch_view(self, round: str | None = None) -> dict[str, Any]:
        return self.svc.next_batch_view(round)


# ---------------------------------------------------------------------------
# 常见动词/命名别名（同一实现，方便不同调用习惯与脚本）
# ---------------------------------------------------------------------------
_ALIASES = {
    "get_available_balance": "available_balance",
    "get_balance": "balance_summary",
    "check_available_balance": "available_balance",
    "get_next_batch": "next_batch_view",
    "allocate_batch": "approve_batch",
    "approve_advances": "approve_batch",
    "run_batch": "approve_batch",
    "supplement_material": "submit_material",
    "add_document": "submit_material",
    "upload_material": "submit_material",
    "missing_materials": "member_deficiencies",
    "get_deficiencies": "deficiencies",
    "check_eligibility": "deficiencies",
    "add_compensation": "register_compensation",
    "record_compensation": "register_compensation",
    "split_compensation": "register_compensation",
    "add_recovery": "record_recovery",
    "repay": "record_recovery",
    "settle": "settle_claim",
    "clear_claim": "settle_claim",
    "settlement": "settle_claim",
    "audit_allocation": "quota_audit",
    "audit_quota": "quota_audit",
    "get_member_view": "member_view",
    "get_household_position": "household_position",
}
for _alias, _target in _ALIASES.items():
    if not hasattr(MutualAidService, _alias):
        setattr(MutualAidService, _alias, getattr(MutualAidService, _target))
