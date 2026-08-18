import React, { useCallback, useEffect, useRef, useState } from "react";
import Chat from "./Chat.jsx";
import Panel from "./Panel.jsx";

// ---- websocket url: same origin in prod, vite proxy in dev -----------------
const WS_URL =
  (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";

let nextId = 1;

export default function App() {
  const [items, setItems] = useState([]); // the chat stream
  const [wf, setWf] = useState(null); // workflow panel snapshot
  const [status, setStatus] = useState({ state: "idle", detail: "" });
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [clip, setClip] = useState(false);
  const wsRef = useRef(null);

  const push = useCallback((item) => {
    setItems((prev) => [...prev, { id: nextId++, ...item }]);
  }, []);

  useEffect(() => {
    let ws;
    let closed = false;
    let retry;

    const connect = () => {
      ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        setBusy(false);
        if (!closed) retry = setTimeout(connect, 1500); // auto-reconnect
      };
      ws.onmessage = (ev) => {
        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        switch (msg.type) {
          case "hello":
            setClip(!!msg.clip);
            push({ kind: "assistant", text: msg.greeting });
            if (msg.clip)
              push({
                kind: "notice",
                text:
                  "Clipboard relay is on: when the status says waiting for " +
                  "clipboard, paste the prompt into Copilot and copy its reply back.",
              });
            break;
          case "user_echo":
            setBusy(true);
            push({ kind: "user", text: msg.text });
            break;
          case "status":
            setStatus({ state: msg.state, detail: msg.detail || "" });
            break;
          case "thought":
            push({ kind: "thought", text: msg.text });
            break;
          case "tool_result":
            push({ kind: "tool_result", name: msg.name, body: msg.body });
            break;
          case "step":
            push({ kind: "step", ...msg });
            break;
          case "approval_request":
            push({ kind: "approval", aid: msg.id, payload: msg.payload });
            break;
          case "rejected":
            push({ kind: "notice", text: `⛔ Rejected: ${msg.command}` });
            break;
          case "workflow":
            setWf(msg.wf);
            break;
          case "final":
            setBusy(false);
            push({
              kind: "final",
              answer: msg.answer,
              report: msg.report,
              offerDeep: msg.offer_deep,
              isDeep: msg.is_deep,
            });
            break;
          case "error":
            setBusy(false);
            push({ kind: "error", text: msg.message });
            break;
          default:
            break;
        }
      };
    };

    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      ws && ws.close();
    };
  }, [push]);

  const send = useCallback((obj) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }, []);

  const sendChat = useCallback(
    (text) => {
      if (!text.trim() || busy) return;
      send({ type: "chat", text: text.trim() });
    },
    [busy, send],
  );

  const answerApproval = useCallback(
    (aid, approved) => {
      send({ type: "approval", id: aid, approved });
      setItems((prev) =>
        prev.map((it) =>
          it.kind === "approval" && it.aid === aid ? { ...it, decided: approved ? "approved" : "rejected" } : it,
        ),
      );
    },
    [send],
  );

  const runDeep = useCallback(() => {
    if (busy) return;
    send({ type: "deep_check" });
  }, [busy, send]);

  const statusPill = () => {
    if (!connected) return <span className="pill warn"><span className="dot" />disconnected</span>;
    if (status.state === "waiting_clipboard")
      return <span className="pill busy"><span className="dot" />waiting for clipboard</span>;
    if (status.state === "approval")
      return <span className="pill warn"><span className="dot" />awaiting approval</span>;
    if (status.state === "executing")
      return <span className="pill busy"><span className="dot" />executing</span>;
    if (status.state === "degraded")
      return <span className="pill warn"><span className="dot" />{status.detail}</span>;
    if (busy) return <span className="pill busy"><span className="dot" />thinking</span>;
    return <span className="pill on"><span className="dot" />ready</span>;
  };

  return (
    <div className="app">
      <div className="header">
        <div className="logo">
          NetOps <span>Agent</span>
        </div>
        {statusPill()}
        {clip && <span className="pill">clipboard relay</span>}
      </div>
      <div className="body">
        <div className="chat-col">
          <Chat
            items={items}
            busy={busy}
            status={status}
            clip={clip}
            onSend={sendChat}
            onApproval={answerApproval}
            onDeep={runDeep}
          />
        </div>
        <div className="panel-col">
          <Panel wf={wf} busy={busy} onRun={sendChat} />
        </div>
      </div>
    </div>
  );
}
