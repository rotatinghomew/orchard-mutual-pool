"""命令行：python3 -m mutual_aid [serve|demo|check]。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .service import MutualAidService

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "source_records.json"
DEFAULT_DB = ROOT / ".runtime" / "state.json"


def _print(title: str, obj: Any) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("-" * 78)
    if isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_check(args: argparse.Namespace) -> int:
    svc = MutualAidService.from_data_file(args.data)
    bal = svc.balance_summary()
    problems: list[str] = []
    # 种子数据的硬口径：B4 两笔拨付正好 150000，且 85928/64072 符合规则。
    c_by_claim = {c["claim_id"]: c for c in svc.state["commitments"].values()}
    if (c_by_claim["E1"]["cents"], c_by_claim["E2"]["cents"]) != (85928, 64072):
        problems.append("B4 拨付金额与比例分摊规则不符")
    if bal["available_cents"] != 0:
        problems.append(f"B4 后下一批真实可用余额应为 0，实际 {bal['available_cents']}")
    sp = svc.state["suspense"]
    if len(sp) != 1 or sp[0]["cents"] != 17000:
        problems.append("Y501 应有 17000 分待核对余额")
    if problems:
        for p in problems:
            print("FAIL:", p)
        return 1
    print("OK: 种子资料口径全部成立；B4=85928/64072，下一批可用=0，suspense=17000")
    print(json.dumps(bal, ensure_ascii=False))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    print("#" * 78)
    print("# 柑橘产季互助池 —— 从 source_records 走一遍完整业务")
    print("#" * 78)

    svc = MutualAidService.from_data_file(args.data)
    _print("1) 开局：B4 拨付已按比例分摊并占满共同额度", {
        "balance": svc.balance_summary(),
        "B4": [svc.quota_audit(ref)["commitment"] for ref in ("P810", "P814")],
    })

    _print("2) 下一批 B5 的真实可用余额（上一批 pending 承诺仍占额）",
           svc.next_batch_view("B5"))
    _print("B5 试算：P03 请求 72800，但当前可拨 0", svc.preview_batch("B5"))

    # 资格 / 经营边界
    from .core import EligibilityError
    try:
        svc.submit_claim(member="P01", shipment="TR-62", leg=2, round="B6", claim_id="X1")
        svc.verify_claim("X1")
    except Exception as e:  # noqa: BLE001
        _print("3) 经营人变更边界 [from,to)：11-26 起 O3 属 P03，P01 不能申领 TR-62",
               {"blocked": type(e).__name__, "detail": getattr(e, "details", str(e))})

    # 缺件查询：新申请未交费用凭证
    svc.submit_claim(member="P02", shipment="TR-99", leg=1, round="B6", claim_id="N1")
    d = svc.deficiencies("N1")
    _print("4) 成员查“哪次临时支出还缺什么”（N1 无运输凭据 -> 资格/材料均缺）", {
        "compliant": d["compliant"],
        "missing": [i["code"] for i in d["missing"]],
        "eligibility": d.get("eligibility"),
    })

    # 资金回执 returned -> 释放 -> B5 部分拨付
    svc.record_receipt("P814", "returned", actor="coop")
    b5 = svc.approve_batch("B5", at="2026-11-26T12:00:00+08:00")
    _print("5) P814 returned 明确退回 -> 释放 64072；B5 单户按可用额部分预付 64072", {
        "available_after_return": 64072,
        "B5_commitments": b5["commitment_ids"],
        "B5_allocated": b5["total_allocated_cents"],
        "balance": svc.balance_summary(),
    })

    # 补偿拆分
    _print("6) Y501 一证多项：100000/82000 先入账，17000 与 P03 无金额项留待核对", {
        "booked": svc.state["compensation"]["Y501"]["split"]["booked"],
        "unallocated": svc.state["compensation"]["Y501"]["split"]["unallocated_cents"],
        "unconfirmed_parts": svc.state["compensation"]["Y501"]["split"]["unconfirmed_parts"],
    })

    # 结清 E1（成员直收补偿）-> 待退 -> 已退
    s1 = svc.settle_claim("E1")
    rid = s1["results"]["receivables"][0]
    svc.record_recovery(rid, cents=12928, ref="R9001")
    _print("7) E1/P01：净损失 173000 - 补偿 100000 = 73000；预付 85928 -> 待退 12928 -> 已退", {
        "settlement": s1["preview"], "receivable": svc.state["receivables"][rid],
        "balance_after_recovery": svc.balance_summary(),
    })

    # E2：P814 已 returned，故结清形成应付 47000（另一个分支见 demo --branch paid）
    s2 = svc.settle_claim("E2")
    _print("8) E2/P02（P814 已退回、成员未收到钱）：最终损失 47000 -> 应付 47000",
           s2["preview"])

    # 待核对补偿归属 P03
    sp = svc.state["suspense"][0]["id"]
    svc.attribute_suspense(sp, [{
        "shipment": "TR-62", "leg": 2, "party_ref": "P03", "part_cents": 17000}])
    _print("9) 核对后把 17000 明确分到 P03 份额：E3 最终损失变为 87000；分不清前绝不扣回",
           svc.preview_settlement("E3"))

    _print("10) 出资代表审计 P810：占用依据（比例分摊+最早核实补 1 分）",
           svc.quota_audit("P810"))
    _print("11) 成员 P01 视角（只有本人权益与调整依据）", svc.member_view("P01"))
    return 0


def cmd_demo_paid_branch(_args: argparse.Namespace) -> int:
    svc = MutualAidService.from_data_file(DEFAULT_DATA)
    svc.record_receipt("P814", "paid")
    s2 = svc.settle_claim("E2")
    rid = s2["results"]["receivables"][0]
    _print("分支：P814 先全额到账，再结清 E2 -> 已到账多付款 17072 走待退/还款", {
        "plan": s2["preview"], "receivable": svc.state["receivables"][rid],
    })
    svc.record_recovery(rid, cents=17072, ref="R9002")
    _print("P02 还款后余额", svc.balance_summary())

    svc3 = MutualAidService.from_data_file(DEFAULT_DATA)
    svc3.receive_compensation_proceeds("Y501", proceeds_to="pool")
    s1 = svc3.settle_claim("E1")
    pid = s1["results"]["receivables"][0]
    _print("分支：Y501 打入池账户，E1 结清 -> 12928 对冲保留，87072 为应付成员的补偿转付", {
        "plan": s1["preview"], "payable": svc3.state["receivables"][pid],
        "balance": svc3.balance_summary(),
    })
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import create_server
    db = Path(args.db) if args.db else (DEFAULT_DB if args.persist else None)
    if db and db.exists() and not args.reset:
        svc = MutualAidService.load(db)
        print(f"[mutual-aid] 已载入运行状态 {db}")
    else:
        svc = MutualAidService.from_data_file(args.data)
        if db:
            svc.save(db)
    httpd = create_server(svc, host=args.host, port=args.port, db_path=db)
    url = f"http://{args.host}:{args.port}/"
    print(f"[mutual-aid] HTTP 服务已启动: {url}  (Ctrl+C 停止)")
    print("[mutual-aid] 身份头: X-Actor-Role: member|coop|funder, X-Actor-Code: P01")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[mutual-aid] 已停止")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mutual_aid", description="产季互助与退回清算服务")
    p.add_argument("--data", default=str(DEFAULT_DATA), help="source_records.json 路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="校验种子资料口径").set_defaults(func=cmd_check)
    sub.add_parser("demo", help="运行完整业务演示").set_defaults(func=cmd_demo)
    sub.add_parser("demo-paid", help="演示已到账多付款分支").set_defaults(func=cmd_demo_paid_branch)

    s = sub.add_parser("serve", help="启动 HTTP+UI 服务")
    import os
    s.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    s.add_argument("--db", default=None, help="状态持久化 JSON 路径")
    s.add_argument("--persist", action="store_true", help="使用默认 .runtime/state.json 持久化")
    s.add_argument("--reset", action="store_true", help="忽略已有 db，从种子重建")
    s.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
