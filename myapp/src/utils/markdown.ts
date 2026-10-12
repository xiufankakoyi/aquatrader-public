/**
 * Markdown 简易渲染工具
 * 将常见 Markdown 语法转换为 HTML，作为后端未返回 HTML 时的降级方案
 */

/**
 * 将 Markdown 文本渲染为简单 HTML。
 *
 * @param md - Markdown 原始文本
 * @returns HTML 字符串
 */
export function renderMarkdown(md?: string): string {
  if (!md) return '';
  return (
    md
      // 代码块（放在行内规则之前，避免内部内容被转义）
      .replace(/```[\s\S]*?```/g, (match) => `<pre><code>${match.slice(3, -3).trim()}</code></pre>`)
      // 标题
      .replace(/^### (.+)$/gm, '<h3>$1</h3>')
      .replace(/^## (.+)$/gm, '<h2>$1</h2>')
      .replace(/^# (.+)$/gm, '<h1>$1</h1>')
      // 粗体 / 斜体
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g, '<em>$1</em>')
      // 无序列表（简单实现：每行一个 li）
      .replace(/^- (.+)$/gm, '<li>$1</li>')
      // 将连续的 li 包成 ul
      .replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>')
      // 段落分隔
      .replace(/\n{2,}/g, '<br/><br/>')
      .replace(/\n/g, '<br/>')
  );
}
