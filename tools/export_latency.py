"""Export request_latency logs or the measured DSH benchmark to Chrome Trace.

Standard library only, except the optional benchmark --chart (matplotlib).
No request/response bodies, commands, model text or tool arguments are exported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def rows(path):
    with path.open(encoding="utf-8-sig") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


class Trace:
    def __init__(self):
        self.events = []
        self.metadata = {"measurement": "wall-clock spans, not CPU samples"}

    def track(self, pid, tid, process, name):
        for field, value in (("process_name", process), ("thread_name", name)):
            self.events.append({"ph": "M", "name": field, "pid": pid, "tid": tid, "args": {"name": value}})

    def span(self, pid, tid, name, start, duration, **args):
        if duration < 0:
            raise ValueError(f"Negative duration for {name}")
        self.events.append({"ph": "X", "name": name, "cat": "wall_time", "pid": pid, "tid": tid,
                            "ts": round(start * 1e6, 3), "dur": round(duration * 1e6, 3), "args": args})

    def mark(self, pid, tid, name, at, **args):
        self.events.append({"ph": "i", "s": "t", "name": name, "pid": pid, "tid": tid,
                            "ts": round(at * 1e6, 3), "args": args})

    def write(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"traceEvents": self.events, "displayTimeUnit": "ms",
                                    "metadata": self.metadata}, ensure_ascii=False, indent=2), encoding="utf-8")


def logs_trace(path, request_id=None):
    records = {}
    with path.open(encoding="utf-8-sig") as source:
        for line in source:
            # Accept normal docker logs, a Docker json-file export, or JSONL.
            try:
                outer = json.loads(line)
            except ValueError:
                outer = None
            if isinstance(outer, dict) and "log" in outer:
                line = outer["log"]
            if "request_latency {" in line:
                row = json.loads(line.split("request_latency ", 1)[1])
            elif isinstance(outer, dict) and outer.get("event") == "request_latency":
                row = outer
            else:
                continue
            if row.get("version") != 1:
                raise ValueError("Unsupported request_latency version")
            if request_id is None or row["request_id"] == request_id:
                records[row["request_id"]] = row
    if not records:
        raise ValueError("No matching request_latency records; older deployments do not emit them")
    trace = Trace()
    origin = min(r["started_unix_us"] for r in records.values())
    trace.metadata["clock"] = "single-server request starts; each duration uses a monotonic clock"
    for tid, r in enumerate(sorted(records.values(), key=lambda r: r["started_unix_us"]), 1):
        start = (r["started_unix_us"] - origin) / 1e6
        args = {k: r[k] for k in ("request_id", "protocol", "outcome", "http_status", "attempts", "repair_attempts", "error_code")}
        trace.track(1, tid, "AGY proxy", f'{r["protocol"]} {r["request_id"]}')
        trace.span(1, tid, "http_request", start, r["total_ms"] / 1000, **args)
        for s in r["spans"]:
            if s["duration_ms"] is None:
                continue
            details = {k: s[k] for k in ("attempt", "after_attempt", "pid", "new_session", "outcome",
                                        "error_code", "prompt_chars", "session_instructions_chars") if k in s}
            if "pid" in details:
                details["cli_pid"] = details.pop("pid")
            trace.span(1, tid, s["name"], start + s["start_ms"] / 1000, s["duration_ms"] / 1000, **details)
        for name, offset in r["marks_ms"].items():
            trace.mark(1, tid, name, start + offset / 1000)
    return trace


def benchmark_trace(folder):
    analysis = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
    http = list(rows(folder / "valid/http-timing.jsonl"))
    groups = {}
    for row in rows(folder / "server-timing.jsonl"):
        groups.setdefault(row["request_number"], []).append(row)
    # No synchronized wall clocks are assumed. Nonnegative request and response
    # transit bound the server->client monotonic offset. Use the intersection
    # across all requests, disclose its uncertainty, and retain exact durations.
    bounds = []
    for r in http:
        g = groups[str(r["number"])]
        begin = next(x for x in g if x["kind"] == "http_start")["monotonic"]
        end = next(x for x in g if x["kind"] == "http_last_body")["monotonic"]
        bounds.append((r["received"]["seconds"] - begin, r["closed"]["seconds"] - end))
    lower, upper = max(a for a, _ in bounds), min(b for _, b in bounds)
    if lower > upper:
        raise ValueError("Clock offset bounds do not intersect; do not align these hosts")
    offset = (lower + upper) / 2
    origin = analysis["dsh"]["summary"]["launch"]["seconds"]
    trace = Trace()
    trace.metadata.update(server_alignment_uncertainty_ms=(upper - lower) * 500,
                          clock="DSH monotonic time; server offset is bounded/estimated. Native run has its own zero.",
                          task_sha256=analysis["task_sha256"],
                          sources_sha256={name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                                          for name in ("analysis.json", "valid/http-timing.jsonl", "server-timing.jsonl")})
    for tid, name in ((1, "DSH whole task"), (2, "Main HTTP requests"), (3, "Auxiliary title request"),
                      (4, "Client tools"), (5, "Server HTTP"), (6, "AGY turns"), (7, "Validation / ready"),
                      (8, "AGY auxiliary title"), (9, "Server auxiliary title"), (10, "Auxiliary validation")):
        trace.track(1, tid, "DSH -> proxy -> AGY", name)
    trace.span(1, 1, "DSH task", 0, analysis["dsh"]["summary"]["wall_seconds"])
    trace.track(2, 1, "Native AGY (separate run, normalized start)", "Whole task")
    trace.span(2, 1, "Native AGY task", 0, analysis["native"]["wall_s"])
    for r in http:
        number = r["number"]
        tid = 2 if r["tool_count"] else 3
        start, end = r["received"]["seconds"] - origin, r["closed"]["seconds"] - origin
        trace.span(1, tid, f"HTTP {number}", start, end - start, request_id=r["request_id"])
        for name in ("headers_received", "first_business"):
            if name in r:
                trace.mark(1, tid, name, r[name]["seconds"] - origin, request_id=r["request_id"])
        g = groups[str(number)]
        first = next(x for x in g if x["kind"] == "http_start")
        last = next(x for x in g if x["kind"] == "http_last_body")
        trace.span(1, 5 if r["tool_count"] else 9, f"Server HTTP {number}", first["monotonic"] + offset - origin,
                   last["monotonic"] - first["monotonic"], request_id=r["request_id"])
        cli = [x for x in g if x["kind"] == "cli_result"]
        for result in cli:
            trace.span(1, 6 if r["tool_count"] else 8, f"AGY {number}", result["monotonic"] - result["seconds"] + offset - origin,
                       result["seconds"], request_id=r["request_id"])
        ready = next((x for x in g if x["kind"] == "profile_mark" and x["name"] == "result"), None)
        if ready and cli:
            trace.span(1, 7 if r["tool_count"] else 10, f"Validate / ready {number}", cli[-1]["monotonic"] + offset - origin,
                       ready["monotonic"] - cli[-1]["monotonic"], request_id=r["request_id"])
    for index, t in enumerate(analysis["dsh"]["tools"], 1):
        trace.span(1, 4, f'{index}: {t["tool"]}', t["start"] - origin, t["seconds"],
                   backgrounded=t["backgrounded"], tests_ok=t["tests_ok"], nonzero_exit=t["nonzero_exit"])
    return trace, analysis


def benchmark_chart(trace, analysis, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    fig, axes = plt.subplots(2, 1, figsize=(15, 7.2), gridspec_kw={"height_ratios": [1.15, 2.1]})
    fig.suptitle("Real coding task: where the time went", x=.075, ha="left", fontsize=20, weight="bold")
    phases = analysis["dsh"]["phases_s"]
    values = [phases[k] for k in ("agy", "client_tools", "network_relay", "server_non_agy", "other_client")]
    labels = ["AGY wall time", "Client tools", "Network / relay residual", "Other server time", "Client scheduling"]
    colors = ["#467fe0", "#e69a35", "#5eb5ad", "#c45986", "#88919e"]
    left = 0
    for value, label, color in zip(values, labels, colors):
        axes[0].barh(1, value, left=left, color=color, height=.5)
        if value > 50:
            axes[0].text(left + value/2, 1, f"{value:.1f}s", ha="center", va="center", color="white", weight="bold")
        left += value
    axes[0].barh(0, analysis["native"]["wall_s"], color="#65758b", height=.5)
    axes[0].text(analysis["native"]["wall_s"] + 8, 0, f'{analysis["native"]["wall_s"]:.1f}s', va="center")
    axes[0].set_yticks([0, 1], ["Native AGY", "DSH + proxy"])
    axes[0].set_xlim(0, 870)
    axes[0].legend(handles=[Patch(color=c, label=f"{l}: {v:.2f}s") for l,c,v in zip(labels,colors,values)],
                   loc="upper center", bbox_to_anchor=(.5, -.3), ncol=3, frameon=False, fontsize=9)
    lanes = {2: (3, "HTTP", "#b2c6e6"), 6: (2, "AGY", colors[0]), 4: (1, "Client tools", colors[1])}
    for e in trace.events:
        if e["ph"] != "X" or e["pid"] != 1 or e["tid"] not in lanes:
            continue
        y, _, color = lanes[e["tid"]]
        axes[1].broken_barh([(e["ts"]/1e6, e["dur"]/1e6)], (y-.26, .52), facecolors=color)
        if e["tid"] == 4 and e["dur"] > 100e6:
            axes[1].annotate("120s wait -> background", (e["ts"]/1e6 + e["dur"]/2e6, y+.27),
                             xytext=(0, 20), textcoords="offset points", ha="center", fontsize=9,
                             arrowprops={"arrowstyle": "-", "color": "#805b26"})
    axes[1].set_yticks([1,2,3], ["Client tools", "AGY", "HTTP"])
    axes[1].set_ylim(.4, 3.7)
    axes[1].set_xlim(0, 870)
    axes[1].set_xlabel("Elapsed seconds since DSH launch")
    for ax in axes:
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="x", alpha=.2)
        ax.set_axisbelow(True)
    uncertainty = trace.metadata["server_alignment_uncertainty_ms"]
    fig.text(.075, .02, f"Wall-clock timeline, not a CPU flame graph. One run per path; Windows vs Linux.\n"
             f"Server alignment uncertainty +/- {uncertainty:.1f} ms; durations unchanged. "
             f"HTTP/tool overlap {phases['http_tool_overlap']*1000:.2f} ms (deduct when summing).", fontsize=9, color="#4d5663")
    fig.subplots_adjust(left=.11, right=.97, top=.86, bottom=.15, hspace=.7)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=160)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--log", type=Path)
    source.add_argument("--benchmark", type=Path)
    parser.add_argument("--request-id")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chart", type=Path, help="Optional benchmark chart; requires matplotlib")
    args = parser.parse_args()
    if args.log:
        if args.chart:
            parser.error("--chart requires --benchmark")
        trace = logs_trace(args.log, args.request_id)
    else:
        trace, analysis = benchmark_trace(args.benchmark)
        if args.chart:
            benchmark_chart(trace, analysis, args.chart)
    trace.write(args.output)
    print(f"Wrote {len(trace.events)} trace events to {args.output}")


if __name__ == "__main__":
    main()
