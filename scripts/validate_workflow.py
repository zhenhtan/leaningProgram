#!/usr/bin/env python3
"""GitHub Actions 工作流本地校验器。

存在的理由：工作流被 GitHub 拒绝时**不会有任何有用的报错**。
症状只有「Actions 里出现一条 0 个 job 的失败记录」，日志为空、注解为空。
你只能靠反复推送去猜，非常痛苦。

这个脚本把几类「本地能查出来、但线上极难定位」的问题提前拦住：

1. 字符串值里出现空的或非法的 ${{ }} 表达式
   —— 最阴的一类。块标量（run: | / script: |）的内容是字符串值，
   GitHub 会对它做表达式求值。哪怕它写在 JS 注释或 bash 注释里也一样会被求值。
   典型事故：为了说明「不要在 JS 里写 ${{ }}」而在注释里写了 ${{ }}，结果整个文件被拒。
2. job 的 name 里带 ${{ }} —— 会导致检查名随 matrix 等变量漂移，
   分支保护里勾的名字失效，合并被静默卡死。
3. 内嵌 JS 的语法错误（github-script 的 script 段）
4. run 段的 shell 语法错误
5. 缺少门禁 step：测试步骤带了 continue-on-error 却没有最后兜底失败的步骤，
   会让测试失败也显示绿灯，门禁形同虚设。

用法：
    python scripts/validate_workflow.py
    python scripts/validate_workflow.py .github/workflows/robot-ci.yml
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

# 合法的表达式内容：不能为空，且必须以字母或 _ 开头（上下文名）
EXPRESSION_PATTERN = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def walk_strings(node, path="(root)"):
    """遍历 YAML 结构，产出所有字符串值及其路径。"""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from walk_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk_strings(value, f"{path}[{index}]")


def check_expressions(doc):
    """检查字符串值里的 ${{ }} 表达式是否合法。

    注意：注释在 YAML 解析阶段就被剥离了，所以这个函数查的是「解析后仍在结构里」的字符串。
    块标量（run: | / script: |）的内容正是这种字符串 —— 也是出事最多的地方。
    """
    problems = []
    for path, value in walk_strings(doc):
        for match in EXPRESSION_PATTERN.finditer(value):
            inner = match.group(1).strip()
            if not inner:
                line = value[: match.start()].count("\n") + 1
                problems.append(
                    f"{path} 第 {line} 行附近存在【空表达式】`${{{{ }}}}` —— "
                    "GitHub 会拒绝整个工作流文件（表现为 0 个 job 的失败记录，无任何提示）"
                )
            elif not re.match(r"^[A-Za-z_]", inner):
                line = value[: match.start()].count("\n") + 1
                problems.append(
                    f"{path} 第 {line} 行附近存在疑似非法表达式 `${{{{ {inner} }}}}`"
                )
    return problems


def check_job_names(doc):
    """检查 job 的 name 是否含表达式（会导致必需检查名漂移）。"""
    problems = []
    for job_id, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        name = job.get("name")
        if isinstance(name, str) and "${{" in name:
            problems.append(
                f"job `{job_id}` 的 name 里含表达式（{name!r}）。"
                "检查名一旦随变量漂移，分支保护里勾的旧名字会失效、合并被静默卡死。请写死常量。"
            )
    return problems


def check_merge_gate(doc):
    """检查是否「有 continue-on-error 但没有兜底门禁」。

    测试步骤带 continue-on-error 是为了让测试失败时仍能出报告，
    但它会让 job 仍然算成功。没有最后的门禁步骤，测试失败也会显示绿灯。
    """
    problems = []
    for job_id, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps") or []
        tolerant = [s for s in steps if isinstance(s, dict) and s.get("continue-on-error")]
        if not tolerant:
            continue
        # 兜底门禁：一个 if: always() 且不以 uses 为主的步骤，通常带 exit 1
        has_gate = any(
            isinstance(s, dict)
            and "run" in s
            and "always()" in str(s.get("if", ""))
            and "exit 1" in str(s.get("run", ""))
            for s in steps
        )
        if not has_gate:
            problems.append(
                f"job `{job_id}` 有步骤设了 continue-on-error，但找不到最后兜底的门禁步骤"
                "（要求：含 `if: always()` 且含 `exit 1`）。"
                "缺了它，测试失败也会显示绿灯，门禁形同虚设。"
            )
    return problems


def run_syntax_check(kind, code, tmpdir, index):
    """用 node --check 或 bash -n 做语法检查。工具不存在时跳过。"""
    if kind == "js":
        exe = shutil.which("node")
        if not exe:
            return "skipped", "未找到 node，跳过 JS 语法检查"
        suffix, argv, wrapper = ".js", [exe, "--check"], ("async function main() {\n", "\n}\n")
    else:
        exe = shutil.which("bash")
        if not exe:
            return "skipped", "未找到 bash，跳过 shell 语法检查"
        suffix, argv, wrapper = ".sh", [exe, "-n"], ("", "")

    path = os.path.join(tmpdir, f"{kind}_{index}{suffix}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(wrapper[0] + code + wrapper[1])

    proc = subprocess.run(argv + [path], capture_output=True, text=True)
    if proc.returncode == 0:
        return "ok", ""
    return "error", (proc.stderr or proc.stdout or "").strip()


def validate(path):
    print(f"校验：{path}")
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()

    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        print(f"  [错误] YAML 解析失败：{exc}")
        return 1

    if not isinstance(doc, dict):
        print("  [错误] 工作流根节点必须是映射")
        return 1

    problems = []
    problems += check_expressions(doc)
    problems += check_job_names(doc)
    problems += check_merge_gate(doc)

    # 结构性语法检查
    syntax_notes = []
    with tempfile.TemporaryDirectory() as tmpdir:
        js_index = sh_run_index = 0
        for job in (doc.get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                script = (step.get("with") or {}).get("script")
                if script:
                    js_index += 1
                    status, detail = run_syntax_check("js", script, tmpdir, js_index)
                    if status == "error":
                        problems.append(f"内嵌 JS 语法错误（步骤 {step.get('name')!r}）：\n{detail}")
                    else:
                        syntax_notes.append(f"JS 段 {js_index}（{step.get('name')}）：{status}")
                    script = None
                if "run" in step:
                    sh_run_index += 1
                    status, detail = run_syntax_check("sh", step["run"], tmpdir, sh_run_index)
                    if status == "error":
                        problems.append(f"shell 语法错误（步骤 {step.get('name')!r}）：\n{detail}")
                    else:
                        syntax_notes.append(f"shell 段 {sh_run_index}（{step.get('name')}）：{status}")

    for note in syntax_notes:
        print(f"  [通过] {note}")

    if problems:
        print("")
        for problem in problems:
            print(f"  [问题] {problem}")
        print(f"\n共发现 {len(problems)} 个问题。")
        return 1

    print("\n  [通过] 未发现问题")
    return 0


def main(argv):
    targets = argv[1:] or [".github/workflows/robot-ci.yml"]
    existing = [t for t in targets if os.path.isfile(t)]
    missing = [t for t in targets if not os.path.isfile(t)]
    for path in missing:
        print(f"跳过不存在的文件：{path}")

    exit_code = 0
    for path in existing:
        exit_code |= validate(path)
        print("")
    return exit_code


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main(sys.argv))
