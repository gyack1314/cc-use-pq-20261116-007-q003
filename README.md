# 具身数据采购组合分析台

研究团队可用本项目比较候选具身数据包对客户任务的分层覆盖、重复样本、采购金额和授权边界。运行仅依赖 Python 3.11 标准库与本地 SQLite；不会访问机器人、外部报价接口或在线数据源。

先用命令 python -m embodied_portfolio demo --workspace fixtures 执行合成数据演示。演示导入两份候选包与一份采购需求，找出符合失败样本覆盖且预算可承受的组合，审批分析后验证冻结快照。演示中的供应商、价格与样本均为合成值。

`python -m embodied_portfolio demo-transfer --workspace fixtures_transfer` 演示便携失败样本的跨机型迁移：只有带 validated 迁移证据的样本进入必需覆盖，pending 证据只作为候选供给单列，报价仍按整包实际购买金额计算。

自有数据可用 python -m embodied_portfolio --db plan.sqlite3 import requirement requirement.json、import offer offer.json 和 import evidence evidence.json 登记。再执行 plan REQUIREMENT_KEY 查看可行组合、缺口和包级排除原因。approve ANALYSIS_ID INPUT_DIGEST 只批准当前需求、报价与证据版本下的可行分析；目录发生修订后，应重新分析。verify ANALYSIS_ID 校验冻结输入与报告摘要；analysis、history、versions 可查看旧版本。gaps、marginal、frontier 支持覆盖缺口和边际价值查看。

候选包声明采集路径、供应商、机型、场景、动作阶段、结果类别、样本数量及可选的逐条样本标识。已给出的标识用于精确去重；没有标识的数量不会被猜测为与另一包相同。报价支持逐条、逐小时与包价，金额按给出的数量计算到分。许可可对客户有限制，限制外的报价不可进入组合。需求声明预算、可用采集路径、各分层最低数量及是否必需。系统不会把失败或人工干预归入成功样本。

### 跨机型迁移证据

需求默认 `transfer_policy: "exact"`，此时机型必须与包内标记完全一致，迁移证据一律不参与计算。需要接纳迁移证据时设为 `"validated"`：只有源机型、目标机型与需求机型逐字一致，且场景、阶段、结果均匹配的证据才能生效；`source_robot` 还必须出现在该包的 `robots` 声明中。

迁移证据是带版本的独立文档（`import evidence evidence.json`，同样可用 `batch` 原子导入），字段包括：

- `key`：证据标识，修订即新增版本，不覆盖旧版本。
- `package`：证据所属候选包。
- `source_robot` / `target_robot`：采集机型与目标机型，必须不同。
- `scopes`：已声明覆盖的场景/阶段/结果列表。
- `status`：`validated`（已确认）、`pending`（待确认）或 `retracted`（已撤回）。也接受 verified/active、provisional/unverified、withdrawn/revoked 等写法并归一化。
- `validity`、`reference`、`notes`：可选来源说明。

覆盖计算分两栏：`validated` 证据计入**已验证覆盖**，`pending` 证据只计入**候选供给**，`retracted` 证据完全忽略。分析报告的 `coverage`/`verified_coverage` 给出已验证的逐层 `credited`/`shortage`，`candidate.coverage` 与各单元的 `candidate_credited`/`candidate_shortage` 给出仅靠候选供给可补足的部分；`evidence_coverage` 按需求分层列出每条证据贡献多少样本。组合选择只在已验证覆盖满足全部必需分层时才判为可行，仅靠 pending 证据达标的组合会以 `candidate` 单列（`reason: mandatory_targets_require_unverified_evidence`），无法批准。报价始终按包的真实购买量（逐条/逐小时/包价）计算，不因部分样本是否可迁移而折价。

分析结果包含所选包、成本、逐层覆盖、重复证据、候选过滤原因和替代组合。每次分析绑定明确的需求、包版本和证据版本；审批后的报告不因日后报价或证据变化而改写。输入清单使用 UTF-8 JSON，错误会返回具体代码。

可用 `batch delivery.json` 原子导入一批文档，文件顶层为 `documents` 数组，每项含 `kind`（offer、requirement 或 evidence）和 `body`；整批先通过验证再提交，重复内容不会产生新修订。`compare-versions evidence EVIDENCE_KEY 1 2` 比较两次证据的具体变化（如状态从 pending 变为 validated）。

分析前可用 `readiness REQUIREMENT` 和 `brief REQUIREMENT` 查看路径分布、许可限制、分层供给、已知重复、证据清单与逐包价格；每条证据列出修订、摘要、状态、适用机型和是否可用。`budgets REQUIREMENT 140 150 180`、`targets REQUIREMENT SCENARIO|STAGE|OUTCOME|ROBOT 3 4 5`、`withdrawal REQUIREMENT PACKAGE`、`evidence-withdrawal REQUIREMENT EVIDENCE_KEY`、`price-scenario REQUIREMENT PACKAGE PRICE` 只做临时情景推演，不修订目录。`compare-packages REQUIREMENT PACKAGE_A PACKAGE_A,PACKAGE_B` 对照人工指定方案的覆盖、成本、许可和供应商支出，并区分可行与仅候选的组合。

`audit ANALYSIS_ID` 使用分析时绑定的输入版本重新计算历史选择，检查来源摘要和报告摘要；`compare EARLIER_ID LATER_ID` 展示同一需求两次分析的包版本、证据版本、选择、成本和覆盖变化。后续目录或证据修订不会让旧的有效快照失真，但会使旧草稿不能直接批准：`stale-drafts` 列出输入已变化、需要重算的未批准方案，`replan ANALYSIS_ID` 用当前目录重算并生成新分析（保留旧分析），已批准方案不可重算。`audit` 的 `evidence_state_current` 指出该分析的证据是否已被后续修订或撤回，同时仍按当时依据复核历史结论。

业务测试：python -m unittest discover -s tests -v。

语法检查：python -m compileall -q embodied_portfolio。

运行验收：python -m embodied_portfolio demo --workspace fixtures 与 python -m embodied_portfolio demo-transfer --workspace fixtures_transfer。
