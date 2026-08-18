import React, { useCallback, useEffect, useRef, useState } from "react";
import Console from "./Console.jsx";

const WS_URL =
  (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";

export default function App() {
  const [wf, setWf] = useState(null);
  const [status, setStatus] = useState({ state: "idle", detail: "" });
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [clip, setClip] = useState(false);
  const [approval, setApproval] = useState(null); // {id, payload}
  const [final, setFinal] = useState(null); // {report, deepReport, offerDeep, answer}
  const [notice, setNotice] = useState(null); // {kind, text}
  const [query, setQuery] = useState(null); // what the current run is about
  const [theme, setTheme] = useState(
    () => localStorage.getItem("netops-theme") || "dark",
  );
  const wsRef = useRef(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("netops-theme", theme);
  }, [theme]);

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
        if (!closed) retry = setTimeout(connect, 1500);
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
            break;
          case "user_echo": {
            setBusy(true);
            setNotice(null);
            setApproval(null);
            const deep = msg.text === "Run deeper checks";
            setQuery(msg.text);
            if (!deep) setFinal(null); // a deep turn extends the current report
            break;
          }
          case "status":
            setStatus({ state: msg.state, detail: msg.detail || "" });
            if (msg.state === "degraded")
              setNotice({ kind: "warn", text: msg.detail });
            break;
          case "workflow":
            setWf(msg.wf);
            break;
          case "approval_request":
            setApproval({ id: msg.id, payload: msg.payload });
            break;
          case "rejected":
            setNotice({ kind: "info", text: `Rejected: ${msg.command}` });
            break;
          case "final":
            setBusy(false);
            setApproval(null);
            setFinal((prev) =>
              msg.is_deep
                ? { ...(prev || {}), deepReport: msg.report, offerDeep: false }
                : {
                    report: msg.report,
                    answer: msg.answer,
                    offerDeep: msg.offer_deep,
                    deepReport: null,
                  },
            );
            break;
          case "error":
            setBusy(false);
            setApproval(null);
            setNotice({ kind: "err", text: msg.message });
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
  }, []);

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
    (id, approved) => {
      send({ type: "approval", id, approved });
      setApproval(null);
    },
    [send],
  );

  const runDeep = useCallback(() => {
    if (busy) return;
    send({ type: "deep_check" });
  }, [busy, send]);

  const statusPill = () => {
    if (!connected)
      return (
        <span className="pill warn">
          <span className="dot" />
          disconnected
        </span>
      );
    if (status.state === "waiting_clipboard")
      return (
        <span className="pill busy">
          <span className="dot" />
          waiting for clipboard
        </span>
      );
    if (approval)
      return (
        <span className="pill warn">
          <span className="dot" />
          awaiting approval
        </span>
      );
    if (status.state === "executing")
      return (
        <span className="pill busy">
          <span className="dot" />
          executing
        </span>
      );
    if (busy)
      return (
        <span className="pill busy">
          <span className="dot" />
          working
        </span>
      );
    return (
      <span className="pill on">
        <span className="dot" />
        ready
      </span>
    );
  };

  return (
    <div className="app">
      <div className="header">
        <div className="logo">
          NetOps <span>Agent</span>
        </div>
        {statusPill()}
        {clip && <span className="pill">clipboard relay</span>}
        <div className="spacer" />
        <button
          className="icon-btn"
          title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
        >
          {theme === "dark" ? "☀" : "🌙"}
        </button>
      </div>
      <Console
        wf={wf}
        busy={busy}
        clip={clip}
        status={status}
        approval={approval}
        final={final}
        notice={notice}
        query={query}
        onRun={sendChat}
        onApproval={answerApproval}
        onDeep={runDeep}
      />
    </div>
  );
}
