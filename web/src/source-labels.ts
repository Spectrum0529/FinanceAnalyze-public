const descriptions: Record<string, { name: string; summary: string }> = {
  yfinance: { name: "Yahoo 财经（yfinance）", summary: "无需密钥，支持美股和 ETF。历史价格和模拟持有数量按拆股换算，收益不计现金分红。" },
  akshare: { name: "AKShare 美股（新浪财经）", summary: "无需密钥，支持部分美股，暂不支持 ETF。新浪把过去的价格换算到近期标准（前复权）；模拟持有数量随价格换算，收益不另加现金分红。" },
  massive: { name: "Massive 美股", summary: "需要在本机保存密钥。可选当时成交价或按拆股换算的价格。股票、基金、日期范围和用量上限取决于账户权限。" },
  csv: { name: "本地 CSV 文件", summary: "导入自己的行情文件。请按文件来源选择价格类型，例如价格是否按拆股换算。" },
  openbb_cboe: { name: "OpenBB / Cboe 日线", summary: "需要启用本机可选组件，无需密钥。可以保存和查看来源提供的价格。目前还无法确认这些价格如何处理拆股和分红。" },
  akshare_cn: { name: "AKShare 沪深（东方财富）", summary: "无需密钥，可下载部分沪深股票的人民币每日行情。保留当时的成交价格，未因拆股或分红重新计算。A 页美股组合回测无法使用这类人民币数据。B 页沪深研究仍需核对股票身份、交易日和历史数据够不够。" },
  tushare: { name: "Tushare Pro 沪深", summary: "需要在本机保存密钥。可下载沪深股票的人民币每日行情，保留当时的成交价格。可下载的股票和日期取决于你的账户权限。" },
  openbb_nasdaq: { name: "OpenBB / Nasdaq 日线", summary: "需要启用本机可选组件，无需密钥。可以保存和查看来源提供的价格。目前还无法确认这些价格如何处理拆股和分红。" },
  wrds: { name: "WRDS / CRSP 美股日线", summary: "需要 WRDS 账号和 CRSP 数据权限。支持符合要求的美股和 ETF。保留当时成交价，回测不计分红。行情和报告仅限本机查看，不可通过外部连接读取或导出。" },
};


export function sourceDescription(id: string, displayName?: unknown, displaySummary?: unknown) {
  const fallback = Object.hasOwn(descriptions, id) ? descriptions[id] : undefined;
  return {
    name: typeof displayName === 'string' && displayName.trim() ? displayName : fallback?.name ?? id,
    summary: typeof displaySummary === 'string' && displaySummary.trim() ? displaySummary : fallback?.summary,
  };
}
