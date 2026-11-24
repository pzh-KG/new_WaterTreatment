# WaterTreatment 新系统

面向所有材料类别，从正文与补充材料中建立“最终双功能材料—实验—污染物/氧化剂—性能”的可追溯记录。领域规则依据前期 v0.5 校准与案例学习；在0.1底座上增加主动阅读与修订流程。

代码：`D:\new_WaterTreatment`；原件快照、解析结果及运行记录：`D:\new_WaterTreatment-data`。旧项目和原始文献目录不作修改。前期 `outputs` 研究记录继续保留。

## 当前可运行的范围

- 单篇目录导入、SHA256 原件快照、SQLite 来源及运行目录。
- PDF 全物理页文字读取与可选整页渲染；原生 DOCX 段落/表格顺序、公式 XML、修订及逐次图片裁剪定位。
- 六核心对象、J/P 双轴、字段状态、版本化多轴分类、来源与关系一致性校验。
- 无模型调用的计划准备；显式开启的 Codex 结构化候选抽取与独立模型复核，各阶段保留输入、事件、失败及用量。
- 离线图谱 JSON/Cypher 导出；小规模人工金标准评测与开发/验证历史限制。
- 独立 Neo4j 实例的显式连接配置、只读连接与实际数据库名称检查。
- 已在 GPT-6.1 Sol MAX 上验证完整解析文本与图片输入和合成调用链；通过校验的注册候选可显式加载为 Neo4j 候选快照。
- 连续阅读会话、全文理解档案、按来源/块回查、PDF局部高分辨率重绘、Word图片实例放大、独立会话审查和有预算的自动修订。

已完成C01与V10两篇开发文献的真实读取、回查、自动修订和再次审查，尚未完成独立科学准确率验收；没有运行393篇批处理、训练推荐模型或完成降维/设计规则发现。新Neo4j实例已经建立并通过实际连接检查。2026-11-12网页联调已将这两篇已有开发记录发布为候选图谱快照，仍不代表人工科学验证。测试通过仅说明所测工程行为符合预期。

2026-11-11 主动阅读联调：240项离线回归通过，两篇最终均为candidate_audited_with_limitations，机器错误0、最终审查要求修订项0，human_validated仍为false。实测修复了Word透明图遮住黑色坐标/图例的问题，并以新预览重新审查、补回SI条件。原文/旧版本/失败保留，原始四份文件及快照哈希未改变。详见 [本轮实施与试跑报告](D:/new_WaterTreatment-data/exports/active-reading-integration-20261111T093724/实施与试跑报告.txt)。

2026-11-11 基础版本验收：162 项离线测试通过，并用 C01/V10 的3份PDF与1份Word完成实际导入通道检查。结果和边界见 [交付记录](docs/implementation-0.1.txt)。

## 本地网页工作台

在工程目录双击 `scripts\start-web.cmd`，或在 PyCharm 终端运行：

```powershell
.\.venv\Scripts\python.exe -m watertreatment --config config/project.local.json web --host 127.0.0.1 --port 8765
```

随后用浏览器打开 [本地工作台](http://127.0.0.1:8765)。实际配置文件是 `config/project.local.json`；命令使用项目的 `watertreatment` 模块入口。启动只打开本机服务，扫描、抽取、回查、入库和问答由页面中的明确操作发起；已排队任务的继续方式见使用说明。新调用使用 `gpt-6.2-sol / max`。关闭网页不会终止后台服务，停止服务请回到启动终端按 Ctrl+C。

工作台有总览、文献库、抽取任务、人工抽查、数据与入库、文献问答六个页面。原文和 Word 补充材料保留原文件名，网页按文件名与有限内容抽样识别正文和 SI，仅有歧义的附件需要确认，不要求每篇手写角色映射。点击“扫描文献目录”选择已配置根目录，再多选文献子文件夹；支持序号范围搜索、未登记筛选和全选当前筛选，默认零选，仅提交选中的路径。打开目录列表不读取文献正文，扫描登记不等于模型抽取或发布到Neo4j。先扫描、处理少量已有开发文献，不默认启动393篇抽取。

人工抽查只登记实际选择的文献、结果和字段范围；保存草稿、模型同意或单项确认都不会把整篇标成“人工已验证”。事实修正形成新的 JSON 版本，通过检查后再同步新 `watertreatment` 数据库。未经人工抽查的文献只要自动流程完成且符合准入规则，也可以入库。问答保留所选记录版本和证据出处，不能把缺失条件视为相同条件或拼接独立最高性能。

完整启动方式、六页操作、抽查步骤和初版边界见 [网页工作台使用说明](docs/web-workbench-guide.txt)。本节是功能使用说明，不代表已完成393篇抽取或科学准确率验收；前文带日期的测试数是各阶段历史记录。

2026-11-12 增加规则0.6的图表定量覆盖与字段级证据审查：区分已读页面和已抽取曲线/循环数据点，估读值与作者明示值分开，保留逐点坐标和缺口；旧JSON与冻结审查保持兼容。当时本机配置启用0.6，修订生成新版本，旧图谱不会因代码更新自动重写。详见 [定量覆盖说明](docs/quantitative-coverage-0.6.txt)。

2026-11-15 新任务默认规则0.8：在完整理解正文与SI的基础上，实际抽取采用“A核心必抽、B场景触发、C来源索引按需补抽”，同时约束理解、结果生成、原文审查和修订。逐次循环、相关条件结果、必要对照、反证与设计比较继续保留；常规密集点、完整峰表/产物网络/毒性矩阵不再默认生成独立观测。模型保持 Sol MAX。旧0.5—0.7候选及审查不会自动升级；224的310条旧候选另有独立研究视图，完整JSON保留。详见 [0.8抽取范围与使用说明](docs/research-design-0.8.txt)；历史 [0.7规则说明](docs/research-core-0.7.txt) 仍保留。这些工程措施不代表已测得新规则的科学准确率或生产抽取耗时。

局部修订支持 `bounded-scientific-revision-v1`：绑定已完成的原完整审查，按实际字段变化计算直接受影响的实验、比较、系列和共享参考关系；逐项核对本轮原文交付与审查处置。后续局部修订累计保留之前的未解决问题，不把局部审查当新的全篇基线。数值、单位、归属或阅读覆盖发生越界变化时，不能沿用这个范围证明。修订使用独立会话及按需来源导航，完整数据与原文始终保留。

失败轮次的最后完整暂存稿可显式通过 `prepare <sources.json> --cohort-role development --reuse-staged-candidate <submitted_record.json> --reuse-dossier <reading_dossier.json> --continuation-repair shared_references --continuation-repair missingness` 准备新的续审运行，`sources.json` 须来自原导入运行。程序核实失败轮次、补丁链、原文哈希和原 Sol MAX 审查；暂存稿不会被伪装成已完成版本。实际模型执行仍需单独 `extract <plan.json> --live`，不会自动写入Neo4j。

若上述局部修补已实际完成、但第一轮原文审查尚未启动，可用 `--reuse-completed-continuation-candidate <candidate.v00.json>` 接续，省略 `--continuation-repair`。入口另行核实完成轮次、最后回执、所有补丁及继承来源，不重复修补，也不跳过已经产生的审查意见。新范围预算分别约束待处置实体与仅供查询的上下文实体，新增引用不会把未改动的参考数值重新传播到其他比较；旧范围按其原有预算重放。若只是审查报告漏列检查项，原预算内最多补全一次报告，不改写候选来掩盖缺项。

已完成的审查若仅剩少量明确字段的来源处置缺项，可使用 `prepare-audit-addendum <parent_run>` 冻结补审，再用 `complete-audit-addendum <plan.json> --live` 执行。补审保持 Sol MAX，最多一次调用，预算从父任务剩余额度内扣除；只读取这些字段和所需原文，不能修订候选数据。新发现以追加方式合并，保留父报告、原完整审查和所有限制，并重新检查原范围的全部义务。任何新的科学问题仍阻止通过。补充报告与合并报告分别保存，合并结果不会冒充某次模型直接输出；不适用的报告缺项不能经此入口绕过审查。

跨组共享对照通过 `reference_associations` 引用既有结果，不复制观测或改变主归属；推断的适用性和未解决条件必须保留。缺失状态提示只要求回查，不自动把空值变成“原文未报告”，也不把制备投料量当作最终保留量。工具支持显式共享字面值和分组路径补丁以减少重复传输，展开后的完整记录、原请求与哈希均保存。

## 环境与检查

已在本机创建 `.venv`（Python 3.11），不使用旧工程环境。重新安装可执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
Copy-Item config/project.example.json config/project.local.json
.\.venv\Scripts\python.exe -m watertreatment init
.\.venv\Scripts\python.exe -m watertreatment doctor
.\.venv\Scripts\python.exe scripts/quality/run_offline_regression.py -q
```

已有 `project.local.json` 时保留并按需修改，不覆盖现有配置。该文件不进入Git；程序不读取旧 `.env` 或复制登录凭证。`requirements.lock.txt` 固定本轮验证依赖版本；改变依赖后重新记录运行版本并测试。

按可选依赖安装时使用 `pip install -e ".[dev,vision,graph,web]"`。`graph` 使用官方 Neo4j Python driver；显式连接检查、图谱加载以及选择图谱范围的问答会连接新数据库。

本机新调用固定为 `gpt-6.2-sol` / `max`，旧调用按各自冻结的模型与原始回执认证，通过官方Codex CLI的现有ChatGPT登录调用。项目不复制认证文件，不自动更换模型或降低推理强度。研究目标及边界见 [目标与约束](docs/research-goals-and-constraints.txt)。

离线测试禁止网络连接及真实 Codex/Neo4j 子进程。依赖缺失会明确失败，不跳过相关测试。

## 操作流程

```powershell
# 仅查看文件夹和文件名，不阅读科研内容
.\.venv\Scripts\python.exe -m watertreatment discover --limit 10

# 导入一个文件夹；--render 生成 PDF 页图和支持的 Word 图片实例预览
.\.venv\Scripts\python.exe -m watertreatment import "D:\文献根目录\序号_热催_期刊" --paper-id paper-example --roles "D:\roles.json" --render

# 从导入结果中的 sources.json 准备计划；开发数据使用 development
.\.venv\Scripts\python.exe -m watertreatment prepare "D:\new_WaterTreatment-data\runs\导入运行ID\sources.json" --cohort-role development

# 显式调用模型。模型及设置必须与计划一致。
.\.venv\Scripts\python.exe -m watertreatment extract "D:\new_WaterTreatment-data\runs\抽取运行ID\plan.json" --live

# 结构、来源和科学一致性检查，仍不能替代原文验收
.\.venv\Scripts\python.exe -m watertreatment validate "D:\候选记录.json" --bundle "D:\来源包\sources.json"

# 仅导出本地投影，不连接数据库
.\.venv\Scripts\python.exe -m watertreatment project "D:\候选记录.json" --bundle "D:\来源包\sources.json"
.\.venv\Scripts\python.exe -m watertreatment runs

# 仅检查新实例的登录与实际库名；不写图数据
.\.venv\Scripts\python.exe -m watertreatment neo4j-check --live

# 一次合成文字/图片/JSON调用测试，不产生科研记录
.\.venv\Scripts\python.exe -m watertreatment codex-check --live

# 加载通过校验的注册候选；仍标为candidate_unverified
.\.venv\Scripts\python.exe -m watertreatment graph-load "D:\new_WaterTreatment-data\runs\抽取运行ID\candidate.json" --live
```

上述示例中的文献路径、运行ID需替换为实际值。模型和MAX强度由项目配置固定；修改后需要重新准备计划。`graph-load --rollback`用于验证数据事务并回滚，不保留图数据。

`roles.json` 是文件名到 `main_article` / `supporting_information` 的映射。未提供时保留 `unknown`，不凭文件名确认附件身份。`--families` 可提供文件名到同源文件族ID的映射，用于明确关联原件与渲染副本；内容不同的文件不会自动算作相互独立的证据。

本机默认启用主动阅读：全文理解、候选抽取、新会话审查，再按问题进行最多两轮“修订+再审查”。每次单篇运行最多7个模型任务轮次、160次文献工具调用，单轮最多1800秒、单次运行总计最多7200秒。这些是上限，不是要求模型必须用满的思考量；不是推理token数。达到预算或失败时保留已有版本和未完成状态，不自动降模型/强度，不自动写图谱。开发文献可核验来源后通过--reuse-dossier和--reuse-candidate从已完成阶段继续；修订后仍需新审查。显式准备续作计划会使用新的运行预算，旧计划和历史消耗不会被覆盖。

模型通过注册工具提交包含全部实质事实的精简记录，由程序补齐schema默认结构并验证，减少反复生成空字段的负担。默认缺失事实保持unread；具体not_reported、冲突、数值、单位和证据仍由模型依据原文明确表达。每次提交、反馈及修订版本都保留。

一份计划只能执行一次。修改源文件、提示、代码、依赖或模型配置后应准备新计划，旧输出和失败记录保留。旧的两阶段CLI适配器仍用于合成连接检查和显式关闭主动阅读的兼容流程。

完整解析文本、证据编号和分类词典嵌入冻结上下文，所有可用页图/Word图片实例按位置索引随首次理解与每次完整审查附送；0.7引用局部复核按冻结依赖范围主动查看相关原文与图像。模型可通过注册的wt_工具继续取证；不依赖模型执行文件读取命令。主动阅读使用app-server标准输入传递JSON和图片路径，避免长图片命令行参数。图片或文本不可用时保留限制，不假称已阅读全文；超长文献、扫描件/OCR仍需专门验证。详见 [主动阅读说明](docs/active-reading.txt)。

## 数据与职责

```text
src/watertreatment/
  settings.py       新旧目录隔离及显式配置
  storage.py        不覆盖产物、原件快照、来源/运行/数据集历史
  sources.py        PDF/DOCX来源表示与定位
  rendering.py      页图和Word图片实例预览
  domain.py         科学记录结构与字段状态
  taxonomy.py       多轴分类和文内缩写消歧
  codex.py          显式、有预算的Codex调用及审计
  app_server.py     连续会话、原生工具调用、流式事件与严格模型设置
  reading_tools.py  来源限定回查、PDF图表放大、Word图片实例与替代文本解析
  reading.py        全文理解档案及可追溯审查结构
  reader_workflow.py 理解、抽取、审查、修订闭环与少量人工抽查包
  review_packet.py  最终体系优先、来源文件映射与最多6项结果抽查
  model_check.py    合成文字/图像/结构化输出连接检查
  workflow.py       导入、计划、候选、模型复核
  validation.py     引用和科学关系一致性检查
  projection.py     轻量图谱投影
  evaluation.py     有限人工验收及独立性判定
  neo4j_connection.py 显式、只读的数据库连接检查
  graph_store.py    参数化、事务化的候选图谱加载
  cli.py            命令入口
config/              分类词典、配置示例
prompts/             版本化抽取与复核提示
tests/               无外部服务的合成与集成回归
```

```text
D:\new_WaterTreatment-data\
  store.json
  catalog.sqlite3
  originals/         按内容哈希保存的原件快照
  runs/<运行ID>/      原始输入、sources、页图、计划、候选、复核和失败
  records/           后续经明确接纳的记录预留位置
  exports/           本地图谱等派生产物
  neo4j/             独立新实例的数据、事务日志、运行日志和进程记录
  secrets/           本机受文件权限保护的连接密码，不进入代码仓库
```

源文件解析不自动推进到已阅读。模型结果不自动推进到科学已验证或已发布图谱；离线投影会标明来源检查范围。Neo4j只承载检索需要的对象和关系，详细字段事实和文档布局仍保存在记录层。

本机新实例使用 `bolt://127.0.0.1:7688` / `watertreatment`，浏览器入口为 `http://127.0.0.1:7475/`。启动、停止、状态检查及密码位置见 [Neo4j实例说明](docs/neo4j-setup.txt)。连接检查要求 `--live`，普通 `doctor` 不联网。用户已将机器级 `NEO4J_HOME` 改为新实例；已有控制台可能继承旧值，需要重新打开。工程本身使用明确的URI和数据库名，不依赖该环境变量。

Neo4j接入后的完整离线回归为188项通过；另外完成了真实连接、业务库身份、正常停止及重启检查。旧实例继续运行，旧配置哈希与数据文件元数据对照未发现变化。

后续Sol MAX接入已完成真实合成读取检查和新库事务写入/回滚验证。具体配置、保留的初次读取失败和修复方式见 [接入说明](docs/codex-neo4j-integration.txt)。上述实例创建阶段的测试数与运行状态为当时记录，最新验证以接入审计目录为准。

## 目前的读取边界

PDF文本提取可能遇到扫描页、异常字体、复杂表格；页图已具备视觉回查入口，OCR和复杂表格解析还需接入并验证。原生Word无可靠物理页码，使用XML部件、段落、表格及图片实例定位。Word图片预览处理已支持的DrawingML裁剪，不是完整页面排版；VML裁剪、旋转/形状变换等未支持情况保留限制。旧 `.doc`、不可读公式和未知文档格式明确登记，不假称已读。

当前核心结构是可扩展骨架，不能保证已覆盖393篇的全部专业指标。扩展字段使用有版本的模块；应继续补充相应的来源、校验与评测，避免把关键科学语义塞进无法检查的自由文本。

## 下一阶段验收

本轮已完成C01/V10开发试跑；少量人工抽查包各提供最多6项结果及来源定位。后续需固定模型、规则和版本，在未用于修订的文献上开展小规模人工验收。C01—C04与原V01—V10已是开发数据；不得把修订后答对当独立验证。详细接口见 `docs/evaluation.txt`，设计边界见 `docs/architecture.txt`。

