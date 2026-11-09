#!/usr/bin/env python3
"""ormeggio: notice that you have been away, and bring the stalled sessions back.

One file, standard library only, so it runs on the python3 that ships with macOS
(3.9) and on any Linux box. Two hooks call into it, and so does the skill.

What it reads, and nothing else:
  - the transcripts Claude Code writes under ~/.claude/projects
  - its own state under ~/.claude/ormeggio
  - if present, the Claude desktop app's session index, to put an app session id
    next to a transcript (read only, never written)
  - the boot time and the free memory of the machine

It writes only under ~/.claude/ormeggio. It sends nothing over the network.
"""

import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - not on macOS or Linux
    fcntl = None

VERSION = "0.1.2"

# ---------------------------------------------------------------------------
# Where things live


def claude_home():
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def data_dir():
    d = os.environ.get("ORMEGGIO_HOME") or os.path.join(claude_home(), "ormeggio")
    os.makedirs(d, exist_ok=True)
    return d


def projects_dir():
    return os.environ.get("ORMEGGIO_PROJECTS") or os.path.join(claude_home(), "projects")


def app_index_dir():
    # The desktop app keeps one JSON per session here. Undocumented, so every
    # field is read defensively and the whole thing is optional.
    v = os.environ.get("ORMEGGIO_APP_INDEX")
    if v is not None:
        return v or None
    return os.path.expanduser("~/Library/Application Support/Claude/claude-code-sessions")


DEFAULTS = {
    # Hours without a message from you before the return counts as a return.
    # `ormeggio.py threshold --save` replaces it with one measured on your own
    # transcripts.
    "threshold_hours": 24,
    # How far before you left a session must have been active to be looked at.
    "window_hours": 24,
    # Never more than this many sessions working at once. Twenty froze the
    # desktop app for hours; twelve did not.
    "max_parallel": 12,
    # Below this share of free memory, nothing new is started.
    "min_free_memory_pct": 30,
    # "propose": the hook asks you first. "run": the hook tells Claude to go
    # ahead with the resume and report.
    "on_return": "propose",
    # A command that serialises heavy jobs (builds, renders, local models).
    # "auto" looks for ~/.local/bin/pesante and mentions it if it exists.
    "heavy_queue": "auto",
    # Language of the messages sent to sessions: "auto" follows the language
    # of your own recent messages, or "en", "it", "es".
    "language": "auto",
    # Extra folders whose sessions are never counted, as shell patterns such
    # as "*/sandbox/*". Temporary folders are always left out.
    "exclude_paths": [],
}


def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(data_dir(), "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
        if isinstance(user, dict):
            cfg.update({k: v for k, v in user.items() if k in DEFAULTS or k.startswith("measured")})
    except (OSError, ValueError):
        pass
    env = os.environ.get("ORMEGGIO_THRESHOLD_HOURS")
    if env:
        try:
            cfg["threshold_hours"] = float(env)
        except ValueError:
            pass
    return cfg


def save_config(updates):
    path = os.path.join(data_dir(), "config.json")
    cfg = {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    cfg.update(updates)
    write_json(path, cfg)
    return path


# ---------------------------------------------------------------------------
# Small helpers


def parse_when(value):
    """Epoch seconds from an epoch number or an ISO 8601 string."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.astimezone()
    return d.timestamp()


def now():
    # A fake clock, so an absence can be tested without waiting a day.
    fake = parse_when(os.environ.get("ORMEGGIO_NOW"))
    return fake if fake is not None else time.time()


def local(ts, with_day=True):
    if ts is None:
        return "?"
    d = dt.datetime.fromtimestamp(ts)
    return d.strftime("%a %d %b %H:%M") if with_day else d.strftime("%H:%M")


def span(seconds):
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} d {h} h"
    if h:
        return f"{h} h {m} min"
    return f"{m} min"


def write_json(path, obj):
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


class Lock:
    """An exclusive lock on a file next to the state, so two hooks firing in
    the same second do not both decide you have just come back."""

    def __init__(self, name="stato.lock"):
        self.path = os.path.join(data_dir(), name)
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "a+")
        if fcntl:
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if fcntl and self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        if self.fh:
            self.fh.close()


def read_state():
    try:
        with open(os.path.join(data_dir(), "stato.json"), encoding="utf-8") as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(st):
    write_json(os.path.join(data_dir(), "stato.json"), st)


# ---------------------------------------------------------------------------
# The machine


def boot_time():
    fake = parse_when(os.environ.get("ORMEGGIO_BOOT"))
    if fake is not None:
        return fake
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                             text=True, timeout=3).stdout
        m = re.search(r"sec\s*=\s*(\d+)", out)
        if m:
            return float(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("btime"):
                    return float(line.split()[1])
    except OSError:
        pass
    return None


def free_memory_pct():
    fake = os.environ.get("ORMEGGIO_FREE_MEM")
    if fake:
        try:
            return int(fake)
        except ValueError:
            pass
    try:
        out = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True,
                             timeout=5).stdout
        m = re.search(r"percentage:\s*(\d+)%", out)
        if m:
            return int(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
        return int(100 * info["MemAvailable"] / info["MemTotal"])
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        pass
    return None


def heavy_queue(cfg):
    v = cfg.get("heavy_queue", "auto")
    if v == "auto":
        p = os.path.expanduser("~/.local/bin/pesante")
        return p if os.access(p, os.X_OK) else None
    return v or None


# ---------------------------------------------------------------------------
# Reading transcripts

# Prompts that reach a session without a person typing them. A hook on
# UserPromptSubmit also fires for these, so they must not reset the clock.
AUTOMATED_PREFIXES = (
    "<task-notification",
    "<cross-session-message",
    "another claude session sent a message",
    "<side-sessions-event",
    "<scheduled-task",
    "<ci-monitor-event",
    "[cross-session",
    "this session is being continued",
    "<local-command-",
    "caveat: the messages below",
    "<channel ",
    "<teammate-message",
    "messaggio automatico del rianimatore",
)

SDK_ENTRYPOINTS = ("sdk-cli", "sdk-py", "sdk-ts")

# Folders where a session is a throwaway: a test run, a scratchpad. A session
# the desktop app knows is always kept, wherever it runs.
TEMP_PATTERNS = ("/private/tmp/*", "/tmp/*", "/private/var/folders/*", "/var/folders/*", "*/scratchpad/*")

# Prompts the desktop app sends by itself, word for word, and a person never
# types. They reach the hook before their transcript line is on disk.
CANNED_PROMPTS = (
    "i hit my usage limit while you were working",
)

BARE_COMMAND = re.compile(r"^\s*(<command-message>[^<]*</command-message>\s*)?<command-name>[^<]*</command-name>"
                          r"(\s*<command-args>\s*</command-args>)?\s*$", re.S)


def excluded_path(cwd, extra=()):
    import fnmatch
    if not cwd:
        return False
    import tempfile
    patterns = list(TEMP_PATTERNS) + [os.path.join(tempfile.gettempdir(), "*")] + list(extra or ())
    return any(fnmatch.fnmatch(cwd.rstrip("/") + "/", pat.rstrip("*") + "*") or fnmatch.fnmatch(cwd, pat)
               for pat in patterns)
SYSTEM_REMINDER = re.compile(r"^\s*<system-reminder>.*?</system-reminder>\s*", re.S)


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def has_tool_result(content):
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def looks_automated(text):
    t = text
    while True:
        stripped = SYSTEM_REMINDER.sub("", t, count=1)
        if stripped == t:
            break
        t = stripped
    t = t.strip().lower()
    if not t:
        return True
    return t.startswith(AUTOMATED_PREFIXES) or t.startswith(CANNED_PROMPTS)


def count_sdk():
    return os.environ.get("ORMEGGIO_COUNT_SDK") == "1"


def is_human_record(r):
    """True when a transcript record is a message a person sent."""
    if not isinstance(r, dict) or r.get("type") != "user":
        return False
    if r.get("isSidechain") or r.get("isMeta"):
        return False
    content = (r.get("message") or {}).get("content")
    if has_tool_result(content):
        return False
    origin = r.get("turnOrigin")
    if isinstance(origin, str):
        if origin == "human":
            return True
        return origin == "sdk" and count_sdk() and not looks_automated(text_of(content))
    if r.get("promptSource") == "typed":  # an interactive terminal session
        return True
    if r.get("promptSource") in ("system", "peer", "scheduled"):
        return False
    if r.get("entrypoint") in SDK_ENTRYPOINTS and not count_sdk():
        return False
    return not looks_automated(text_of(content))


def record_ts(r):
    return parse_when(r.get("timestamp")) if isinstance(r, dict) else None


def read_tail(path, nbytes):
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        start = max(0, size - nbytes)
        fh.seek(start)
        data = fh.read()
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first one is cut in half
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out, start == 0


def transcripts(since=None):
    """Top-level session transcripts, newest first. Subagent transcripts live
    one folder deeper and are not sessions of their own."""
    out = []
    for path in glob.glob(os.path.join(projects_dir(), "*", "*.jsonl")):
        try:
            m = os.path.getmtime(path)
        except OSError:
            continue
        if since is not None and m < since:
            continue
        out.append((m, path))
    out.sort(reverse=True)
    return out


# ---------------------------------------------------------------------------
# Measuring your own rhythm


def human_times(days):
    """Every message you sent in the last `days` days, as epoch seconds."""
    since = time.time() - days * 86400
    times = []
    for _mtime, path in transcripts(since):
        try:
            fh = open(path, "rb")
        except OSError:
            continue
        with fh:
            for line in fh:
                if b'"type":"user"' not in line:
                    continue
                if b'"turnOrigin"' in line and b'"turnOrigin":"human"' not in line and not count_sdk():
                    continue
                if b'"tool_use_id"' in line or b'"isMeta":true' in line or b'"isSidechain":true' in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if is_human_record(r):
                    t = record_ts(r)
                    if t and t >= since:
                        times.append(t)
    times.sort()
    merged = []
    for t in times:
        if not merged or t - merged[-1] > 30:
            merged.append(t)
    return merged


def suggest_threshold(daily_longest_h):
    """The longest pause you take as a matter of routine, plus a quarter.

    Routine pauses are the nights: one long gap a day. For every day with a
    message, take its single longest gap. On three days out of four that gap
    stays under the 75th percentile, and a pause a quarter longer than that is
    not one of your nights. The absences themselves sit in the top quarter, so
    they do not drag the number up, which a mean or a 90th percentile would.
    Kept between 12 and 48 hours: a long sleep is never a return, and two days
    away always is."""
    days = sorted(daily_longest_h)
    if len(days) < 14:
        return None, {"days_measured": len(days)}
    p75 = days[int(0.75 * (len(days) - 1))]
    suggestion = min(48, max(12, int(p75 * 1.25 + 0.999)))
    return suggestion, {"days_measured": len(days), "p75_daily_longest_h": round(p75, 1)}


def cmd_threshold(args):
    days = args.days
    t0 = time.time()
    times = human_times(days)
    took = time.time() - t0
    if len(times) < 2:
        print(f"Only {len(times)} messages from you in the last {days} days: nothing to measure.")
        return 1
    gaps = [(b - a) / 3600 for a, b in zip(times, times[1:])]
    buckets = [(0, 1), (1, 4), (4, 8), (8, 12), (12, 16), (16, 24), (24, 48), (48, 96), (96, 10**9)]
    hist = []
    for lo, hi in buckets:
        n = sum(1 for g in gaps if lo <= g < hi)
        label = f"{lo}-{hi} h" if hi < 10**9 else f"{lo}+ h"
        hist.append((label, n))
    # One longest gap per calendar day the gap started on: the nights.
    per_day = {}
    for a, b in zip(times, times[1:]):
        day = dt.datetime.fromtimestamp(a).date()
        per_day[day] = max(per_day.get(day, 0), (b - a) / 3600)
    nightly = sorted(per_day.values())
    suggestion, info = suggest_threshold(nightly)
    longest = sorted(gaps, reverse=True)[:8]
    absences = [(a, b) for a, b in zip(times, times[1:]) if (b - a) >= 24 * 3600]

    report = {
        "days": days,
        "messages": len(times),
        "first": local(times[0]),
        "last": local(times[-1]),
        "scan_seconds": round(took, 1),
        "histogram": hist,
        "daily_longest_gap_median_h": round(nightly[len(nightly) // 2], 1) if nightly else None,
        "suggested_threshold_h": suggestion,
        "longest_gaps_h": [round(g, 1) for g in longest],
        "absences_over_24h": [{"from": local(a), "to": local(b), "hours": round((b - a) / 3600, 1)}
                              for a, b in absences],
    }
    report.update(info)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"{len(times)} messages from you between {report['first']} and {report['last']}"
              f" ({took:.1f} s to read).")
        print("\nPauses between one message and the next:")
        for label, n in hist:
            print(f"  {label:>9}  {n:5d}  {'#' * min(60, n // max(1, len(gaps) // 200 or 1))}")
        if nightly:
            print(f"\nLongest pause of each day: median {report['daily_longest_gap_median_h']} h,"
                  f" under {info.get('p75_daily_longest_h', '?')} h on three days out of four.")
        print("Longest pauses: " + ", ".join(f"{g} h" for g in report["longest_gaps_h"]))
        if absences:
            print(f"\n{len(absences)} absences of a day or more:")
            for a in report["absences_over_24h"][-10:]:
                print(f"  {a['from']}  ->  {a['to']}   {a['hours']} h")
        if suggestion:
            print(f"\nSuggested threshold: {suggestion} h. A pause longer than that is not one of"
                  " your nights.")
    if args.save and suggestion:
        path = save_config({"threshold_hours": suggestion,
                            "measured_on": dt.datetime.now().strftime("%Y-%m-%d"),
                            "measured_from_messages": len(times)})
        print(f"Saved to {path}")
    return 0


# ---------------------------------------------------------------------------
# The desktop app's index, when there is one


def load_app_index():
    root = app_index_dir()
    out = {}
    if not root or not os.path.isdir(root):
        return out
    for path in glob.glob(os.path.join(root, "*", "*", "local_*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        info = {
            "app_id": d.get("sessionId"),
            "title": d.get("title"),
            "archived": bool(d.get("isArchived")),
            "permission_mode": d.get("permissionMode"),
            "scheduled": bool(d.get("scheduledTaskId")),
            "error": d.get("error") if isinstance(d.get("error"), str) else None,
            "error_category": d.get("errorCategory") if isinstance(d.get("errorCategory"), str) else None,
            "cut_short": bool(d.get("lastTurnCutShort")),
            "cwd": d.get("cwd"),
            "started_by": d.get("spawnedFrom") if isinstance(d.get("spawnedFrom"), str) else None,
        }
        ids = [d.get("cliSessionId"), d.get("lastAssistantCliSessionId")]
        ids += [x for x in (d.get("priorCliSessionIds") or []) if isinstance(x, str)]
        for cid in ids:
            if isinstance(cid, str) and cid:
                # The current id wins over a prior one if two sessions claim it.
                if cid not in out or cid == d.get("cliSessionId"):
                    out[cid] = info
    return out


def app_running():
    try:
        return subprocess.run(["pgrep", "-x", "Claude"], capture_output=True,
                              timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------------------
# Failures recorded by the StopFailure hook


def read_failures(since=None):
    out = {}
    path = os.path.join(data_dir(), "cadute.jsonl")
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if since and (r.get("at") or 0) < since:
                continue
            sid = r.get("session_id")
            if sid:
                out[sid] = r  # the last one wins
    return out


# ---------------------------------------------------------------------------
# Sessions already sent a resume message, so a second run does not send twice


def read_sent():
    try:
        with open(os.path.join(data_dir(), "inviati.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def mark_sent(ids, at=None):
    at = at or now()
    with Lock("inviati.lock"):
        d = read_sent()
        for sid in ids:
            d.setdefault(sid, []).append(at)
            d[sid] = d[sid][-10:]
        write_json(os.path.join(data_dir(), "inviati.json"), d)


def cmd_mark_sent(args):
    ids = [x for x in args.ids.split(",") if x]
    # An app id is stored as well as the transcript id it maps to, so either
    # one finds the record later.
    app = load_app_index()
    by_app = {v.get("app_id"): k for k, v in app.items() if v.get("app_id")}
    full = set(ids) | {by_app[i] for i in ids if i in by_app}
    mark_sent(sorted(full))
    print(f"marked {len(ids)} as sent")
    return 0


# ---------------------------------------------------------------------------
# Classifying a session

ERROR_CAUSES = {
    "rate_limit": "usage_limit",
    "authentication_failed": "login_expired",
    "oauth_org_not_allowed": "login_expired",
    "overloaded": "server_error",
    "server_error": "server_error",
    "unknown": "server_error",
    "max_output_tokens": "server_error",
    "invalid_request": "blocked",
    "billing_error": "account",
    "account_on_hold": "account",
    "model_not_found": "blocked",
    "cloud_credential_error": "login_expired",
}

CAUSE_TEXT = {
    "usage_limit": "the usage limit (429)",
    "login_expired": "an expired login (401)",
    "server_error": "an API error",
    "mac_slept": "the Mac going to sleep mid-reply",
    "mac_restarted": "the Mac shutting down or restarting",
    "process_killed": "the session's process being killed",
    "tool_unfinished": "a tool call that never returned",
    "turn_unfinished": "a turn that stopped halfway",
    "agents_stopped": "background agents reported as stopped, with nobody answering",
    "background_lost": "background work that never reported back",
    "message_unread": "a message that arrived and was never answered",
    "prompt_unanswered": "your own message, never answered",
    "blocked": "a request the API refused",
    "context_full": "a conversation too long to continue",
    "account": "an account or billing problem",
}

# Causes that a plain "carry on" message would only repeat.
NEEDS_PERSON = {"blocked", "context_full", "account"}

QUESTION_HINTS = re.compile(
    r"\?\s*$|^(vuoi|preferisci|procedo|dimmi|fammi sapere|confermi|should i|do you want|want me to|"
    r"shall i|quieres|prefieres|confirmas)\b", re.I | re.M)

BACKGROUND_STARTS = re.compile(
    r"Async agent launched|running in background with ID|moved to the background|"
    r"Workflow .{0,40}(started|launched)|Command running in background", re.I)
WAITING_FOR = re.compile(
    r"notif|aspett|attend|quando (finisc|termin|arriv|torn)|appena (finisc|termin|arriv)|"
    r"when (it|they|the \w+) (finish|complete|report|land|come)|will (report|notify|let you know)|"
    r"wait(ing)? for|i'll check back|sigo cuando|esper", re.I)
TASK_ID = re.compile(r"(?:agentId|ID|task_id|task-id)[\"']?\s*[:=(]?\s*[\"']?([A-Za-z0-9_\-]{6,})")


def _clip(text, n=400):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _last_paragraph(text):
    parts = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return parts[-1] if parts else ""


def classify(path, app, failures, boot, at, absence_end=None, extra_excluded=()):
    """What state a session was left in, read from the end of its transcript."""
    records, whole = read_tail(path, 600_000)
    conv = [r for r in records if r.get("type") in ("user", "assistant") and not r.get("isSidechain")]
    if not conv and not whole:
        records, whole = read_tail(path, 6_000_000)
        conv = [r for r in records if r.get("type") in ("user", "assistant") and not r.get("isSidechain")]
    sid = os.path.splitext(os.path.basename(path))[0]
    info = app.get(sid, {})
    s = {
        "session_id": sid,
        "app_id": info.get("app_id"),
        "title": info.get("title"),
        "cwd": info.get("cwd"),
        "transcript": path,
        "permission_mode": info.get("permission_mode"),
        "entrypoint": None,
        "last_activity": None,
        "last_human": None,
        "state": "done",
        "cause": None,
        "detail": None,
        "resume": False,
    }
    try:
        s["mtime"] = os.path.getmtime(path)
    except OSError:
        s["mtime"] = None

    typed_outside_sdk = False
    entrypoints = set()
    prompts = []
    for r in records:
        t = r.get("type")
        if t == "user" and r.get("entrypoint") and r.get("entrypoint") not in SDK_ENTRYPOINTS:
            typed_outside_sdk = True
        if t in ("user", "assistant") and r.get("entrypoint"):
            entrypoints.add(r["entrypoint"])
        if t == "user" and is_human_record(r):
            prompts.append(text_of((r.get("message") or {}).get("content")))
        if t == "custom-title" and r.get("customTitle"):
            s["title"] = s["title"] or r["customTitle"]
        elif t == "ai-title" and r.get("aiTitle") and not s["title"]:
            s["title"] = r.get("aiTitle")
        if t in ("user", "assistant"):
            if r.get("cwd"):
                s["cwd"] = r["cwd"]
            if r.get("entrypoint"):
                s["entrypoint"] = r["entrypoint"]
            if t == "user" and r.get("permissionMode") and not info.get("permission_mode"):
                s["permission_mode"] = r["permissionMode"]
            ts = record_ts(r)
            if ts:
                s["last_activity"] = max(s["last_activity"] or 0, ts)
            if is_human_record(r):
                s["last_human"] = ts
    if not s["title"]:
        for r in records:
            if is_human_record(r):
                s["title"] = _clip(text_of(r["message"].get("content")), 60)
                break
    if not s["title"]:
        s["title"] = os.path.basename(s["cwd"] or "") or sid[:8]

    if info.get("archived"):
        s["state"] = "archived"
        return s
    if info.get("scheduled"):
        s["state"] = "scheduled"
        return s
    if not conv:
        s["state"] = "empty"
        return s
    # A transcript written only by `claude -p` and unknown to the app is a
    # script's one-off run (a daily summary, a batch job), not a session
    # somebody is waiting on.
    if not info and not typed_outside_sdk and s["entrypoint"] in SDK_ENTRYPOINTS:
        s["state"] = "automation"
        return s
    if not info:
        # `claude -p` started from inside a desktop-app session inherits the
        # app's entrypoint, so its transcript says claude-desktop, and the app
        # has never heard of it: a test or a helper run, not a session.
        if app and entrypoints and entrypoints <= {"claude-desktop", *SDK_ENTRYPOINTS}:
            s["state"] = "automation"
            s["detail"] = "a claude -p run started from inside an app session"
            return s
        if excluded_path(s["cwd"], extra_excluded):
            s["state"] = "test"
            s["detail"] = "runs in a temporary folder"
            return s
        if prompts and all(BARE_COMMAND.match(p or "") for p in prompts):
            s["state"] = "test"
            s["detail"] = "only a slash command, nothing asked"
            return s
    elif extra_excluded and excluded_path(s["cwd"], extra_excluded):
        s["state"] = "test"
        s["detail"] = "in a folder excluded in config.json"
        return s
    if absence_end and s["last_human"] and s["last_human"] >= absence_end - 120:
        s["state"] = "back_already"  # you have written to it since you came back
        return s
    # The file's mtime is a fact of the real clock, so it is compared with the
    # real clock even when a fake one is set for a test.
    if s["mtime"] and time.time() - s["mtime"] < 20 * 60:
        s["state"] = "running"
        return s

    def interrupted(cause, detail=None):
        s["state"] = "interrupted"
        s["cause"] = cause
        s["detail"] = detail
        s["resume"] = cause not in NEEDS_PERSON

    last = conv[-1]
    last_ts = record_ts(last) or s["last_activity"]
    content = (last.get("message") or {}).get("content")

    if last["type"] == "assistant" and last.get("isApiErrorMessage"):
        err = last.get("error") if isinstance(last.get("error"), str) else "unknown"
        text = text_of(content)
        cause = ERROR_CAUSES.get(err, "server_error")
        if "went to sleep" in text:
            cause = "mac_slept"
        elif "prompt is too long" in text.lower():
            cause = "context_full"
        interrupted(cause, _clip(text, 200))
    elif last["type"] == "assistant":
        blocks = content if isinstance(content, list) else []
        uses = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        if uses:
            asking = [b for b in uses if b.get("name") in ("AskUserQuestion", "ExitPlanMode")]
            if asking:
                q = asking[0].get("input") or {}
                qs = [x for x in (q.get("questions") or []) if isinstance(x, dict)]
                parts = []
                for i, x in enumerate(qs, 1):
                    opts = " / ".join(o.get("label", "") for o in (x.get("options") or []) if isinstance(o, dict))
                    line = x.get("question") or ""
                    parts.append((f"{i}. " if len(qs) > 1 else "") + line + (f" ({opts})" if opts else ""))
                text = "\n".join(parts) if parts else (q.get("plan") or "")
                s["state"] = "waiting"
                s["detail"] = _clip(text or "a question in a dialog", 900)
            else:
                interrupted("tool_unfinished", ", ".join(sorted({b.get("name", "?") for b in uses})))
        else:
            _ended_turn(s, conv, interrupted)
    else:  # the last record is from the user side
        if has_tool_result(content):
            interrupted("turn_unfinished")
        else:
            text = text_of(content)
            low = text.lower()
            if "[request interrupted by user" in low:
                s["state"] = "stopped"
                s["detail"] = "you pressed stop"
            elif low.lstrip().startswith("<task-notification"):
                statuses = re.findall(r"<status>(\w+)</status>", text)
                if any(x in ("stopped", "killed", "failed", "cancelled") for x in statuses):
                    interrupted("agents_stopped", ", ".join(statuses))
                else:
                    interrupted("message_unread", "a background task finished and nobody read it")
            elif is_human_record(last):
                interrupted("prompt_unanswered", _clip(text, 200))
            else:
                interrupted("message_unread", _clip(text, 120))

    # Evidence from outside the transcript beats a guess from inside it.
    fail = failures.get(sid)
    if fail and last_ts and (fail.get("at") or 0) >= last_ts - 10:
        cause = ERROR_CAUSES.get(fail.get("error") or "unknown", "server_error")
        interrupted(cause, _clip(fail.get("error_details") or fail.get("last_assistant_message") or "", 200))
    if s["state"] in ("done",) and info.get("error") and "limit" in info["error"].lower():
        interrupted("usage_limit", _clip(info["error"], 200))
    if s["state"] == "done" and info.get("cut_short"):
        interrupted("process_killed")
    restart_causes = ("tool_unfinished", "turn_unfinished", "prompt_unanswered", "message_unread",
                      "background_lost", "process_killed")
    if s["state"] == "interrupted" and s["cause"] in restart_causes and boot and last_ts \
            and last_ts < boot <= at:
        s["detail"] = s["cause"] if not s["detail"] else f"{s['cause']}: {s['detail']}"
        s["cause"] = "mac_restarted"
    s["last_activity"] = last_ts or s["last_activity"]
    return s


def _ended_turn(s, conv, interrupted):
    """The last turn finished normally. Was there a question in it, or work
    left running in the background that never reported back?"""
    start = 0
    for i in range(len(conv) - 1, -1, -1):
        r = conv[i]
        if r["type"] == "user" and not has_tool_result((r.get("message") or {}).get("content")):
            start = i
            break
    turn = conv[start:]
    ask = None
    wrapped = False
    launched = set()
    for r in turn:
        c = (r.get("message") or {}).get("content")
        if r["type"] == "assistant" and isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use" and \
                        str(b.get("name", "")).endswith("wrap_up"):
                    inp = b.get("input") or {}
                    wrapped = True
                    if inp.get("ask"):
                        ask = inp["ask"]
        if r["type"] == "user" and has_tool_result(c):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    txt = b.get("content")
                    txt = txt if isinstance(txt, str) else text_of(txt)
                    if BACKGROUND_STARTS.search(txt or ""):
                        m = TASK_ID.search(txt)
                        launched.add(m.group(1) if m else "?")
    final = ""
    for r in reversed(turn):
        if r["type"] == "assistant":
            t = text_of((r.get("message") or {}).get("content"))
            if t.strip():
                final = t
                break
    # Work started in the background only counts as lost when the turn ended
    # by saying it would wait for it. A dev server left running on purpose, or
    # a job whose answer the turn did not need, is not a stalled session; a
    # turn closed with a wrap-up has declared itself done.
    if launched and not wrapped and WAITING_FOR.search(_last_paragraph(final) or ""):
        interrupted("background_lost", f"{len(launched)} background job(s) started in the last turn")
        return
    if ask:
        s["state"] = "waiting"
        s["detail"] = _clip(ask, 400)
        return
    tail = _last_paragraph(final)
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if lines and (QUESTION_HINTS.search(lines[-1]) or (len(lines) > 1 and lines[-2].rstrip().endswith("?"))):
        s["state"] = "waiting"
        s["detail"] = _clip(tail, 400)
        return
    s["state"] = "done"
    s["detail"] = _clip(final, 200) if final else None


def scan_sessions(since, at=None, absence_end=None, exclude=(), absence_start=None):
    at = at or now()
    app = load_app_index()
    sent = read_sent() if absence_end else {}
    extra = load_config().get("exclude_paths") or ()
    failures = read_failures(since=since - 86400 if since else None)
    boot = boot_time()
    out = []
    for _m, path in transcripts(since):
        sid = os.path.splitext(os.path.basename(path))[0]
        if sid in exclude:
            continue
        try:
            s = classify(path, app, failures, boot, at, absence_end, extra)
        except (OSError, ValueError) as e:  # a transcript being written right now
            s = {"session_id": sid, "transcript": path, "state": "unreadable", "detail": str(e)}
        if s.get("app_id") and s["app_id"] in exclude:
            continue
        # The file's mtime moves when metadata is appended; what counts is the
        # last turn, so a session that stalled days before the window is not
        # pulled in by a title update.
        if since and s.get("state") not in ("running",) and s.get("last_activity") and s["last_activity"] < since:
            continue
        if s.get("state") == "interrupted":
            sends = [t for t in sent.get(sid, []) if t >= (absence_end or 0) - 60]
            if sends and (s.get("last_activity") or 0) <= max(sends) + 5:
                s["state"] = "resumed"  # the message is out; the session has not written since
            elif len(sends) >= 2:
                s["resume"] = False
                s["detail"] = f"resumed {len(sends)} times since the return and stopped again"
        out.append(s)
    order = {"interrupted": 0, "waiting": 1, "stopped": 2, "running": 3, "done": 4}
    out.sort(key=lambda s: (order.get(s.get("state"), 9), -(s.get("last_activity") or 0)))  # stylecheck: allow
    return out


def absence_window(cfg, st=None, at=None):
    """Where the last absence started and ended, from the state the hook
    keeps. Without one, the last 24 hours before now."""
    st = st if st is not None else read_state()
    at = at or now()
    ab = st.get("absence") or {}
    start = ab.get("from")
    end = ab.get("to")
    if not start:
        start = st.get("previous_human_at") or (at - cfg["window_hours"] * 3600)
        end = st.get("last_human_at") or at
    since = start - cfg["window_hours"] * 3600
    return start, end, since


# ---------------------------------------------------------------------------
# The message a stalled session receives

LANG_WORDS = {
    "it": {"che", "non", "di", "il", "lo", "per", "sono", "fai", "questo", "questa", "anche", "perché",
           "voglio", "come", "cosa", "della", "delle", "degli", "nel", "nella", "più", "ho", "hai", "ma",
           "se", "mi", "ti", "ci", "gli", "dei", "sul", "alla", "poi", "quando", "dove", "ancora", "tutto",
           "tutte", "fatto", "fare", "deve", "puoi", "riprendi", "sessioni", "grazie", "allora", "ok"},
    "es": {"que", "los", "las", "por", "para", "pero", "está", "quiero", "esto", "también", "como", "del",
           "muy", "hay", "puedes", "eso", "sí", "cuando", "donde", "todo", "gracias", "hacer", "tengo"},
    "en": {"the", "and", "you", "that", "this", "with", "for", "want", "please", "what", "is", "are", "it",
           "to", "of", "from", "have", "can", "where", "when", "thanks", "should", "make"},
}


def recent_texts_from_transcripts(limit=20, days=7):
    """Your own last messages, newest first, read from the transcripts.

    Only lines the transcript marks as typed by a person count first (an origin
    of "human", or a prompt typed in a terminal). Lines without an origin, from
    older transcripts, are added only from sessions the app knows, and only if
    the marked ones are too few: an unmarked line is as likely to be a script's
    prompt as yours."""
    strong, weak = [], []
    since = now() - days * 86400
    app = load_app_index()
    for _m, path in transcripts(since):
        sid = os.path.splitext(os.path.basename(path))[0]
        try:
            records, _ = read_tail(path, 1_000_000)
        except OSError:
            continue
        for r in records:
            if r.get("type") != "user" or r.get("isSidechain") or r.get("isMeta"):
                continue
            text = text_of((r.get("message") or {}).get("content"))
            if not text.strip() or looks_automated(text):
                continue
            ts = record_ts(r) or 0
            if r.get("turnOrigin") == "human" or r.get("promptSource") == "typed":
                strong.append((ts, text))
            elif "turnOrigin" not in r and sid in app and is_human_record(r):
                weak.append((ts, text))
        if len(strong) >= limit:
            break
    strong.sort(reverse=True)
    weak.sort(reverse=True)
    picked = strong[:limit]
    if len(picked) < 5:
        picked += weak[: limit - len(picked)]
    return [t for _ts, t in picked]


NOISE = re.compile(r"<(system-reminder|pasted_content|bash-input|bash-stdout|command-[a-z]+)[^>]*>.*?</\1>|"
                   r"```.*?```|`[^`]*`|https?://\S+", re.S)


def detect_language(cfg, sample=None):
    """The language of your own recent messages, by counting common words.

    One message is not enough: a short "ok", or a line the app sends by itself
    in English, would decide for the whole return. So it counts over the last
    messages you sent, kept by the hook, or read from the transcripts when the
    hook has not seen enough of them yet."""
    lang = cfg.get("language", "auto")
    if lang in ("en", "it", "es"):
        return lang
    if sample is not None:
        texts = [sample]
    else:
        texts = list(read_state().get("recent_human_texts") or [])
        if len(texts) < 5:
            texts += recent_texts_from_transcripts()
    score = {k: 0 for k in LANG_WORDS}
    for text in texts:
        if looks_automated(text or ""):
            continue
        words = re.findall(r"[a-zàèéìòóùñ]+", NOISE.sub(" ", text or "").lower())
        for k, vocab in LANG_WORDS.items():
            score[k] += sum(w in vocab for w in words)
    best = max(score.items(), key=lambda kv: (kv[1], kv[0] == "en"))  # stylecheck: allow
    return best[0] if best[1] > 0 else "en"


MESSAGES = {
    "it": {
        "intro": "Messaggio di ormeggio: chi lavora con te è tornato dopo {away} di assenza.",
        "away_unknown": "un po' di tempo",
        "stop": "Ti eri fermata {when}, per {cause}.",
        "ask": "Riprendi da dove eri. Prima controlla cosa è rimasto su disco (git status, file a metà, "
               "lock rimasti, lavori in background che non hanno mai risposto) e non rifare ciò che "
               "è già fatto: niente doppioni di file, commit, agenti o sessioni.",
        "reboot": "Il Mac si è riavviato {boot}: quello che stava in /private/tmp, scratchpad compresi, "
                  "non c'è più, e va rifatto dentro il progetto.",
        "heavy": "I lavori pesanti (build, render, modelli locali) uno alla volta, con {queue}.",
        "heavy_generic": "I lavori pesanti (build, render, modelli locali) uno alla volta: altre sessioni "
                         "stanno ripartendo insieme a te.",
        "limit": "Se il limite d'uso è ancora esaurito, fermati e dillo, senza riprovare in giro.",
        "end": "Poi porta avanti il lavoro e alla fine scrivi in poche righe cosa è fatto e cosa manca.",
    },
    "es": {
        "intro": "Mensaje de ormeggio: la persona con la que trabajas ha vuelto tras {away} fuera.",
        "away_unknown": "un tiempo",
        "stop": "Te detuviste {when}, por {cause}.",
        "ask": "Retoma donde lo dejaste. Antes revisa qué quedó en disco (git status, archivos a medias, "
               "locks, trabajos en segundo plano que nunca respondieron) y no repitas lo ya hecho: "
               "nada de duplicados de archivos, commits, agentes o sesiones.",
        "reboot": "El Mac se reinició {boot}: lo que estaba en /private/tmp, scratchpads incluidos, ya no "
                  "existe y hay que rehacerlo dentro del proyecto.",
        "heavy": "Los trabajos pesados (builds, renders, modelos locales) de uno en uno, con {queue}.",
        "heavy_generic": "Los trabajos pesados (builds, renders, modelos locales) de uno en uno: otras "
                         "sesiones arrancan a la vez que tú.",
        "limit": "Si el límite de uso sigue agotado, detente y dilo, sin reintentar en bucle.",
        "end": "Luego sigue con el trabajo y al final escribe en pocas líneas qué está hecho y qué falta.",
    },
    "en": {
        "intro": "Message from ormeggio: the person you work with is back after {away} away.",
        "away_unknown": "a while",
        "stop": "You stopped {when}, because of {cause}.",
        "ask": "Pick up where you were. First check what was left on disk (git status, half-written "
               "files, leftover locks, background jobs that never reported back) and do not redo what "
               "is already done: no duplicate files, commits, agents or sessions.",
        "reboot": "The Mac restarted {boot}: whatever was in /private/tmp, scratchpads included, is gone "
                  "and has to be redone inside the project.",
        "heavy": "Heavy jobs (builds, renders, local models) one at a time, through {queue}.",
        "heavy_generic": "Heavy jobs (builds, renders, local models) one at a time: other sessions are "
                         "starting up alongside you.",
        "limit": "If the usage limit is still used up, stop and say so instead of retrying.",
        "end": "Then carry the work forward, and end with a few lines on what is done and what is left.",
    },
}

CAUSE_SHORT = {
    "it": {"usage_limit": "il limite d'uso (429)", "login_expired": "il login scaduto (401)",
           "mac_restarted": "lo spegnimento o il riavvio del Mac", "mac_slept": "il Mac andato a dormire",
           "process_killed": "il processo chiuso", "tool_unfinished": "un tool rimasto a metà",
           "turn_unfinished": "un turno rimasto a metà", "agents_stopped": "agenti in background fermati",
           "background_lost": "lavori in background che non hanno mai risposto",
           "message_unread": "un messaggio arrivato e mai letto",
           "prompt_unanswered": "un messaggio rimasto senza risposta",
           "server_error": "un errore dell'API"},
    "es": {"usage_limit": "el límite de uso (429)", "login_expired": "la sesión caducada (401)",
           "mac_restarted": "el apagado o reinicio del Mac", "mac_slept": "el Mac dormido",
           "process_killed": "el proceso cerrado", "tool_unfinished": "una herramienta a medias",
           "turn_unfinished": "un turno a medias", "agents_stopped": "agentes en segundo plano detenidos",
           "background_lost": "trabajos en segundo plano que nunca respondieron",
           "message_unread": "un mensaje que nadie leyó",
           "prompt_unanswered": "un mensaje sin respuesta", "server_error": "un error de la API"},
}


def span_in(seconds, lang):
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    units = {"it": ("giorni", "giorno", "ore", "ora", "minuti"),
             "es": ("días", "día", "horas", "hora", "minutos"),
             "en": ("days", "day", "hours", "hour", "minutes")}[lang]
    days = f"{d} {units[0] if d != 1 else units[1]}"
    hours = f"{h} {units[2] if h != 1 else units[3]}"
    joiner = {"it": " e ", "es": " y ", "en": " and "}[lang]
    if d:
        return days + (joiner + hours if h else "")
    if h:
        return hours
    return f"{m} {units[4]}"


def when_in(ts, lang):
    """A moment in the words of the message: "il 08/11 alle 23:46"."""
    if ts is None:
        return "?"
    d = dt.datetime.fromtimestamp(ts)
    if lang == "it":
        return d.strftime("il %d/%m alle %H:%M")
    if lang == "es":
        return d.strftime("el %d/%m a las %H:%M")
    return d.strftime("on %a %d %b at %H:%M")


def resume_message(s, cfg, st=None, at=None):
    st = st if st is not None else read_state()
    at = at or now()
    lang = detect_language(cfg)
    m = MESSAGES[lang]
    ab = st.get("absence") or {}
    away = span_in((ab.get("to") or at) - ab["from"], lang) if ab.get("from") else m["away_unknown"]
    cause = s.get("cause") or "turn_unfinished"
    cause_txt = CAUSE_SHORT.get(lang, {}).get(cause) or CAUSE_TEXT.get(cause, cause)
    when = when_in(s.get("last_activity"), lang)
    parts = [m["intro"].format(away=away), m["stop"].format(when=when, cause=cause_txt), m["ask"]]
    boot = boot_time()
    if boot and s.get("last_activity") and s["last_activity"] < boot <= at:
        parts.append(m["reboot"].format(boot=when_in(boot, lang)))
    q = heavy_queue(cfg)
    parts.append(m["heavy"].format(queue=q) if q else m["heavy_generic"])
    if cause == "usage_limit":
        parts.append(m["limit"])
    parts.append(m["end"])
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Commands


def session_line(s):
    bits = [f"[{s.get('state')}]", s.get("title") or s["session_id"][:8]]
    if s.get("cause"):
        bits.append(f"- {CAUSE_TEXT.get(s['cause'], s['cause'])}")
    if s.get("last_activity"):
        bits.append(f"- last {local(s['last_activity'])}")
    return " ".join(bits)


def cmd_status(args):
    cfg = load_config()
    st = read_state()
    at = now()
    last = st.get("last_human_at")
    boot = boot_time()
    out = {
        "now": local(at),
        "last_message_from_you": local(last) if last else None,
        "since_then": span(at - last) if last else None,
        "threshold_hours": cfg["threshold_hours"],
        "last_absence": None,
        "machine_booted": local(boot) if boot else None,
        "free_memory_pct": free_memory_pct(),
        "data_dir": data_dir(),
    }
    ab = st.get("absence")
    if ab:
        out["last_absence"] = {"from": local(ab.get("from")), "to": local(ab.get("to")),
                               "length": span((ab.get("to") or 0) - (ab.get("from") or 0))}
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        for k, v in out.items():
            print(f"{k.replace('_', ' '):>22}  {v}")
    return 0


def _selected(cfg, args):
    st = read_state()
    at = now()
    start, end, since = absence_window(cfg, st, at)
    if getattr(args, "since", None):
        since = parse_when(args.since)
    exclude = set(filter(None, (getattr(args, "exclude", "") or "").split(",")))
    sessions = scan_sessions(since, at=at, absence_end=end, exclude=exclude, absence_start=start)
    return st, at, start, end, since, sessions


def cmd_sessions(args):
    cfg = load_config()
    st, at, start, end, since, sessions = _selected(cfg, args)
    if not args.all:
        sessions = [s for s in sessions if s.get("state") in ("interrupted", "waiting", "stopped", "running", "done", "resumed")]
    if args.json:
        print(json.dumps({"absence": {"from": start, "to": end, "from_local": local(start), "to_local": local(end)},
                          "since": since, "sessions": sessions}, indent=2, ensure_ascii=False, default=str))
        return 0
    print(f"Sessions active since {local(since)} (you were away {local(start)} -> {local(end)}):")
    if not sessions:
        print("  none")
    for s in sessions:
        print("  " + session_line(s))
        if s.get("detail") and s.get("state") in ("waiting", "interrupted"):
            print(f"      {s['detail'][:200]}")
    return 0


def cmd_plan(args):
    """Who gets resumed now, who waits for a second wave, who waits for you."""
    cfg = load_config()
    st, at, start, end, since, sessions = _selected(cfg, args)
    free = free_memory_pct()
    running = [s for s in sessions if s.get("state") == "running"]
    resume = [s for s in sessions if s.get("state") == "interrupted" and s.get("resume")]
    review = [s for s in sessions if s.get("state") == "interrupted" and not s.get("resume")]
    waiting = [s for s in sessions if s.get("state") == "waiting"]
    cap = int(args.max or cfg["max_parallel"])
    slots = max(0, cap - len(running))
    memory_ok = free is None or free >= cfg["min_free_memory_pct"]
    if not memory_ok:
        slots = 0
    limited = [s for s in resume if s.get("cause") == "usage_limit"]
    plan = {
        "absence": {"from": local(start), "to": local(end), "length": span(end - start) if start and end else None},
        "free_memory_pct": free,
        "min_free_memory_pct": cfg["min_free_memory_pct"],
        "memory_ok": memory_ok,
        "max_parallel": cap,
        "running_now": len(running),
        "slots": slots,
        "machine_booted": local(boot_time()),
        "heavy_queue": heavy_queue(cfg),
        "first_wave": [_brief(s) for s in resume[:slots]],
        "later_waves": [_brief(s) for s in resume[slots:]],
        "need_review": [_brief(s) for s in review],
        "waiting_for_you": [_brief(s) for s in waiting],
        "hit_usage_limit": len(limited),
        "done": [_brief(s) for s in sessions if s.get("state") == "done"],
        "stopped_by_you": [_brief(s) for s in sessions if s.get("state") == "stopped"],
        "running": [_brief(s) for s in running],
        "resumed_already": [_brief(s) for s in sessions if s.get("state") == "resumed"],
    }
    if args.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0
    print(f"Away {plan['absence']['from']} -> {plan['absence']['to']} ({plan['absence']['length']}).")
    print(f"Free memory {free}% (needs {cfg['min_free_memory_pct']}%), {len(running)} running, "
          f"room for {slots} more out of {cap}.")
    for field, label in (("first_wave", "Resume now"), ("later_waves", "Resume after the first wave"),
                       ("need_review", "Interrupted, but need you first"),
                       ("waiting_for_you", "Finished with a question for you"),
                       ("stopped_by_you", "Stopped by you"), ("done", "Finished"), ("running", "Working now"),
                       ("resumed_already", "Already sent a resume message")):
        if plan[field]:
            print(f"\n{label} ({len(plan[field])}):")
            for b in plan[field]:
                print(f"  - {b['title']}  ({b.get('cause_text') or b.get('state')})")
    return 0


def _brief(s):
    return {
        "title": s.get("title"),
        "session_id": s.get("session_id"),
        "app_id": s.get("app_id"),
        "cwd": s.get("cwd"),
        "state": s.get("state"),
        "cause": s.get("cause"),
        "cause_text": CAUSE_TEXT.get(s.get("cause") or "", None),
        "detail": s.get("detail"),
        "last_activity": local(s.get("last_activity")),
        "permission_mode": s.get("permission_mode"),
        "resume_command": f"cd {shell_quote(s.get('cwd') or '.')} && claude --resume {s.get('session_id')}",
    }


def shell_quote(p):
    return p if re.fullmatch(r"[A-Za-z0-9_./~@%+=:,-]+", p or "") else "'" + (p or "").replace("'", "'\\''") + "'"


def cmd_message(args):
    cfg = load_config()
    st, at, start, end, since, sessions = _selected(cfg, args)
    want = args.session
    for s in sessions:
        if want in (s.get("session_id"), s.get("app_id")):
            print(resume_message(s, cfg, st, at))
            return 0
    # Not in the window: build the message from the transcript alone.
    for _m, path in transcripts():
        if os.path.splitext(os.path.basename(path))[0] == want:
            s = classify(path, load_app_index(), read_failures(), boot_time(), at, end)
            print(resume_message(s, cfg, st, at))
            return 0
    print(f"No session {want}", file=sys.stderr)
    return 1


def cmd_questions(args):
    """Every open question in one file, with a way back to each session."""
    cfg = load_config()
    st, at, start, end, since, sessions = _selected(cfg, args)
    waiting = [s for s in sessions if s.get("state") == "waiting"]
    review = [s for s in sessions if s.get("state") == "interrupted" and not s.get("resume")]
    stamp = dt.datetime.fromtimestamp(at).strftime("%Y%m%d-%H%M")
    path = args.out or os.path.join(data_dir(), f"ritorno-{stamp}.md")
    lines = [f"# Back after {span(end - start) if start and end else 'a while'}", "",
             f"Away from {local(start)} to {local(end)}. "
             f"{len(waiting)} sessions ended with a question for you, {len(review)} stopped on something "
             "only you can sort out.", ""]
    for title, group in (("Questions waiting for you", waiting), ("Stopped, need you", review)):
        if not group:
            continue
        lines += [f"## {title}", ""]
        for i, s in enumerate(group, 1):
            link = f"claude://claude.ai/epitaxy/{s['app_id']}" if s.get("app_id") else None
            lines.append(f"### {i}. {s.get('title')}")
            lines.append("")
            if s.get("detail"):
                lines.append("> " + s["detail"].replace("\n", "\n> "))
                lines.append("")
            if s.get("cause"):
                lines.append(f"Cause: {CAUSE_TEXT.get(s['cause'], s['cause'])}.")
            if link:
                lines.append(f"Open in the app: {link}")
            lines.append(f"From a terminal: `{_brief(s)['resume_command']}`")
            lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")
    if args.json:
        print(json.dumps({"file": path, "waiting": [_brief(s) for s in waiting],
                          "need_review": [_brief(s) for s in review]}, indent=2, ensure_ascii=False))
    else:
        print(path)
    return 0


def busy_sessions(window=120):
    at = time.time()
    return [p for m, p in transcripts(at - window)]


def cmd_wait(args):
    """Return once fewer than N sessions are writing to their transcripts, so a
    second wave starts only when the first has made room."""
    deadline = time.time() + args.timeout
    cfg = load_config()
    while True:
        busy = len(busy_sessions(args.window))
        free = free_memory_pct()
        mem_ok = free is None or free >= cfg["min_free_memory_pct"]
        if busy < args.below and mem_ok:
            print(json.dumps({"busy": busy, "free_memory_pct": free, "ready": True}))
            return 0
        if time.time() > deadline:
            print(json.dumps({"busy": busy, "free_memory_pct": free, "ready": False}))
            return 3
        time.sleep(args.every)


# ---------------------------------------------------------------------------
# Relaunching from a terminal, when there is no app to send messages through


def claude_binary():
    return os.environ.get("ORMEGGIO_CLAUDE") or shutil.which("claude")


def cmd_relaunch(args):
    cfg = load_config()
    st, at, start, end, since, sessions = _selected(cfg, args)
    chosen = [s for s in sessions if s.get("state") == "interrupted" and s.get("resume")]
    if args.only:
        wanted = set(args.only.split(","))
        chosen = [s for s in chosen if s["session_id"] in wanted or s.get("app_id") in wanted]
    skipped = []
    if app_running() and not args.include_app:
        keep = []
        for s in chosen:
            (skipped if s.get("app_id") else keep).append(s)
        chosen = keep
    binary = claude_binary()
    print(f"{len(chosen)} sessions to resume, at most {args.max or cfg['max_parallel']} at a time, "
          f"only with {cfg['min_free_memory_pct']}% memory free.")
    for s in chosen:
        print("  " + session_line(s))
    if skipped:
        print(f"\n{len(skipped)} live in the desktop app, which is open: resume those from the app"
              " (or close it and pass --include-app).")
        for s in skipped:
            print("  " + session_line(s))
    if not chosen:
        return 0
    if not binary:
        print("\nNo `claude` command on PATH; set ORMEGGIO_CLAUDE to its path.", file=sys.stderr)
        return 1
    if not args.apply:
        print("\nNothing started. Run again with --apply to do it.")
        return 0
    stamp = dt.datetime.fromtimestamp(at).strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(data_dir(), "rilanci", stamp)
    os.makedirs(run_dir, exist_ok=True)
    jobs = [{"session_id": s["session_id"], "title": s.get("title"), "cwd": s.get("cwd") or os.getcwd(),
             "mode": s.get("permission_mode") or "default", "message": resume_message(s, cfg, st, at)}
            for s in chosen]
    write_json(os.path.join(run_dir, "jobs.json"), {"claude": binary, "max": int(args.max or cfg["max_parallel"]),
                                                     "min_free": cfg["min_free_memory_pct"], "jobs": jobs})
    mark_sent([j["session_id"] for j in jobs], at)
    log = open(os.path.join(run_dir, "runner.log"), "a")
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "_runner", run_dir],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    print(f"\nStarted in the background. Log: {run_dir}/runner.log")
    print(f"To stop it: touch {run_dir}/STOP")
    return 0


def cmd_runner(args):
    run_dir = args.run_dir
    with open(os.path.join(run_dir, "jobs.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    queue = list(cfg["jobs"])
    live = {}

    def say(msg):
        print(f"{dt.datetime.now().strftime('%d/%m %H:%M:%S')} {msg}", flush=True)

    # Fifteen seconds between two starts, so a wave does not land on the
    # machine in one second. The tests shorten both pauses.
    pace = float(os.environ.get("ORMEGGIO_RUNNER_PACE", "15"))
    poll = float(os.environ.get("ORMEGGIO_RUNNER_POLL", "20"))
    say(f"runner up, {len(queue)} sessions")
    while queue or live:
        for sid, (proc, job) in list(live.items()):
            if proc.poll() is not None:
                say(f"done {job['title']} (exit {proc.returncode})")
                del live[sid]
        if os.path.exists(os.path.join(run_dir, "STOP")):
            say("STOP found, starting nothing else")
            queue = []
        while queue and len(live) < cfg["max"]:
            free = free_memory_pct()
            if free is not None and free < cfg["min_free"]:
                say(f"free memory {free}%: waiting")
                break
            job = queue.pop(0)
            out = open(os.path.join(run_dir, f"{job['session_id']}.log"), "a")
            cmd = [cfg["claude"], "-p", "--resume", job["session_id"], "--permission-mode", job["mode"],
                   job["message"]]
            try:
                proc = subprocess.Popen(cmd, cwd=job["cwd"] if os.path.isdir(job["cwd"]) else None,
                                        stdout=out, stderr=out, stdin=subprocess.DEVNULL)
            except OSError as e:
                say(f"could not start {job['title']}: {e}")
                continue
            live[job["session_id"]] = (proc, job)
            say(f"started {job['title']} (pid {proc.pid}, {job['mode']})")
            time.sleep(pace)
        time.sleep(poll)
    say("runner finished")
    return 0


# ---------------------------------------------------------------------------
# Hooks


def _hook_input():
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except (ValueError, OSError):
        return {}


def prompt_is_human(inp):
    """Decide whether the prompt behind a UserPromptSubmit came from a person.

    The hook fires for background notifications and messages from other
    sessions too, and its input carries no origin field. The transcript does:
    the record for this prompt is usually on disk by the time the hook runs,
    with a `turnOrigin`. When it is not there yet, the text decides."""
    prompt = inp.get("prompt") or ""
    path = inp.get("transcript_path")
    pid = inp.get("prompt_id")
    if path and os.path.exists(path):
        # The line for this prompt is sometimes written a moment after the
        # hook starts. A prompt the app sent by itself then looked typed, and
        # reset the clock in the middle of an absence. So look a few times.
        tries = int(os.environ.get("ORMEGGIO_LOOKUP_TRIES", "4"))
        for attempt in range(max(1, tries)):
            match = _find_prompt_record(path, pid, prompt)
            if match is not None:
                return is_human_record(match)
            if attempt < tries - 1:
                time.sleep(0.15)
    return bool(prompt.strip()) and not looks_automated(prompt)


def _find_prompt_record(path, pid, prompt):
    try:
        records, _ = read_tail(path, 300_000)
    except OSError:
        return None
    users = [r for r in records if r.get("type") == "user" and not r.get("isSidechain")
             and not has_tool_result((r.get("message") or {}).get("content"))]
    if pid:
        match = next((r for r in reversed(users) if r.get("promptId") == pid), None)
        if match is not None:
            return match
    p = prompt.strip()
    if not p:
        return None
    # By text only among the last few minutes, so an identical older prompt
    # (an "ok", a canned line) is not taken for this one.
    recent = time.time() - 180
    return next((r for r in reversed(users)
                 if (record_ts(r) or 0) >= recent
                 and text_of((r.get("message") or {}).get("content")).strip() == p), None)


def last_human_from_transcripts(exclude_session=None, days=30):
    """Bootstrap for the first run: when did you last write, anywhere?"""
    best = None
    since = now() - days * 86400
    for _m, path in transcripts(since):
        sid = os.path.splitext(os.path.basename(path))[0]
        if sid == exclude_session:
            continue
        if best and _m < best:
            break  # files are newest first; an older file cannot hold a newer message
        try:
            records, _ = read_tail(path, 2_000_000)
        except OSError:
            continue
        for r in reversed(records):
            if is_human_record(r):
                t = record_ts(r)
                if t and (best is None or t > best):
                    best = t
                break
    return best


def hook_prompt():
    inp = _hook_input()
    if not prompt_is_human(inp):
        return 0
    cfg = load_config()
    at = now()
    sid = inp.get("session_id")
    with Lock():
        st = read_state()
        last = st.get("last_human_at")
        if last is None:
            last = last_human_from_transcripts(exclude_session=sid)
        st["previous_human_at"] = last
        st["last_human_at"] = at
        st["last_human_session"] = sid
        st["last_human_text"] = (inp.get("prompt") or "")[:400]
        recent = [t for t in (st.get("recent_human_texts") or []) if isinstance(t, str)]
        text = (inp.get("prompt") or "").strip()
        if len(text.split()) >= 3:
            recent = ([text[:300]] + recent)[:15]
        st["recent_human_texts"] = recent
        gap = at - last if last else 0
        returning = bool(last) and gap >= float(cfg["threshold_hours"]) * 3600
        if returning:
            st["absence"] = {"from": last, "to": at, "session": sid}
        write_state(st)
    if not returning:
        return 0
    since = last - cfg["window_hours"] * 3600
    try:
        sessions = scan_sessions(since, at=at, absence_end=at, exclude={sid} if sid else ())
    except Exception as e:  # never let a scan break the prompt
        sessions = []
        print(f"ormeggio: scan failed: {e}", file=sys.stderr)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": render_return(cfg, last, at, sessions)}}, ensure_ascii=False))
    return 0


def render_return(cfg, last, at, sessions):
    boot = boot_time()
    groups = {}
    for s in sessions:
        groups.setdefault(s.get("state"), []).append(s)
    inter = groups.get("interrupted", [])
    resumable = [s for s in inter if s.get("resume")]
    waiting = groups.get("waiting", [])
    causes = {}
    for s in inter:
        causes[s.get("cause")] = causes.get(s.get("cause"), 0) + 1
    lines = [
        f"ormeggio: the user is back. Their previous message, in any session, was {span(at - last)} ago "
        f"({local(last)}); the threshold for counting a return is {cfg['threshold_hours']} h."
    ]
    if boot and last < boot <= at:
        lines.append(f"The machine booted at {local(boot)}, during the absence: whatever was in /private/tmp "
                     "is gone, scratchpads included.")
    if sessions:
        why = ", ".join(f"{n} by {CAUSE_TEXT.get(c, c)}" for c, n in causes.items())
        lines.append(
            f"Sessions active in the {cfg['window_hours']} h before they left or since: "
            f"{len(inter)} interrupted{f' ({why})' if why else ''}, "
            f"{len(waiting)} ended with a question for the user, {len(groups.get('done', []))} finished, "
            f"{len(groups.get('running', []))} working right now.")
        shown = (resumable + [s for s in inter if not s.get("resume")] + waiting)[:15]
        for s in shown:
            lines.append("  - " + session_line(s) + (f" | {s['detail'][:160]}" if s.get("detail") and s.get("state") == "waiting" else ""))
    else:
        lines.append("No session was active around the absence, so there is nothing to resume.")
        return "\n".join(lines)
    if not inter and not waiting:
        lines.append("Nothing is waiting: mention it in half a line at most, then answer the message as usual.")
        return "\n".join(lines)
    if cfg.get("on_return") == "run":
        lines.append("They chose to have this done without being asked: invoke the ormeggio skill now "
                     "(/ormeggio), then answer their message.")
    else:
        lines.append("Before anything else, offer in one short sentence to run /ormeggio, which resumes the "
                     "interrupted sessions (at most "
                     f"{cfg['max_parallel']} at once, after a memory check) and gathers the open questions in one "
                     "place. Then answer their message as usual. If they say yes, invoke the ormeggio skill.")
    return "\n".join(lines)[:9500]


def hook_failure():
    inp = _hook_input()
    entry = {
        "at": now(),
        "session_id": inp.get("session_id"),
        "error": inp.get("error"),
        "error_details": inp.get("error_details"),
        "last_assistant_message": (inp.get("last_assistant_message") or "")[:300],
        "cwd": inp.get("cwd"),
        "transcript_path": inp.get("transcript_path"),
    }
    path = os.path.join(data_dir(), "cadute.jsonl")
    with Lock("cadute.lock"):
        lines = []
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()[-499:]
        except OSError:
            pass
        lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        os.replace(tmp, path)
    return 0


def cmd_hook(args):
    try:
        if args.event == "prompt":
            return hook_prompt()
        if args.event == "failure":
            return hook_failure()
    except Exception as e:  # a hook must never break the session it runs in
        print(f"ormeggio: {e}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------


def main(argv=None):
    p = argparse.ArgumentParser(prog="ormeggio", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("status", help="when you last wrote, the threshold, the machine")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_status)

    for name, func, helptext in (
            ("sessions", cmd_sessions, "the sessions around your last absence, and the state each was left in"),
            ("plan", cmd_plan, "who to resume now, who later, who waits for you"),
            ("questions", cmd_questions, "write every open question to one Markdown file")):
        a = sub.add_parser(name, help=helptext)
        a.add_argument("--json", action="store_true")
        a.add_argument("--since", help="look at sessions active since this time (ISO or epoch)")
        a.add_argument("--exclude", default="", help="comma-separated session ids to leave out")
        if name == "sessions":
            a.add_argument("--all", action="store_true", help="include archived, scheduled and empty ones")
        if name == "plan":
            a.add_argument("--max", type=int, help="override max_parallel")
        if name == "questions":
            a.add_argument("--out", help="where to write the file")
        a.set_defaults(func=func)

    a = sub.add_parser("message", help="the resume message for one session")
    a.add_argument("session", help="transcript session id or app id (local_...)")
    a.add_argument("--since")
    a.add_argument("--exclude", default="")
    a.set_defaults(func=cmd_message)

    a = sub.add_parser("mark-sent", help="record that sessions were sent a resume message")
    a.add_argument("ids", help="comma-separated transcript ids or app ids")
    a.set_defaults(func=cmd_mark_sent)

    a = sub.add_parser("threshold", help="measure your pauses and suggest a threshold")
    a.add_argument("--days", type=int, default=60)
    a.add_argument("--save", action="store_true", help="write the suggestion to config.json")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_threshold)

    a = sub.add_parser("wait", help="return when fewer than N sessions are busy")
    a.add_argument("--below", type=int, default=12)
    a.add_argument("--timeout", type=int, default=3600)
    a.add_argument("--every", type=int, default=30)
    a.add_argument("--window", type=int, default=120, help="seconds of transcript silence that count as idle")
    a.set_defaults(func=cmd_wait)

    a = sub.add_parser("relaunch", help="resume interrupted sessions with claude -p, from a terminal")
    a.add_argument("--apply", action="store_true", help="actually start them")
    a.add_argument("--max", type=int)
    a.add_argument("--only", help="comma-separated session ids")
    a.add_argument("--include-app", action="store_true", help="also resume app sessions while the app is open")
    a.add_argument("--since")
    a.add_argument("--exclude", default="")
    a.set_defaults(func=cmd_relaunch)

    a = sub.add_parser("_runner")
    a.add_argument("run_dir")
    a.set_defaults(func=cmd_runner)

    a = sub.add_parser("hook", help="entry point for the plugin hooks")
    a.add_argument("event", choices=["prompt", "failure"])
    a.set_defaults(func=cmd_hook)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
