export function ResearchContextBoundary() {
  return (
    <details>
      <summary>团队怎样使用资料和密钥</summary>
      <p>例如，两个团队可以用同一把 DeepSeek 密钥或同一份行情。共用密钥时，模型费用和使用限额可能合计。研究工具只读取你为当前任务选定的资料，每次研究各自保存对话。</p>
      <p>你可以打开自己有权查看的已保存资料。有相应读取权限的外部 AI 也可以查看这些资料。</p>
      <p>模型厂商可重用相同开头的内容来加快处理。工作台自己的计算缓存可在<a href="#settings?section=computation-cache">服务设置</a>查看。</p>
    </details>
  );
}
