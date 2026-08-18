import React, { useEffect, useRef, useState } from "react";

// ---- tiny markdown: escape first, then **bold**, `code`, ``` fences --------
function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function md(text) {
  const src = esc(text);
  const parts = src.split(/```(?:\w*\n)?/);
  let html = "";
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      html += `<pre><code>${part}</code></pre>`;
      return;
    }
    let t = part
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/^[-•]\s+(.+)$/gm, "· $1");
    html += t
      .split(/\n{2,}/)
      .filter((p) => p.trim())
      .map((p) => `<p>${p.replace(/\n/g, "<br/>")}</p>`)
      .join("");
  });
  return { __html: html };
}

const VERDICT = {
  SUCCESS: { icon: "✓", color: "var(--green)" },
  REACHABLE: { icon: "✓", color: "var(--green)" },
  FAILED: { icon: "✕", color: "var(--red)" },
  "NOT REACHABLE": { icon: "✕", color: "var(--red)" },
  INCONCLUSIVE: { icon: "!", color: "var(--amber)" },
  "NOT RUN": { icon: "–", color: "var(--gray)" },
};

function Verdict({ value }) {
  const v = VERDICT[String(value || "").toUpperCase()] || {
    icon: "•",
    color: "var(--dim)",
  };
  return (
    <span className="verdict">
      <span className="ic" style={{ background: v.color }}>
        {v.icon}
      </span>
      {String(value)}
    </span>
  );
}

// ---- the structured troubleshooting report ---------------------------------
function ReportCard({ report, answer }) {
  const isSchema =
    report && (report.source || report.destination || report.result || report.ping);
  if (!isSchema) {
    const body = report && report.text ? report.text : answer;
    return <div className="md" dangerouslySetInnerHTML={md(body)} />;
  }
  const chain = Array.isArray(report.path)
    ? report.path.join("  →  ")
    : report.path;
  const evidence = Array.isArray(report.evidence)
    ? report.evidence
    : report.evidence
      ? [report.evidence]
      : [];
  const skip = (v) =>
    !v || ["none", "-", "n/a"].includes(String(v).toLowerCase());
  const local = /agent host/i.test(String(report.source || ""));
  return (
    <div className="report">
      <div className="grid">
        {report.source && (
          <>
            <span className="k">Source</span>
            <span>{report.source}</span>
          </>
        )}
        {report.destination && (
          <>
            <span className="k">Destination</span>
            <span>{report.destination}</span>
          </>
        )}
        {report.ping && (
          <>
            <span className="k">Ping</span>
            <Verdict value={report.ping} />
          </>
        )}
        {report.result && (
          <>
            <span className="k">Result</span>
            <Verdict value={report.result} />
          </>
        )}
      </div>
      {local && (
        <div className="local-note">
          ⚠ Probed from the agent machine — the source is not in the CMDB, so
          policy on the real path is unverified.
        </div>
      )}
      {chain && <div className="chain">{chain}</div>}
      {evidence.length > 0 && (
        <div>
          <div className="lbl">Evidence</div>
          <ul>
            {evidence.map((e, i) => (
              <li key={i}>{String(e)}</li>
            ))}
          </ul>
        </div>
      )}
      {!skip(report.cause) && (
        <div>
          <div className="lbl">Cause</div>
          <div>{String(report.cause)}</div>
        </div>
      )}
      {!skip(report.next_step) && (
        <div>
          <div className="lbl">Next step</div>
          <div>{String(report.next_step)}</div>
        </div>
      )}
    </div>
  );
}

function Approval({ item, onApproval }) {
  const p = item.payload || {};
  const decided = item.decided;
  return (
    <div className="approval">
      <h4>Device command needs your approval</h4>
      <table>
        <tbody>
          <tr>
            <td>Device</td>
            <td className="v">{String(p.device_ip || "?")}</td>
          </tr>
          {p.region && (
            <tr>
              <td>Region</td>
              <td className="v">{String(p.region)}</td>
            </tr>
          )}
          <tr>
            <td>Command</td>
            <td className="v">{String(p.command || "")}</td>
          </tr>
        </tbody>
      </table>
      {!decided ? (
        <div className="row">
          <button className="ok" onClick={() => onApproval(item.aid, true)}>
            ✓ Approve
          </button>
          <button className="no" onClick={() => onApproval(item.aid, false)}>
            ✕ Reject
          </button>
        </div>
      ) : (
        <div className="decided">
          {decided === "approved" ? "✓ approved" : "✕ rejected"}
        </div>
      )}
    </div>
  );
}

function WaitBanner({ status, clip }) {
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    setSecs(0);
    const t = setInterval(() => setSecs((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [status.state]);
  const mins = Math.floor(secs / 60);
  const clock = mins ? `${mins}m ${String(secs % 60).padStart(2, "0")}s` : `${secs}s`;
  const label =
    status.state === "waiting_clipboard"
      ? "Prompt is on your clipboard — paste it into Copilot, then copy the reply."
      : status.state === "executing"
        ? `Executing: ${status.detail}`
        : clip
          ? "Working…"
          : "Thinking…";
  return (
    <div className="banner">
      <div className="spinner" />
      <span>{label}</span>
      <span className="clock">⏱ {clock}</span>
    </div>
  );
}

export default function Chat({ items, busy, status, clip, onSend, onApproval, onDeep }) {
  const [text, setText] = useState("");
  const scrollRef = useRef(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items, busy, status.state]);

  const submit = () => {
    if (!text.trim() || busy) return;
    onSend(text);
    setText("");
  };

  const showBanner =
    busy && ["waiting_clipboard", "executing", "thinking", "idle", "degraded"].includes(status.state) &&
    status.state !== "approval";

  return (
    <>
      <div className="chat-scroll" ref={scrollRef}>
        <div className="chat-inner">
          {items.map((it) => {
            switch (it.kind) {
              case "user":
                return (
                  <div key={it.id} className="msg user">
                    {it.text}
                  </div>
                );
              case "assistant":
                return (
                  <div key={it.id} className="msg assistant">
                    <div className="md" dangerouslySetInnerHTML={md(it.text)} />
                  </div>
                );
              case "notice":
                return (
                  <div key={it.id} className="msg notice">
                    {it.text}
                  </div>
                );
              case "error":
                return (
                  <div key={it.id} className="msg error">
                    ⚠ {it.text}
                  </div>
                );
              case "thought":
                return (
                  <details key={it.id} className="fold">
                    <summary>reasoning</summary>
                    <div className="fold-body">{it.text}</div>
                  </details>
                );
              case "tool_result":
                return (
                  <details key={it.id} className="fold">
                    <summary>{it.name} · result</summary>
                    <div className="fold-body">
                      <pre>{it.body || "(empty)"}</pre>
                    </div>
                  </details>
                );
              case "step":
                return (
                  <div key={it.id} className="step-card">
                    <div className="head">
                      <b>Step {it.no}</b> · {it.where}
                      {it.region ? ` (${it.region})` : ""} ·{" "}
                      <code>{it.command}</code>
                    </div>
                    <pre>{it.output}</pre>
                  </div>
                );
              case "approval":
                return <Approval key={it.id} item={it} onApproval={onApproval} />;
              case "final":
                return (
                  <React.Fragment key={it.id}>
                    <div className="msg assistant">
                      <ReportCard report={it.report} answer={it.answer} />
                    </div>
                    {it.offerDeep && (
                      <div className="offer">
                        <button onClick={onDeep} disabled={busy}>
                          🔎 Run deeper checks
                        </button>
                      </div>
                    )}
                  </React.Fragment>
                );
              default:
                return null;
            }
          })}
          {showBanner && <WaitBanner status={status} clip={clip} />}
        </div>
      </div>
      <div className="composer">
        <div className="inner">
          <input
            value={text}
            placeholder="troubleshoot 10.10.1.20 to 172.20.5.10"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />
          <button onClick={submit} disabled={busy || !text.trim()}>
            Send
          </button>
        </div>
      </div>
    </>
  );
}
