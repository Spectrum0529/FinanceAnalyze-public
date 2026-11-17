import { Notice, JsonView } from './components';
import type { RecordData } from './api';
export function HistoryPricePolicyNote({
  label,
  message,
  evidence,
  quantityBasis = null,
}: {
  label: string | null;
  message: string;
  evidence: RecordData | null;
  quantityBasis?: string | null;
}) {
  return (
    <>
      <Notice>
        <strong>本次价格如何处理：{label || '服务尚未确认'}</strong>
        <p>{message}</p>
        <p>
          这是按所选价格数据和费用规则模拟的表现，可能与实际成交收益或计入分红的总收益不同。
        </p>
        {quantityBasis === 'ADJUSTED_SIMULATION_UNITS' && (
          <p>
            股数按调整后的价格换算。每单位费用、按美分报价和按整数下单都是模拟设定，可能和当时的券商规则不同。模拟不会另计拆股、派息或分红再投资，也不会按成交量估计当时能买卖多少。
          </p>
        )}
      </Notice>
      {evidence && (
        <details>
          <summary>查看价格调整和数据版本</summary>
          <JsonView value={evidence} />
        </details>
      )}
    </>
  );
}
