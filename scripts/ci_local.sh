#!/usr/bin/env bash
# 本地复现 CI 的「测试 + 报告」部分，方便推送前先自查。
#
# 它做的事情和 .github/workflows/robot-ci.yml 里的步骤一一对应：
#   1. 建虚拟环境      2. 装 requirements.txt 里的依赖
#   3. 只跑 example.robot（绝不跑整个目录）
#   4. 用同一个脚本生成 review 报告
#
# 不包含的部分：开 PR、发 PR 评论（那两步依赖 GitHub 环境，只能在 CI 里跑）。
#
# 用法：
#   bash scripts/ci_local.sh
#   PYTHON=/c/path/to/python.exe bash scripts/ci_local.sh   # 指定解释器

set -uo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
VENV_DIR="${VENV_DIR:-.ci_venv}"
ARTIFACT_NAME="${ARTIFACT_NAME:-robot-results}"

echo "==> 1/4 准备虚拟环境：$VENV_DIR"
if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON" -m venv "$VENV_DIR" || { echo "创建虚拟环境失败，请检查 PYTHON 是否指向可用解释器"; exit 1; }
fi

# Windows 的 venv 把解释器放在 Scripts/，Linux/macOS 放在 bin/，两边都要认
if [ -x "$VENV_DIR/bin/python" ]; then
  VENV_PY="$VENV_DIR/bin/python"
elif [ -x "$VENV_DIR/Scripts/python.exe" ]; then
  VENV_PY="$VENV_DIR/Scripts/python.exe"
else
  echo "在 $VENV_DIR 里找不到 python 解释器"; exit 1
fi
echo "    使用解释器：$VENV_PY"

echo "==> 2/4 安装依赖"
"$VENV_PY" -m pip install --upgrade pip --quiet
"$VENV_PY" -m pip install -r robot_demo/requirements.txt --quiet || { echo "依赖安装失败"; exit 1; }

echo "==> 3/4 运行 example.robot"
echo "    注意：只跑 example.robot。selenium_example.robot 需要真实 Chrome，本地/CI 都会失败。"
"$VENV_PY" -m robot --outputdir robot_demo robot_demo/example.robot
ROBOT_EXIT_CODE=$?
echo "    robot 退出码：$ROBOT_EXIT_CODE（0=全过，1~249=有 N 个用例失败，251/252/253/255=环境或参数问题）"

echo "==> 4/4 生成 review 报告"
ARGS=(
  --xml robot_demo/output.xml
  --out robot_demo/review.md
  --json robot_demo/review.json
  --artifact-name "$ARTIFACT_NAME"
  --quiet
)
if [ -n "${ROBOT_EXIT_CODE:-}" ]; then
  ARGS+=(--exit-code "$ROBOT_EXIT_CODE")
fi
"$VENV_PY" scripts/robot_summary.py "${ARGS[@]}"

echo ""
echo "================== 报告全文 =================="
cat robot_demo/review.md
echo "=============================================="
echo "报告文件：robot_demo/review.md（这份内容就是 CI 会贴到 PR 里的评论）"

if [ "$ROBOT_EXIT_CODE" != "0" ]; then
  echo ""
  echo "结论：测试未通过（退出码 $ROBOT_EXIT_CODE）。推上去后 PR 检查会变红、禁止合并。"
  exit 1
fi

echo ""
echo "结论：测试全部通过。推送到 ci-demo/* 分支即可触发 CI 并自动开单。"
