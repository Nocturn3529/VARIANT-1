import {Component, useEffect, useMemo, useRef, useState, type ReactNode} from "react";
import {notifyChat} from "../chatStore";
import runtimeLib, {type InlineToken, type MdBlock} from "./runtimeLib";
import {useSurfaceDocument} from "../ui/SurfaceDocument";

function openExternal(href: string) {
  const api = window.variant1Deck;
  if (api && typeof api.openExternal === "function") api.openExternal(href);
  else notifyChat("External links open in the VARIANT-1 desktop app");
}

function InlineNodes({tokens}: {tokens: InlineToken[]}): ReactNode {
  return <>
    {(tokens || []).map((token, index) => {
      if (token.type === "text") return <span key={index}>{token.text}</span>;
      if (token.type === "code") return <code key={index}>{token.text}</code>;
      if (token.type === "strong" || token.type === "em") {
        const Tag = token.type;
        return <Tag key={index}><InlineNodes tokens={token.children || []} /></Tag>;
      }
      if (token.type === "link") {
        const href = runtimeLib.safeHttpUrl ? runtimeLib.safeHttpUrl!(token.href) : "";
        if (!href) return <InlineNodes key={index} tokens={token.children || []} />;
        return <a
          key={index}
          href={href}
          rel="noreferrer noopener"
          data-external-link={href}
          onClick={event => {
            event.preventDefault();
            openExternal(href);
          }}
        >
          <InlineNodes tokens={token.children || []} />
        </a>;
      }
      return null;
    })}
  </>;
}

function CodeBlock({language, text, deferHighlight = false}: {language: string; text: string; deferHighlight?: boolean}) {
  const tokens = !deferHighlight && runtimeLib.highlightCode
    ? runtimeLib.highlightCode!(text, language)
    : [{type: "text", text}];
  return <figure className="runtime-code-block">
    <figcaption>
      <span>{language || "code"}</span>
      <button
        type="button"
        onClick={() => {
          navigator.clipboard.writeText(text || "")
            .then(() => notifyChat("Code copied"))
            .catch(() => notifyChat("Couldn't copy code"));
        }}
      >
        Copy
      </button>
    </figcaption>
    <pre>
      <code data-language={language || undefined}>
        {tokens.map((token, index) => (
          token.type === "text"
            ? <span key={index}>{token.text}</span>
            : <span key={index} className={`syntax-${token.type}`}>{token.text}</span>
        ))}
      </code>
    </pre>
  </figure>;
}

function Blocks({blocks, streaming = false}: {blocks: MdBlock[]; streaming?: boolean}): ReactNode {
  return <>
    {(blocks || []).map((block, index) => {
      if (block.type === "heading") {
        const level = Math.max(1, Math.min(6, block.level || 1));
        const Tag = `h${level}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
        return <Tag key={index}><InlineNodes tokens={block.inline || []} /></Tag>;
      }
      if (block.type === "paragraph") {
        return <p key={index}>
          {(block.lines || []).map((line, lineIndex) => (
            <span key={lineIndex}>
              {lineIndex > 0 ? <br /> : null}
              <InlineNodes tokens={line} />
            </span>
          ))}
        </p>;
      }
      if (block.type === "blockquote") {
        return <blockquote key={index}><Blocks blocks={block.blocks || []} streaming={streaming}/></blockquote>;
      }
      if (block.type === "list") {
        const Tag = block.ordered ? "ol" : "ul";
        return <Tag key={index}>
          {(block.items || []).map((item, itemIndex) => (
            <li key={itemIndex}>
              <span><InlineNodes tokens={item.inline || []} /></span>
              <Blocks blocks={item.children || []} streaming={streaming}/>
            </li>
          ))}
        </Tag>;
      }
      if (block.type === "table") {
        return <div key={index} className="runtime-markdown-table-wrap">
          <table>
            <thead>
              <tr>
                {(block.headers || []).map((cell, cellIndex) => (
                  <th key={cellIndex}><InlineNodes tokens={cell} /></th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(block.rows || []).map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {Array.from({length: Math.max(row.length, (block.headers || []).length)}).map((_, cellIndex) => (
                    <td key={cellIndex}><InlineNodes tokens={row[cellIndex] || []} /></td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>;
      }
      if (block.type === "code") {
        return <CodeBlock key={index} language={block.language || ""} text={block.text || ""} deferHighlight={streaming}/>;
      }
      return null;
    })}
  </>;
}

class MarkdownBoundary extends Component<{
  children: ReactNode;
  text: string;
}, {failed: boolean}> {
  state = {failed: false};
  static getDerivedStateFromError() { return {failed: true}; }
  render() {
    return this.state.failed
      ? <span className="runtime-markdown-fallback">{this.props.text}</span>
      : this.props.children;
  }
}

function ParsedRichText({value, streaming}: {value: string; streaming?: boolean}) {
  const parse = runtimeLib.parseMarkdown;
  if (!parse) return <span style={{whiteSpace: "pre-wrap"}}>{value || ""}</span>;
  const blocks = useMemo(() => parse(value || ""), [value, parse]);
  return <Blocks blocks={blocks} streaming={streaming}/>;
}

export function RichText({value, streaming}: {value: string; streaming?: boolean}) {
  const ownerWindow = useSurfaceDocument().defaultView || window;
  const [painted, setPainted] = useState(value);
  const latest = useRef(value);
  const frame = useRef<number | null>(null);
  latest.current = value;
  useEffect(() => {
    if (!streaming) {
      if (frame.current != null) {
        if (ownerWindow.cancelAnimationFrame) ownerWindow.cancelAnimationFrame(frame.current);
        else ownerWindow.clearTimeout(frame.current);
        frame.current = null;
      }
      setPainted(value);
      return;
    }
    if (frame.current != null) return;
    const request = ownerWindow.requestAnimationFrame?.bind(ownerWindow) || ((callback: FrameRequestCallback) => ownerWindow.setTimeout(() => callback(performance.now()), 16));
    frame.current = request(() => {
      frame.current = null;
      setPainted(latest.current);
    });
  }, [streaming, value, ownerWindow]);
  useEffect(() => () => {
      if (frame.current == null) return;
      if (ownerWindow.cancelAnimationFrame) ownerWindow.cancelAnimationFrame(frame.current);
      else ownerWindow.clearTimeout(frame.current);
      frame.current = null;
  }, [ownerWindow]);
  return <MarkdownBoundary text={painted}>
    <ParsedRichText value={painted} streaming={streaming}/>
  </MarkdownBoundary>;
}


/** One readable line for a folded thought; links stay noninteractive inside its button. */
export function thoughtPreview(value: string): string {
  const line = value.split(/\r?\n/).find(part => part.trim())?.slice(0, 320) || "";
  const text = (tokens: InlineToken[]): string => tokens.map(token => "text" in token ? token.text : text(token.children || [])).join("");
  const block = runtimeLib.parseMarkdown(line)[0];
  if (!block) return "";
  if (block.type === "heading") return text(block.inline || []);
  if (block.type === "paragraph") return (block.lines || []).map(text).join(" ");
  if (block.type === "list") return text(block.items[0]?.inline || []);
  return line;
}
