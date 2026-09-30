import { useMemo } from 'react';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import './WorkflowDocumentPreview.css';

/** Untrusted model/file text: no embedded media, active HTML, or unsafe links. */
export default function SafeMarkdown({ content, className = '' }: { content: string; className?: string }) {
  const html = useMemo(() => {
    const raw = marked.parse(content, { async: false, gfm: true, breaks: false });
    const fragment = DOMPurify.sanitize(raw, {
      ALLOWED_TAGS: ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br', 'hr', 'ul', 'ol', 'li', 'strong', 'b', 'em', 'i', 's', 'del', 'blockquote', 'pre', 'code', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a'],
      ALLOWED_ATTR: ['href', 'title', 'start', 'colspan', 'rowspan'],
      RETURN_DOM_FRAGMENT: true,
    });
    fragment.querySelectorAll('a').forEach((link) => {
      const href = link.getAttribute('href') || '';
      try {
        const url = new URL(href, window.location.origin);
        if (!['http:', 'https:'].includes(url.protocol)) { link.removeAttribute('href'); return; }
        link.setAttribute('rel', 'noopener noreferrer');
        link.setAttribute('target', '_blank');
      } catch { link.removeAttribute('href'); }
    });
    const container = document.createElement('div');
    container.appendChild(fragment);
    return container.innerHTML;
  }, [content]);
  return <div className={`cw-markdown ${className}`} dangerouslySetInnerHTML={{ __html: html }} />;
}
