import { useId } from 'react';

export type GoalProvider = {
  id: string;
  name: string;
  summary?: string;
  market?: string;
  kind?: string;
  assets?: string;
  frequency?: string;
  adjustment?: string;
  currency?: string;
  credentials?: string;
  entitlement?: string;
  coverage?: string;
  readiness: string;
  connectionConfigurable: boolean;
};

export function GoalProviderSettings({ providers, showConnectionLinks = true }: { providers: GoalProvider[]; showConnectionLinks?: boolean }) {
  const heading = useId();
  return (
    <section aria-labelledby={heading} className="goal-provider-settings">
      <h3 id={heading}>各数据来源支持什么</h3>
      {providers.length === 0 ? <p>暂未读取到来源信息。请刷新页面后再查看。</p> : (
        <ul className="goal-detail-list">
          {providers.map((provider) => {
            const scope = [provider.market, provider.assets, provider.kind, provider.frequency, provider.adjustment, provider.currency]
              .filter(Boolean).join(' · ');
            return (
              <li key={provider.id}>
                <strong>{provider.name}</strong>
                {provider.summary && <p>{provider.summary}</p>}
                {scope && <p>{scope}</p>}
                <p>{provider.readiness}</p>
                {provider.credentials && <p>{provider.credentials}</p>}
                {provider.entitlement && <p>权限 {provider.entitlement}</p>}
                {provider.coverage && <p>日期范围 {provider.coverage}</p>}
                {showConnectionLinks && provider.connectionConfigurable && <a href="#data">到数据管理添加此来源</a>}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
