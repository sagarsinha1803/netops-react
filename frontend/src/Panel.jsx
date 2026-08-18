import React, { useEffect, useState } from "react";

// ---- run form ---------------------------------------------------------------
function RunForm({ initial, busy, onRun }) {
  const seed = initial || {};
  const [source, setSource] = useState(seed.source || "");
  const [dest, setDest] = useState(seed.dest || "");
  const [protocol, setProtocol] = useState(seed.protocol || "TCP");
  const [port, setPort] = useState(seed.port || "22");
  const [err, setErr] = useState("");

  // a request typed in the chat reflects back into the form
  useEffect(() => {
    if (seed.source) setSource(seed.source);
    if (seed.dest) setDest(seed.dest);
    if (seed.protocol) setProtocol(seed.protocol);
    if (seed.port) setPort(seed.port);
  }, [seed.source, seed.dest, seed.protocol, seed.port]);

  const run = () => {
    if (busy) return;
    if (!source.trim() || !dest.trim()) {
      setErr("Source and destination are both required");
      return;
    }
    setErr("");
    onRun(
      `troubleshoot ${source.trim()} to ${dest.trim()} ${protocol} ${String(port).trim()}`,
    );
  };

  const onKey = (e) => e.key === "Enter" && run();

  return (
    <div className="form">
      <div className="row">
        <div className="field">
          <label>Source IP / device</label>
          <input value={source} placeholder="10.10.1.20" onKeyDown={onKey}
                 onChange={(e) => setSource(e.target.value)} />
        </div>
        <div className="field">
          <label>Destination IP / device</label>
          <input value={dest} placeholder="172.20.5.10" onKeyDown={onKey}
                 onChange={(e) => setDest(e.target.value)} />
        </div>
      </div>
      <div className="row">
        <div className="field">
          <label>Protocol</label>
          <select value={protocol} onChange={(e) => setProtocol(e.target.value)}>
            <option>TCP</option>
            <option>UDP</option>
            <option>HTTP</option>
          </select>
        </div>
        <div className="field">
          <label>Port</label>
          <input value={port} placeholder="22" onKeyDown={onKey}
                 onChange={(e) => setPort(e.target.value)} />
        </div>
      </div>
      <button onClick={run} disabled={busy}>
        {busy ? "Running…" : "Run troubleshooting"}
      </button>
      {err && <div style={{ color: "var(--red)", fontSize: 12 }}>{err}</div>}
    </div>
  );
}

// ---- timeline ---------------------------------------------------------------
const ICON = { done: "✓", failed: "✕", skipped: "–" };

function Timeline({ steps }) {
  const runningIdx = steps.findIndex((s) => s.status === "running");
  return (
    <div className="tl">
      {steps.map((s, i) => {
        const last = i === steps.length - 1;
        const lineClass =
          i + 1 === runningIdx
            ? "tl-line active"
            : ["done", "failed", "skipped"].includes(s.status) &&
                steps[i + 1] &&
                steps[i + 1].status !== "pending"
              ? "tl-line complete"
              : "tl-line";
        return (
          <div className="tl-row" key={s.key}>
            <div className="tl-rail">
              <div className={`tl-icon ${s.status}`}>{ICON[s.status] || ""}</div>
              {!last && <div className={lineClass} />}
            </div>
            <div className="tl-text">
              <div className={`t ${s.status === "pending" ? "dim" : ""}`}>
                {s.label}
              </div>
              {s.detail && <div className="d">{s.detail}</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ---- report tab -------------------------------------------------------------
const VERDICT = {
  SUCCESS: { icon: "✓", color: "var(--green)" },
  REACHABLE: { icon: "✓", color: "var(--green)" },
  FAILED: { icon: "✕", color: "var(--red)" },
  "NOT REACHABLE": { icon: "✕", color: "var(--red)" },
  INCONCLUSIVE: { icon: "!", color: "var(--amber)" },
  "NOT RUN": { icon: "–", color: "var(--gray)" },
};

function ReportBody({ report }) {
  if (!report) return <div className="hint">No report yet.</div>;
  if (report.text && !report.result)
    return <div style={{ whiteSpace: "pre-wrap" }}>{report.text}</div>;
  const chain = Array.isArray(report.path) ? report.path.join("  →  ") : report.path;
  const evidence = Array.isArray(report.evidence)
    ? report.evidence
    : report.evidence
      ? [report.evidence]
      : [];
  const skip = (v) => !v || ["none", "-", "n/a"].includes(String(v).toLowerCase());
  const V = ({ value }) => {
    const v = VERDICT[String(value || "").toUpperCase()] || { icon: "•", color: "var(--dim)" };
    return (
      <span className="verdict">
        <span className="ic" style={{ background: v.color }}>{v.icon}</span>
        {String(value)}
      </span>
    );
  };
  return (
    <div className="report">
      <div className="grid">
        {report.source && (<><span className="k">Source</span><span>{report.source}</span></>)}
        {report.destination && (<><span className="k">Destination</span><span>{report.destination}</span></>)}
        {report.ping && (<><span className="k">Ping</span><V value={report.ping} /></>)}
        {report.result && (<><span className="k">Result</span><V value={report.result} /></>)}
      </div>
      {chain && <div className="chain">{chain}</div>}
      {evidence.length > 0 && (
        <div>
          <div className="lbl">Evidence</div>
          <ul>{evidence.map((e, i) => <li key={i}>{String(e)}</li>)}</ul>
        </div>
      )}
      {!skip(report.cause) && (
        <div><div className="lbl">Cause</div><div>{String(report.cause)}</div></div>
      )}
      {!skip(report.next_step) && (
        <div><div className="lbl">Next step</div><div>{String(report.next_step)}</div></div>
      )}
    </div>
  );
}

// ---- basic / deep rows ------------------------------------------------------
function CmdRow({ row }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="cmd-row">
      <div className="head" onClick={() => setOpen(!open)}>
        <div className={`ic ${row.status}`}>
          {row.status === "done" ? "✓" : row.status === "failed" ? "✕" : ""}
        </div>
        <div className="cmd">{row.cmd}</div>
        {row.device && <div className="where">{row.device}</div>}
      </div>
      {row.detail && <div className="detail">{row.detail}</div>}
      {open && row.thought && <div className="thought">{row.thought}</div>}
    </div>
  );
}

// ---- path -------------------------------------------------------------------
function PathView({ path }) {
  const nodes = (path && path.nodes) || [];
  if (!nodes.length)
    return <div className="hint">No path yet — run a troubleshooting request.</div>;
  return (
    <div>
      {nodes.map((n, i) => (
        <div className="path-node" key={i}>
          <div className="path-rail">
            <div className={`path-dot ${n.kind}`} />
            {i < nodes.length - 1 && <div className="path-line" />}
          </div>
          <div className="path-text">
            <div className="t">{n.label}</div>
            {n.ip && <div className="ip">{n.ip}</div>}
          </div>
        </div>
      ))}
      {path.truncated && (
        <div className="hint">Path is truncated: hops beyond this never answered.</div>
      )}
    </div>
  );
}

// ---- the panel --------------------------------------------------------------
const TABS = [
  { key: "report", label: "Report" },
  { key: "basic", label: "Basic" },
  { key: "deep", label: "Deep" },
  { key: "path", label: "Path" },
];

export default function Panel({ wf, busy, onRun }) {
  const [tab, setTab] = useState("basic");
  const [touched, setTouched] = useState(false);

  const steps = (wf && wf.steps) || [];
  const basics = (wf && wf.basics) || [];
  const checks = (wf && wf.checks) || [];
  const report = wf && wf.report;
  const deepReport = wf && wf.deepReport;
  const path = wf && wf.path;
  const summary = (wf && wf.summary) || {};

  // jump to the freshest content unless the user picked a tab themselves
  useEffect(() => {
    if (touched) return;
    if (report || deepReport) setTab("report");
    else if (checks.length) setTab("deep");
    else setTab("basic");
  }, [report, deepReport, checks.length, touched]);

  const total = steps.length || 1;
  const finished = steps.filter((s) =>
    ["done", "failed", "skipped"].includes(s.status),
  ).length;
  const pct = Math.round((finished / total) * 100);

  const counts = {
    basic: basics.length,
    deep: checks.length,
    path: path && path.nodes ? path.nodes.length : 0,
    report: (report ? 1 : 0) + (deepReport ? 1 : 0),
  };

  return (
    <div className="panel">
      <h3>{(wf && wf.title) || "Guided troubleshooting"}</h3>
      <RunForm initial={wf && wf.params} busy={busy} onRun={onRun} />
      <div className="progress">
        <div className="fill" style={{ width: `${pct}%` }} />
      </div>
      <Timeline steps={steps} />
      {wf && wf.local && (
        <div className="local-note" style={{ marginBottom: 10 }}>
          ⚠ Source not in CMDB — probing from the agent machine.
        </div>
      )}
      <div className="tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={tab === t.key ? "on" : ""}
            onClick={() => {
              setTab(t.key);
              setTouched(true);
            }}
          >
            {t.label}
            {counts[t.key] > 0 && <span className="n">{counts[t.key]}</span>}
          </button>
        ))}
      </div>
      <div className="tab-body">
        {tab === "report" && (
          <>
            <ReportBody report={report} />
            {deepReport && (
              <>
                <div className="lbl" style={{ margin: "14px 0 6px" }}>
                  After deeper checks
                </div>
                <ReportBody report={deepReport} />
              </>
            )}
          </>
        )}
        {tab === "basic" && (
          <>
            {basics.length === 0 && (
              <div className="hint">The standard workflow's commands appear here.</div>
            )}
            {basics.map((b, i) => (
              <CmdRow key={i} row={b} />
            ))}
            {summary.path && (
              <div className="hint" style={{ marginTop: 8 }}>
                Verified independently of the model: {summary.path}
              </div>
            )}
          </>
        )}
        {tab === "deep" && (
          <>
            {checks.length === 0 && (
              <div className="hint">
                Deeper checks appear here when you run them.
              </div>
            )}
            {checks.map((c, i) => (
              <CmdRow key={i} row={c} />
            ))}
          </>
        )}
        {tab === "path" && <PathView path={path} />}
      </div>
    </div>
  );
}
