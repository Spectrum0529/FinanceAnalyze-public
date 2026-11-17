"""Frozen exchange-filed identity evidence for the reviewed 2024 window."""

EVIDENCE = {
    "600000.SH": (
        "XSHG", "1999-11-10",
        "https://big5.sse.com.cn/site/cht/www.sse.com.cn/disclosure/listedinfo/announcement/c/new/2025-03-29/600000_20250329_1OTF.pdf",
        "https://www.sse.com.cn/assortment/stock/list/info/company/index.shtml?COMPANY_CODE=600000",
    ),
    "000001.SZ": (
        "XSHE", "1991-04-03",
        "https://disc.static.szse.cn/download/disc/disk03/finalpage/2025-03-15/1aa52a0c-7904-4329-bd44-54a6ec667fbf.PDF",
        "https://disc.static.szse.cn/disc/disk03/finalpage/2024-08-16/e997ceb0-6558-42dd-9c96-8ce6e16a75ad.PDF",
    ),
}


def reviewed_identity(owner, provider, ticker, start, end):
    entry = EVIDENCE.get(ticker)
    if provider not in {"akshare_cn", "tushare"} or entry is None:
        return None
    mic, listed_on, annual, corroboration = entry
    # Admit only the reviewed 2024 reporting interval, not the entire lifetime.
    valid_from, valid_through = "2024-01-01", "2024-12-31"
    if not valid_from <= start <= end <= valid_through:
        return None
    return dict(symbol=ticker, mic=mic, asset="CN_EQUITY",
                valid_from=valid_from, valid_through=valid_through,
                evidence=dict(schema="CN_EXCHANGE_FILED_SECURITY_2024_1", listed_on=listed_on,
                              annual=annual, corroboration=corroboration))
