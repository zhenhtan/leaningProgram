# GitHub Actions CI/CD 配置指南（Robot Framework）

本文档说明本仓库的 Robot Framework 流水线：改 `keywords.py` → 推送 → 自动出 review 单子（PR）
→ 测试通过后解锁 Merge 按钮。

---

## 一、流程总览

```
改 robot_demo/keywords.py
        ↓ git push origin ci-demo/xxx
GitHub Actions 自动触发
        ↓
① 跑 example.robot
② 解析 output.xml 生成测试报告
③ 自动开一个指向 main 的 PR        ← 这就是「review 单子」
④ 在 PR 里发测试报告评论（✅/❌ + 失败原因表格）
        ↓
测试通过 → robot-tests 检查变绿 → Merge 按钮解锁
测试失败 → 检查变红 → Merge 按钮锁死，评论里直接写明哪个用例挂了
```

关键文件：

| 文件 | 作用 |
|---|---|
| `.github/workflows/robot-ci.yml` | 流水线定义 |
| `scripts/robot_summary.py` | 把 `output.xml` 转成 Markdown 报告 |
| `scripts/ci_local.sh` | 本地复现 CI 的测试与报告部分 |
| `robot_demo/requirements.txt` | 锁死 `robotframework==7.4.2` |
| `robot_demo/.gitignore` | 忽略测试产物，避免每次跑测试都产生假 diff |

---

## 二、一次性配置（3 个开关）

> ⚠️ **第 2 条是整条链路的命门。** GitHub 默认**禁止** Actions 创建 PR。
> 不勾它，流水线会一路跑绿、测试也通过，但**开不出单子**，报错信息还很容易被忽略。

### 1. 放开 Workflow 权限

`仓库 → Settings → Actions → General → Workflow permissions`

- 选 **Read and write permissions**
- 点 Save

（`robot-ci.yml` 里虽然已经声明了 `permissions: pull-requests: write`，
但如果仓库这里是只读，权限会被这个总开关压住。）

### 2. 允许 Actions 创建 PR

同一个页面，往下拉到最底部：

- 勾选 **Allow GitHub Actions to create and approve pull requests**
- 点 Save

不勾的话会看到这个报错：

```
GitHub Actions is not permitted to create or approve pull requests.
```

### 3. 给 main 配置分支保护

> 顺序很重要：**必需检查必须先成功跑过一次，才能在下拉框里搜到它。**
> 所以先完成第四节「首次运行」，再回来做这一步。

**方式 A：Rulesets（新版，推荐）**

1. `Settings → Rules → Rulesets → New ruleset → New branch ruleset`
2. **Ruleset Name**：`protect-main`
3. **Enforcement status**：`Active`
4. **Target branches** → `Add target` → `Include default branch`
5. 勾选 **Require a pull request before merging**
   （`Required approvals` 设为 `0` 即可，个人仓库不需要他人批准）
6. 勾选 **Require status checks to pass** → 在 `Add checks` 搜索框里找 **`robot-tests`** → 选中
7. `Create`

**方式 B：经典分支保护规则**

1. `Settings → Branches → Add branch protection rule`
2. **Branch name pattern**：`main`
3. 勾 **Require a pull request before merging**
4. 勾 **Require status checks to pass** → 搜索并选中 **`robot-tests`**
5. `Create` / `Save changes`

**建议不要勾** `Require branches to be up to date before merging`：
它要求每次合并前都先把 main 合进分支并重跑，个人仓库里纯属徒增步骤。

配置完成后，PR 页面会显示 `Merging is blocked`，直到 `robot-tests` 变绿。

---

## 三、日常使用

```bash
# 1. 开分支（前缀必须是 ci-demo/ 才会自动触发）
git checkout -b ci-demo/my-change

# 2. 改关键字
vim robot_demo/keywords.py

# 3. 推送前先本地自查（可选，但强烈建议）
bash scripts/ci_local.sh

# 4. 提交并推送
git add -A
git commit -m "update keywords"
git push -u origin ci-demo/my-change

# 5. 到 GitHub 上看自动开出来的 PR，等检查变绿后点 Merge
```

推送后几十秒内会自动出现 PR。**不需要手动点 New pull request。**

想在 Actions 页面手动重跑一次（不开 PR，只跑测试）：`Actions → Robot CI → Run workflow`。

---

## 四、首次运行（让检查出现，才能配分支保护）

分支保护里的必需检查只能从「已经成功跑过的检查」里选。所以第一次要这样：

1. 推一个 `ci-demo/init` 分支上去
2. 等 `robot-tests` 跑完（绿或红都行，只要跑过）
3. 回到第二节第 3 步，配置分支保护
4. 后续每次推送就都会受门禁约束了

---

## 五、报告长什么样

PR 里的评论由 CI 自动创建，并且**每次重跑都是原地更新同一条**（靠隐藏标记
`<!-- robot-ci-report -->` 去重），不会在时间线上堆一长串。

全部通过时：

```markdown
### Robot Framework 测试报告 ✅ 全部通过

**结论**：2 通过 / 0 失败 / 0 跳过（共 2 个用例）· 全部用例通过

| 用例 | 状态 | 耗时 (s) | 失败原因 |
|:--|:--|--:|:--|
| 加法计算应该正确 | ✅ PASS | 0.005 | - |
| 字符串拼接应该正确 | ✅ PASS | 0.002 | - |
```

有失败时，表格里直接给出失败所在的关键字和断言差异，还会折叠完整失败消息：

```markdown
| 加法计算应该正确 | ❌ FAIL | 0.002 | `Should Be Equal As Integers`：-1 != 5 |
```

完整的 `log.html` / `report.html` / `output.xml` 在 Run 页面 → **Artifacts** → `robot-results` 下载。
（制品下载需要登录，所以评论里给的是 Run 页面链接，不是伪造的直链。）

---

## 六、Robot 退出码语义

排查问题时别踩这个坑 —— **退出码是「失败用例数」，不是布尔值**：

| 退出码 | 含义 | 该查什么 |
|---|---|---|
| `0` | 全部通过 | — |
| `1~249` | 有 N 个用例失败 | 看报告里的失败用例表 |
| `250` | 失败用例 ≥ 250 | 同上 |
| `251` | 打印了 help / version | 命令行参数写错了 |
| `252` | 测试数据或参数非法 | `.robot` 语法、库导入路径 |
| `253` | 被中断 | — |
| `255` | robot 内部错误 | — |

另外有个隐蔽情况：**库导入失败时退出码可能是 `0`**，错误只写在 `output.xml` 的
`<errors>` 节点里。所以报告脚本会单独把 `<errors>` 提出来高亮，避免环境坏掉的构建显示成全绿。

---

## 七、失败排查表

| 现象 | 原因 | 解决 |
|---|---|---|
| 推送后完全没有触发工作流 | 分支名前缀不是 `ci-demo/` | 用 `ci-demo/xxx` 命名，或手动开 PR |
| 工作流跑了，但没开出 PR | 第二节 #2 的开关没勾 | 勾上 `Allow GitHub Actions to create and approve pull requests` |
| 报错 `Resource not accessible by integration` | 权限不足 | 检查第二节 #1 设为 Read and write |
| PR 上 `robot-tests` 一直 pending | 工作流没被触发 | 留意：本仓库**故意没加 `paths` 过滤**，就是为了避免这种情况 |
| 评论没出现 | token 只读（fork 的 PR） | fork 场景只跑测试不评论，属预期行为；从本仓库分支推送即可 |
| 分支保护里搜不到 `robot-tests` | 该检查还没跑过 | 先按第四节跑一次 |
| 测试报 `No keyword with name 'Add Numbers' found.` | `keywords.py` 有语法错误或装饰器名改了 | 跑 `bash scripts/ci_local.sh` 本地定位 |
| 报 `Library 'SeleniumLibrary' does not exist` | `selenium_example.robot` 被误跑了 | 确认命令是 `robot_demo/example.robot`，不要写成 `robot robot_demo/` |

---

## 八、设计上刻意为之的地方（改动前请先读）

这几条都是踩过坑之后定下来的，看起来「多余」但其实不能删：

1. **不加 `paths` 过滤**
   直觉上应该写「只在 `keywords.py` 变更时触发」，但那会**永久锁死合并**：
   分支保护的语义是「必需检查必须存在且成功」，一旦某次推送因路径不匹配而没触发工作流，
   PR 上这个检查会永远停在 pending。本仓库只有 2 个用例，全量跑几秒钟，不值得为省这点时间冒险。

2. **检查名硬编码为 `robot-tests`**
   不要改成 `test (${{ matrix.python-version }})` 这类带变量的名字。以后一加 matrix，
   名字会漂移成 `robot-tests (3.10)`，分支保护里勾的旧名字失效，合并被静默卡死，极难排查。

3. **只跑 `example.robot`**
   `selenium_example.robot` 依赖 SeleniumLibrary 和真实 Chrome，CI 里跑到必然失败。

4. **最后一定要有 `Enforce test result` 这一步**
   测试步骤带 `continue-on-error: true` 是为了「测试挂了也能出报告」，
   但这会让整个 job 仍然算成功。没有最后的门禁步骤，测试失败也会显示绿灯 —— 门禁形同虚设。

5. **`concurrency` 不开启 `cancel-in-progress`**
   取消正在跑的检查会产生 `cancelled` 状态，而它不满足分支保护的「成功」要求，同样会卡住合并。

---

## 九、与 Jenkinsfile 的关系

仓库里原有的 `Jenkinsfile` 与 `JENKINS_GITHUB_TRIGGER_SETUP.md` **保持原样**，作为学习对照。

需要明确的是：

- **GitHub Actions 是当前唯一生效的合并门禁。** 分支保护里绑定的是 `robot-tests`。
- Jenkins 那套需要一台公网可达的 Jenkins 服务器 + Webhook + 插件配置才能跑起来，
  目前**没有在跑**。两套同时启用会出现两个门禁语义，容易混淆。

---

## 十、回滚

| 想撤销什么 | 怎么做 |
|---|---|
| 撤销 CI 流程 | 删掉 `.github/workflows/robot-ci.yml` 并提交 |
| 撤销分支保护 | `Settings → Rules → Rulesets`（或 Branches）里删掉对应规则 |
| 撤销单个 PR | 关闭 PR + 删除对应分支 |
| 恢复被取消追踪的产物文件 | `git add -f robot_demo/log.html robot_demo/output.xml robot_demo/report.html` |

---

## 十一、本地自查

推送前跑一遍，和 CI 用的是同一个脚本，结果一致：

```bash
bash scripts/ci_local.sh
```

它会建一个 `.ci_venv`，装好 `robotframework==7.4.2`，只跑 `example.robot`，
然后打印出「CI 会贴到 PR 里的那份报告」。

Windows Git Bash 下如果 `python3` 不可用，显式指定解释器：

```bash
PYTHON="/c/path/to/python.exe" bash scripts/ci_local.sh
```
