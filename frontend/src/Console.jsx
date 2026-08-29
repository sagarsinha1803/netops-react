import React, { useEffect, useState } from "react";

// ------------------------------------------------------------- command bar --
function CommandBar({ initial, busy, onRun }) {
  const seed = initial || {};
  const [source, setSource] = useState("");
  const [dest, setDest] = useState("");
  const [protocol, setProtocol] = useState("TCP");
  const [port, setPort] = useState("22");

  useEffect(() => {
    if (seed.source) setSource(seed.source);
    if (seed.dest) setDest(seed.dest);
    if (seed.protocol) setProtocol(seed.protocol);
    if (seed.port) setPort(seed.port);
  }, [seed.source, seed.dest, seed.protocol, seed.port]);

  const run = () => {
    if (busy || !source.trim() || !dest.trim()) return;
    onRun(
      `troubleshoot ${source.trim()} to ${dest.trim()} ${protocol} ${String(port).trim() || "22"}`,
    );
  };
  const onKey = (e) => e.key === "Enter" && run();

  return (
    <div className="card cmdbar">
      <div className="field">
        <label>Source</label>
        <input className="src mono" value={source} placeholder="10.10.1.20"
               onChange={(e) => setSource(e.target.value)} onKeyDown={onKey} />
      </div>
      <div className="arrow">→</div>
      <div className="field">
        <label>Destination</label>
        <input className="dst mono" value={dest} placeholder="172.20.5.10"
               onChange={(e) => setDest(e.target.value)} onKeyDown={onKey} />
      </div>
      <div className="field">
        <label>Service</label>
        <select className="proto" value={protocol}
                onChange={(e) => setProtocol(e.target.value)}>
          <option>TCP</option>
          <option>UDP</option>
          <option>HTTP</option>
        </select>
      </div>
      <div className="field">
        <label>Port</label>
        <input className="port mono" value={port}
               onChange={(e) => setPort(e.target.value)} onKeyDown={onKey} />
      </div>
      <button className="run" onClick={run}
              disabled={busy || !source.trim() || !dest.trim()}>
        {busy ? "Running…" : "Run"}
      </button>
    </div>
  );
}

// ------------------------------------------------------------- stage strip --
function StageStrip({ wf }) {
  const steps = (wf && wf.steps) || [];
  if (!steps.length) return null;
  return (
    <div className="card">
      <div className="strip">
        {steps.map((s, i) => (
          <React.Fragment key={s.key}>
            {i > 0 && (
              <div className={`stage-link ${
                ["done", "warn", "failed", "skipped"]
                  .includes(steps[i - 1].status) &&
                s.status !== "pending" ? "done" : ""}`} />
            )}
            <div className="stage" title={s.detail || s.label}>
              {/* amber: the stage ran and the answer was bad news -- a ping
                  with no replies, a trace that never arrived. Red is for a
                  stage that could not be run at all. */}
              <div className={`ic ${s.status}`}>
                {s.status === "done" ? "✓" : s.status === "warn" ? "!"
                  : s.status === "failed" ? "✕"
                    : s.status === "skipped" ? "–" : ""}
              </div>
              <div className={`lbl ${s.status === "running" ? "active" : ""}`}>
                {s.label.replace(" / path discovery", "")
                        .replace("Firewall policy (Tufin)", "Policy")
                        .replace("Basic reachability", "Ping")}
              </div>
            </div>
          </React.Fragment>
        ))}
      </div>
      {wf && wf.cmdbMiss && (
        <div className="substatus">
          <span className="warn">
            ⚠ source not in CMDB — no device to run commands on, so the
            firewall policy is the only check available
          </span>
        </div>
      )}
      {wf && wf.local && (
        <div className="substatus">
          <span className="warn">⚠ probed from the agent machine, not the source</span>
        </div>
      )}
    </div>
  );
}

// -------------------------------------------------------------------- feed --
function FeedRow({ row, showWhy }) {
  const [open, setOpen] = useState(false);
  const canOpen = !!(row.output || row.thought);
  return (
    <div className="row">
      <div className="line" onClick={() => canOpen && setOpen(!open)}>
        {/* A red cross means the device never answered -- it refused the
            command. A command that RAN and came back with bad news (a ping
            with no replies, a table with no route) is amber: the network is
            the problem, not the syntax. */}
        <div className={`st ${row.status}`
          + (row.status === "failed" && row.answered ? " negative" : "")}>
          {row.status === "skipped" ? "–"
            : row.status === "done" ? "✓"
              : row.status === "failed" ? (row.answered ? "!" : "✕") : ""}
        </div>
        <div className="cmd mono">{row.cmd}</div>
        {row.device && <div className="chip">{row.device}</div>}
        {row.detail && <div className="meta">{row.detail}</div>}
      </div>
      {open && (
        <div className="expand">
          {showWhy && row.thought && (
            <div className={`why${row.saidIt ? "" : " step"}`}>
              {/* the model's own words, or -- when it attached none to its
                  tool call -- our description of the step. Labelled, because
                  the feed is evidence and must not put words in its mouth. */}
              {!row.saidIt && <span className="tag">step</span>}
              {row.thought}
            </div>
          )}
          {row.output && <pre className="mono">{row.output}</pre>}
        </div>
      )}
    </div>
  );
}

function Feed({ wf, showWhy, onToggleWhy, waiting }) {
  const basics = (wf && wf.basics) || [];
  const checks = (wf && wf.checks) || [];
  const [collapsed, setCollapsed] = useState(false);
  if (!basics.length && !checks.length && !waiting) return null;
  const count = basics.length + checks.length;
  return (
    <div className="card">
      <div className="feed-head" onClick={() => setCollapsed(!collapsed)}>
        <span className={`chevron ${collapsed ? "" : "open"}`}>▸</span>
        Activity
        {collapsed && count > 0 && (
          <span className="count">{count} command{count === 1 ? "" : "s"}</span>
        )}
        {!collapsed && (
          <button
            className="toggle"
            onClick={(e) => {
              e.stopPropagation();
              onToggleWhy();
            }}
          >
            {showWhy ? "hide reasoning" : "show reasoning"}
          </button>
        )}
      </div>
      {collapsed ? null : (
      <div className="feed">
        {basics.map((r, i) => (
          <React.Fragment key={`b${i}`}>
            <FeedRow row={r} showWhy={showWhy} />
            {showWhy && r.thought && (
              <div className={`row thought${r.saidIt ? "" : " step"}`}>
                <div className="line">
                  <div style={{ width: 16 }} />
                  <div className="cmd">
                    {!r.saidIt && <span className="tag">step</span>}
                    {r.thought}
                  </div>
                </div>
              </div>
            )}
          </React.Fragment>
        ))}
        {checks.length > 0 && (
          <div className="row section">
            <div className="line"><div style={{ width: 16 }} />
              <div className="cmd">Deeper checks</div>
            </div>
          </div>
        )}
        {checks.map((r, i) => (
          <React.Fragment key={`c${i}`}>
            <FeedRow row={r} showWhy={showWhy} />
            {showWhy && r.thought && (
              <div className={`row thought${r.saidIt ? "" : " step"}`}>
                <div className="line">
                  <div style={{ width: 16 }} />
                  <div className="cmd">
                    {!r.saidIt && <span className="tag">step</span>}
                    {r.thought}
                  </div>
                </div>
              </div>
            )}
          </React.Fragment>
        ))}
        {waiting && <WaitLine waiting={waiting} />}
      </div>
      )}
    </div>
  );
}

function WaitLine({ waiting }) {
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    setSecs(0);
    const t = setInterval(() => setSecs((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [waiting]);
  const mins = Math.floor(secs / 60);
  const clock = mins ? `${mins}m ${String(secs % 60).padStart(2, "0")}s` : `${secs}s`;
  return (
    <div className="waitline">
      <div className="spinner" />
      <span>{waiting}</span>
      <span className="clock mono">{clock}</span>
    </div>
  );
}

// ---------------------------------------------------------------- approval --
function Approval({ approval, onApproval }) {
  const p = approval.payload || {};
  return (
    <div className="approval">
      <div className="what">
        <div className="t">Approval required</div>
        <div className="c mono">{String(p.command || "")}</div>
        <div className="w">
          {/* "on ?" told the reviewer nothing about what they were approving,
              and the honest answer to that is always Reject. A tool that names
              no device is not being run on one. */}
          {p.device_ip ? `on ${p.device_ip}` : `${p.tool || "tool"} · no device`}
          {p.region ? ` · ${p.region}` : ""} · read-only, validated in code
        </div>
      </div>
      <button className="ok" onClick={() => onApproval(approval.id, true)}>
        Approve
      </button>
      <button className="no" onClick={() => onApproval(approval.id, false)}>
        Reject
      </button>
    </div>
  );
}

// ------------------------------------------------------------------ report --
const GOOD = new Set(["REACHABLE", "SUCCESS"]);
const BAD = new Set(["NOT REACHABLE", "FAILED"]);

function verdictClass(result) {
  const r = String(result || "").toUpperCase();
  if (GOOD.has(r)) return "ok";
  if (BAD.has(r)) return "bad";
  return "warn";
}

function PathChips({ path, nodes }) {
  // `nodes` carries what each step IS -- a device, the interface it leaves by,
  // silence, a wall. `path` is the older plain-string form, still used by the
  // report's own path field.
  const hops = nodes && nodes.length
    ? nodes
    : (Array.isArray(path) ? path : path ? [path] : []).map((h) => ({
        label: String(h),
        kind: String(h).trim() === "X" ? "dead" : "hop",
      }));
  if (!hops.length) return null;
  return (
    <div className="pathchips">
      {hops.map((h, i) => {
        const last = i === hops.length - 1;
        const label = String(h.label ?? h).trim();
        const dead = label === "X" || h.kind === "dead";
        // "?" is not a device: it is the checks reaching the end of what they
        // can prove. Drawing it as a hop would claim the path continues there.
        const unknown = label === "?" || h.kind === "unknown";
        const cls = h.kind === "intf" ? "intf"
          : i === 0 || h.kind === "source" ? "src"
          : dead ? "dead" : unknown ? "unk"
          : last || h.kind === "dest" ? "dst" : "";
        // A bare cross makes the reader open a tab to find out what broke.
        // When the checks named the blockage, it goes on the chip.
        const text = dead ? `✕ ${h.why || "blocked"}`
          : label === "?" ? "? unconfirmed"
          : label === "hidden hop" ? "· hidden hop"
          : label;
        return (
          <React.Fragment key={i}>
            {i > 0 && <span className="sep">▸</span>}
            <span className={`hop mono ${cls}`} title={h.alt || ""}>
              {text}
              {h.alt && <span className="altmark">alt</span>}
            </span>
          </React.Fragment>
        );
      })}
    </div>
  );
}

// Three instruments answer "where does the traffic go", and they answer
// differently on purpose: the traceroute says what the packets DID, Tufin says
// what the topology and the rules SAY should happen, and the deeper checks say
// what the source's own forwarding table intends to do next. Drawing only the
// traceroute hides the disagreement between them -- and the disagreement is
// usually the finding.
const PATH_VIEWS = [
  { key: "traceroute", label: "Traceroute", sub: "live, from the source" },
  { key: "tufin", label: "Firewall policy", sub: "modelled by SecureTrack" },
  { key: "deep", label: "Deeper checks", sub: "the source's own forwarding" },
];

function PathViews({ paths, fallback, verdictClass }) {
  const have = PATH_VIEWS.filter((v) => paths && paths[v.key] &&
                                 (paths[v.key].nodes || []).length);
  if (!have.length) {
    return (
      <>
        <PathChips path={fallback} />
        <div className="hint" style={{ marginTop: 8 }}>
          {verdictClass === "ok"
            ? "Every hop answered through to the destination."
            : "The chain stops where traffic no longer gets through."}
        </div>
      </>
    );
  }
  return (
    <div className="pathviews">
      {have.map((v) => {
        const p = paths[v.key];
        const nodes = (p.nodes || []).map((n) => ({
          ...n,
          label: n.label + (n.ip ? ` (${n.ip})` : ""),
        }));
        return (
          <div className="pathview" key={v.key}>
            <div className="ph">
              {/* three states, not two: proven, disproven, and not settled */}
              <span className={`pt ${p.reached === true ? "ok"
                : p.reached === false ? "bad" : "warn"}`}>{v.label}</span>
              <span className="ps">{v.sub}</span>
            </div>
            <PathChips nodes={nodes} />
            {p.note && <div className="hint pn">{p.note}</div>}
          </div>
        );
      })}
    </div>
  );
}

// The report's detail, minus the path (which has its own tab). Used for both
// the primary report and the deeper-checks report.
function ReportBody({ report, answer }) {
  const isSchema =
    report && (report.source || report.destination || report.result);
  if (!isSchema) {
    const text = (report && report.text) || answer || "";
    return <div style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>{text}</div>;
  }
  const evidence = Array.isArray(report.evidence)
    ? report.evidence
    : report.evidence ? [report.evidence] : [];
  const skip = (v) => !v || ["none", "-", "n/a"].includes(String(v).toLowerCase());
  const local = /agent host/i.test(String(report.source || ""));
  return (
    <>
      <div className="endpoints">
        <div className="ep">
          <div className="k">Source</div>
          {String(report.source || "?")}
        </div>
        <span className="sep">→</span>
        <div className="ep">
          <div className="k">Destination</div>
          {String(report.destination || "?")}
        </div>
      </div>
      {local && (
        <div className="local-note">
          ⚠ Probed from the agent machine, not the source — this says whether
          THIS host can reach the destination, not whether the source can.
        </div>
      )}
      {!skip(report.cause) && (
        <div className="kv">
          <div className="k">Cause</div>
          {String(report.cause)}
        </div>
      )}
      {!skip(report.next_step) && (
        <div className="kv">
          <div className="k">Next step</div>
          {String(report.next_step)}
        </div>
      )}
      {evidence.length > 0 && (
        <details>
          <summary>Evidence ({evidence.length})</summary>
          <ul>{evidence.map((e, i) => <li key={i}>{String(e)}</li>)}</ul>
        </details>
      )}
    </>
  );
}

const REPORT_TABS = [
  { key: "report", label: "Report" },
  { key: "path", label: "Path" },
  { key: "alerts", label: "Alerts" },
  { key: "deep", label: "Deep" },
];

// Open alerts from Archangel. A table rather than prose: an engineer scans for
// the interface in the path and the ticket to open, and both are easier to find
// in a column than in a sentence.
function AlertsTable({ alerts }) {
  const rows = alerts || [];
  if (!rows.length)
    return (
      <div className="hint">
        No open alerts were returned for these devices — or the devices were
        not in the CMDB, so there was no name to look them up by.
      </div>
    );
  const tickets = [...new Set(rows.map((r) => r.ticket_id).filter(Boolean))];
  return (
    <>
      <div className="hint" style={{ marginBottom: 8 }}>
        {rows.length} open alert{rows.length === 1 ? "" : "s"}
        {tickets.length
          ? ` across ${tickets.length} ticket${tickets.length === 1 ? "" : "s"}`
          : ""}
      </div>
      <div className="table-wrap">
        <table className="alerts">
          <thead>
            <tr>
              <th>Device</th><th>Alert</th><th>Check</th>
              <th>Type</th><th>Ticket</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.alert_id || i} title={r.alert_id ? `alert ${r.alert_id}` : ""}>
                <td className="mono">{r.device_name}</td>
                <td>{r.alert_title}</td>
                <td className="mono">{r.check_name}</td>
                <td>{r.alert_type}</td>
                <td className="mono">{r.ticket_id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Report({ final, busy, deepRunning, cmdbMiss, alerts, paths, onDeep }) {
  const rep = final.report || {};
  // The deeper checks SUPERSEDE the first pass -- that is what they are for.
  // Leaving the banner on the earlier verdict meant a run whose escalation
  // proved the destination reachable still read "not reachable" at the top,
  // with the proof three tabs away.
  const deep = final.deepReport || {};
  // The banner keeps the RUN's verdict -- the one the workflow reached with
  // the request as it was asked. A deeper check that succeeds in another
  // routing context has not made the original request work: the ping the user
  // asked about still failed, and a green "ping success" over it would be a
  // different claim from the one the evidence supports. The revision is shown
  // as its own line instead, and drawn in the Path tab.
  const revised = deep.result && deep.result !== rep.result ? deep.result : "";
  const verdict = rep.result || (rep.text || final.answer ? "ANSWER" : "?");
  const cls = verdictClass(rep.result);
  const showVerdict = rep.result || rep.ping;
  const isSchema = !!(rep.source || rep.destination || rep.result);
  const [tab, setTab] = useState("report");

  // when the deeper checks start, bring their tab forward so the spinner and
  // then the second report are where the eye already is
  useEffect(() => {
    if (deepRunning) setTab("deep");
  }, [deepRunning]);

  // a plain text answer (a chat reply that landed here) has no tabs
  if (!isSchema) {
    return (
      <div className="card report">
        <div className="body">
          <ReportBody report={final.report} answer={final.answer} />
        </div>
      </div>
    );
  }

  const effPath = (final.deepReport && final.deepReport.path) || rep.path;
  const hasDeep = deepRunning || !!final.deepReport || final.offerDeep;

  return (
    <div className="card report">
      {showVerdict && (
        <div className={`verdict-bar ${cls}`}>
          {cls === "ok" ? "✓" : cls === "bad" ? "✕" : "!"} {String(verdict)}
          {rep.ping && (
            <span className="sub">ping {String(rep.ping).toLowerCase()}</span>
          )}
        </div>
      )}
      {revised && (
        <div className={`revised ${verdictClass(revised)}`}>
          Deeper checks reached a different result: <b>{String(revised)}</b>
          {" — see the Path and Deep tabs for what changed."}
        </div>
      )}
      <div className="report-tabs">
        {REPORT_TABS.map((t) => (
          <button
            key={t.key}
            className={tab === t.key ? "on" : ""}
            onClick={() => setTab(t.key)}
          >
            {t.label}
            {t.key === "alerts" && (alerts || []).length > 0 && (
              <span className="n">{(alerts || []).length}</span>
            )}
            {t.key === "deep" && deepRunning && <span className="tab-spin" />}
            {t.key === "deep" && !deepRunning && final.deepReport && (
              <span className="tab-dot" />
            )}
          </button>
        ))}
      </div>
      <div className="body">
        {tab === "report" && (
          <ReportBody report={final.report} answer={final.answer} />
        )}
        {tab === "path" && (
          <PathViews paths={paths} fallback={effPath} verdictClass={cls} />
        )}
        {tab === "alerts" && <AlertsTable alerts={alerts} />}
        {tab === "deep" && (
          <>
            {deepRunning && (
              <div className="deep-running">
                <div className="spinner" />
                <span>Running deeper checks — one command at a time…</span>
              </div>
            )}
            {final.deepReport && !deepRunning && (
              <ReportBody report={final.deepReport} />
            )}
            {!deepRunning && !final.deepReport && (
              <div className="deep-offer">
                <p className="hint">
                  {final.offerDeep
                    ? "Reachability is unconfirmed. Dig into the route, VRF, forwarding entry, next hop, interface state and ACLs — one check at a time."
                    : cmdbMiss
                      ? "Not available: the source is not in the CMDB, so there is no device to run show commands on. The firewall verdict above is all that can be established."
                      : "No deeper checks were run for this result."}
                </p>
                {final.offerDeep && !busy && (
                  <button className="deep-btn" onClick={onDeep}>
                    {/* after a deep turn that settled nothing, the same button
                        carries on down the ladder rather than starting over */}
                    🔎 {final.isDeep ? "Continue deeper checks" : "Run deeper checks"}
                  </button>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------- console --
export default function Console({
  wf, busy, clip, status, approval, final, notice, deepRunning,
  onRun, onApproval, onDeep,
}) {
  const [showWhy, setShowWhy] = useState(false);

  const started = wf && wf.steps && wf.steps.some((s) => s.status !== "pending");
  const hasFeed = wf && ((wf.basics || []).length || (wf.checks || []).length);

  const waiting =
    busy && !approval
      ? status.state === "waiting_clipboard"
        ? "Prompt on clipboard — paste into Copilot, copy the reply back"
        : status.state === "executing"
          ? `Executing ${status.detail}`
          : "Working…"
      : null;

  return (
    <div className="main">
      <div className="col">
        <CommandBar initial={wf && wf.params} busy={busy} onRun={onRun} />
        {!started && !busy && !final && (
          <div className="empty">
            <h2>Is it reachable?</h2>
            <p>
              Source and destination — the agent finds the path, checks the
              policy, and tells you where it breaks. Questions go in the chat.
              {clip ? " Clipboard relay is on: paste prompts into Copilot when asked." : ""}
            </p>
          </div>
        )}
        {started && <StageStrip wf={wf} />}
        {approval && <Approval approval={approval} onApproval={onApproval} />}
        {(hasFeed || waiting) && (
          <Feed wf={wf} showWhy={showWhy} waiting={waiting}
                onToggleWhy={() => setShowWhy(!showWhy)} />
        )}
        {notice && (
          <div className={`notice ${notice.kind === "err" ? "err" : ""}`}>
            {notice.text}
          </div>
        )}
        {final && (
          <Report final={final} busy={busy} deepRunning={deepRunning}
                  cmdbMiss={!!(wf && wf.cmdbMiss)}
                  alerts={(wf && wf.alerts) || []}
                  paths={(wf && wf.paths) || {}} onDeep={onDeep} />
        )}
      </div>
    </div>
  );
}
