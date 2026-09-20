#!/usr/bin/env python3
"""把 Robot Framework 的 output.xml 转成一份人看的测试报告。

CI 和本地共用同一个脚本，保证「PR 里看到的」和「本地看到的」完全一致。

用法：
    python scripts/robot_summary.py --xml robot_demo/output.xml
    python scripts/robot_summary.py --xml robot_demo/output.xml --out robot_demo/review.md
    python scripts/robot_summary.py --xml robot_demo/output.xml --markdown robot_demo/review.md \
        --exit-code $ROBOT_EXIT --github-summary

设计要点（都踩过坑）：
1. robot 的退出码语义是「失败用例数」，不是布尔值：0=全过，1~249=有 N 个用例失败，
   250=失败数达上限，251/252/253/255=环境或参数问题。所以必须把退出码和 output.xml
   的统计一起判断，否则会把「用例挂了」和「测试根本没跑起来」混为一谈。
2. 库导入失败这类错误只写进 <errors> 节点，此时退出码可能仍是 0。
   所以必须单独把 <errors> 提出来，否则环境坏掉的构建会显示成全绿。
3. 本脚本是报告生成器，不是测试器 —— 永远以退出码 0 结束，不要因为它把构建搞挂。
"""

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET

# 隐藏标记：CI 靠它找到自己上次发的那条评论并原地更新，避免 PR 里堆一串报告。
COMMENT_MARKER = "<!-- robot-ci-report -->"

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

STATUS_ICON = {PASS: "✅", FAIL: "❌", SKIP: "⏭️"}


def classify_exit_code(exit_code):
    """把 robot 退出码翻译成 (verdict, 中文说明)。

    verdict 取值：pass / fail / error / unknown
    """
    if exit_code is None:
        return "unknown", "未知（未提供退出码）"
    if exit_code == 0:
        return "pass", "全部用例通过"
    if 1 <= exit_code <= 249:
        return "fail", f"有 {exit_code} 个用例失败"
    if exit_code == 250:
        return "fail", "失败用例达到上限（250 个及以上）"
    if exit_code == 251:
        return "error", "robot 打印了帮助或版本信息，命令行参数有误"
    if exit_code == 252:
        return "error", "测试数据或命令行参数非法（检查 .robot 语法与库导入路径）"
    if exit_code == 253:
        return "error", "测试执行被中断"
    if exit_code == 255:
        return "error", "robot 内部错误"
    return "unknown", f"未预期的退出码 {exit_code}"


def first_failing_keyword(test_node):
    """找到用例里第一个失败的关键字名，用来定位「挂在哪一步」。"""
    for kw in test_node.iter("kw"):
        status = kw.find("status")
        if status is not None and status.get("status") == FAIL:
            return kw.get("name") or kw.get("type") or ""
    return ""


def collect_fail_messages(test_node):
    """收集用例下所有 level=FAIL 的消息正文（含期望值 vs 实际值）。"""
    messages = []
    for msg in test_node.iter("msg"):
        if msg.get("level") == FAIL and msg.text:
            text = msg.text.strip()
            if text and text not in messages:
                messages.append(text)
    return messages


def parse_output_xml(xml_path):
    """解析 output.xml，返回结构化结果。文件不存在或损坏时返回 None。"""
    if not os.path.isfile(xml_path):
        return None
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as exc:
        return {"parse_error": str(exc), "tests": [], "errors": [], "totals": {}}

    totals = {"pass": 0, "fail": 0, "skip": 0}
    total_stat = root.find(".//statistics/total/stat")
    if total_stat is not None:
        for key in totals:
            totals[key] = int(total_stat.get(key) or 0)

    tests = []
    for test_node in root.findall(".//test"):
        status_node = test_node.find("status")
        status = (status_node.get("status") if status_node is not None else SKIP) or SKIP
        text = (status_node.text or "").strip() if status_node is not None else ""
        tests.append(
            {
                "name": test_node.get("name") or "(未命名用例)",
                "status": status,
                "elapsed": status_node.get("elapsed") if status_node is not None else None,
                "message": text,
                "failing_keyword": first_failing_keyword(test_node) if status == FAIL else "",
                "fail_messages": collect_fail_messages(test_node) if status == FAIL else [],
            }
        )

    # 注意：ElementTree.iter() 只认单个标签名，传 "errors/msg" 这种路径会静默返回空，
    # 必须用 findall(".//errors/msg")。这里踩过坑：写错会导致环境错误被完全漏掉。
    errors = []
    for msg in root.findall(".//errors/msg"):
        text = (msg.text or "").strip()
        if text:
            errors.append({"level": msg.get("level") or "ERROR", "text": text})

    suite_node = root.find(".//suite")
    return {
        "generator": root.get("generator") or "",
        "suite_name": suite_node.get("name") if suite_node is not None else "",
        "totals": totals,
        "tests": tests,
        "errors": errors,
    }


def escape_cell(text):
    """Markdown 表格单元格转义：竖线会破坏表格结构，换行会破坏行结构。"""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def build_markdown(result, exit_code, run_url=None, artifact_name="robot-results"):
    """生成给 PR 评论 / job summary 用的 Markdown 报告。"""
    verdict, verdict_text = classify_exit_code(exit_code)
    lines = [COMMENT_MARKER, ""]

    if result is None:
        lines += [
            "### Robot Framework 测试报告 ⚠️ 未产出结果",
            "",
            f"**结论**：找不到 `output.xml`，测试大概率根本没跑起来。{verdict_text}",
            "",
        ]
        if exit_code is not None:
            lines += [f"robot 退出码：`{exit_code}`", ""]
        lines += ["> 排查方向：依赖是否装成功、`robot_demo/example.robot` 路径是否存在、命令行参数是否正确。", ""]
        if run_url:
            lines += [f"📄 完整日志：[在 Run 页面查看]({run_url})", ""]
        return "\n".join(lines)

    if result.get("parse_error"):
        lines += [
            "### Robot Framework 测试报告 ⚠️ 结果文件损坏",
            "",
            f"`output.xml` 解析失败：`{escape_cell(result['parse_error'])}`",
            "",
        ]
        return "\n".join(lines)

    totals = result["totals"]
    headline = {
        "pass": "✅ 全部通过",
        "fail": "❌ 存在失败用例",
        "error": "⚠️ 测试未能正常执行",
        "unknown": "❓ 结果未知",
    }[verdict]

    # 退出码 0 但 <errors> 非空 = 用例虽然过了，环境本身有问题（库导入失败等）。
    # 这种情况不能显示成一片绿，否则等于把隐患藏起来。
    if verdict == "pass" and result["errors"]:
        headline = "✅ 用例通过，但环境有异常"
        verdict_text = f"{verdict_text}，另有 {len(result['errors'])} 条环境错误需关注"

    lines += [f"### Robot Framework 测试报告 {headline}", ""]
    lines += [
        f"**结论**：{totals['pass']} 通过 / {totals['fail']} 失败 / {totals['skip']} 跳过"
        f"（共 {len(result['tests'])} 个用例）· {verdict_text}",
        "",
    ]

    if result["tests"]:
        lines += ["| 用例 | 状态 | 耗时 (s) | 失败原因 |", "|:--|:--|--:|:--|"]
        for test in result["tests"]:
            icon = STATUS_ICON.get(test["status"], "")
            elapsed = test["elapsed"]
            elapsed_text = f"{float(elapsed):.3f}" if elapsed else "-"
            reason = test["message"]
            if test["failing_keyword"]:
                reason = f"`{test['failing_keyword']}`：{reason}" if reason else f"`{test['failing_keyword']}`"
            lines.append(
                f"| {escape_cell(test['name'])} | {icon} {test['status']} | {elapsed_text} | {escape_cell(reason) or '-'} |"
            )
        lines.append("")

    # <errors> 里放的是环境级问题（库导入失败等），退出码可能是 0，必须单独高亮。
    if result["errors"]:
        lines += [f"<details><summary>⚠️ 环境提示（{len(result['errors'])} 条，非用例失败）</summary>", ""]
        for err in result["errors"]:
            lines.append(f"- **{err['level']}**：{escape_cell(err['text'])}")
        lines += ["", "</details>", ""]

    detailed = [t for t in result["tests"] if t["status"] == FAIL and t["fail_messages"]]
    if detailed:
        lines += [f"<details><summary>完整失败消息（{len(detailed)} 个用例）</summary>", ""]
        for test in detailed:
            lines.append(f"**{escape_cell(test['name'])}**")
            lines.append("")
            lines.append("```")
            lines.extend(test["fail_messages"])
            lines.append("```")
            lines.append("")
        lines += ["</details>", ""]

    lines += [
        f"📦 报告与日志：Run 页面 → Artifacts → `{artifact_name}`"
        "（含 `log.html` / `report.html` / `output.xml`）",
        "",
    ]
    if run_url:
        lines += [f"🔗 [打开本次 Run 页面]({run_url})", ""]
    if result.get("generator"):
        lines += [f"<sub>生成环境：{escape_cell(result['generator'])}</sub>"]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="把 robot output.xml 转成 Markdown 测试报告")
    parser.add_argument("--xml", default="robot_demo/output.xml", help="output.xml 路径")
    parser.add_argument("--out", "--markdown", dest="out", help="把 Markdown 报告写到这个文件")
    parser.add_argument("--json", dest="json_out", help="把结构化结果写到这个 JSON 文件")
    parser.add_argument("--exit-code", type=int, default=None, help="robot 进程的退出码")
    parser.add_argument("--run-url", default=None, help="本次 CI run 的页面链接")
    parser.add_argument("--artifact-name", default="robot-results", help="制品名称")
    parser.add_argument("--github-summary", action="store_true", help="追加到 GITHUB_STEP_SUMMARY")
    parser.add_argument("--quiet", action="store_true", help="不把报告打印到 stdout")
    args = parser.parse_args(argv)

    result = parse_output_xml(args.xml)
    markdown = build_markdown(
        result,
        args.exit_code,
        run_url=args.run_url,
        artifact_name=args.artifact_name,
    )

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(markdown)

    if args.json_out:
        payload = {
            "exit_code": args.exit_code,
            "verdict": classify_exit_code(args.exit_code)[0],
            "result": result,
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    if args.github_summary:
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(markdown + "\n")

    if not args.quiet:
        sys.stdout.write(markdown + "\n")

    # 报告生成器永远不失败，否则会把「出报告」这件事变成构建失败的原因。
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
