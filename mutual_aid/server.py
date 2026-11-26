"""仅用标准库的 JSON HTTP 服务 + 单页操作界面。

启动：
    python3 app.py --port 8000
    python3 -m mutual_aid serve --port 8000

身份通过请求头 ``X-Actor-Role`` (member|coop|funder) 与
``X-Actor-Code``（成员代码）传递，也可用 query 参数 role/as。
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .core import MutualAidError, NotFoundError
from .service import MutualAidService

HERE = Path(__file__).resolve().parent
INDEX_HTML = (HERE / "static" / "index.html").read_text(encoding="utf-8") \
    if (HERE / "static" / "index.html").exists() else "<meta charset=utf-8><title>互助池</title>"


# 允许通过 /api/call 直接调用的服务方法（只读 + 业务动作）。
RPC_ALLOWED = {
    "available_balance", "next_batch_view", "household_position",
    "member_view", "member_deficiencies", "deficiencies",
    "submit_claim", "correct_claim", "submit_material", "review_material",
    "accept_material", "reject_material", "verify_claim",
    "preview_batch", "approve_batch", "top_up_batch_shortfall",
    "record_receipt", "cancel_commitment", "release_commitment_excess",
    "register_compensation", "confirm_compensation",
    "receive_compensation_proceeds",
    "attribute_suspense", "preview_settlement", "settle_claim", "settle_all",
    "record_recovery", "pay_receivable", "quota_audit", "quota_statement",
    "register_member", "resolve_member", "claim_loss",
}


def create_server(service: MutualAidService, host: str = "127.0.0.1",
                  port: int = 8000, db_path: str | Path | None = None
                  ) -> ThreadingHTTPServer:
    ctx = {"svc": service, "db_path": Path(db_path) if db_path else None,
           "lock": threading.RLock()}

    class Handler(BaseHTTPRequestHandler):
        server_version = "MutualAid/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 静默默认访问日志
            return

        # ------------------------------------------------------ helpers
        def _actor(self, q: dict[str, list[str]]) -> tuple[str, str | None]:
            role = self.headers.get("X-Actor-Role") or q.get("role", [None])[0] or "coop"
            code = self.headers.get("X-Actor-Code") or q.get("as", [None])[0]
            return str(role), code

        def _send(self, status: int, payload: Any, *, html: bool = False) -> None:
            if html:
                body = payload.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            else:
                body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as e:
                raise MutualAidError("请求体不是合法 JSON", ) from e
            if not isinstance(data, dict):
                raise MutualAidError("请求体必须是 JSON 对象")
            return data

        def _persist(self) -> None:
            if ctx["db_path"]:
                ctx["svc"].save(ctx["db_path"])

        def _require(self, role: str, *allowed: str) -> None:
            if role not in allowed:
                from .core import PermissionDeniedError
                raise PermissionDeniedError(
                    f"当前身份 {role} 无权调用；允许角色: {', '.join(allowed)}")

        # ------------------------------------------------------ routing
        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            q = parse_qs(parsed.query)
            role, code = self._actor(q)
            try:
                with ctx["lock"]:
                    body = self._body() if method == "POST" else {}
                    result, status = self._route(method, path, q, body, role, code)
                    if method == "POST" and status < 400:
                        self._persist()
                    if path == "/" or path == "/index.html":
                        self._send(200, INDEX_HTML, html=True)
                    else:
                        self._send(status, result)
            except MutualAidError as e:
                self._send(e.http_status, e.to_dict())
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": "internal_error", "message": str(e)})

        def _route(self, method: str, path: str, q: dict[str, list[str]],
                   body: dict[str, Any], role: str, code: str | None
                   ) -> tuple[Any, int]:
            svc = ctx["svc"]

            if path in ("/", "/index.html"):
                return INDEX_HTML, 200
            if path in ("/health", "/api/health"):
                return {"ok": True, "service": "citrus-mutual-aid",
                        "bootstrapped": svc.state.get("bootstrapped")}, 200
            if path in ("/api", "/api/index", "/api/routes") and method == "GET":
                return {
                    "service": "citrus-mutual-aid",
                    "roles": {"member": "X-Actor-Role: member + X-Actor-Code",
                              "coop": "互助社经办人（默认）",
                              "funder": "出资代表，可查额度审计"},
                    "key_get": [
                        "/api/available", "/api/next-batch?round=B5",
                        "/api/quota/statement", "/api/quota/audit?ref=P814",
                        "/api/members/P01/view", "/api/members/P01/deficiencies",
                        "/api/claims/E1/deficiencies", "/api/batches/B5",
                        "/api/state"],
                    "key_post": [
                        "/api/claims", "/api/claims/E1/materials",
                        "/api/claims/E1/correct", "/api/claims/E1/verify",
                        "/api/claims/E1/settle", "/api/batches/B5/preview",
                        "/api/batches/B5/approve", "/api/batches/B5/top-up",
                        "/api/transfers/receipt", "/api/commitments/C2/cancel",
                        "/api/commitments/C2/release-excess",
                        "/api/compensation", "/api/compensation/Y501/confirm",
                        "/api/compensation/Y501/receive-proceeds",
                        "/api/suspense/S1/attribute", "/api/recoveries",
                        "/api/receivables/D1/pay", "/api/call"],
                    "rpc": "POST /api/call {\"method\": \"member_view\", \"args\": {\"member\":\"P01\"}}",
                    "seed_invariants": {
                        "B4_allocations": {"E1": 85928, "E2": 64072},
                        "next_batch_available_cents": 0,
                        "Y501_booked": {"P01": 100000, "P02": 82000},
                        "Y501_suspense_cents": 17000,
                        "E1_refund_if_member_routed_cents": 12928,
                    },
                }, 200
            if path in ("/rpc", "/api/rpc", "/api/call") and method == "POST":
                return self._rpc(body, role, code), 200

            if path == "/api/agreement":
                return svc.state["agreement"], 200
            if path == "/api/source":
                kind = q.get("kind", ["trips"])[0]
                if kind not in ("trips", "tenure"):
                    raise MutualAidError("kind 只能是 trips/tenure")
                return {kind: svc.state[kind]}, 200

            if path == "/api/available":
                self._require(role, "coop", "funder")
                return svc.available_balance(), 200
            if path == "/api/next-batch":
                self._require(role, "coop", "funder")
                return svc.next_batch_view(q.get("round", [None])[0]), 200
            if path == "/api/quota/statement":
                self._require(role, "coop", "funder")
                return svc.quota_statement(), 200
            if path == "/api/quota/audit":
                self._require(role, "coop", "funder")
                ref = q["ref"][0]
                return svc.quota_audit(ref), 200

            if path == "/api/members" and method == "GET":
                self._require(role, "coop")
                return {"members": list(svc.state["members"].values())}, 200
            if path == "/api/members" and method == "POST":
                self._require(role, "coop")
                return svc.register_member(**body), 201

            m = re.fullmatch(r"/api/members/([^/]+)/view", path)
            if m:
                target = m.group(1)
                if role == "member":
                    self._assert_same_member(code, target, svc)
                else:
                    self._require(role, "coop")
                return svc.member_view(target), 200

            m = re.fullmatch(r"/api/members/([^/]+)/deficiencies", path)
            if m:
                target = m.group(1)
                if role == "member":
                    self._assert_same_member(code, target, svc)
                else:
                    self._require(role, "coop")
                return {"deficiencies": svc.member_deficiencies(target)}, 200

            if path == "/api/claims" and method == "POST":
                if role == "member":
                    body["member"] = code
                    body.pop("actor", None)
                else:
                    self._require(role, "member", "coop")
                return svc.submit_claim(actor=f"{role}:{code or ''}", **body), 201

            m = re.fullmatch(r"/api/claims/([^/]+)", path)
            if m and method == "GET":
                cid = m.group(1)
                claim = svc._claim(cid)
                if role == "member":
                    self._assert_same_member(code, claim["member"], svc)
                return {"claim": claim, "loss": svc.claim_loss(cid),
                        "deficiencies": svc.deficiencies(cid),
                        "commitments": svc.claim_commitments(cid)}, 200

            m = re.fullmatch(r"/api/claims/([^/]+)/deficiencies", path)
            if m and method == "GET":
                cid = m.group(1)
                claim = svc._claim(cid)
                if role == "member":
                    self._assert_same_member(code, claim["member"], svc)
                return svc.deficiencies(cid), 200

            m = re.fullmatch(r"/api/claims/([^/]+)/materials", path)
            if m and method == "POST":
                cid = m.group(1)
                claim = svc._claim(cid)
                if role == "member":
                    self._assert_same_member(code, claim["member"], svc)
                else:
                    self._require(role, "member", "coop")
                return svc.submit_material(cid, by=code or role, **body), 201

            m = re.fullmatch(r"/api/claims/([^/]+)/correct", path)
            if m and method == "POST":
                cid = m.group(1)
                claim = svc._claim(cid)
                if role == "member":
                    self._assert_same_member(code, claim["member"], svc)
                return svc.correct_claim(cid, actor=f"{role}:{code}", **body), 200

            m = re.fullmatch(r"/api/claims/([^/]+)/verify", path)
            if m and method == "POST":
                self._require(role, "coop")
                return svc.verify_claim(m.group(1), **body), 200

            m = re.fullmatch(r"/api/claims/([^/]+)/settle", path)
            if m and method == "POST":
                self._require(role, "coop")
                return svc.settle_claim(m.group(1), **body), 200

            m = re.fullmatch(r"/api/materials/([^/]+)/review", path)
            if m and method == "POST":
                self._require(role, "coop")
                accept = bool(body.pop("accept", True))
                return svc.review_material(m.group(1), accept=accept, **body), 200

            m = re.fullmatch(r"/api/batches/([^/]+)", path)
            if m and method == "GET":
                self._require(role, "coop", "funder")
                rnd = m.group(1)
                if rnd == "preview":
                    return svc.preview_batch(q.get("round", [""])[0]), 200
                if rnd in svc.state["batches"]:
                    return svc.state["batches"][rnd], 200
                return svc.preview_batch(rnd), 200

            m = re.fullmatch(r"/api/batches/([^/]+)/approve", path)
            if m and method == "POST":
                self._require(role, "coop")
                return svc.approve_batch(m.group(1), **body), 200

            m = re.fullmatch(r"/api/batches/([^/]+)/preview", path)
            if m and method == "POST":
                self._require(role, "coop")
                return svc.preview_batch(m.group(1), **body), 200

            m = re.fullmatch(r"/api/batches/([^/]+)/top-up", path)
            if m and method == "POST":
                self._require(role, "coop")
                return svc.top_up_batch_shortfall(m.group(1), **body), 200

            if path == "/api/transfers/receipt" and method == "POST":
                self._require(role, "coop")
                return svc.record_receipt(**body), 200

            m = re.fullmatch(r"/api/commitments/([^/]+)/cancel", path)
            if m and method == "POST":
                self._require(role, "coop")
                return svc.cancel_commitment(m.group(1), **body), 200

            m = re.fullmatch(r"/api/commitments/([^/]+)/release-excess", path)
            if m and method == "POST":
                self._require(role, "coop")
                return svc.release_commitment_excess(m.group(1), **body), 200

            if path == "/api/compensation" and method == "POST":
                self._require(role, "coop")
                voucher = body.get("voucher", body)
                opts = {k: v for k, v in body.items() if k in ("proceeds_to", "at", "actor")}
                return svc.register_compensation(voucher, **opts), 201

            m = re.fullmatch(r"/api/compensation/([^/]+)/receive-proceeds", path)
            if m and method == "POST":
                self._require(role, "coop")
                return svc.receive_compensation_proceeds(m.group(1), **body), 200

            m = re.fullmatch(r"/api/compensation/([^/]+)/confirm", path)
            if m and method == "POST":
                self._require(role, "coop")
                return svc.confirm_compensation(m.group(1), **body), 200

            m = re.fullmatch(r"/api/suspense/([^/]+)/attribute", path)
            if m and method == "POST":
                self._require(role, "coop")
                return svc.attribute_suspense(m.group(1), body.get("allocations", []),
                                              **{k: v for k, v in body.items()
                                                 if k in ("at", "actor")}), 200

            if path == "/api/settle-all" and method == "POST":
                self._require(role, "coop")
                return {"results": svc.settle_all(**body)}, 200

            if path == "/api/recoveries" and method == "POST":
                if role == "member":
                    body["member"] = code
                    rid = body.get("receivable_id")
                    if rid:
                        r = svc.state["receivables"].get(rid)
                        if not r or svc.resolve_member(r["member"]) != svc.resolve_member(code):
                            from .core import PermissionDeniedError
                            raise PermissionDeniedError("只能归还本人的待退款项")
                else:
                    self._require(role, "member", "coop")
                return svc.record_recovery(actor=f"{role}:{code or ''}", **body), 201

            m = re.fullmatch(r"/api/receivables/([^/]+)/pay", path)
            if m and method == "POST":
                self._require(role, "coop")
                return svc.pay_receivable(m.group(1), **body), 200

            if path == "/api/state" and method == "GET":
                self._require(role, "coop")
                return svc.to_dict(), 200

            return {"error": "not_found", "path": path}, 404

        @staticmethod
        def _assert_same_member(code: str | None, target: str, svc: MutualAidService) -> None:
            from .core import PermissionDeniedError
            if not code:
                raise PermissionDeniedError("成员访问需要 X-Actor-Code 或 ?as=成员代码")
            if svc.resolve_member(code) != svc.resolve_member(target):
                raise PermissionDeniedError("成员只能查看本人的权益")

        def _rpc(self, body: dict[str, Any], role: str, code: str | None) -> dict[str, Any]:
            method = str(body.get("method", ""))
            if not method or method.startswith("_"):
                raise MutualAidError("非法 method")
            args = body.get("args", {}) or {}
            if not isinstance(args, dict):
                raise MutualAidError("args 必须是对象")
            svc = ctx["svc"]
            fn = getattr(svc, method, None)
            if not callable(fn):
                raise NotFoundError(f"没有可调用方法 {method}")
            # 角色边界：出资代表只读额度审计；成员限定本人；合作社可办理。
            funder_read = {
                "quota_statement", "quota_audit", "next_batch_view",
                "available_balance", "balance_summary",
                "get_available_balance", "get_balance", "get_next_batch",
                "audit_allocation", "audit_quota",
            }
            if role == "funder":
                if method not in funder_read:
                    self._require(role, "coop")  # 必然抛出 403
            elif role not in ("coop", "member"):
                from .core import PermissionDeniedError
                raise PermissionDeniedError(f"未知角色 {role!r}")
            # 成员角色强制限定本人视角/本人单据。
            member_scoped = {"member_view", "member_deficiencies"}
            member_claim_methods = {"submit_material", "correct_claim",
                                    "submit_claim"}
            if role == "member":
                if not code:
                    from .core import PermissionDeniedError
                    raise PermissionDeniedError("member 角色需要 X-Actor-Code 或 ?as=")
                if method in member_scoped:
                    args["member"] = code
                elif method == "submit_claim":
                    args["member"] = code
                elif method in member_claim_methods:
                    cid = args.get("claim_id")
                    claim = svc._claim(cid) if cid else None
                    if not claim or svc.resolve_member(claim["member"]) != svc.resolve_member(code):
                        from .core import PermissionDeniedError
                        raise PermissionDeniedError("只能办理本人的申请单据")
                elif method == "record_recovery":
                    args["member"] = code
                    rid = args.get("receivable_id")
                    if rid:
                        r = svc.state["receivables"].get(rid)
                        if not r or svc.resolve_member(r["member"]) != svc.resolve_member(code):
                            from .core import PermissionDeniedError
                            raise PermissionDeniedError("只能归还本人的待退款项")
                elif method not in {"deficiencies", "resolve_member", "claim_loss"}:
                    self._require(role, "coop", "funder")
            fn = getattr(svc, method)
            return {"method": method, "result": fn(**args)}

    httpd = ThreadingHTTPServer((host, port), Handler)
    ctx["httpd"] = httpd  # type: ignore[assignment]
    return httpd
