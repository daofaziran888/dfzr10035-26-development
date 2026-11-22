"""采集续跑 · 缺字段解析与投递续跑 端到端验证。

两种模式：
- HTTP 模式（默认）：对已启动的 uvicorn 发真实 HTTP 请求，覆盖路由/序列化全链路。
- 进程内模式（--in-process）：直接驱动领域服务，便于无网络环境快速核对状态机。

运行：
  python3 -I verify_resume_delivery.py                 # HTTP，需先启动后端
  python3 -I verify_resume_delivery.py --in-process    # 进程内直连服务

覆盖题面全部不变量：
1. 异常行（A 缺必填 amount）补齐前不投递、不按零额/默认值交付；
2. 补录沿原请求+原响应行重新定位，原始响应行永不被改写；
3. 重试只重发未确认行，已确认行（C）绝不重复投递/重复确认；
4. 显式 null（B）按映射保留，重解析/重试/补录均不改写。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

# -I 隔离模式会忽略 PYTHONPATH，这里显式加入后端目录以便进程内模式导入 app 包。
sys.path.insert(0, "/workspace/backend")

BASE = "http://127.0.0.1:8000/api/resume"
REQUEST_NO = "COLL-RESUME-0001"
REJECT_REQUEST_NO = "COLL-REJECT-0002"

# 一次采集响应三行：
# A 缺 amount（路径不存在）；B 的 amount 显式 null（映射允许空值）；C 的 amount=8。
ROWS = [
    {"row_id": "A", "order_id": "ORD-A", "currency": "CNY"},
    {"row_id": "B", "order_id": "ORD-B", "currency": "CNY", "amount": None},
    {"row_id": "C", "order_id": "ORD-C", "currency": "CNY", "amount": 8},
]
MAPPINGS = [
    {"field": "order_id", "path": "order_id", "required": True, "allow_null": False},
    {"field": "amount", "path": "amount", "required": True, "allow_null": True},
]

PASS = "✅"
FAIL = "❌"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = PASS if condition else FAIL
    print(f"  {mark} {label}" + (f" —— {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


# ---------------- HTTP 客户端 ----------------

def http_call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - 仅在接口报错时出现
        raise AssertionError(f"HTTP {exc.code}: {exc.read().decode('utf-8')}") from exc


# ---------------- 进程内客户端 ----------------

def inprocess_call_factory():
    from app.services.resume_delivery import resume_delivery_service as svc

    def call(method: str, path: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        if path == "/intake":
            return svc.intake_collection(
                request_no=payload["request_no"], endpoint=payload.get("endpoint", ""),
                rows=payload["rows"], mappings=payload["mappings"])
        # 其余路径均含 /requests/{编号}，统一按段解析。
        segments = [s for s in path.split("/") if s]
        rn = segments[segments.index("requests") + 1]
        if path.endswith("/reset"):
            return svc.reset_request(rn)
        if path.endswith("/start"):
            return svc.start_delivery(rn)
        if path.endswith("/interrupt"):
            return svc.interrupt_delivery(rn, payload.get("reason", "接收方中断"))
        if path.endswith("/retry"):
            return svc.retry_delivery(rn)
        if path.endswith("/receiver-results"):
            return svc.receiver_results(rn)
        if path.endswith("/ops-view"):
            return svc.ops_view(rn)
        if path.endswith("/parse-issues"):
            return svc.parse_issues(rn)
        if "/rows/" in path:
            row_key = segments[segments.index("rows") + 1]
            if path.endswith("/acknowledge"):
                return svc.acknowledge_row(
                    rn, row_key, receiver=payload.get("receiver", "接收方"),
                    receipt_no=payload.get("receipt_no"))
            if path.endswith("/reject"):
                return svc.reject_row(
                    rn, row_key, reason=payload["reason"],
                    receiver=payload.get("receiver", "接收方"))
            if path.endswith("/supplement"):
                return svc.supplement_field(
                    rn, row_key, payload["field"], payload["value"],
                    operator=payload.get("operator", "补齐人"))
        raise AssertionError(f"未覆盖的路径：{path}")

    return call


def run_main_flow(call) -> None:
    rn = REQUEST_NO
    call("POST", f"/requests/{rn}/reset")

    # 1) 解析三行：A 阻断、B 保留 null 可交付、C=8 可交付
    print("\n[1] 采集解析 A/B/C（A 缺 amount，B amount=null 允许空，C amount=8）")
    intake = call("POST", "/intake", {
        "request_no": rn, "endpoint": "ENDP-RESUME-0001",
        "rows": ROWS, "mappings": MAPPINGS,
    })
    check("intake 返回成功", intake["ok"] is True, str(intake))
    rows_by_key = {r["row_key"]: r for r in intake["rows"]}
    check("A 被标记为缺字段待补齐", rows_by_key["A"]["status"] == "缺字段待补齐")
    check("A 未通过字段为 amount 且原因为路径不存在",
          rows_by_key["A"]["failed_fields"] == [
              {"field": "amount", "path": "amount", "reason": "路径不存在"}])
    check("B 核验通过可交付", rows_by_key["B"]["status"] == "待投递")
    check("B 的 amount 明确保留为 null（不是零额/缺省）",
          rows_by_key["B"]["mapped_data"].get("amount") is None
          and "amount" in rows_by_key["B"]["mapped_data"])
    check("C 核验通过且 amount=8",
          rows_by_key["C"]["status"] == "待投递"
          and rows_by_key["C"]["mapped_data"]["amount"] == 8)

    issues = call("GET", f"/requests/{rn}/parse-issues")
    check("问题清单只有 A 一条，且留存原响应行",
          issues["summary"]["total"] == 1
          and issues["issues"][0]["row_key"] == "A"
          and issues["issues"][0]["raw_response_row"] == ROWS[0])
    results = call("GET", f"/requests/{rn}/receiver-results")
    check("投递前待办只有 B、C，A 未核验不得进入待办",
          sorted(t["row_key"] for t in results["delivery_todo"]) == ["B", "C"])
    check("未确认行为 B、C，A 计入阻断行",
          results["unconfirmed_rows"] == ["B", "C"]
          and results["blocked_rows"] == ["A"])

    # 2) 启动投递：实际发出 B、C 两行
    print("\n[2] 启动投递，实际发出可交付行")
    started = call("POST", f"/requests/{rn}/start")
    sent_keys = sorted(r["row_key"] for r in started["sent_rows"])
    check("实际发出 B、C 两行", sent_keys == ["B", "C"], str(sent_keys))
    check("禁止事项①：被阻断的 A 没有被发出（不当零额/默认值交付）", "A" not in sent_keys)

    # 3) 接收方确认 C，然后中断
    print("\n[3] 接收方确认 C 后投递中断")
    ack_c = call("POST", f"/requests/{rn}/rows/C/acknowledge",
                 {"receiver": "收货方-财务", "receipt_no": "RCPT-C-1"})
    check("C 确认成功并生成回执", ack_c["ok"] and ack_c["receipt"]["receipt_no"] == "RCPT-C-1")
    check("C 回执载荷 amount=8", ack_c["receipt"]["confirmed_payload"]["amount"] == 8)
    interrupt = call("POST", f"/requests/{rn}/interrupt", {"reason": "网络中断"})
    check("投递状态置为中断", interrupt["ok"])

    # 4) 重试投递：C 已确认不可重复，只重发未确认的 B
    print("\n[4] 中断后重试投递")
    retry = call("POST", f"/requests/{rn}/retry")
    retry_keys = sorted(r["row_key"] for r in retry["sent_rows"])
    check("重试只发出未确认的 B", retry_keys == ["B"], str(retry_keys))
    check("禁止事项③：已确认的 C 不在重发行列", "C" not in retry_keys)
    results = call("GET", f"/requests/{rn}/receiver-results")
    c_row = next(t for t in results["delivery_todo"] if t["row_key"] == "C")
    b_row = next(t for t in results["delivery_todo"] if t["row_key"] == "B")
    check("C 投递次数保持 1（未重复交付）", c_row["deliver_attempts"] == 1,
          f"attempts={c_row['deliver_attempts']}")
    check("B 重试后投递次数为 2", b_row["deliver_attempts"] == 2,
          f"attempts={b_row['deliver_attempts']}")
    check("未确认行只剩 B", results["unconfirmed_rows"] == ["B"])
    check("已确认行只有 C", results["confirmed_rows"] == ["C"])

    # 5) 对 C 重复确认：幂等，不产生第二条回执
    print("\n[5] 重复确认 C 的幂等性")
    ack_c_again = call("POST", f"/requests/{rn}/rows/C/acknowledge",
                       {"receiver": "收货方-财务", "receipt_no": "RCPT-C-DUP"})
    check("重复确认被识别为 duplicate", ack_c_again.get("duplicate") is True)
    check("重复确认不新增回执，仍指向原回执 RCPT-C-1",
          ack_c_again["receipt"]["receipt_no"] == "RCPT-C-1")
    results = call("GET", f"/requests/{rn}/receiver-results")
    check("C 始终只有一条确认回执",
          sum(1 for r in results["receipts"] if r["row_key"] == "C") == 1)

    # 6) 确认 B：null 保持不变
    print("\n[6] 接收方确认 B（显式 null 必须原样保留）")
    ack_b = call("POST", f"/requests/{rn}/rows/B/acknowledge",
                 {"receiver": "收货方-财务", "receipt_no": "RCPT-B-1"})
    check("B 确认成功", ack_b["ok"] and not ack_b.get("duplicate"))
    check("B 回执中 amount 仍为 null",
          ack_b["receipt"]["confirmed_payload"].get("amount") is None
          and "amount" in ack_b["receipt"]["confirmed_payload"])
    issues = call("GET", f"/requests/{rn}/parse-issues")
    check("问题清单仍只有 A 一项待补齐（B 从未进问题清单）",
          issues["summary"]["total"] == 1
          and issues["issues"][0]["status"] == "待补齐"
          and issues["outstanding_row_keys"] == ["A"])

    # 7) 补 A 的 amount=5：只进下一次投递待办，不立即发出
    print("\n[7] 补交 A 的 amount=5（进入下一次投递，不立即发出）")
    supp = call("POST", f"/requests/{rn}/rows/A/supplement",
                {"field": "amount", "value": 5, "operator": "采集运维"})
    check("补录核验通过", supp["ok"], str(supp))
    check("补录不隐式投递（响应不含 sent_rows）", "sent_rows" not in supp)
    results = call("GET", f"/requests/{rn}/receiver-results")
    a_todo = next(t for t in results["delivery_todo"] if t["row_key"] == "A")
    check("A 在待办中状态为待投递、投递次数仍为 0",
          a_todo["status"] == "待投递" and a_todo["deliver_attempts"] == 0)
    check("禁止事项②：补录沿原行定位，A 的原始响应行未被改写",
          next(d for d in call("GET", f"/requests/{rn}/ops-view")["rows"]
               if d["row_key"] == "A")["raw_response_row_immutable"] == ROWS[0])

    # 8) 下一次投递实际发出 A；B、C 已确认均不重发；随后确认 A
    print("\n[8] 下一次投递发出 A 并确认，完成全链路")
    started_again = call("POST", f"/requests/{rn}/start")
    check("下一次投递只发出新补录的 A",
          [r["row_key"] for r in started_again["sent_rows"]] == ["A"])
    results = call("GET", f"/requests/{rn}/receiver-results")
    a_todo = next(t for t in results["delivery_todo"] if t["row_key"] == "A")
    c_todo = next(t for t in results["delivery_todo"] if t["row_key"] == "C")
    b_todo = next(t for t in results["delivery_todo"] if t["row_key"] == "B")
    check("A 补齐后仅投递一次（attempts=1）", a_todo["deliver_attempts"] == 1)
    check("禁止事项③：C 仍只投递一次", c_todo["deliver_attempts"] == 1)
    check("B 仍为两次（重试一次）且未被补录流程重发", b_todo["deliver_attempts"] == 2)
    ack_a = call("POST", f"/requests/{rn}/rows/A/acknowledge",
                 {"receiver": "收货方-财务", "receipt_no": "RCPT-A-1"})
    check("A 确认成功，金额为 5",
          ack_a["ok"] and ack_a["receipt"]["confirmed_payload"]["amount"] == 5)

    # 9) 最终核对 + 双视图 + 事件轨迹
    print("\n[9] 最终全量核对（接收结果 / 问题清单 / 运维视图）")
    results = call("GET", f"/requests/{rn}/receiver-results")
    issues = call("GET", f"/requests/{rn}/parse-issues")
    ops = call("GET", f"/requests/{rn}/ops-view")
    receipts = {r["row_key"]: r for r in results["receipts"]}
    check("三行全部确认，无未确认/阻断/拒收行",
          results["confirmed_rows"] == ["A", "B", "C"]
          and results["unconfirmed_rows"] == []
          and results["blocked_rows"] == []
          and results["rejected_rows"] == [])
    check("请求状态为投递完成", results["request_status"] == "投递完成")
    check("C 全程仅确认一次（回执唯一）",
          sum(1 for r in results["receipts"] if r["row_key"] == "C") == 1
          and receipts["C"]["receipt_no"] == "RCPT-C-1")
    check("禁止事项④：B 的 null 全链路保持不变",
          receipts["B"]["confirmed_payload"]["amount"] is None
          and next(r for r in results["row_view"] if r["row_key"] == "B")["null_fields"] == ["amount"])
    check("A 补齐前未交付、补齐后只有一笔 5",
          receipts["A"]["confirmed_payload"] == {"order_id": "ORD-A", "amount": 5}
          and sum(1 for r in results["receipts"] if r["row_key"] == "A") == 1)
    check("问题清单中 A 已解决并关联回执",
          issues["issues"][0]["status"] == "已解决"
          and issues["issues"][0]["resolved_receipt"] == "RCPT-A-1"
          and issues["outstanding_row_keys"] == [])
    check("问题清单保留 A 的原响应行与补齐历史（含原请求/原行号定位坐标）",
          issues["issues"][0]["raw_response_row"] == ROWS[0]
          and issues["issues"][0]["supplement_history"][0]["located_by"]["line_no"] == 1
          and issues["issues"][0]["supplement_history"][0]["value"] == 5)
    ops_rows = {r["row_key"]: r for r in ops["rows"]}
    check("运维视图：异常行仅 A，补录记录可追溯，重试次数与最终状态齐全",
          ops["abnormal_rows"] == ["A"]
          and ops_rows["A"]["supplement_records"][0]["field"] == "amount"
          and ops_rows["A"]["final_status"] == "已确认"
          and ops_rows["C"]["deliver_attempts"] == 1
          and ops_rows["B"]["final_status"] == "已确认")
    event_actions = [e["action"] for e in ops["events"]]
    check("事件轨迹按序覆盖 解析→投递→确认→中断→重试→补录→再投递→完成",
          ["采集解析", "启动投递", "接收确认", "投递中断", "重试投递",
           "字段补录", "启动投递", "接收确认", "请求完成"][0] in event_actions
          and event_actions.count("接收确认") == 3
          and event_actions.count("字段补录") == 1
          and event_actions[-1] == "请求完成",
          str(event_actions))

    # 10) 防护类接口
    print("\n[10] 防护：重复建批 / 改写已确认行 / 完成后重试")
    r = call("POST", "/intake", {"request_no": rn, "rows": [], "mappings": MAPPINGS})
    check("重复建批被拒绝", not r["ok"])
    r = call("POST", f"/requests/{rn}/rows/C/supplement", {"field": "amount", "value": 99})
    check("改写已确认行 C 被拒绝", not r["ok"])
    r = call("POST", f"/requests/{rn}/retry")
    check("完成后再重试被拒绝", not r["ok"])


def run_reject_flow(call) -> None:
    """独立请求验证拒收：拒收行进接收视图、不再自动投递、不产生确认回执。"""
    rn = REJECT_REQUEST_NO
    call("POST", f"/requests/{rn}/reset")
    print("\n[11] 拒收分支（独立请求）")
    call("POST", "/intake", {
        "request_no": rn, "rows": ROWS, "mappings": MAPPINGS,
    })
    call("POST", f"/requests/{rn}/start")
    rej = call("POST", f"/requests/{rn}/rows/B/reject",
               {"reason": "金额空值需业务复核", "receiver": "收货方-财务"})
    check("B 拒收成功并记录原因",
          rej["ok"] and rej["status"] == "已拒收")
    call("POST", f"/requests/{rn}/rows/C/acknowledge", {"receipt_no": "RJ-C-1"})
    call("POST", f"/requests/{rn}/interrupt", {"reason": "中断"})
    retry = call("POST", f"/requests/{rn}/retry")
    check("重试不重发已拒收的 B 与已确认的 C", retry["sent_rows"] == [])
    results = call("GET", f"/requests/{rn}/receiver-results")
    check("接收视图列出拒收行 B 与拒收原因",
          results["rejected_rows"] == ["B"]
          and next(r for r in results["row_view"] if r["row_key"] == "B")["reject_reason"]
          == "金额空值需业务复核")
    check("B 没有确认回执", all(x["row_key"] != "B" for x in results["receipts"]))
    check("A 仍阻断，请求未被标记完成",
          results["blocked_rows"] == ["A"] and results["request_status"] != "投递完成")
    r = call("POST", f"/requests/{rn}/rows/B/supplement", {"field": "order_id", "value": "X"})
    check("拒收行补录被拒绝（须先解除拒收）", not r["ok"])
    r = call("POST", f"/requests/{rn}/rows/B/acknowledge", {})
    check("已拒收行不能再确认", not r["ok"])


def main() -> int:
    in_process = "--in-process" in sys.argv
    call = inprocess_call_factory() if in_process else http_call
    mode = "进程内直连服务" if in_process else "真实 HTTP 链路"
    print(f"== 采集续跑 E2E（{mode}） ==")

    run_main_flow(call)
    run_reject_flow(call)

    print("\n== 未确认行随操作的实际变化轨迹 ==")
    print("  解析后       ：未确认=[B, C]，阻断=[A]（A 未核验，不进待办）")
    print("  确认C后中断  ：未确认=[B]，已确认=[C]")
    print("  重试投递后   ：未确认=[B]（C 未重发，attempts 仍为 1）")
    print("  确认B后      ：未确认=[]（B 的 null 原样回执），问题清单仍欠 A")
    print("  补齐A        ：A 进下一次投递待办（attempts=0），B/C 状态不变")
    print("  再投递并确认A：未确认=[]，问题清单清零，请求完成")

    print()
    if failures:
        print(f"{FAIL} 失败 {len(failures)} 项：{failures}")
        return 1
    print(f"{PASS} 全部断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
