// Local reviewed code only. Remote input is the string argument to pinned d().
function __fa_sina_decode(payload) {
    "use strict";
    const rows = d(payload);
    if (!Array.isArray(rows) || rows.length === 0 || rows.length > 20000)
        throw new Error("SINA_FULL_HISTORY_LIMIT");
    const fields = ["open", "high", "low", "close", "volume", "amount"];
    let previous = "";
    for (const row of rows) {
        if (!row || typeof row !== "object" || Array.isArray(row))
            throw new Error("SINA_SCHEMA");
        const keys = Object.keys(row);
        if (keys.length < 7 || keys.length > 8 || keys.some(k => !["date", ...fields, "prevclose"].includes(k)))
            throw new Error("SINA_SCHEMA");
        const value = row.date instanceof Date ? row.date.toISOString() : row.date;
        if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}(T00:00:00\.000Z)?$/.test(value))
            throw new Error("SINA_DATE");
        const day = value.slice(0, 10);
        if (new Date(day + "T00:00:00.000Z").toISOString().slice(0, 10) !== day || day <= previous)
            throw new Error("SINA_DATE");
        previous = day;
        row.date = day;
        for (const key of fields) {
            if (typeof row[key] !== "number" || !Number.isFinite(row[key]) || Math.abs(row[key]) >= 1e24)
                throw new Error("SINA_NUMBER");
        }
        if (row.prevclose !== undefined && (typeof row.prevclose !== "number" || !Number.isFinite(row.prevclose) || Math.abs(row.prevclose) >= 1e24))
            throw new Error("SINA_NUMBER");
        if (["open", "high", "low", "close"].some(k => row[k] < 1e-12)
                || row.low > Math.min(row.open, row.close) || row.high < Math.max(row.open, row.close)
                || !Number.isSafeInteger(row.volume) || row.volume < 0 || row.amount < 0)
            throw new Error("SINA_FINANCIAL");
    }
    // JSON is ASCII after strict dates/numeric fields; length is byte length.
    // This check runs inside V8, before full history becomes a Python object.
    const encoded = JSON.stringify(rows);
    if (encoded.length > 2097152) throw new Error("SINA_JSON_LIMIT");
    return encoded;
}
