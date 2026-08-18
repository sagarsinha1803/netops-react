import React, { useEffect, useRef, useState } from "react";

// **bold** and `code` only -- answers are prose, and escaping first keeps a
// hostname or a CLI fragment in the reply from being read as markup.
function fmt(text) {
  const esc = String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return {
    __html: esc
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/`([^`]+)`/g, '<code class="mono">$1</code>'),
  };
}

// Optional chat drawer. Questions go through the SAME agent conversation, so
// the model answers from the run's context (CMDB records, probe output, the
// Tufin verdict) — ask "why is it blocked?" after a run and it knows.
export default function ChatPanel({ msgs, busy, clip, onAsk, onClose }) {
  const [text, setText] = useState("");
  const scrollRef = useRef(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs, busy]);

  const submit = () => {
    if (!text.trim() || busy) return;
    onAsk(text.trim());
    setText("");
  };

  return (
    <div className="chatpanel">
      <div className="chat-head">
        <span>Chat</span>
        <span className="hint-inline">answers use the current run's context</span>
        <button className="icon-btn sm" title="Close chat" onClick={onClose}>
          ✕
        </button>
      </div>
      <div className="chat-msgs" ref={scrollRef}>
        {msgs.length === 0 && (
          <div className="chat-empty">
            Ask anything — “why is it blocked?”, “device details for edge-a1”,
            “what should I check next?”
          </div>
        )}
        {msgs.map((m, i) =>
          m.role === "agent" ? (
            <div key={i} className="chat-msg agent"
                 dangerouslySetInnerHTML={fmt(m.text)} />
          ) : (
            <div key={i} className="chat-msg user">
              {m.text}
            </div>
          ),
        )}
        {busy && (
          <div className="chat-msg agent typing">
            {clip ? "waiting for Copilot…" : "thinking…"}
          </div>
        )}
      </div>
      <div className="chat-input">
        <input
          value={text}
          placeholder={busy ? "agent is busy…" : "Ask a question…"}
          disabled={busy}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
        />
        <button onClick={submit} disabled={busy || !text.trim()}>
          ↵
        </button>
      </div>
    </div>
  );
}
