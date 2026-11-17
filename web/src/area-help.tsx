import { useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
type Help = { purpose: string; steps: string; example: string; note?: string };
const topics: Record<string, Help> = {
  'market-onboarding': {
    purpose: '按你要的市场选一项适合的行情来源。',
    steps: '先选择数据类型，再选或保存一个来源。查看来源说明，填写证券和日期，最后确认下载或预览文件。',
    example: '下载美股 ETF 时选美国 ETF 和 Yahoo，再输入 SPY。沪深行情用人民币，不能当作美元行情回测。',
  },
  'first-example': {
    purpose: '用真实行情完整体验一次下载、回测与查看报告。',
    steps: '读清示例的标的、日期、虚拟本金和费用，然后确认开始。可以先离开，回来后接着查看进度。',
    example: '示例下载 AAPL，再模拟买入持有。完成后点“查看结果”。',
  },
  migration: {
    purpose: '从原电脑导出选定的行情、策略版本和报告，在新电脑登录后迁入。',
    steps: '先在原电脑导出文件，再到新电脑预览。查看哪些项目可导入、重复或冲突后，点确认迁入。',
    example: '迁入一份保存的回测报告，从迁入记录打开历史报告；新电脑需要重新配置密钥并连接 AI。',
  },
  'ai-research-choices': { purpose: '选择适合你的研究方式，也可以管理研究需要的模型和团队。', steps: '如果只有一个问题，可用“按目标研究”。已有数据并想自己设置时，选“自己选研究方式”。需要让外部客户端使用工作台时，选“连接外部 AI”。', example: '已有 AAPL 日线时，可自己选研究方式，用已保存的数据检查过去的价格走势。' },
  'goal-research': { purpose: '从一个目标开始准备数据并启动研究。', steps: '写清市场、想比较的对象和时间。使用已保存的设置直接开始，也可以展开研究设置。遇到缺项时按提示配置，再继续同一任务。', example: '比较 AAPL 和 MSFT 在 2020 至 2024 年的月度动量。', note: '可自动选择哪些市场和数据，以当前来源和工具能处理的范围为准。' },
  'goal-research-progress': { purpose: '查看任务从理解目标到保存结果的记录。', steps: '查看实际找到或补齐的数据版本、已用额度和相关研究。需要暂停、取消或继续时，使用当前状态允许的操作。', example: '部分日期缺失时先查看缺口；补齐后打开本次使用的数据和相关报告。', note: '追加数据形成新版本，原研究继续引用原数据。' },
  'storage-locations':{purpose:'分别设置三类内容以后保存在哪里。',steps:'查看当前有效目录，先创建文件夹，再粘贴新的Windows绝对目录（可含中文和空格），检查后保存；需要回到应用默认位置时，点击恢复默认。',example:'行情与数据集放在D盘，结果报告和模型文件分别放在E盘的不同目录。',note:'从新提交的任务开始生效；已排队或正在运行的任务继续使用原位置，旧内容仍可打开。不会自动移动或删除旧文件；目录不可用时会提示，不会悄悄另存。'},
  'storage-market-data':{purpose:'设置导入或下载的行情与数据集保存位置。',steps:'先创建文件夹，再粘贴Windows绝对目录路径，检查后保存。这里只设置以后保存的内容，不会重新下载或搬动以前的数据。',example:'D:\\FinanceAnalyze\\MarketData。',note:'当前有效目录以服务读取的结果为准；示例不是你的实际设置。'},
  'storage-reports':{purpose:'设置回测、数据研究、AI与策略试验报告的保存位置。',steps:'先创建报告文件夹，填写目录，检查后保存。以前保存的报告仍按原来的位置读取。',example:'E:\\FinanceAnalyze\\Reports。',note:'恢复默认只用于后续新内容，不会把旧报告搬回来。'},
  'storage-artifacts':{purpose:'设置训练模型文件、策略或因子代码和实验归档的位置；不搬动AI厂商密钥或模型卡配置。',steps:'先创建独立文件夹，填写目录，检查后保存；已开始的任务继续使用原来确定的位置。',example:'E:\\FinanceAnalyze\\Research。',note:'只影响应用管理的内容；不会整理、移动或删除你其他目录里的文件。'},

  'model-usage':{purpose:'查看模型实际返回的Token用量，以及最近一次调用的平均输出速度。',steps:'先看任务旁的小卡，再按模型和角色查看详情。未提供表示没有确认的数据；已确认的小计可能还没有包含未返回用量的调用。',example:'单次速度是确认的输出Token除以整次请求耗时；已记录平均速度用配对记录的合计输出除以合计请求耗时，都包含等待，不是逐字生成速度。',note:'Token 是模型拆分文字后计量用量的单位，一个 Token 可能是一部分或几个字符。缓存输入和推理输出是其中的子项，不再加入累计数。配置输入上限不是实际用量，也不是厂商完整上下文窗口；窗口未核实时显示未提供。'},
  'saved-candidate-backtest':{purpose:'固定一份已保存候选，再评价一次。',steps:'在候选库点再次回测，选择固定数据，日期留空用全部记录或明确填写开始结束日期，再填写总资金与成本、评分和计算上限，明确开始；到结果页读取实际记录。',example:'对同一份策略代码换一份固定数据评价一次，零模型请求、不需AI密钥。若想让AI改代码，另外创建新研究会话。',note:'固定回测与独立最终数据检查是不同流程。此入口不自动生成新代码，也不把受理任务当成完成。'},
  'openbb-source':{purpose:'通过可选OpenBB/Cboe来源获取公开日线。',steps:'在新增来源选OpenBB/Cboe，先核对本机启用与依赖状态，再保存连接、选证券与日期并明确开始下载。',example:'最多16个美国股票或ETF证券，不需账号密钥；目前还不清楚行情是否调整过。',note:'来源采集与OpenBB格式导出是两项功能，分别显示实际状态；本轮未作真实供应商验收。'},
  'source-capabilities':{purpose:'先核对来源支持范围。',steps:'选择保存连接后查看能选多少只、支持哪些品种，以及价格如何处理。是否能取到数据，请看实际下载结果。',example:'AKShare 美股使用新浪前复权价，即把过去价格向当前价格标准调整。可用于支持范围内的股票，ETF 不适用。'},
  'native-resume':{purpose:'从已保存完整证券段继续下载。',steps:'刷新状态；只有服务确认原任务失败或停止且仍有预算时，用原参数续传。结果未知则确认原请求。',example:'已保存两只证券，第三只失败时复用已保存段；不会通过改日期重置累计额度。'},
  'bounded-roster':{purpose:'按交易所和证券类型保存有限筛选名单。',steps:'选择Massive连接，填写日期、交易所、普通股或ETF和数量，明确发起。任务完成后刷新范围，再勾选名单。',example:'按字母顺序取前100只普通股，不称为全市场或指数全部成分。'},
  'dated-roster':{purpose:'保存自己采用的带日期股票清单。',steps:'填写名称、名单日期、代码和来源，明确保存；任务完成后刷新范围并勾选该名单。',example:'导入一份有日期的自选股票清单；不把它称为每天的历史成分。'},
  'research-export':{purpose:'导出固定数据中的指定日期研究窗口。',steps:'在数据卡展开导出，日期可留空。选择可用格式并创建任务，成功后点击下载。',example:'从较长数据集中仅导出2024年的记录；OpenBB格式转换不等于OpenBB行情采集。'},
  'autonomous-scoring':{purpose:'选择用来比较实验结果的评分方式。',steps:'选择当前支持的评分指标、优化方向和权重。先用历史数据探索并评价因子。是否开放独立数据检查以服务实际能力为准；开放后需明确固定候选、独立检查不参与开发反馈。其他因子诊断只展示实际保存字段，不从缺少记录推断已实现。',example:'策略用扣费后收益率或最大回撤；因子可用IC。指标不足时评分应为空，不能补0。'},
  'autonomous-costs':{purpose:'固定实际回测使用的资金与费用。',steps:'填写一笔总资金、手续费和价差比例；核对后，回测会用这些设置模拟买卖。调用模型的费用另算。',example:'总资金10000USD、手续费0.001表示0.1%；不是每只股票获得10000。'},
  'autonomous-artifact':{purpose:'只读查看已保存策略、因子、评价或反馈。',steps:'打开保存的结果，系统会检查内容完整后展示。空白指标表示数据不足或暂时无法计算；代码只供查看，不会在主应用或浏览器运行。',example:'打开实际评价，查看开发净值与基准；开发验证已用于反馈，不能当作独立最终测试。'},
  'autonomous-create':{purpose:'创建可以保存、以后继续查看的研究任务。',steps:'选择保存的数据、研究目标、三个必需角色的模型和评分方式；点击核对研究设置后创建研究会话，再点击开始研究。可展开查看所选保存模型的推理强度、输入输出预算、Prompt和上下文；超过首批的保存数据、模型与团队用加载更多读取。报告角色可不选。',example:'生成新的价格因子，保留失败与样本不足记录；最终数据不进入开发循环。'},
  'autonomous-budget':{purpose:'限制长时间研究的累计消耗。',steps:'填写截止时间、实验数、模型用量、计算分钟和保存空间MB，核对后查看最多支持几次实验；重新开始不会退回已占用的额度。',example:'默认最多3次实验；如果token或空间只够1次，核对后会明确显示1次及限制来源。截止1小时不保证持续研究1小时。'},
  'autonomous-control':{purpose:'开始、暂停、继续或停止已保存的研究任务。',steps:'点击操作后要等待服务任务状态更新。同标签页刷新后保留待确认原请求；只确认原操作，不自动重新开始。关闭应用后先重读服务保存会话。中断前的调用可能已经计费，继续或重新开始不会退回已占用额度；按页面当前可用的操作处理。',example:'暂停后等待状态变成已暂停，重开页面读取同一会话，再继续；不要根据百分比猜测保存进度。'},
  'massive-check':{purpose:'检查所选证券和日期的访问情况。',steps:'保存Massive连接及本地密钥后，选择七天内日期，明确点击检查；只核对本次范围，不推断其他权限。',example:'检查AAPL两天的原始日线；空返回不能证明权限通过。'},
  'massive-resume':{purpose:'查看共享额度和保留段，按原请求续传。',steps:'任务失败或取消后刷新进度；只有服务允许时用原参数续传，保留消耗与完成段。',example:'遇429等待共享冷却结束，再按原日期和证券续传；不是重新发起全新下载来重置预算。'},
  'strategy-search': { purpose: '在设定的次数和时间内比较几种买卖方法，保留每次试验的记录。', steps: '选择一份已保存的数据，设置试验次数、时间和费用，核对计划后开始。用较早的行情试验，再用较晚的行情检查选定的方法。完成后到“我的结果”打开报告。', example: '在同一笔起始资金下比较买入持有、20 日均线和 5 日与 20 日双均线。价差设 5 个基点就是 0.05%。', note: '每只股票至少需要 80 个训练交易日和 40 个验证交易日。反复在同一批历史记录上试验，选出的策略可能只适合这批数据。' },
  'strategy-package': { purpose: '把自己编写的策略准备成可以用于回测的版本。', steps: '从模板开始或上传 ZIP，选择策略版本，核对所需数据并保存参数，再创建和检查可运行的策略。通过后，到回测页面选择它。', example: '策略需要价格阈值时，按模板填写参数，例如 {"threshold":"100"}，保存后用练习数据检查。', note: 'ZIP 上限为 1 MiB，须包含 manifest.json、strategy.py 和 dependencies.lock.json。发布须由管理员明确批准，此处仅供回测。' },
  'native-research': { purpose: '用 Qlib 模板训练模型，或让 TradingAgents 的多个角色围绕一只股票讨论。', steps: '训练时选择已保存的数据、模板和训练及检查日期。角色讨论时选择股票、日期和模型配置，再核对任务设置与额度。完成后打开实际保存的结果。', example: '用 Qlib 的 LightGBM 模板在练习数据上试训，或让 TradingAgents 的角色讨论 AAPL。', note: '外部模型调用可能收费。练习数据和离线演练的输出可用于核对流程。' },
  wrds: { purpose: '用你的 WRDS 权限查找并下载 CRSP CIZ 日线。', steps: '配置本机 WRDS 登录，选择查询日期，按股票或 ETF 代码查找，也可填 CRSP 永久证券编号（PERMNO）。核对证券和日期后再下载。', example: '输入 AAPL 和一个查询日期，确认匹配的证券后选择下载区间。', note: '实际下载取决于你的数据权限。这里保存当时的成交价格，回测不计现金分红。' },
  'market-merge': { purpose: '把同一批证券的两份行情合在一起，用于后续研究。', steps: '选择两份要合并的行情，先查看重叠日期和不同的价格或成交量，再选择采用哪份记录。需要核对全段时，另选一份覆盖全部日期的核验行情。', example: '把 1 至 6 月与 5 至 12 月的日线合并，先检查重叠的 5 至 6 月。', note: '合并会保存新版本。原数据可继续查看，缺少交易日时结果仍会显示缺口。' },
  'benchmark-download': { purpose: '为回测准备可用来比较的指数或 ETF 历史数据。', steps: '选择要下载的指数或 ETF，核对数据来源、币种和日期，完成后到回测页面选择这份数据作对照。', example: '下载跟踪纳斯达克 100 的 QQQ 日线，对比策略与买入持有 QQQ 的历史表现。' },
  source: { purpose: '保存一个可重复使用的数据来源连接。', steps: '在已有连接中选择来源；首次使用时展开“新增来源连接”，填写名称，选择 CSV、yfinance、AKShare 或 Massive，再保存。两个公开来源不需密钥，Massive 留待本机配置。', example: '把 yfinance 来源命名为“公开美股日线”，核对 AAPL 的日期后点击下载；缺失数据不会伪装成完整成功。' },
  universe: { purpose: '确定这一批数据包含哪些股票。', steps: '展开分类并多选，按需手输补充代码或排除代码，再点“核对最终范围”。查看合并后的证券清单和名单日期。', example: '勾选存储股，再输入 AAPL；需要移除某只股票时把其代码放入排除框。', note: 'Mag 7 指七家公司，其中一家公司有两种股票代码。有日期的名单只记录当时选了哪些股票，不能据此判断回测期间每天有哪些股票。其他名单也要标明来源和日期。' },
  dates: { purpose: '确定下载范围，并给固定数据版本取一个容易识别的名字。', steps: '用日历选择开始与结束日期；名称可自填，也可留空由程序生成。查看建议显示名后继续校验保存。', example: '日期选 2020-01-01 至 2025-12-31，名称填“存储股日线”。' },
  csv: { purpose: '先检查文件格式，再把原文件和校验后的数据一起保存。', steps: '选择 CSV 文件，点“预览并识别表格列”。按提示补充少量不明确字段，再次预览；选择合成练习或公开来源类型，最后点“导入并保存数据”。', example: '文件包含日期、代码、开高低收和成交量时，先预览；只有提示缺少币种时才填写 USD。', note: '小文件入口上限 1MiB；大量供应商行情使用后台下载。' },
  'csv-mapping': { purpose: '告诉程序每个数据字段对应文件里的哪一列。', steps: '只为提示缺少的字段填写 CSV 表头原文，再预览。单位、复权、币种和时区按来源说明选择。', example: '文件用 Date 表示交易日期，就在“日期对应的文件列名”填 Date；用 Symbol 表示代码时填 Symbol。' },
  datasets: { purpose: '选择和管理已保存的固定数据版本。', steps: '在“我的数据集”查看名称、日期、证券数和实际覆盖；切换数据类型可查看基准版本。要暂时停用一份数据，点“移入回收站”。', example: '重新获取同一批股票形成新版本；旧回测继续引用原版本。', note: '保存副本后，以后可以用同一批数据重做。回测那一天是否已能获取这些资料，仍需另外核对。' },
  trash: { purpose: '恢复暂时停用的数据版本。', steps: '在回收站找到需要的数据卡，点“恢复为可选数据”；回到可选数据后即可用于新任务。', example: '误移入回收站的“存储股日线”可以恢复，旧报告仍可追溯。' },
  backtest: { purpose: '用已保存的数据检查策略在过去记录中的表现。', steps: '先选数据集和策略，再填写一笔起始总资金。日期留空使用全部可用记录；按需展开成本与基准后开始回测。', example: '选择练习数据、买入持有和总资金 10000，日期留空，检查保存结果。', note: '回测表现不能保证未来收益。' },
  costs: { purpose: '把模拟交易的费用与价差纳入结果。', steps: '填写每股费用、每次最低费用和价差基点，再开始回测。费用按成交计算，盈亏表中的相关费用已计入净盈亏。', example: '每股费用 0.005、最低费用 1、价差 5 个基点；最终以保存报告的费用记录为准。' },
  'index-etf-benchmarks': {
    purpose: '比较策略与 SPY、QQQ、ASHR 的买入持有表现。',
    steps: '三个 ETF 各用一份与策略相同的全额起始本金，在同一个起始交易日开盘买入一次，然后独立持有。勾选三个颜色对应的 ETF 曲线只影响图表显示。',
    example: '策略起始本金为 10,000 美元时，三个 ETF 账户各自从 10,000 美元开始。',
    note: 'ASHR 用美国上市基金的美元价格。收益受汇率、基金费用及跟踪差异影响。共同首日缺行情时保持缺失。日期或规则不同时仅供参考。',
  },
  benchmark: { purpose: '查看策略以外的买入持有或指数定投参考。', steps: '先选已保存的指数或 ETF 基准。买入持有或“同起始资金分批定投”在资金、日期和收益算法都相同时，可画在同一张图上；选择“追加本金定投（另列参考）”时，填写追加计划，在独立参考中查看本金和净收益。', example: '选择已保存的 QQQ ETF 价格数据，每月追加 500，查看它自己的累计投入和净收益。', note: '追加本金模式的新增资金只进入基准账户；资金不同的账户总额不能直接比较。QQQ ETF 也不同于纳斯达克综合指数。' },
  'reserved-contributions': { purpose: '用同一笔起始资金安排分批买入，比较不同买入节奏。', steps: '在基准比较中选“同起始资金分批定投”，添加日期和每笔预算，或按月快捷生成。每笔预算含费用，所有预算合计不得超过起始资金；剩余资金保持现金。', example: '起始资金 10000，每月买入预算 1000，可安排最多十笔；费用包含在每笔预算内。', note: '空预算表示全现金参考。实际模拟成交、剩余现金、还没用掉的预算和收益怎么算，请查看报告。' },
  contributions: { purpose: '方便地填写只投向基准账户的追加资金计划。', steps: '可点“添加一笔定投”，用日历选日期、填写金额，也可输入起止日期和每月金额，点“生成并替换定投日期”。生成后逐笔修改或移除。', example: '起始日选 2020-01-31、截止日选 2020-03-31，每月 500，会生成 1月31日、2月29日、3月31日。', note: '追加本金单列，不计作利润；交易日处理由后台基准规则确定。' },
  calendar: { purpose: '按天或按月快速查看保存的盈亏分布。', steps: '选择月视图或年视图，再选日期范围。月视图每格是一日，年视图每格是一月；可展开日期金额明细查看准确数字。', example: '切换到某年，先找亏损月份，再切到该月查看每天盈亏。', note: '空白表示缺记录或未提供估值，不是零收益；本金流入不算利润。' },
  'security-pnl': { purpose: '查看实际成交过的股票分别赚亏多少。', steps: '默认查看净盈亏最高的前五只；点“展开全部”查看所有成交证券，或勾选“亏损最多优先”。核对已实现、未实现和相关费用。', example: '先看亏损最多的股票，再对照其交易次数和未平仓盈亏。', note: '表内相关费用已计入净盈亏，无需再扣一次。' },
  models: { purpose: '保存以后可以直接选择的模型配置。', steps: '选厂商、模型名和支持的推理强度，填写指令与预算，再保存。模型卡可编辑、复制、删除；研究任务可以选一个模型、多个模型分别报告，或明确组建固定角色团队。', example: '复制一张模型卡，只修改任务要求和名称，保留原卡用于对照。' },
  prompts: { purpose: '说明你希望模型遵循的要求和输出方式。', steps: '预设指令填写长期规则，任务要求填写这一配置的研究重点，输出格式填写希望的版式；留空可沿用上游默认。', example: '预设指令填“用中文，结论注明证据”；输出格式填“先列结论，再列支持与反对证据”。' },
  context: { purpose: '给模型补充与任务相关的资料和背景。', steps: '粘贴简短材料、来源说明或研究约束，按需要调整历史条数、输入和输出预算。长材料可使用任务页的资料正文入口注明时间。', example: '填写某公司财报节选，并说明来源、统计期间和待验证问题。', note: '请在密钥保存处填写密钥。不要把密钥放进发给模型的资料。' },
  reasoning: { purpose: '在模型实际支持时调整推理投入。', steps: '先选择厂商和模型，再从可用推理强度中选择。自定义模型能力未知时保留模型默认；高级参数只显示已核对的支持项。', example: '同一个模型可分别保存“默认”和“高强度”两张卡，再选择用于研究。' },
  ai: { purpose: '用一句话安排数据研究，按需要让模型解读结果。', steps: '选择数据集，说明研究问题，再选只用工具、一个模型、多个模型分别报告或已保存的十二角色团队。补充资料时，填写来源、发布日期和首次可查到的时间。核对计划后开始。', example: '输入“比较这个数据集的动量因子”，把当时可查到的财报作为补充资料，并按本地时间填写资料截止日期。', note: '截止时间以后才可查到的资料，以及时间无法确认的资料，会留在检查记录里。模型调用可能产生供应商费用。' },
  teams: { purpose: '保存角色分工，并把一个明确版本用于研究。', steps: '配置团队的默认模型与角色差异，保存后预览。可直接在任务页选择团队；也可冻结后点“使用此团队开始研究”，自动带入合并研究页。', example: '让同一个模型承担多个角色，或把风险复核角色分配给另一张模型卡。', note: '固定角色团队明确包含十二角色，单模型助手和多模型分别报告是其他模式。' },
  task: { purpose: '查看任务的真实状态、进度和保存结果。', steps: '提交后观察排队与执行状态；需要结束时点“请求停止”，等最终状态。完成后打开保存结果，历史记录可再次打开。', example: '任务显示执行中时请求停止；看到已取消后再决定是否新建任务。' },
  schedules: { purpose: '安排任务在指定时间自动开始，可以只运行一次或重复运行。', steps: '选择任务、填写首次运行时间（世界标准时间 UTC），再选择单次或固定间隔。重复任务的间隔按秒填写，错过的运行可按页面提供的方式处理。', example: '每天运行一次可填写 86400 秒。需要暂停后续运行时，在计划列表找到这项任务。' },
  'computation-cache': { purpose: '再次使用同一份数据和设置时，可直接取用前次计算结果，节省时间。', steps: '查看缓存中的记录和占用空间。清理前先选好要清理的范围，再核对操作提示。', example: '缓存命中表示这次使用了前次计算，未命中表示需要重新计算。清理出的数据库空间可供以后使用。' },
  'simulation-accounts': { purpose: '用模拟资金练习交易，查看余额和持仓。', steps: '选一个模拟账户，核对余额、持仓和记录。需要限制增加风险的操作时，使用冻结。若使用 Alpaca，请先检查连接和账户类型。', example: '若想保留现有记录并暂停增加仓位，可冻结这个模拟账户。', note: '本地模拟账户使用虚拟资金。Alpaca 账户由外部服务管理。' },
  operations: { purpose: '查看后台能否执行任务，并处理任务中断或数据恢复。', steps: '先看后台服务和任务状态，再打开相关任务或诊断信息。需要恢复时，先检查备份的时间和内容，再按页面说明操作。', example: '下载长时间停在排队时，先检查后台服务，再打开任务详情查看仍可使用的操作。' },
  results: { purpose: '集中阅读保存的回测、研究、AI 报告和训练结果。', steps: '选择结果类别，点报告名称预览。需要走势或明细时，打开完整报告。按需导出保存结果。', example: '在“回测”打开一份报告，查看扣费后赚亏金额和每只股票的明细。', note: '列表里的保存时间按本机时区显示。按日期筛选时使用世界标准时间（UTC，比北京时间早 8 小时）。例如北京时间 16 日凌晨保存的报告，按 UTC 可能在 15 日。' },
  mcp: { purpose: '把当前本机应用连接到兼容的 AI 客户端。', steps: '保持应用运行，点“刷新连接配置”，复制到客户端的 MCP 配置处，再在客户端查看能力与连接结果。', example: '连接后先让 AI 列出已有数据集，再明确启动所需任务。', note: '配置可用表示本机组件齐备，实际连接结果以客户端检查为准。' },
  'mcp-accounts': { purpose: '给这台电脑上的 MCP 客户端授予当前账户允许的操作。', steps: '选择客户端和权限，点“生成配对码”。在本机 PowerShell 运行配对命令，等它提示输入配对码时再粘贴。随后在客户端添加配置。', example: '先用只读授权，让客户端列出可用的固定版本数据集。', note: '可以在此撤销授权。配对后，到客户端检查工具能否使用。' },
  'mcp-easy-connect': {
    purpose: '让本机的 Codex 或 DeepSeek 使用你在这里授予的能力。',
    steps: '选客户端，再选只查看或帮我操作。点击连接，按提示验证身份。页面会显示授权状态和客户端最近一次连接确认。',
    example: '想让 Codex 下载行情并运行策略，选择帮我操作即可。',
    note: '可以到高级设置查看或撤销授权，也可在那里连接其他客户端。',
  },
};
export function helpForTitle(title: string, topic?: string): Help {
  if (topic && topics[topic]) return topics[topic];
  const match = helpTitles.find(([pattern]) => pattern.test(title));
  if (match) return topics[match[1]];
  return { purpose: `查看“${title}”里的信息。`, steps: '打开一条记录，查看当前状态和可用操作。', example: '准备再次使用一条记录时，先核对名称和日期。' };
}
const helpTitles: [RegExp, string][] = [
  [/保存位置/, 'storage-locations'],
  [/准备研究任务/, 'autonomous-create'],
  [/已保存研究文件/, 'autonomous-artifact'],
  [/找证券|WRDS/, 'wrds'],
  [/下载.*对照|下载指数/, 'benchmark-download'],
  [/选择来源连接|选数据来源|已保存来源连接/, 'source'],
  [/选择股票范围|选股票和基金/, 'universe'],
  [/日期与名称|选日期并命名/, 'dates'],
  [/检查并保存|CSV.?预览/, 'csv'],
  [/回收站/, 'trash'],
  [/固定版本数据卡|管理数据|已保存行情/, 'datasets'],
  [/运行状态|后台任务与恢复/, 'operations'],
  [/当前研究任务|后台任务|任务详情|任务进度|任务记录/, 'task'],
  [/定时任务|执行计划/, 'schedules'],
  [/计算缓存/, 'computation-cache'],
  [/模拟账户|账户快照|余额与持仓/, 'simulation-accounts'],
  [/本功能.*历史|已保存的回测|已保存的研究|保存.*结果|已保存报告|我的.*结果/, 'results'],
  [/模型与 Agent|团队工作区|已保存团队/, 'teams'],
  [/Qlib|TradingAgents/, 'native-research'],
  [/自主研究/, 'autonomous-control'],
  [/开始回测/, 'backtest'],
  [/AI\s*研究|用一句话/, 'ai'],
  [/连接外部 AI/, 'mcp-easy-connect'],
  [/MCP/, 'mcp'],
  [/模型配置|我的模型/, 'models'],
  [/上下文|参考资料/, 'context'],
  [/Prompt|预设指令|模型指令/, 'prompts'],
  [/推理/, 'reasoning'],
  [/团队/, 'teams'],
  [/模型/, 'models'],
  [/来源|连接/, 'source'],
  [/数据|样本|导入/, 'datasets'],
  [/盈亏|结果|报告/, 'results'],
  [/策略|回测|资金|成本/, 'backtest'],
  [/研究|任务|计划/, 'ai'],
];
export function keepDialogFocus(event: KeyboardEvent<HTMLDialogElement>) {
    if (event.key !== 'Tab' || event.altKey || event.ctrlKey || event.metaKey) return;
    const current = event.currentTarget;
    const targets = Array.from(current.querySelectorAll<HTMLElement>('a[href], button, input, select, textarea, [tabindex]'))
      .filter((node) => node.tabIndex >= 0 && !node.matches(':disabled, [hidden], input[type="hidden"]') && !node.closest('[inert]') && node.getClientRects().length > 0);
    const active = current.ownerDocument.activeElement;
    const first = targets[0], last = targets.at(-1);
    if (!first || !targets.includes(active as HTMLElement) || (event.shiftKey ? active === first : active === last)) {
      event.preventDefault();
      (event.shiftKey ? last ?? current : first ?? current).focus({ preventScroll: true });
    }
  }
export function AreaHelp({ topic, title }: { topic?: string; title: string }) {
  const help = helpForTitle(title, topic), id = useId(), dialog = useRef<HTMLDialogElement>(null), trigger = useRef<HTMLButtonElement>(null), closeButton = useRef<HTMLButtonElement>(null), [open, setOpen] = useState(false);
  function close() { dialog.current?.close(); setOpen(false); trigger.current?.focus(); }
  return <div className="area-help"><button type="button" className="text-button" ref={trigger} aria-label={`${title}：用途、步骤和例子`} aria-haspopup="dialog" aria-controls={id} aria-expanded={open} onClick={() => { dialog.current?.showModal(); setOpen(true); closeButton.current?.focus(); }}>? 用法</button><dialog id={id} ref={dialog} className="help-dialog" tabIndex={-1} onKeyDown={keepDialogFocus} aria-modal="true" aria-labelledby={`${id}-title`} aria-describedby={`${id}-purpose`} onCancel={(event) => { event.preventDefault(); close(); }} onClose={() => { setOpen(false); trigger.current?.focus(); }}><div className="panel-heading"><h3 id={`${id}-title`}>{title} · 使用帮助</h3><button type="button" ref={closeButton} autoFocus onClick={close} aria-label={`关闭${title}帮助`}>关闭帮助</button></div><p id={`${id}-purpose`}><strong>用途：</strong>{help.purpose}</p><p><strong>怎么操作：</strong>{help.steps}</p><p><strong>例子：</strong>{help.example}</p>{help.note && <p>{help.note}</p>}</dialog></div>;
}
const errorCopy: Record<string, string> = {
  NOT_AUTHENTICATED: '登录已结束，请重新登录。',
  PORTFOLIO_PRICE_BASIS_UNSUPPORTED: '这个回测入口无法使用所选价格类型。请到数据管理检查价格处理方式，再选择本入口支持的数据。',
  SDK_DNS_FAILED: '找不到行情网站。请检查网络连接，稍后重试。',
  SDK_RESPONSE_LIMIT_EXCEEDED: '行情网站这次返回的数据超出下载限制。请查看任务详情。若网站总是返回完整历史，可尝试另一个可用来源。',
  SDK_RUNTIME_BINDING_CHANGED: '本机的行情组件与当前安装版本不匹配。请检查软件安装。',
  SDK_TIMEOUT: '等待行情网站响应超时。请打开这次下载任务查看进度，选择可用的重试或继续方式。',
  SDK_TRANSPORT_FAILED: '与行情网站的连接未完成。请打开这次下载任务查看详情。',
  SECURITY_IDENTITY_UNAVAILABLE: '无法确认这个代码对应哪只股票或基金。请检查代码和所选市场。',
  RESULT_VIEW_CHANGED: '读取报告时，保存内容已改变。请关闭预览，再打开这条结果。',
  RESULT_EXPORT_UNAVAILABLE: '这份历史报告不能直接导出。请打开完整报告查看保存内容。',
  CSRF_REJECTED: '登录状态刚刚更新。请刷新页面，再检查上次操作是否已保存。',
  REAUTH_REQUIRED: '请先重新验证身份，再继续操作。',
  RATE_LIMITED: '尝试次数过多。请稍后再试。',
  PASSWORD_POLICY: '新密码需使用 15–128 个字符，并避开常见密码。',
};
export function plainError(message: string, status: number, code?: string) {
  if (code && Object.hasOwn(errorCopy, code)) return errorCopy[code];
  if (/[\u3400-\u9fff]/.test(message)) return message;
  return status === 409 ? '记录已更新，请刷新并核对内容后再修改。'
    : status === 403 ? '当前账户无法执行这项操作。请检查权限或另选可访问的记录。'
    : status === 404 ? '暂时找不到记录，或当前服务未提供这项功能。请刷新列表。'
    : status === 422 || status === 400 ? '填写的内容不符合要求。请核对所选项目、日期和设置。'
    : status === 401 ? '请重新登录，再打开工作台。'
    : '这次操作未完成。请先刷新查看状态，再决定是否重试。';
}
