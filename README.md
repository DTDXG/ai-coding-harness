# AI Coding 规则体系 —— 可用的第一版

这个目录是**能直接装进项目跑起来的实现**。
每条规则背后的推导和实测数据,见下面「删掉过两条规则」和「已知的洞」两节。

只解决**短任务**(一次修复、一个小功能)的失败模式。长任务方案没做。

---

## 装法

```bash
# 1. 规则正文
cp AGENTS.md CLAUDE.md            <你的项目>/

# 2. 检查脚本(单文件、零依赖、只用标准库)
mkdir -p <你的项目>/scripts && cp scripts/check.py <你的项目>/scripts/

# 3. Claude Code hook(可选,但这是「不依赖模型记得」的关键)
cp -r .claude/                    <你的项目>/

# 4. ruff 配置 —— 不要整份复制,把 pyproject.ruff.toml 的内容合并进已有的 pyproject.toml
# 5. pre-commit(可选)
cp .pre-commit-config.yaml        <你的项目>/

echo ".check-hits.log" >> <你的项目>/.gitignore
```

装完自测一下:

```bash
python selftest/run.py
# OK — 6 条规则全部触发,邻近反例零误报,豁免机制正常
```

---

## 各文件干什么

| 文件 | 作用 | 常驻上下文成本 |
|---|---|---|
| `AGENTS.md` | 唯一规则正文,70 行 | **每轮都付费** |
| `CLAUDE.md` | 一行 `@AGENTS.md` | ~0 |
| `scripts/check.py` | 6 条 AST 检查,全是警告 | 0 |
| `pyproject.ruff.toml` | ruff 配置片段,替代手写风格指南 | 0 |
| `.claude/settings.json` | PostToolUse + Stop hook | 0 |
| `.claude/skills/code-simplifier/` | 语义级审查,description 写触发条件 | 0(只有一行 description) |
| `.pre-commit-config.yaml` | 兜底 | 0 |
| `selftest/` | check.py 自己的回归测试 | 0 |
| `experiments/falsification.md` | **先跑这个** | — |

**只有 AGENTS.md 那 70 行是每轮对话都要付的钱。** 这是整个设计的核心约束 ——
凡是能被脚本查、能被 skill 承载的,都不许写进 AGENTS.md。

---

## 跑法

```bash
python scripts/check.py                # 查 git 改动过的文件(默认)
python scripts/check.py --all          # 全仓库
python scripts/check.py --advise       # 附带「要不要跑 code-simplifier」的判断
python scripts/check.py --report       # 月度复盘:哪条规则该删、哪条该收窄
python scripts/check.py --strict       # 有未豁免命中就 exit 1(CI 用,默认关)
```

误报了就在那行加豁免,**理由是必须写的**:

```python
if "ping" in text:   # check: ignore[keyword-match] 协议探活,不是猜用户意图
```

整个文件豁免用 `# check: ignore-file[规则名] 理由`。

`--report` 看的就是这些豁免。某条规则十天里全靠豁免活着 → 规则错了,删掉它。
规则应该是长出来的,不是一次设计出来的。

---

## 6 条检查

| 规则 | 抓什么 | 只对生产代码 |
|---|---|---|
| `keyword-match` | 对自由文本做关键词/正则判断猜意图 | 否 |
| `fake-success` | except 里返回假数据、空值、占位 DTO | 是 |
| `prod-mock` | 生产代码里的 mock / fake / stub | 是 |
| `version-suffix` | `_v2` / `_legacy` / `_deprecated` 函数名 | 否 |
| `dup-func` | 结构几乎相同的两个函数(比对 AST,改名骗不过) | 否 |
| `file-too-long` | 超过 800 行 | 否 |

只查 `.py`。`tests/`、`conftest.py`、`fixtures/`、`repro_*.py`、`debug_*.py` 一律不算生产代码 ——
精确回归测试正需要硬编码具体输入。

### 删掉过两条规则(2026-07-31,实测后)

在两个真实的生产仓库(共 611 个 py 文件,记为仓库 A / 仓库 B)上实跑,
按误报率砍了两条。**这是这套东西唯一的正当留存理由:量过,不是拍的。**

| 规则 | 结局 | 实测证据 |
|---|---|---|
| `hardcoded-id` | **删除** | 前缀表里 `run_`/`user_`/`task_`/`org_` 是常见英文词缀,结果它在报普通字段名(`"user_confirmed"`、`"run_capability"`)。收窄到只认 UUID / 长 hex 之后,611 个文件里只剩 1 条,还是个 git SHA —— 47 个误报或 1 个无用命中,都不值得留 |
| `prompt-case-patch` | **删除** | 分不清**选项标签**(`关联到"启动SEO全局检测"这几个选项`)、**few-shot 示例**(`例如:"女装"、"iphone壳"`)、**输出模板**(`格式为:"好的,AI帮你定制了…"`)和真正的逐条堆砌 —— 这四者的区别是语义的。而且它本身就是「对自然语言做正则匹配猜意图」,正是 AGENTS.md 禁止的那件事 |

两条规则删掉,但**对应的 AGENTS.md 禁令保留** —— 原则是对的,只是静态脚本判不了,
交给 code-simplifier skill 的清单 + diff review。

### 修好的三处

| 修复 | 效果 |
|---|---|
| `keyword-match` 的字面量必须像自然语言(不是字段名/枚举值) | 113 → 15,砍 86%。`if "text" in cur_content_tmp` 是查 dict 键,不是搜关键词 |
| `keyword-match` 的正则模式必须是字面量且含实际词汇 | 46 → 17。`re.findall(r"\{[^{}]*\}", response)` 是抽 JSON,不是猜意图 |
| `version-suffix` 去掉 `copy\|backup\|orig\|tmp\|temp` | 12 → 10。那几个是普通英文词,把 `_json_safe_copy` 也报了 |

总量:仓库 A 155 → **83**,仓库 B 870 → **167**。

---

## 已知的洞(**这些是设计结论,不是待办**)

1. **静态检查抓不到「只修一个 case」。**
   codex 实测过:真正的单 case 补丁(`if "退款" in query`,1 个分支)零报警,
   而合法的 13 分支协议解析器被 `C901` 拦下。该抓的抓不到,不该抓的抓了。
   `SIM116` 更糟 —— 它建议把 if 链改成字典,而字典正是特例最好的藏身处,所以已经进了 `ignore`。

   **真正防这件事的是 AGENTS.md「验证」段那条:变体 + 邻近反例。**
   `check.py` 全绿不是「修了根因」的证据,只是「没留下明显痕迹」。

2. **迁到 prompt 不算逃逸。**
   判据不是「代码 vs prompt」,是**这个机制会不会泛化**:`if "退款" in query` 换个说法就漏;
   prompt 里写「用户想退款就调 refund」由 LLM 执行,变体都覆盖得到 —— 那是**修好了**,不是藏起来。
   向量 / 语义召回同理。
   只有**逐条粘用户原话**是同一个病换了载体,而那个已经证明静态判不了(见上表)。
   prompt 改动靠 code-simplifier 清单 + diff review。

3. **`keyword-match` 的召回缺口:** `if "refund" in query` 这种纯英文单词抓不到 ——
   它和 `if "text" in some_dict` 静态上完全一样。中文和带空格的短语能抓到。
   参数叫 `q` / `s` / `inp` 时也抓不到 —— 把这些短名字加进词表会误伤队列变量和状态字符串,不划算。

4. **`dup-func` 只在本次检查的文件之间比对。** 跨全仓库要 `--all`,函数超过 600 个会跳过。

5. **code-simplifier 是同一个 agent 在同一上下文里自查。**
   它已经相信自己找到根因时,清单不会提供外部视角 —— 会自然地把新增分支解释成
   「真实的不同情况」。所以 skill 里把**经验验证(跑变体和反例)放在了清单前面**,
   清单只是补充。真正的外部视角需要干净上下文的审查者,短任务里这一层是缺的。

6. **pre-commit + CI 不是保险。** `--no-verify` 能跳过;而且它只拦得住检查器认识的模式,
   证明不了模型没改坏测试、没伪造验证结论。

---

## 需要你确认的三件事

1. **`required-version`** —— `pyproject.ruff.toml` 里我填的是 `0.12.0` 占位。
   跑 `ruff --version` 换成你实际装的版本。**必须固定**,ruff 规则行为会随版本漂移。

2. **代码注释和 commit message 用什么语言** —— 设计文档里这一条一直是 TODO。
   我在 `AGENTS.md` 里先写了「代码注释、commit message 用英文,文档和对话用中文」。
   不同意就改那一行。

3. **Stop hook 会拦一次。** 有警告或触发 code-simplifier 条件时,
   Stop hook 返回 `decision: block`,模型会多跑一轮来处理。
   靠 `stop_hook_active` 保证只拦一次,不会死循环。
   嫌烦就设 `CHECK_STOP_BLOCK=0`,或者直接删掉 `.claude/settings.json` 里的 Stop 段。

另外 **Codex CLI 的 hook 能力我没核实过**。就算它没有,`check.py` 是纯 Python,
在 Codex 那边靠 `AGENTS.md` 里那句「改完跑 `python scripts/check.py --diff`」
加 pre-commit 兜底,架构不受影响。

---

## 下一步

**别急着往项目里铺。先跑 `experiments/falsification.md`。**

那份实验直接判定这套设计的核心假设成不成立:
拿一个真实发生过的单 case 故障,造 5-8 个变体和 3-5 个邻近反例,
让 AI 在新规则下修一遍 —— **如果三层全绿而只有原始输入能过,核心代理指标就被证伪了**。

写这个实验的成本比把规则铺进三个项目再发现没用低得多。

---

## 一条不适用于这个目录的建议

设计文档第九节:**不要抄别人的 CLAUDE.md。**

抄来的规则你不会去检查它有没有被执行 —— 而写了不检查的规则,
会让模型学到「这个文件里的规则可以不遵守」,连带削弱整份文件的权重。

这里每条规则都对应你自己反馈过的一个坑,而且都配了检查手段。
但如果跑一个月发现某条从来没命中过(`--report` 会告诉你),就删掉它。
留着不检查的规则,比没有规则更糟。
