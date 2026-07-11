(function attachMarkdownRenderer(root, factory) {
  const renderer = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = renderer;
  if (root) root.GeowikiMarkdown = renderer;
})(typeof window !== "undefined" ? window : globalThis, function createMarkdownRenderer() {
  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function safeUrl(value, baseUrl) {
    try {
      const url = new URL(value, baseUrl || "http://localhost/");
      if (url.protocol === "http:" || url.protocol === "https:") return url.href;
    } catch {
      return "";
    }
    return "";
  }

  function renderInline(value, baseUrl) {
    const tokens = [];
    let text = String(value ?? "");
    text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_, label, url) => {
      const href = safeUrl(url, baseUrl);
      if (!href) return label;
      const token = `@@GEOWIKI_LINK_${tokens.length}@@`;
      tokens.push(`<a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`);
      return token;
    });
    text = escapeHtml(text);
    text = text.replace(/(https?:\/\/[^\s<|]+)/g, (url) => {
      const normalized = safeUrl(url.replaceAll("&amp;", "&"), baseUrl);
      return normalized ? `<a href="${escapeHtml(normalized)}" target="_blank" rel="noreferrer">${url}</a>` : url;
    });
    text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    tokens.forEach((link, index) => {
      text = text.replace(`@@GEOWIKI_LINK_${index}@@`, link);
    });
    return text;
  }

  function splitTableRow(line) {
    let text = String(line ?? "").trim();
    if (text.startsWith("|")) text = text.slice(1);
    if (text.endsWith("|") && !text.endsWith("\\|")) text = text.slice(0, -1);
    const cells = [];
    let cell = "";
    let escaped = false;
    for (const character of text) {
      if (escaped) {
        cell += character;
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (character === "|") {
        cells.push(cell.trim());
        cell = "";
      } else {
        cell += character;
      }
    }
    if (escaped) cell += "\\";
    cells.push(cell.trim());
    return cells;
  }

  function tableAlignments(line, expectedColumns) {
    const cells = splitTableRow(line);
    if (cells.length !== expectedColumns || cells.some((cell) => !/^:?-{3,}:?$/.test(cell))) return null;
    return cells.map((cell) => {
      if (cell.startsWith(":") && cell.endsWith(":")) return "center";
      if (cell.endsWith(":")) return "right";
      return "left";
    });
  }

  function tableCell(tag, value, alignment, baseUrl) {
    const align = alignment && alignment !== "left" ? ` style="text-align:${alignment}"` : "";
    return `<${tag}${align}>${renderInline(value, baseUrl)}</${tag}>`;
  }

  function render(value, options = {}) {
    const lines = String(value ?? "").replaceAll("\r\n", "\n").split("\n");
    const output = [];
    const baseUrl = options.baseUrl || "http://localhost/";
    let listType = null;
    const closeList = () => {
      if (listType) output.push(`</${listType}>`);
      listType = null;
    };

    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      const trimmed = line.trim();
      if (!trimmed) {
        closeList();
        continue;
      }

      const headerCells = splitTableRow(line);
      const alignments = index + 1 < lines.length && headerCells.length >= 2
        ? tableAlignments(lines[index + 1], headerCells.length)
        : null;
      if (alignments) {
        closeList();
        const bodyRows = [];
        index += 2;
        while (index < lines.length && lines[index].trim()) {
          const cells = splitTableRow(lines[index]);
          if (cells.length < 2) break;
          while (cells.length < headerCells.length) cells.push("");
          bodyRows.push(cells.slice(0, headerCells.length));
          index += 1;
        }
        index -= 1;
        output.push(
          `<div class="markdown-table-wrap"><table class="markdown-table"><thead><tr>${headerCells
            .map((cell, column) => tableCell("th", cell, alignments[column], baseUrl))
            .join("")}</tr></thead><tbody>${bodyRows
            .map((row) => `<tr>${row.map((cell, column) => tableCell("td", cell, alignments[column], baseUrl)).join("")}</tr>`)
            .join("")}</tbody></table></div>`,
        );
        continue;
      }

      const heading = trimmed.match(/^#{2,4}\s+(.+)$/);
      if (heading) {
        closeList();
        output.push(`<h3>${renderInline(heading[1], baseUrl)}</h3>`);
        continue;
      }
      const bullet = trimmed.match(/^[-*]\s+(.+)$/);
      const ordered = trimmed.match(/^\d+[.)]\s+(.+)$/);
      if (bullet || ordered) {
        const nextType = bullet ? "ul" : "ol";
        if (listType !== nextType) {
          closeList();
          listType = nextType;
          output.push(`<${listType}>`);
        }
        output.push(`<li>${renderInline((bullet || ordered)[1], baseUrl)}</li>`);
        continue;
      }
      closeList();
      output.push(`<p>${renderInline(trimmed, baseUrl)}</p>`);
    }
    closeList();
    return output.join("");
  }

  return { render, splitTableRow, tableAlignments };
});
