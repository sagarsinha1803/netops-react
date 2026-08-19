import React, { useCallback, useEffect, useRef, useState } from "react";
import Console from "./Console.jsx";
import ChatPanel from "./ChatPanel.jsx";
import * as api from "./api.js";

// The whole UI is a view of one run object fetched from the REST API. A run is
// started with POST, followed over SSE, and acted on with POST -- there is no
// state here the backend does not also hold, so a reload never loses a run.
export default function App() {
  const [run, setRun] = useState(null); // the current run, straight from the API
  const [health, setHealth] = useState(null);
  const [notice, setNotice] = useState(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [chatMsgs, setChatMsgs] = useState([]);
  const [theme, setTheme] = useState(
    () => localStorage.getItem("netops-theme") || "dark",
  );
  const unsubRef = useRef(null);
  // which run the chat is waiting on, so its answer lands in the drawer and
  // does not disturb the report on the left
  const chatRunRef = useRef(null);
  // the troubleshooting run whose report is on screen; a question does not
  // replace it, and Run deeper checks extends it
  const reportRunRef = useRef(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("netops-theme", theme);
  }, [theme]);

  // ---- follow a run ------------------------------------------------------
  // `adopt` false for a question: the console keeps showing the run being
  // asked about, and only the question's status is mirrored onto it.
  const follow = useCallback((started, adopt = true) => {
    if (unsubRef.current) unsubRef.current();
    if (adopt) setRun(started);
    else setRun((prev) => (prev ? { ...prev, status: started.status } : started));
    unsubRef.current = api.subscribeToRun(started.id, (fresh) => {
      if (chatRunRef.current === fresh.id) {
        // a question: its answer belongs in the chat drawer
        if (fresh.status === "done") {
          chatRunRef.current = null;
          setChatMsgs((prev) => [
            ...prev,
            { role: "agent", text: fresh.answer || "(no answer)" },
          ]);
          // a lookup/troubleshoot typed as a question still owns the panel
          if (fresh.kind !== "question") setRun(fresh);
          else setRun((prev) => (prev ? { ...prev, status: "done" } : fresh));
          return;
        }
        if (fresh.status === "error") {
          chatRunRef.current = null;
          setChatMsgs((prev) => [
            ...prev,
            { role: "agent", text: `⚠ ${fresh.error || "failed"}` },
          ]);
          setRun((prev) => (prev ? { ...prev, status: "error" } : fresh));
          return;
        }
        setRun((prev) => (prev ? { ...prev, status: fresh.status } : fresh));
        return;
      }
      setRun(fresh);
      if (fresh.status === "error" && fresh.error)
        setNotice({ kind: "err", text: fresh.error });
    });
  }, []);

  // on load: adopt whatever run the backend is already working on, so a
  // refresh mid-run rejoins it instead of showing a blank console
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const h = await api.getHealth();
        if (cancelled) return;
        setHealth(h);
        const runs = await api.listRuns(5);
        if (cancelled || !runs.length) return;
        const live =
          runs.find((r) => !["done", "error"].includes(r.status)) ||
          runs.find((r) => r.kind === "troubleshoot");
        if (!live) return;
        const full = await api.getRun(live.id);
        if (cancelled) return;
        reportRunRef.current = full.id;
        if (["done", "error"].includes(full.status)) setRun(full);
        else follow(full);
      } catch {
        if (!cancelled) setNotice({ kind: "err", text: "backend unreachable" });
      }
    })();
    return () => {
      cancelled = true;
      if (unsubRef.current) unsubRef.current();
    };
  }, [follow]);

  const busy = !!run && !["done", "error"].includes(run.status);

  // ---- actions -----------------------------------------------------------
  const onRun = useCallback(
    async (params) => {
      setNotice(null);
      setRun(null);
      try {
        const started = await api.startRun(params);
        reportRunRef.current = started.id;
        chatRunRef.current = null;
        follow(started);
      } catch (e) {
        setNotice({ kind: "err", text: e.message });
      }
    },
    [follow],
  );

  const onApproval = useCallback(
    async (approvalId, approved) => {
      if (!run) return;
      try {
        await api.answerApproval(run.id, approvalId, approved);
        // clear it locally so the card goes at once; SSE confirms right after
        setRun((prev) => (prev ? { ...prev, pending_approval: null } : prev));
      } catch (e) {
        setNotice({ kind: "err", text: e.message });
      }
    },
    [run],
  );

  const onDeep = useCallback(async () => {
    if (!run) return;
    try {
      const started = await api.startDeep(run.id);
      follow(started);
    } catch (e) {
      setNotice({ kind: "err", text: e.message });
    }
  }, [run, follow]);

  const onAsk = useCallback(
    async (question) => {
      setChatMsgs((prev) => [...prev, { role: "user", text: question }]);
      try {
        const started = await api.ask(question, reportRunRef.current);
        chatRunRef.current = started.id;
        // a lookup or troubleshoot typed into the chat owns the console; a
        // plain question must not wipe the report it is being asked about
        follow(started, started.kind !== "question");
      } catch (e) {
        setChatMsgs((prev) => [...prev, { role: "agent", text: `⚠ ${e.message}` }]);
      }
    },
    [follow],
  );

  // ---- header ------------------------------------------------------------
  const clip = health && health.llm_mode === "clipboard";
  const statusPill = () => {
    if (!health)
      return (
        <span className="pill warn">
          <span className="dot" />
          connecting
        </span>
      );
    if (run && run.pending_approval)
      return (
        <span className="pill warn">
          <span className="dot" />
          awaiting approval
        </span>
      );
    if (run && run.status === "waiting_clipboard")
      return (
        <span className="pill busy">
          <span className="dot" />
          waiting for clipboard
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
        {health && health.mocks && <span className="pill">mock data</span>}
        <div className="spacer" />
        <a className="chat-btn api-link" href="/docs" target="_blank"
           rel="noreferrer" title="Swagger: every endpoint this page uses">
          API
        </a>
        <button
          className={`chat-btn ${chatOpen ? "on" : ""}`}
          title="Ask questions about the current run"
          onClick={() => setChatOpen(!chatOpen)}
        >
          💬 Chat
        </button>
        <button
          className="icon-btn"
          title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
        >
          {theme === "dark" ? "☀" : "🌙"}
        </button>
      </div>
      <div className="body-row">
        <Console
          run={run}
          busy={busy}
          clip={clip}
          notice={notice}
          onRun={onRun}
          onApproval={onApproval}
          onDeep={onDeep}
        />
        {chatOpen && (
          <ChatPanel
            msgs={chatMsgs}
            busy={!!chatRunRef.current}
            clip={clip}
            onAsk={onAsk}
            onClose={() => setChatOpen(false)}
          />
        )}
      </div>
    </div>
  );
}
