"""产季互助预付与实际损失份额结清 —— 核心领域规则（纯函数 / 无 IO）。

口径来自《互助约定与往来凭证》：
* 金额一律为整数分（cents），比例用基点 bp（7000 = 70%）。
* 园地经营边界为 [from, to)，to 当日由后任经营。
* 业务份额键 = (shipment, leg, party_ref)，party_ref 是成员代码，
  果园代码不代替经营主体，花名也不能单独确定主体。
* 同一项中断的多次处理按份额键汇总 cost / resale。
* 批次不足时按各户合规请求比例分摊，整除余数按
  “最早核实时间，相同则成员代码”的顺序每分依次补 1。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------
class MutualAidError(Exception):
    """所有业务错误的基类，code 供接口层映射 HTTP 状态。"""

    code = "mutual_aid_error"
    http_status = 400

    def __init__(self, message: str | None = None, *, details: Any | None = None):
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(MutualAidError):
    code = "validation_error"
    http_status = 400


class NotFoundError(MutualAidError):
    code = "not_found"
    http_status = 404


class PermissionDeniedError(MutualAidError):
    code = "permission_denied"
    http_status = 403


class ConflictError(MutualAidError):
    code = "conflict"
    http_status = 409


class EligibilityError(MutualAidError):
    code = "eligibility_error"
    http_status = 422


# ---------------------------------------------------------------------------
# 时间 / 经营边界
# ---------------------------------------------------------------------------
def parse_day(value: str | date) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def parse_instant(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        # 本地交换资料默认使用合作社所在地时区（+08:00）。
        dt = dt.replace(tzinfo=CST)
    return dt


def iso_now(now: datetime | None = None) -> str:
    dt = now or datetime.now(CST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.isoformat(timespec="seconds")


def tenure_member(
    tenure: Sequence[Mapping[str, Any]], orchard: str, day: str | date
) -> str | None:
    """返回某日某果园在 [from,to) 口径下的经营成员代码；查不到返回 None。"""
    d = parse_day(day)
    for row in tenure:
        if row["orchard"] != orchard:
            continue
        start = parse_day(row["from"])
        end = parse_day(row["to"])
        if start <= d < end:
            return row["member"]
    return None


def tenure_history(
    tenure: Sequence[Mapping[str, Any]], orchard: str
) -> list[Mapping[str, Any]]:
    return sorted(
        [r for r in tenure if r["orchard"] == orchard],
        key=lambda r: parse_day(r["from"]),
    )


# ---------------------------------------------------------------------------
# 金额 / 分摊
# ---------------------------------------------------------------------------
def bp_part(cents: int, bp: int) -> int:
    """按基点比例计算并向下取整到分（保守给付，余数走批次分摊规则）。"""
    if cents < 0:
        raise ValidationError("金额不能为负", details={"cents": cents})
    if not 0 <= bp <= 10000:
        raise ValidationError("bp 必须在 0..10000", details={"bp": bp})
    return (int(cents) * int(bp)) // 10000


def apportion(
    amount: int,
    weights: Mapping[str, int],
    priority: Mapping[str, tuple[Any, ...]] | Sequence[str] | None = None,
) -> dict[str, int]:
    """把整数 amount 按 weights 比例“最大余数法”分摊。

    先向下取整；剩余的每一分按 priority 顺序（键越小越优先）补 1。
    priority 可以是 {key: 可排序元组}，也可以是已排好序的键序列。
    """
    amount = int(amount)
    if amount < 0:
        raise ValidationError("分摊总额不能为负")
    keys = [k for k, w in weights.items() if int(w) > 0]
    if not keys:
        if amount > 0:
            raise ValidationError("没有正权重请求，无法分摊非零金额")
        return {k: 0 for k in weights}
    total_w = sum(int(weights[k]) for k in keys)

    floors = {k: (amount * int(weights[k])) // total_w for k in keys}
    leftover = amount - sum(floors.values())

    if priority is None:
        order = sorted(keys)
    elif isinstance(priority, Mapping):
        order = sorted(keys, key=lambda k: priority.get(k, ()))  # type: ignore[arg-type]
    else:
        seq = list(priority)
        rank = {k: i for i, k in enumerate(seq)}
        order = sorted(keys, key=lambda k: rank.get(k, len(rank)))

    for i in range(int(leftover)):
        floors[order[i % len(order)]] += 1
    return {k: floors.get(k, 0) for k in weights}


# ---------------------------------------------------------------------------
# 运输份额 / 暂定损失
# ---------------------------------------------------------------------------
SHARE_FIELDS = ("shipment", "leg", "party_ref")


def share_key(row: Mapping[str, Any]) -> tuple[str, Any, str]:
    return (str(row["shipment"]), row["leg"], str(row["party_ref"]))


def loss_share(
    trips: Iterable[Mapping[str, Any]],
    shipment: str,
    leg: Any,
    party_ref: str,
) -> dict[str, Any]:
    """汇总一个业务份额 (shipment, leg, party_ref) 的费用与补送收入。"""
    rows = [
        t
        for t in trips
        if str(t["shipment"]) == str(shipment)
        and t["leg"] == leg
        and str(t["party_ref"]) == str(party_ref)
    ]
    cost = sum(int(t.get("cost_cents", 0)) for t in rows)
    resale = sum(int(t.get("resale_cents", 0)) for t in rows)
    units = sum(int(t.get("unit", 0)) for t in rows)
    return {
        "shipment": shipment,
        "leg": leg,
        "party_ref": party_ref,
        "trip_count": len(rows),
        "units": units,
        "cost_cents": cost,
        "resale_cents": resale,
        "tentative_loss_cents": max(0, cost - resale),
        "raw_net_cents": cost - resale,
        "trip_refs": [
            f"{t['shipment']}#L{t['leg']}/{t['party_ref']}@{t.get('at')}" for t in rows
        ],
    }


def eligible_shares(
    trips: Iterable[Mapping[str, Any]],
    tenure: Sequence[Mapping[str, Any]],
    member: str,
    shipment: str | None = None,
    leg: Any = None,
) -> list[dict[str, Any]]:
    """找出成员代码名下、且行为当日确实在对应果园经营的费用份额。"""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, Any, str]] = set()
    for t in trips:
        if str(t["party_ref"]) != str(member):
            continue
        if shipment is not None and str(t["shipment"]) != str(shipment):
            continue
        if leg is not None and t["leg"] != leg:
            continue
        key = share_key(t)
        if key in seen:
            continue
        seen.add(key)
        info = loss_share(
            [x for x in trips if share_key(x) == key], key[0], key[1], key[2]
        )
        operator = tenure_member(tenure, t["orchard"], t["at"])
        info["orchard"] = t["orchard"]
        info["at"] = t["at"]
        info["tenure_member_at"] = operator
        info["tenure_ok"] = operator == member
        out.append(info)
    return out


def check_claim_eligibility(
    trips: Iterable[Mapping[str, Any]],
    tenure: Sequence[Mapping[str, Any]],
    member: str,
    shipment: str,
    leg: Any,
) -> dict[str, Any]:
    """校验申领主体：费用份额 party_ref 与当日经营人都必须是该成员代码。"""
    rows = [
        t
        for t in trips
        if str(t["shipment"]) == str(shipment) and t["leg"] == leg
    ]
    if not rows:
        raise NotFoundError(
            "找不到对应运输/费用凭据",
            details={"shipment": shipment, "leg": leg},
        )
    mine = [t for t in rows if str(t["party_ref"]) == str(member)]
    if not mine:
        parties = sorted({str(t["party_ref"]) for t in rows})
        raise EligibilityError(
            "该份额的实际承担方不是此成员代码",
            details={
                "shipment": shipment,
                "leg": leg,
                "credential_party_refs": parties,
                "claim_member": member,
            },
        )
    problems: list[dict[str, Any]] = []
    for t in mine:
        operator = tenure_member(tenure, t["orchard"], t["at"])
        if operator != member:
            problems.append(
                {
                    "orchard": t["orchard"],
                    "at": t["at"],
                    "tenure_member_at": operator,
                    "claim_member": member,
                }
            )
    share = loss_share(trips, shipment, leg, member)
    if problems:
        raise EligibilityError(
            "费用发生当日的经营主体与申领成员不一致",
            details={"problems": problems, "share": share},
        )
    return {"eligible": True, "share": share, "tenure_checks": [
        {"orchard": t["orchard"], "at": t["at"], "operator": tenure_member(tenure, t["orchard"], t["at"])}
        for t in mine
    ]}


# ---------------------------------------------------------------------------
# 采购补偿凭证拆分
# ---------------------------------------------------------------------------
@dataclass
class SplitVoucher:
    booked: list[dict[str, Any]]
    unconfirmed_parts: list[dict[str, Any]]
    unknown_parts: list[dict[str, Any]]
    explicit_cents: int
    unallocated_cents: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "booked": self.booked,
            "unconfirmed_parts": self.unconfirmed_parts,
            "unknown_parts": self.unknown_parts,
            "explicit_cents": self.explicit_cents,
            "unallocated_cents": self.unallocated_cents,
        }


def split_compensation_voucher(
    voucher: Mapping[str, Any],
    known_keys: set[tuple[str, Any, str]] | None = None,
) -> SplitVoucher:
    """把一证多项的采购补偿拆成“可入账份额”和“待核对金额”。

    * part_cents 缺失 -> 该项不能确认金额，绝不估价入账。
    * 凭证总额 - 已明确且可归属部分 = 待核对 suspense。
    * 金额明确但份额键无法核对 -> 同样留待归属查验。
    """
    total = int(voucher["cents"])
    booked: list[dict[str, Any]] = []
    unconfirmed: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    explicit = 0

    for p in voucher.get("parts", []):
        key = (str(p["shipment"]), p["leg"], str(p["party_ref"]))
        amount = p.get("part_cents", None)
        if amount is None:
            unconfirmed.append({**p, "reason": "part_cents_missing"})
            continue
        amount = int(amount)
        if amount < 0:
            raise ValidationError("分摊金额不能为负", details={"part": p})
        if known_keys is not None and key not in known_keys:
            unknown.append({**p, "reason": "share_not_found"})
            continue
        booked.append(
            {
                "shipment": key[0],
                "leg": key[1],
                "party_ref": key[2],
                "part_cents": amount,
            }
        )
        explicit += amount

    if explicit > total:
        raise ValidationError(
            "凭证明细分摊额合计超过凭证总额",
            details={"ref": voucher.get("ref"), "explicit": explicit, "total": total},
        )
    # 无法核对的明确份额，其金额也进入待核对，不能任选一户扣回。
    unknown_cents = sum(int(p["part_cents"]) for p in unknown)
    unallocated = total - explicit  # 含 unknown_cents 与无金额项可能对应的钱
    return SplitVoucher(
        booked=booked,
        unconfirmed_parts=unconfirmed,
        unknown_parts=unknown,
        explicit_cents=explicit,
        unallocated_cents=unallocated,
    )


# ---------------------------------------------------------------------------
# 结清测算（纯函数，便于单测与复用）
# ---------------------------------------------------------------------------
def settlement_plan(
    *,
    cost_cents: int,
    resale_cents: int,
    compensation_cents: int,
    advance_paid_cents: int,
    advance_pending_cents: int,
    compensation_held_by_pool_cents: int = 0,
    final_relief_bp: int = 10000,
) -> dict[str, int]:
    """计算单个成员/份额终局结清的双边头寸。

    口径：
      final_loss = cost - resale - confirmed_compensation
      recognized_aid = final_loss * final_relief_bp（默认全额确认）

    与已拨付/已承诺预付比较：
      * 尚未拨付的承诺多余部分 -> pending_releasable（撤销/调减承诺）
      * 已拨付形成的多余部分   -> paid_excess_refund（成员应退）
      * 预付不足               -> extra_payable_aid（互助社应付）

    若采购补偿打进池里（held_by_pool），则先与应退对冲，
    对冲后剩余的补偿款是池应转付成员的 comp_remit。
    """
    net_before_comp = max(0, int(cost_cents) - int(resale_cents))
    comp = int(compensation_cents)
    over_compensation = max(0, comp - net_before_comp)
    final_loss = max(0, net_before_comp - comp)
    recognized_aid = bp_part(final_loss, final_relief_bp)

    paid = int(advance_paid_cents)
    pending = int(advance_pending_cents)

    # 待拨付承诺先向最终应助额靠拢。
    desired_pending = min(pending, max(0, recognized_aid - paid))
    pending_releasable = pending - desired_pending

    paid_excess_refund = max(0, paid - recognized_aid)
    extra_payable_aid = max(0, recognized_aid - paid - pending)

    held = max(0, int(compensation_held_by_pool_cents))
    retained_recovery = min(held, paid_excess_refund)
    refund_due = paid_excess_refund - retained_recovery
    comp_remit = held - retained_recovery

    total_payable = extra_payable_aid + comp_remit
    return {
        "net_before_comp_loss_cents": net_before_comp,
        "confirmed_compensation_cents": int(compensation_cents),
        "over_compensation_cents": over_compensation,
        "final_loss_cents": final_loss,
        "recognized_aid_cents": recognized_aid,
        "advance_paid_cents": paid,
        "advance_pending_cents": pending,
        "desired_pending_cents": desired_pending,
        "pending_releasable_cents": pending_releasable,
        "paid_excess_refund_cents": paid_excess_refund,
        "extra_payable_aid_cents": extra_payable_aid,
        "comp_held_cents": held,
        "retained_recovery_cents": retained_recovery,
        "refund_due_cents": refund_due,
        "comp_remit_payable_cents": comp_remit,
        "total_payable_cents": total_payable,
    }
