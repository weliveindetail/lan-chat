import { useEffect, useState } from "react";
import {
  ActionBarPrimitive,
  AssistantRuntimeProvider,
  AuiIf,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAuiState,
  useLocalRuntime,
  useMessagePartText,
} from "@assistant-ui/react";
import { MarkdownTextPrimitive } from "@assistant-ui/react-markdown";
import remarkGfm from "remark-gfm";
import { chatAdapter } from "./chatAdapter.js";
import { loadHistory, persistHistory } from "./history.js";

const remarkPlugins = [remarkGfm];

function MarkdownLink({ href, children }) {
  // The Markdown URL filter blanks unsafe schemes such as javascript:
  if (!href) return <span>{children}</span>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}

// Never load images named by the model: the fetch itself could leak data
function MarkdownImage({ src, alt }) {
  const label = `Image: ${alt || "no description"}`;
  return <MarkdownLink href={src}>[{label}]</MarkdownLink>;
}

function MarkdownTable(props) {
  return (
    <div className="table-scroll">
      <table {...props} />
    </div>
  );
}

const markdownComponents = { a: MarkdownLink, img: MarkdownImage, table: MarkdownTable };

// No rehype-raw, so HTML in replies is shown as text and never parsed
const AssistantText = () => (
  <MarkdownTextPrimitive
    className="markdown"
    remarkPlugins={remarkPlugins}
    components={markdownComponents}
    smooth={false}
  />
);

function UserText() {
  const { text } = useMessagePartText();
  return <p className="user-text">{text}</p>;
}

function Progress({ label }) {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const started = Date.now();
    const timer = setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(timer);
  }, []);
  return (
    <div className="progress" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
      {seconds > 0 && <span className="elapsed">{seconds}s</span>}
    </div>
  );
}

function RunIndicator() {
  const phase = useAuiState((s) => s.message.metadata?.custom?.phase);
  const hasText = useAuiState((s) => s.message.content.some((part) => part.type === "text" && part.text));
  if (phase === "thinking") return <Progress label="Thinking" />;
  if (hasText) return null;
  return <Progress label="Waiting for the model" />;
}

function noticeFor(status, hasText) {
  if (status.type === "complete") {
    return hasText ? null : { tone: "warn", text: "The model returned an empty reply." };
  }
  if (status.type !== "incomplete") return null;
  switch (status.reason) {
    case "cancelled":
      return { tone: "muted", text: "Stopped." };
    case "length":
      return {
        tone: "warn",
        text: hasText
          ? "Reply cut off: the output limit was reached."
          : "No reply: the output limit was reached while the model was thinking. Try a narrower question.",
      };
    case "error":
      return {
        tone: "error",
        text: status.error?.message ?? "Something went wrong.",
      };
    default:
      return { tone: "warn", text: "The connection ended early, so this reply is incomplete." };
  }
}

function MessageNotice() {
  const status = useAuiState((s) => s.message.status);
  const isLast = useAuiState((s) => s.message.isLast);
  const hasText = useAuiState((s) => s.message.content.some((part) => part.type === "text" && part.text));
  if (!status) return null;
  const notice = noticeFor(status, hasText);
  if (!notice) return null;
  return (
    <div className={`notice notice-${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>
      <span>{notice.text}</span>
      {isLast && (
        <ActionBarPrimitive.Root hideWhenRunning>
          <ActionBarPrimitive.Reload className="button">Retry</ActionBarPrimitive.Reload>
        </ActionBarPrimitive.Root>
      )}
    </div>
  );
}

const UserMessage = () => (
  <MessagePrimitive.Root className="message message-user">
    <MessagePrimitive.Parts components={{ Text: UserText }} />
  </MessagePrimitive.Root>
);

const AssistantMessage = () => (
  <MessagePrimitive.Root className="message message-assistant">
    <MessagePrimitive.Parts components={{ Text: AssistantText }} />
    <AuiIf condition={(s) => s.message.status?.type === "running"}>
      <RunIndicator />
    </AuiIf>
    <MessageNotice />
  </MessagePrimitive.Root>
);

const messageComponents = { UserMessage, AssistantMessage };

// Phone keyboards have no Shift+Enter, so there Enter adds a line and the button sends
const submitMode = matchMedia("(pointer: coarse)").matches ? "none" : "enter";

const Composer = () => (
  <ComposerPrimitive.Root className="composer">
    <ComposerPrimitive.Input
      className="composer-input"
      placeholder="Message"
      aria-label="Message"
      rows={1}
      maxRows={8}
      autoFocus
      submitMode={submitMode}
    />
    <AuiIf condition={(s) => !s.thread.isRunning}>
      <ComposerPrimitive.Send className="button button-primary">Send</ComposerPrimitive.Send>
    </AuiIf>
    <AuiIf condition={(s) => s.thread.isRunning}>
      <ComposerPrimitive.Cancel className="button button-stop">Stop</ComposerPrimitive.Cancel>
    </AuiIf>
  </ComposerPrimitive.Root>
);

export function App() {
  const [initialMessages] = useState(loadHistory);
  const runtime = useLocalRuntime(chatAdapter, { initialMessages });

  useEffect(() => persistHistory(runtime.thread), [runtime]);

  const newChat = () => {
    runtime.thread.cancelRun();
    runtime.thread.reset();
    runtime.thread.composer.setText("");
    document.querySelector(".composer-input")?.focus();
  };

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="app">
        <header className="header">
          <h1>AkrylZeit Chat</h1>
          <button type="button" className="button" onClick={newChat}>
            New chat
          </button>
        </header>
        <ThreadPrimitive.Root className="thread">
          <ThreadPrimitive.Viewport className="viewport">
            <AuiIf condition={(s) => s.thread.isEmpty}>
              <p className="empty">Send a message to start. Chats stay in this tab and aren't saved on the server.</p>
            </AuiIf>
            <ThreadPrimitive.Messages components={messageComponents} />
            <ThreadPrimitive.ViewportFooter className="footer">
              <ThreadPrimitive.ScrollToBottom className="button scroll-down" aria-label="Scroll to latest">
                ↓
              </ThreadPrimitive.ScrollToBottom>
              <Composer />
            </ThreadPrimitive.ViewportFooter>
          </ThreadPrimitive.Viewport>
        </ThreadPrimitive.Root>
      </div>
    </AssistantRuntimeProvider>
  );
}
