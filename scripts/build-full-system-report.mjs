#!/usr/bin/env node

import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const [inputArg, outputArg] = process.argv.slice(2);
if (!inputArg || !outputArg || process.argv.length > 4) {
  throw new Error(
    "Usage: node scripts/build-full-system-report.mjs input.md output.html",
  );
}

const inputPath = path.resolve(process.cwd(), inputArg);
const outputPath = path.resolve(process.cwd(), outputArg);
const markdown = await readFile(inputPath, "utf8");

const escapeHtml = (value) =>
  value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");

const inline = (value) =>
  escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');

function markdownToHtml(source) {
  const lines = source.split(/\r?\n/);
  const out = [];
  let index = 0;
  let inCode = false;
  let code = [];
  let list = null;

  const closeList = () => {
    if (list) out.push(`</${list}>`);
    list = null;
  };

  while (index < lines.length) {
    const line = lines[index];
    if (line.startsWith("```")) {
      closeList();
      if (!inCode) {
        inCode = true;
        code = [];
      } else {
        const content = escapeHtml(code.join("\n"));
        const diagram = /[-─>|→┌┐└┘]/.test(code.join("\n"));
        out.push(
          diagram
            ? `<div class="text-diagram"><pre>${content}</pre></div>`
            : `<pre class="code-block">${content}</pre>`,
        );
        inCode = false;
      }
      index += 1;
      continue;
    }
    if (inCode) {
      code.push(line);
      index += 1;
      continue;
    }
    if (/^\|.*\|$/.test(line) && /^\|[\s:|-]+\|$/.test(lines[index + 1] || "")) {
      closeList();
      const headers = line.slice(1, -1).split("|").map((cell) => cell.trim());
      index += 2;
      const rows = [];
      while (/^\|.*\|$/.test(lines[index] || "")) {
        rows.push(lines[index].slice(1, -1).split("|").map((cell) => cell.trim()));
        index += 1;
      }
      out.push("<table><thead><tr>");
      for (const header of headers) out.push(`<th>${inline(header)}</th>`);
      out.push("</tr></thead><tbody>");
      for (const row of rows) {
        out.push("<tr>");
        for (const cell of row) out.push(`<td>${inline(cell)}</td>`);
        out.push("</tr>");
      }
      out.push("</tbody></table>");
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      closeList();
      const level = heading[1].length;
      const text = heading[2];
      const risk = level === 3 && /^\d+\.\s/.test(text);
      const critical = /CRITICAL/i.test(text);
      const cls = risk ? "risk-title" : critical ? "critical-title" : "";
      out.push(
        `<h${level}${cls ? ` class="${cls}"` : ""}>${inline(text)}</h${level}>`,
      );
      index += 1;
      continue;
    }
    const item = /^(\s*)([-*]|\d+\.)\s+(.*)$/.exec(line);
    if (item) {
      const nextList = /\d+\./.test(item[2]) ? "ol" : "ul";
      if (list !== nextList) {
        closeList();
        list = nextList;
        out.push(`<${list}>`);
      }
      const checked = /^\[[ xX]\]\s/.test(item[3]);
      const text = checked
        ? `${/^\[[xX]\]/.test(item[3]) ? "☑" : "☐"} ${item[3].slice(4)}`
        : item[3];
      out.push(`<li>${inline(text)}</li>`);
      index += 1;
      continue;
    }
    if (!line.trim()) {
      closeList();
      index += 1;
      continue;
    }
    closeList();
    const paragraph = [line.trim()];
    while (
      lines[index + 1] &&
      !/^(#{1,4})\s+/.test(lines[index + 1]) &&
      !/^(\s*)([-*]|\d+\.)\s+/.test(lines[index + 1]) &&
      !lines[index + 1].startsWith("```") &&
      !/^\|.*\|$/.test(lines[index + 1])
    ) {
      paragraph.push(lines[index + 1].trim());
      index += 1;
    }
    out.push(`<p>${inline(paragraph.join(" "))}</p>`);
    index += 1;
  }
  closeList();
  return out.join("\n");
}

const node = (label, tone = "") =>
  `<div class="node ${tone}">${label}</div>`;
const arrow = (label = "→") => `<div class="arrow">${label}</div>`;
const flow = (...items) => `<div class="flow">${items.join("")}</div>`;
const diagram = (title, subtitle, body) => `
  <article class="diagram">
    <div class="diagram-head"><strong>${title}</strong><span>${subtitle}</span></div>
    ${body}
  </article>`;

const visualAtlas = `
<section class="page atlas">
  <h2>Visual system atlas</h2>
  <p class="section-lede">Twelve views connect service topology, trust, data flow, consistency, retention, recovery, and the performance bottleneck → child map. Blue nodes are application-controlled; amber nodes are external or capacity-sensitive; red nodes are explicit failure boundaries.</p>
  ${diagram(
    "1 · Complete topology",
    "current production-confirmed service shape",
    flow(
      node("Indexer UI"),
      arrow(),
      node("Backend<br>API + indexer", "primary"),
      arrow(),
      node("Postgres"),
      arrow("↔"),
      node("Qdrant"),
    ) +
      flow(
        node("Carousel<br>Next.js"),
        arrow(),
        node("Backend", "primary"),
        arrow("↔"),
        node("Volume<br>/app/data", "risk"),
        arrow("↔"),
        node("Google + LLMs", "external"),
      ),
  )}
  ${diagram(
    "2 · Trust boundaries",
    "credentials, content, and derived intelligence",
    `<div class="zones">
      <div class="zone"><b>User/browser</b><small>queries · previews · Picker token · edits</small></div>
      <div class="zone primary"><b>Application trust</b><small>frontends · API · workers · policy enforcement</small></div>
      <div class="zone"><b>Data trust</b><small>Postgres tokens/metadata · Qdrant vectors · media bytes</small></div>
      <div class="zone external"><b>Provider trust</b><small>Google · Gemini · Claude · OpenRouter · YouTube</small></div>
    </div><div class="boundary-note">Every crossing needs identity, least privilege, provenance, retention, and audit controls.</div>`,
  )}
</section>

<section class="page atlas">
  ${diagram(
    "3 · Google Drive sync and index flow",
    "OAuth through permanent library",
    flow(
      node("OAuth + Picker", "external"),
      arrow(),
      node("DriveUser +<br>IndexedFolder"),
      arrow(),
      node("Push / fallback sync"),
      arrow(),
      node("DriveFile inventory", "primary"),
    ) +
      flow(
        node("Claim + lease"),
        arrow(),
        node("Image / video stage"),
        arrow(),
        node("Media + vectors"),
        arrow(),
        node("Permanent library", "primary"),
      ),
  )}
  ${diagram(
    "4 · Image pipeline",
    "durable bytes to multimodal retrieval",
    flow(
      node("Cache + disk gate"),
      arrow(),
      node("Decode / InsightFace", "risk"),
      arrow(),
      node("Faces · body · thumbs"),
      arrow(),
      node("Caption + embed", "external"),
      arrow(),
      node("Qdrant + status", "primary"),
    ),
  )}
  ${diagram(
    "5 · Video pipeline",
    "Drive, uploads, and retained YouTube converge",
    `<div class="sources">${node("Drive<br>reproducible")}${node(
      "Upload<br>app-owned",
      "risk",
    )}${node("YouTube<br>retained", "risk")}</div>${flow(
      node("Local media / stream"),
      arrow(),
      node("Captions / ASR"),
      arrow(),
      node("ffmpeg frames", "risk"),
      arrow(),
      node("VLM + face + embed", "external"),
      arrow(),
      node("Segments + vectors", "primary"),
    )}`,
  )}
</section>

<section class="page atlas">
  ${diagram(
    "6 · Search and RAG",
    "hybrid retrieval and evidence-aware ranking",
    flow(
      node("Query + context"),
      arrow(),
      node("Expand + embed", "external"),
      arrow(),
      node("Parallel retrieval", "primary"),
    ) +
      `<div class="branches">${node("image vectors")}${node(
        "caption vectors",
      )}${node("frame vectors")}${node("transcript vectors")}${node(
        "SQL / regex",
      )}${node("faces")}</div>` +
      flow(
        node("Fuse + dedupe"),
        arrow(),
        node("threshold / rerank", "external"),
        arrow(),
        node("grounded results", "primary"),
      ),
  )}
  ${diagram(
    "7 · Carousel and run-wide LLM flow",
    "one immutable provider/model route per run",
    flow(
      node("Video + transcript"),
      arrow(),
      node("Themes"),
      arrow(),
      node("Topics + hooks"),
      arrow(),
      node("Copy"),
      arrow(),
      node("Frames"),
      arrow(),
      node("Finalize", "primary"),
    ) +
      `<div class="rail">provider + model + transcript hash + prompt/algorithm version → generation provenance/cache key</div>`,
  )}
</section>

<section class="page atlas">
  ${diagram(
    "8 · Data consistency flow",
    "Postgres authority with derived asynchronous indexes",
    `<div class="consistency">
      ${node("Postgres<br>source + workflow", "primary")}
      <div class="outbox">transactional outbox<br><span>target</span></div>
      ${node("Qdrant<br>derived vectors")}
      ${node("Media store<br>bytes + artifacts")}
    </div>
    <div class="reconcile">daily reconciler: expected artifact version ↔ vector payload ↔ media checksum ↔ active lease</div>`,
  )}
  ${diagram(
    "9 · Current versus target execution",
    "separate failure domains and durable work",
    `<div class="compare">
      <div><b>Current</b>${flow(
        node("API + indexer + maintenance", "risk"),
        arrow(),
        node("local queues"),
      )}<small>shared CPU, DB, disk, process lifetime</small></div>
      <div><b>Target</b>${flow(
        node("API", "primary"),
        arrow(),
        node("durable queue"),
        arrow(),
        node("worker pools", "primary"),
      )}<small>leases, fencing, independent scaling and restart</small></div>
    </div>`,
  )}
</section>

<section class="page atlas">
  ${diagram(
    "10 · Media retention lifecycle",
    "source-aware policy, never filename-only deletion",
    flow(
      node("Discover / upload"),
      arrow(),
      node("Classify source"),
      arrow(),
      node("Active lease?"),
      arrow(),
      node("Reproducible?"),
      arrow(),
      node("Backup + policy"),
    ) +
      `<div class="branches">${node("KEEP<br>upload / YouTube")}${node(
        "AUTO-UNLINK<br>Drive processed + Media",
        "primary",
      )}${node("AUTO-UNLINK<br>Drive ERROR / no-Media", "risk")}${node(
        "KEEP<br>PROCESSING / orphan",
        "external",
      )}</div>` +
      `<div class="rail danger">CRITICAL: leftover Drive downloads must auto-clean after index. Manual cleanup is not a control. These leftovers filled the volume and caused ENOSPC.</div>`,
  )}
  ${diagram(
    "11 · Failure containment and recovery",
    "break amplification before replay",
    flow(
      node("Detect + classify", "risk"),
      arrow(),
      node("Close resource circuit"),
      arrow(),
      node("Persist error / lease"),
      arrow(),
      node("Reconcile"),
      arrow(),
      node("Canary replay"),
      arrow(),
      node("Reopen", "primary"),
    ) +
      `<div class="rail danger">ENOSPC/auth/quota are not generic transient retries. Preserve source relations and isolate unaffected reads.</div>`,
  )}
</section>

<section class="page atlas">
  ${diagram(
    "12 · Performance bottleneck → child map",
    "ranked by what saturates first under load",
    `<div class="zones">
      <div class="zone risk"><b>Disk volume</b><small>retry storms · partials · upload/YouTube · health hide</small></div>
      <div class="zone risk"><b>API + indexer process</b><small>CPU into API latency · health lie · startup 503</small></div>
      <div class="zone risk"><b>Process-local queues</b><small>status-batch lag · claim races · sync LLM · no DLQ</small></div>
      <div class="zone external"><b>LLM quota</b><small>timeouts · spend suspension · cache waste · /test</small></div>
    </div>
    <div class="zones" style="margin-top:3mm">
      <div class="zone"><b>DB pool</b><small>over-admit vs 5+5 connections</small></div>
      <div class="zone"><b>Media buffering</b><small>Next proxy · deploy drift · missing range tests</small></div>
      <div class="zone"><b>PG / Qdrant gap</b><small>orphan vectors · timestamps · relevance</small></div>
      <div class="zone"><b>Volume durability</b><small>backup co-fail · irreplaceable sources</small></div>
    </div>
    <div class="rail danger">CRITICAL required control on #1: auto-unlink leftover Drive downloads after PROCESSED+Media and after ERROR/no-Media. 12 bottlenecks · 26 children.</div>`,
  )}
</section>`;

const content = markdownToHtml(markdown);
const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Full System Architecture and Risk Review</title>
<style>
:root{--ink:#162132;--muted:#5a6676;--line:#d6dde6;--soft:#f4f7fa;--blue:#174e9b;--blue2:#eaf2ff;--red:#9f2432;--red2:#fff0f1;--amber:#835800;--amber2:#fff6dc;--green:#176641;--green2:#eaf7f0;--white:#fff}
*{box-sizing:border-box}html{font-size:8.7pt}body{margin:0;color:var(--ink);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;line-height:1.38;background:#fff}
@page{size:A4;margin:15mm 14mm 17mm;@top-left{content:"FULL SYSTEM ARCHITECTURE & RISK REVIEW";font-size:7pt;color:#6a7481;letter-spacing:.06em}@top-right{content:"14 AUGUST 2026";font-size:7pt;color:#6a7481}@bottom-left{content:"Video / Image Search Indexer";font-size:7pt;color:#6a7481}@bottom-right{content:"PAGE " counter(page) " OF " counter(pages);font-size:7pt;color:#6a7481}}
main{max-width:182mm;margin:auto}.cover{min-height:260mm;display:flex;flex-direction:column;justify-content:space-between;break-after:page}.eyebrow{font-weight:750;letter-spacing:.14em;text-transform:uppercase;color:var(--blue);font-size:9pt;margin-top:20mm}.cover h1{font-size:30pt;line-height:1.04;letter-spacing:-.03em;max-width:160mm;margin:8mm 0 6mm}.lede{font-size:14pt;color:#344154;max-width:158mm}.rule{width:48mm;height:3px;background:var(--blue);margin:8mm 0}.facts{display:grid;grid-template-columns:repeat(4,1fr);gap:4mm;margin:16mm 0}.fact{border-top:2px solid var(--blue);padding-top:3mm}.fact b{font-size:18pt;display:block}.fact span{font-size:7.5pt;color:var(--muted)}.scope{border-left:4px solid var(--amber);background:var(--amber2);padding:4mm}.critical{border-left:5px solid var(--red);background:var(--red2);padding:4mm 5mm;margin:6mm 0 0;color:var(--red)}.critical b{display:block;letter-spacing:.08em;text-transform:uppercase;margin-bottom:1.5mm;font-size:9pt}.critical p{margin:0;color:var(--ink);font-size:9pt}.meta{display:grid;grid-template-columns:35mm 1fr;gap:2mm 4mm;border-top:1px solid var(--line);padding-top:5mm;color:var(--muted)}
h1,h2,h3,h4,p{margin-top:0}h1{font-size:24pt}h2{font-size:15pt;color:var(--blue);margin:6mm 0 3mm;break-after:avoid}h2:not(:first-child){break-before:page}h3{font-size:10.5pt;margin:4mm 0 2mm;break-after:avoid}h4{font-size:9pt;margin:3mm 0 1.5mm}p{margin-bottom:2.5mm}ul,ol{margin:0 0 3mm 5mm;padding-left:4mm}li{margin-bottom:.8mm}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:7.8pt;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;margin:3mm 0 5mm;font-size:7.4pt}thead{display:table-header-group}tr{break-inside:avoid}th{background:#35445a;color:#fff;text-align:left;padding:2mm}td{padding:1.8mm 2mm;border-bottom:1px solid var(--line);vertical-align:top}tbody tr:nth-child(even) td{background:var(--soft)}
.risk-title{border-top:2px solid var(--line);padding-top:3mm;color:var(--ink);break-before:auto}.risk-title+ p{border-left:3px solid var(--blue);padding-left:3mm}.risk-title,.risk-title+p,.risk-title+p+p{break-inside:avoid}.critical-title{color:var(--red);border-top:3px solid var(--red);padding-top:3mm}.critical-title+p{border-left:4px solid var(--red);background:var(--red2);padding:3mm}
.text-diagram,.code-block{background:var(--soft);border:1px solid var(--line);padding:4mm;margin:3mm 0 5mm;break-inside:avoid}.text-diagram pre,.code-block{font:7.4pt/1.35 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;margin:0}
.page{break-before:page}.atlas h2{break-before:auto}.section-lede{font-size:10pt;color:var(--muted)}.diagram{border:1px solid var(--line);padding:4mm;margin:4mm 0 6mm;break-inside:avoid;background:#fbfcfe}.diagram-head{display:flex;justify-content:space-between;gap:5mm;border-bottom:1px solid var(--line);padding-bottom:2mm;margin-bottom:4mm}.diagram-head strong{color:var(--blue);font-size:10pt}.diagram-head span{color:var(--muted);font-size:7pt;text-align:right}.flow{display:flex;align-items:stretch;justify-content:center;gap:2mm;margin:3mm 0}.node{border:1px solid #9aa8b8;background:#fff;border-radius:3px;padding:2.5mm;text-align:center;min-width:25mm;font-size:7.5pt;line-height:1.2}.node.primary,.zone.primary{background:var(--blue2);border-color:#6d96cc;font-weight:700}.node.external,.zone.external{background:var(--amber2);border-color:#c7a14c}.node.risk{background:var(--red2);border-color:#cc8992}.arrow{align-self:center;color:var(--blue);font-size:15pt;font-weight:700}.zones{display:grid;grid-template-columns:repeat(4,1fr);gap:3mm}.zone{border:1px dashed #8b98a8;padding:4mm;text-align:center}.zone small,.compare small{display:block;color:var(--muted);margin-top:2mm}.boundary-note,.reconcile,.rail{margin-top:3mm;padding:2.5mm;text-align:center;background:var(--blue2);font-size:7.4pt}.rail.danger{background:var(--red2);color:var(--red)}.sources,.branches{display:flex;justify-content:center;gap:3mm;flex-wrap:wrap}.sources .node,.branches .node{min-width:28mm}.consistency{display:grid;grid-template-columns:1fr 30mm 1fr 1fr;gap:3mm;align-items:center}.outbox{text-align:center;color:var(--blue);font-weight:700}.outbox span{display:block;font-size:7pt;color:var(--muted)}.compare{display:grid;grid-template-columns:1fr 1fr;gap:6mm}.compare>div{border-left:3px solid var(--line);padding-left:3mm}
@media screen{body{background:#edf1f5;padding:24px}main{background:#fff;padding:15mm 14mm}}
</style>
</head>
<body><main>
<section class="cover">
  <div>
    <div class="eyebrow">Standalone architecture and system-design review</div>
    <h1>Full System Architecture<br>and Risk Review</h1>
    <div class="rule"></div>
    <p class="lede">From Google OAuth and permanent media ingestion through hybrid search, carousel generation, storage lifecycle, consistency, and disaster recovery. Issues are ranked by maximum system-performance impact and classified as primary bottlenecks or child issues.</p>
    <div class="facts">
      <div class="fact"><b>77.107 GiB</b><span>free after leftover cleanup</span></div>
      <div class="fact"><b>12</b><span>primary performance bottlenecks</span></div>
      <div class="fact"><b>26</b><span>child issues of those bottlenecks</span></div>
      <div class="fact"><b>21</b><span>video files remaining (keep set)</span></div>
    </div>
    <div class="critical"><b>Critical warning — leftover downloads must auto-clean</b><p>Drive ERROR/no-Media caches, processed Drive caches after Media exists, and failed partials filled the volume and caused ENOSPC. Manual cleanup is not enough. The indexer must unlink leftovers in the success and ERROR paths. Keep upload, YouTube, active PROCESSING, and unknown/orphan files. This is a required control of bottleneck #1, not a child footnote.</p></div>
  </div>
  <div>
    <div class="scope"><strong>Evidence boundary.</strong> Production facts are from the 14 August 2026 07:16 UTC read-only re-check plus the prior audit. No deploy, settings change, cache delete, or DB mutation was performed for this report. Auto-clean is required and is not yet implemented in the indexer paths.</div>
    <div class="meta"><b>Review date</b><span>14 August 2026</span><b>System</b><span>Video/Image Search Indexer + Carousel Studio</span><b>Generated from</b><span>docs/full-system-architecture-and-risks.md</span><b>Rendering</b><span>Offline local Chromium · A4 · tagged PDF</span></div>
  </div>
</section>
${visualAtlas}
${content}
</main></body></html>`;

await writeFile(outputPath, html, "utf8");
process.stdout.write(`Built ${path.relative(process.cwd(), outputPath)}\n`);
