#!/usr/bin/env python3
"""The checks. Every one runs against a throwaway Claude home built on the spot:
transcripts written by hand for each way a session can stall, a fake desktop
app index, a fake clock, a fake boot time and a fake `claude` binary. Nothing
here reads or writes the real ~/.claude.

    python3 tools/prova.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "skills", "ormeggio", "scripts", "ormeggio.py")
sys.path.insert(0, os.path.dirname(SCRIPT))

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    mark = "ok  " if ok else "FAIL"
    print(f"  {mark} {name}" + (f"\n         {detail}" if detail and not ok else ""))


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000Z"


REGISTER = object()


def typed(ts, text, **kw):
    """A prompt typed in an interactive terminal session."""
    r = {"type": "user", "timestamp": iso(ts), "entrypoint": "cli", "promptSource": "typed",
         "message": {"role": "user", "content": text}, "permissionMode": "default"}
    r.update(kw)
    return r


class Home:
    """A fake Claude home with a projects folder and an app index."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="ormeggio-prova-")
        self.claude = os.path.join(self.root, "claude")
        self.projects = os.path.join(self.claude, "projects")
        self.app = os.path.join(self.root, "app", "acct", "org")
        self.data = os.path.join(self.root, "data")
        os.makedirs(self.projects)
        os.makedirs(self.app)
        os.makedirs(self.data)
        self.now = time.time()

    def env(self, **extra):
        e = dict(os.environ)
        for k in list(e):
            if k.startswith("ORMEGGIO_"):
                del e[k]
        e.update({
            "CLAUDE_CONFIG_DIR": self.claude,
            "ORMEGGIO_HOME": self.data,
            "ORMEGGIO_APP_INDEX": os.path.join(self.root, "app"),
            "ORMEGGIO_NOW": str(self.now),
            "ORMEGGIO_FREE_MEM": "60",
        })
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def run(self, *args, stdin=None, **extra):
        p = subprocess.run([sys.executable, SCRIPT, *args], input=stdin, capture_output=True,
                           text=True, env=self.env(**extra), timeout=60)
        return p

    def session(self, records, project="-Users-x-dev-proj", sid=None, cwd="/Users/x/dev/proj", mtime_ago=2 * 86400,
                title=None, app=REGISTER):
        """A transcript. Like in the real app, every desktop session is in the
        app's index unless app=None says it is not."""
        sid = sid or str(uuid.uuid4())
        folder = os.path.join(self.projects, project)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, sid + ".jsonl")
        with open(path, "w") as fh:
            if title:
                fh.write(json.dumps({"type": "custom-title", "customTitle": title, "sessionId": sid}) + "\n")
            for r in records:
                r = dict(r)
                r.setdefault("sessionId", sid)
                r.setdefault("cwd", cwd)
                r.setdefault("isSidechain", False)
                fh.write(json.dumps(r) + "\n")
        t = self.now - mtime_ago
        os.utime(path, (t, t))
        if app is REGISTER:
            app = {}
        if app is not None:
            info = {"sessionId": "local_" + sid, "cliSessionId": sid, "cwd": cwd, "title": title or sid[:8],
                    "isArchived": False}
            info.update(app)
            with open(os.path.join(self.app, "local_" + sid + ".json"), "w") as fh:
                json.dump(info, fh)
        return sid

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


# Record builders, in the shape Claude Code writes them.

def human(ts, text, origin="human", **kw):
    r = {"type": "user", "timestamp": iso(ts), "entrypoint": "claude-desktop", "promptSource": "sdk",
         "turnOrigin": origin, "message": {"role": "user", "content": text}, "permissionMode": "auto"}
    r.update(kw)
    return r


def peer(ts, text="Another Claude session sent a message: <cross-session-message from=\"local_x\">hi</cross-session-message>"):
    return {"type": "user", "timestamp": iso(ts), "entrypoint": "claude-desktop", "promptSource": "system",
            "turnOrigin": "peer", "isMeta": True, "message": {"role": "user", "content": text}}


def notification(ts, status):
    text = (f"<task-notification>\n<task-id>a1b2c3d4e5</task-id>\n<status>{status}</status>\n"
            f"<summary>Background agent finished</summary>\n</task-notification>")
    return {"type": "user", "timestamp": iso(ts), "entrypoint": "claude-desktop", "promptSource": "system",
            "turnOrigin": "task_notification", "message": {"role": "user", "content": text}}


def say(ts, text, stop="end_turn"):
    return {"type": "assistant", "timestamp": iso(ts),
            "message": {"role": "assistant", "stop_reason": stop, "content": [{"type": "text", "text": text}]}}


def tool(ts, name, inp, tid):
    return {"type": "assistant", "timestamp": iso(ts),
            "message": {"role": "assistant", "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def result(ts, tid, content="ok"):
    return {"type": "user", "timestamp": iso(ts),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]}}


def api_error(ts, error, text):
    return {"type": "assistant", "timestamp": iso(ts), "isApiErrorMessage": True, "error": error,
            "message": {"role": "assistant", "stop_reason": "stop_sequence", "content": [{"type": "text", "text": text}]}}


# ---------------------------------------------------------------------------


def build_fleet(h):
    """One session per way of stopping. The user left three days ago."""
    t0 = h.now - 3 * 86400  # the last message before the absence
    s = {}
    s["limit"] = h.session([human(t0 - 3600, "porta avanti il backlog"), tool(t0 - 3500, "Bash", {}, "t1"),
                            result(t0 - 3400, "t1"),
                            api_error(t0 + 7200, "rate_limit", "You've hit your 5-hour limit · resets 8:20pm")],
                           title="Limite", app={"permissionMode": "acceptEdits"})
    s["login"] = h.session([human(t0 - 3600, "vai"),
                            api_error(t0 + 3600, "authentication_failed",
                                      "Failed to authenticate. API Error: 401 OAuth access token has expired")],
                           title="Login")
    s["tool"] = h.session([human(t0 - 3600, "build"), tool(t0 + 3600, "Bash", {"command": "make"}, "t2")],
                          title="Riavvio")
    after = t0 + 11 * 3600  # after the fake boot at t0 + 10 h, so the cause stays its own
    s["turn"] = h.session([human(t0 - 3600, "build"), tool(after, "Bash", {}, "t3"), result(after + 100, "t3")],
                          title="Turno a meta")
    s["agents"] = h.session([human(t0 - 3600, "lancia gli agenti"), say(t0 - 3000, "Lanciati."),
                             notification(t0 + 600, "stopped")], title="Agenti")
    s["wrap"] = h.session([human(t0 - 3600, "fai la release"),
                           tool(t0 - 3000, "mcp__ccd_turn__wrap_up",
                                {"recap": "Pronta.", "ask": "Pubblico la versione 2.0?"}, "t4"),
                           result(t0 - 2990, "t4"), say(t0 - 2980, "Pronta per la pubblicazione.")],
                          title="Domanda nel riquadro", app={})
    s["question"] = h.session([human(t0 - 3600, "scegli tu"),
                               say(t0 - 3000, "Ho preparato due varianti.\n\nQuale preferisci, la A o la B?")],
                              title="Domanda nel testo")
    s["dialog"] = h.session([human(t0 - 3600, "preparami"),
                             tool(t0 - 3000, "AskUserQuestion", {"questions": [
                                 {"question": "In che giorno è la prova?", "options": [{"label": "lunedì"}, {"label": "martedì"}]},
                                 {"question": "Hanno risposto alla mail?", "options": [{"label": "sì"}, {"label": "no"}]}]}, "tq")],
                            title="Domande in un dialogo")
    s["done"] = h.session([human(t0 - 3600, "sistema il bug"), say(t0 - 3000, "Sistemato e committato in abc123.")],
                          title="Finita")
    s["stopped"] = h.session([human(t0 - 3600, "vai"), tool(t0 - 3500, "Bash", {}, "t5"),
                              human(t0 - 3400, "[Request interrupted by user for tool use]")], title="Fermata")
    s["bg_lost"] = h.session([human(t0 - 3600, "analizza"), tool(after + 10, "Agent", {}, "t6"),
                              result(after + 20, "t6", "Async agent launched successfully. agentId: abc123def"),
                              say(after + 30, "L'agente lavora in background: ti avviso quando finisce.")],
                             title="Background perso")
    s["bg_ok"] = h.session([human(t0 - 3600, "avvia il server"), tool(t0 - 3500, "Bash", {}, "t7"),
                            result(t0 - 3490, "t7", "Command running in background with ID: srv12345"),
                            tool(t0 - 3480, "mcp__ccd_turn__wrap_up", {"recap": "Server su."}, "t8"),
                            result(t0 - 3470, "t8"), say(t0 - 3460, "Il server gira sulla porta 3000.")],
                           title="Server lasciato acceso")
    s["blocked"] = h.session([human(t0 - 3600, "vai"),
                              api_error(t0 + 100, "invalid_request", "API Error: the safeguards flagged this message")],
                             title="Bloccata")
    s["sched"] = h.session([human(t0 - 3600, "giro"), tool(t0 + 100, "Bash", {}, "t9")], title="Programmata",
                           app={"scheduledTaskId": "x"})
    s["arch"] = h.session([human(t0 - 3600, "vai"), tool(t0 + 100, "Bash", {}, "t10")], title="Archiviata",
                          app={"isArchived": True})
    s["old"] = h.session([human(t0 - 6 * 86400, "vecchia"), tool(t0 - 6 * 86400 + 60, "Bash", {}, "t11")],
                         title="Vecchia", mtime_ago=8 * 86400)
    s["failure"] = h.session([human(t0 - 3600, "vai"), tool(t0 + 50, "Bash", {}, "t12"), result(t0 + 60, "t12")],
                             title="Caduta registrata")
    s["running"] = h.session([human(t0 - 3600, "lavora"), tool(h.now - 60, "Bash", {}, "t13")],
                             title="Al lavoro", mtime_ago=30)
    s["peer_last"] = h.session([human(t0 - 3600, "vai"), say(after + 100, "Fatto."), peer(after + 900)],
                               title="Messaggio non letto")
    s["spawned"] = h.session([human(t0 - 3600, "Rientro ora. Una riga sola: cosa ti ha detto?", origin=None),
                              say(t0 - 3500, "Vuoi che lanci /ormeggio?")],
                             title="Prova lanciata da dentro l'app", app=None)
    s["tmp_cli"] = h.session([typed(t0 - 3600, "prova il plugin"), tool(t0 + 100, "Bash", {}, "tt1")],
                             title="Terminale in una cartella temporanea", cwd="/private/tmp/prova", app=None)
    s["bare_cmd"] = h.session([typed(t0 - 3600, "<command-message>plancia:briefing</command-message>\n"
                                     "<command-name>/plancia:briefing</command-name>"),
                               say(t0 - 3500, "Lego questa cartella al progetto?")],
                              title="Solo un comando", app=None)
    s["real_cli"] = h.session([typed(t0 - 3600, "porta avanti la migrazione"), tool(t0 + 200, "Bash", {}, "tt2")],
                              title="Terminale vero", app=None)
    s["copie"] = h.session([human(t0 - 3600, "continua col negozio"), tool(t0 + 300, "Bash", {}, "tt3")],
                           title="Sessione vera in una copia", cwd="/Volumes/x/dev/picco-copie/L6-NEGOZIO")
    s["sandbox"] = h.session([human(t0 - 3600, "prova"), tool(t0 + 300, "Bash", {}, "tt4")],
                             title="Cartella esclusa a mano", cwd="/Users/x/sandbox/uno")
    s["script"] = h.session([dict(human(t0 + 600, "Scrivi il riepilogo della giornata"), entrypoint="sdk-cli",
                                  turnOrigin=None),
                             say(t0 + 700, "Vuoi che lo legga ad alta voce?")], title="Riepilogo automatico",
                            app=None)
    return t0, s


def by_title(sessions):
    return {x.get("title"): x for x in sessions}


def test_classification(h, t0):
    print("\nclassification")
    boot = t0 + 10 * 3600
    with open(os.path.join(h.data, "cadute.jsonl"), "w") as fh:
        fh.write(json.dumps({"at": t0 + 61, "session_id": FLEET["failure"], "error": "rate_limit",
                             "error_details": "429 Too Many Requests"}) + "\n")
    p = h.run("sessions", "--json", "--all", "--since", str(t0 - 86400), ORMEGGIO_BOOT=boot)
    check("sessions --json runs", p.returncode == 0, p.stderr[-400:])
    data = json.loads(p.stdout)
    got = by_title(data["sessions"])
    expect = {
        "Limite": ("interrupted", "usage_limit", True),
        "Login": ("interrupted", "login_expired", True),
        "Riavvio": ("interrupted", "mac_restarted", True),
        "Turno a meta": ("interrupted", "turn_unfinished", True),
        "Agenti": ("interrupted", "agents_stopped", True),
        "Domanda nel riquadro": ("waiting", None, False),
        "Domanda nel testo": ("waiting", None, False),
        "Finita": ("done", None, False),
        "Fermata": ("stopped", None, False),
        "Background perso": ("interrupted", "background_lost", True),
        "Server lasciato acceso": ("done", None, False),
        "Bloccata": ("interrupted", "blocked", False),
        "Programmata": ("scheduled", None, False),
        "Archiviata": ("archived", None, False),
        "Caduta registrata": ("interrupted", "usage_limit", True),
        "Al lavoro": ("running", None, False),
        "Messaggio non letto": ("interrupted", "message_unread", True),
    }
    for title, (state, cause, resume) in expect.items():
        s = got.get(title)
        ok = s is not None and s["state"] == state and (cause is None or s.get("cause") == cause) \
            and bool(s.get("resume")) == resume
        check(f"{title}: {state}{' / ' + cause if cause else ''}", ok,
              f"got {s and (s['state'], s.get('cause'), s.get('resume'))}")
    check("a session that stalled before the window is left out", "Vecchia" not in got)
    for title, state in (("Prova lanciata da dentro l'app", "automation"),
                         ("Terminale in una cartella temporanea", "test"),
                         ("Solo un comando", "test"),
                         ("Terminale vero", "interrupted"),
                         ("Sessione vera in una copia", "interrupted"),
                         ("Cartella esclusa a mano", "interrupted")):
        x = got.get(title) or {}
        check(f"{title}: {state}", x.get("state") == state, str((x.get("state"), x.get("detail"))))
    with open(os.path.join(h.data, "config.json"), "w") as fh:
        json.dump({"exclude_paths": ["*/sandbox/*"]}, fh)
    p3 = h.run("sessions", "--json", "--all", "--since", str(t0 - 86400), ORMEGGIO_BOOT=boot)
    x = by_title(json.loads(p3.stdout)["sessions"]).get("Cartella esclusa a mano") or {}
    check("a folder listed in exclude_paths is left out, even for an app session", x.get("state") == "test",
          str(x.get("state")))
    os.remove(os.path.join(h.data, "config.json"))
    p2 = h.run("sessions", "--json", "--all", "--since", str(t0 - 86400), ORMEGGIO_COUNT_SDK=1)
    auto = by_title(json.loads(p2.stdout)["sessions"]).get("Riepilogo automatico") or {}
    check("a claude -p script's run is an automation, even when -p prompts count as a person",
          auto.get("state") == "automation", str(auto.get("state")))
    d = got.get("Domande in un dialogo") or {}
    check("every question of a pending dialog is kept, with its options",
          d.get("state") == "waiting" and "mail" in (d.get("detail") or "") and "martedì" in (d.get("detail") or ""),
          d.get("detail"))
    w = got.get("Domanda nel riquadro") or {}
    check("the wrap-up question is the one reported", "2.0" in (w.get("detail") or ""), w.get("detail"))
    check("the app id is attached when the app knows the session",
          (got.get("Limite") or {}).get("app_id") == "local_" + FLEET["limit"])
    check("the app's permission mode is used", (got.get("Limite") or {}).get("permission_mode") == "acceptEdits")
    check("the transcript's permission mode is the fallback", (got.get("Login") or {}).get("permission_mode") == "auto")


def test_hook(h, t0):
    print("\nhook on UserPromptSubmit")
    sid = str(uuid.uuid4())
    inp = json.dumps({"session_id": sid, "prompt": "eccomi, a che punto siamo?", "transcript_path": "/nope"})
    p = h.run("hook", "prompt", stdin=inp, ORMEGGIO_BOOT=t0 + 36000)
    check("first message after three days: the hook speaks", '"additionalContext"' in p.stdout, p.stdout[:300] + p.stderr[-300:])
    ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"] if p.stdout.strip() else ""
    check("it says the user is back and for how long", "the user is back" in ctx and "3 d" in ctx, ctx[:300])
    check("it found the previous message in the transcripts on its own", "threshold" in ctx)
    check("it mentions the restart and /private/tmp", "/private/tmp" in ctx)
    check("it counts the interrupted sessions and names the causes", "interrupted (" in ctx and "usage limit" in ctx, ctx[:600])
    check("it offers /ormeggio instead of running it", "offer" in ctx and "/ormeggio" in ctx)
    check("it stays under the 10,000 character cap", len(ctx) < 10000)
    st = json.load(open(os.path.join(h.data, "stato.json")))
    check("the absence is recorded", abs(st["absence"]["to"] - h.now) < 2 and st["absence"]["from"] < t0 + 1)

    p = h.run("hook", "prompt", stdin=inp)
    check("a second message a minute later: silent", p.stdout.strip() == "", p.stdout[:200])

    later = h.now + 2 * 86400
    for label, text in (("a message from another session", "Another Claude session sent a message: <cross-session-message from=\"local_a\">x</cross-session-message>"),
                        ("a background notification", "<task-notification>\n<status>completed</status>\n</task-notification>"),
                        ("a scheduled task", "<scheduled-task name=\"x\">run</scheduled-task>"),
                        ("only a system reminder", "<system-reminder>Linked sessions now</system-reminder>")):
        p = h.run("hook", "prompt", stdin=json.dumps({"session_id": sid, "prompt": text}), ORMEGGIO_NOW=later)
        st2 = json.load(open(os.path.join(h.data, "stato.json")))
        check(f"{label}: silent, and the clock is not reset",
              p.stdout.strip() == "" and abs(st2["last_human_at"] - h.now) < 2, p.stdout[:200])

    # The transcript knows better than the text: a prompt that reads like a
    # person but was recorded as coming from another session.
    tp = os.path.join(h.root, "t.jsonl")
    with open(tp, "w") as fh:
        fh.write(json.dumps(human(later, "riprendi da dove eri", origin="sdk", promptId="p-1")) + "\n")
    p = h.run("hook", "prompt", stdin=json.dumps({"session_id": sid, "prompt": "riprendi da dove eri",
                                                   "transcript_path": tp, "prompt_id": "p-1"}), ORMEGGIO_NOW=later)
    st2 = json.load(open(os.path.join(h.data, "stato.json")))
    check("a prompt the transcript records as sent by a session does not count",
          p.stdout.strip() == "" and abs(st2["last_human_at"] - h.now) < 2)
    with open(tp, "w") as fh:
        fh.write(json.dumps(human(later, "<cross-session-message>looks automated</cross-session-message>",
                                  promptId="p-2")) + "\n")
    p = h.run("hook", "prompt", stdin=json.dumps({"session_id": sid, "prompt": "x", "transcript_path": tp,
                                                   "prompt_id": "p-2"}), ORMEGGIO_NOW=h.now + 30 * 3600)
    check("a prompt the transcript records as typed by a person counts, whatever it says",
          '"additionalContext"' in p.stdout, p.stdout[:200] + p.stderr[-200:])

    canned = ("I hit my usage limit while you were working, but it has reset now. "
              "Please continue from where you left off.")
    before = json.load(open(os.path.join(h.data, "stato.json")))["last_human_at"]
    p = h.run("hook", "prompt", stdin=json.dumps({"session_id": sid, "prompt": canned}), ORMEGGIO_NOW=later + 86400)
    st2 = json.load(open(os.path.join(h.data, "stato.json")))
    check("the line the app sends after a usage limit: silent, and the clock is not reset",
          p.stdout.strip() == "" and st2["last_human_at"] == before, p.stdout[:200])

    # The line for the prompt reaches the transcript a moment after the hook
    # starts: the hook has to wait for it instead of trusting the text.
    tp2 = os.path.join(h.root, "late.jsonl")
    open(tp2, "w").close()
    proc = subprocess.Popen([sys.executable, SCRIPT, "hook", "prompt"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, env=h.env(ORMEGGIO_NOW=later + 86400))
    proc.stdin.write(json.dumps({"session_id": sid, "prompt": "continua pure da dove eri",
                                 "transcript_path": tp2, "prompt_id": "p-late"}))
    proc.stdin.close()
    time.sleep(0.2)
    with open(tp2, "a") as fh:
        fh.write(json.dumps(human(time.time(), "continua pure da dove eri", origin="sdk", promptId="p-late")) + "\n")
    out = proc.stdout.read()
    proc.wait(timeout=30)
    st2 = json.load(open(os.path.join(h.data, "stato.json")))
    check("a line written 0.2 s after the hook started is still read, and decides",
          out.strip() == "" and st2["last_human_at"] == before, out[:200])

    p = h.run("hook", "prompt", stdin=json.dumps({"session_id": sid, "prompt": "ciao"}),
              ORMEGGIO_NOW=h.now + 50 * 3600, ORMEGGIO_THRESHOLD_HOURS=40)
    check("under a threshold of 40 h, a 20 h pause is silent", p.stdout.strip() == "", p.stdout[:200])

    with open(os.path.join(h.data, "stato.json"), "w") as fh:
        json.dump({"last_human_at": t0}, fh)
    with open(os.path.join(h.data, "config.json"), "w") as fh:
        json.dump({"on_return": "run"}, fh)
    p = h.run("hook", "prompt", stdin=json.dumps({"session_id": sid, "prompt": "ciao"}))
    check("with on_return set to run, it tells Claude to go ahead", "invoke the ormeggio skill now" in p.stdout, p.stdout[:400])
    os.remove(os.path.join(h.data, "config.json"))

    p = h.run("hook", "prompt", stdin="not json at all")
    check("garbage on stdin: exit 0, nothing printed", p.returncode == 0 and p.stdout.strip() == "")

    p = h.run("hook", "prompt", stdin=json.dumps({"session_id": sid, "prompt": "ciao"}), ORMEGGIO_HOME="/dev/null/x")
    check("an unwritable state folder does not break the prompt", p.returncode == 0)


def test_failure_hook(h):
    print("\nhook on StopFailure")
    path = os.path.join(h.data, "cadute.jsonl")
    before = sum(1 for _ in open(path)) if os.path.exists(path) else 0
    p = h.run("hook", "failure", stdin=json.dumps({"session_id": "abc", "error": "authentication_failed",
                                                    "error_details": "401", "last_assistant_message": "Failed"}))
    lines = open(path).read().splitlines()
    check("it appends one line", p.returncode == 0 and len(lines) == before + 1)
    check("with the session and the error", json.loads(lines[-1])["error"] == "authentication_failed")


def test_plan(h, t0):
    print("\nplan")
    state = {"last_human_at": h.now, "previous_human_at": t0, "absence": {"from": t0, "to": h.now},
             "last_human_text": "fai ripartire tutte le sessioni che si sono fermate, per favore"}
    with open(os.path.join(h.data, "stato.json"), "w") as fh:
        json.dump(state, fh)
    p = h.run("plan", "--json", "--max", "4", ORMEGGIO_BOOT=t0 + 36000)
    plan = json.loads(p.stdout)
    total = len(plan["first_wave"]) + len(plan["later_waves"])
    check("the running session takes one of the four places",
          plan["running_now"] == 1 and len(plan["first_wave"]) == 3, json.dumps(plan)[:300])
    check("the rest wait for a later wave", total == 11, f"total {total}")
    check("the refused request needs the user", [b["title"] for b in plan["need_review"]] == ["Bloccata"])
    check("all three questions are listed", sorted(b["title"] for b in plan["waiting_for_you"]) ==
          sorted(["Domande in un dialogo", "Domanda nel riquadro", "Domanda nel testo"]))
    p = h.run("plan", "--json", ORMEGGIO_FREE_MEM=12)
    plan = json.loads(p.stdout)
    check("at 12% free memory nothing starts", plan["memory_ok"] is False and plan["first_wave"] == [])

    first = json.loads(h.run("plan", "--json").stdout)["first_wave"][:2]
    h.run("mark-sent", ",".join(b["session_id"] for b in first))
    plan = json.loads(h.run("plan", "--json").stdout)
    again = {b["session_id"] for b in plan["first_wave"] + plan["later_waves"]}
    check("a session already sent its message is not sent again",
          not (again & {b["session_id"] for b in first}) and len(plan["resumed_already"]) == 2)


def test_messages(h, t0):
    print("\nmessages")
    m = h.run("message", FLEET["tool"], ORMEGGIO_BOOT=t0 + 36000).stdout
    check("in the user's language", "Riprendi da dove eri" in m, m[:200])
    check("it asks to check the disk and not duplicate", "doppioni" in m and "disco" in m)
    check("it names the restart and /private/tmp", "/private/tmp" in m)
    check("durations and dates are written in the message's language",
          "3 giorni" in m and " alle " in m and "Sun" not in m and " d " not in m, m[:260])
    m = h.run("message", FLEET["limit"]).stdout
    check("after a usage limit, it says to stop if the limit is still used up", "limite d'uso" in m)
    fake_queue = os.path.join(h.root, "coda")
    with open(os.path.join(h.data, "config.json"), "w") as fh:
        json.dump({"heavy_queue": fake_queue, "language": "en"}, fh)
    m = h.run("message", FLEET["limit"]).stdout
    check("a configured queue for heavy jobs is named", fake_queue in m and "Pick up where you were" in m, m[:300])
    os.remove(os.path.join(h.data, "config.json"))


def test_language(h):
    print("\nlanguage of the messages")
    import ormeggio
    st_path = os.path.join(h.data, "stato.json")
    saved = open(st_path).read()
    st = json.loads(saved)
    st["last_human_text"] = "I hit my usage limit while you were working, but it has reset now."
    st["recent_human_texts"] = [
        "sto proprio messo male di spazio sul mac, ma non sono sicuro che si possa risolvere",
        "ok ora ho iterato sulla metodologia, voglio che catturi le modifiche che ho fatto",
        "<pasted_content id=\"x\">Your request has been closed. Thank you for the application and the time</pasted_content>",
        "https://example.com/a https://example.com/b",
        "sì, fai ripartire tutte le sessioni che si sono fermate",
    ]
    with open(st_path, "w") as fh:
        json.dump(st, fh)
    m = h.run("message", FLEET["limit"]).stdout
    check("several Italian messages outweigh an English line the app sent and a pasted English text",
          "Riprendi da dove eri" in m, m[:120])
    check("pasted text, links and tags do not count as words",
          ormeggio.detect_language({"language": "auto"}, sample="<pasted_content>the and you that this with</pasted_content> "
                                   "https://the.and/you voglio che sia fatto") == "it")
    with open(st_path, "w") as fh:
        fh.write(saved)


def test_questions(h):
    print("\nquestions")
    out = os.path.join(h.root, "domande.md")
    p = h.run("questions", "--out", out)
    text = open(out).read() if os.path.exists(out) else ""
    check("one file with every question", "2.0" in text and "la A o la B" in text, text[:300])
    check("with a link back to the app session", "claude://claude.ai/epitaxy/local_" + FLEET["wrap"] in text)
    check("and a terminal command for the others", "claude --resume " + FLEET["question"] in text)
    check("the sessions that need the user are in it too", "Bloccata" in text)


def test_relaunch(h, t0):
    print("\nrelaunch from a terminal")
    log = os.path.join(h.root, "calls.log")
    fake = os.path.join(h.root, "claude-finto")
    with open(fake, "w") as fh:
        fh.write("#!/bin/sh\n"
                 f"echo \"start $(date +%s) $*\" >> {log}\n"
                 "sleep 2\n"
                 f"echo \"end $(date +%s)\" >> {log}\n")
    os.chmod(fake, 0o755)
    # Fresh sent log, so every resumable session is in.
    for f in ("inviati.json",):
        try:
            os.remove(os.path.join(h.data, f))
        except OSError:
            pass
    plan = json.loads(h.run("plan", "--json", ORMEGGIO_APP_INDEX="").stdout)
    expected = len(plan["first_wave"]) + len(plan["later_waves"])
    p = h.run("relaunch", "--max", "2", ORMEGGIO_CLAUDE=fake, ORMEGGIO_APP_INDEX="")
    check("without --apply, nothing starts", "Nothing started" in p.stdout and not os.path.exists(log), p.stdout[-300:])
    p = h.run("relaunch", "--apply", "--max", "2", ORMEGGIO_CLAUDE=fake, ORMEGGIO_APP_INDEX="",
              ORMEGGIO_RUNNER_PACE="0.2", ORMEGGIO_RUNNER_POLL="0.5")
    check("with --apply, a runner starts in the background", "Started in the background" in p.stdout, p.stdout[-300:])
    deadline = time.time() + 120
    runlog = None
    while time.time() < deadline:
        dirs = sorted(os.listdir(os.path.join(h.data, "rilanci")))
        runlog = os.path.join(h.data, "rilanci", dirs[-1], "runner.log")
        if os.path.exists(runlog) and "runner finished" in open(runlog).read():
            break
        time.sleep(2)
    lines = open(log).read().splitlines() if os.path.exists(log) else []
    starts = [ln for ln in lines if ln.startswith("start")]
    check(f"every resumable session was resumed once ({expected})", len(starts) == expected and expected >= 8, f"{len(starts)} starts\n" + "\n".join(lines[:4]))
    live = peak = 0
    for ln in lines:
        live += 1 if ln.startswith("start") else -1
        peak = max(peak, live)
    check("never more than two at once", peak <= 2, f"peak {peak}")
    check("each with --resume and the session's permission mode",
          all("--resume" in ln and "--permission-mode" in ln for ln in starts))
    sent = json.load(open(os.path.join(h.data, "inviati.json")))
    check("the resumed sessions are recorded as sent", len(sent) == expected, f"{len(sent)} vs {expected}")


def test_threshold():
    print("\nthreshold")
    import ormeggio
    nights = [8, 9, 10, 9.5, 11, 8.5, 10.5, 12, 9, 10, 13, 9, 8, 10, 11, 9, 14, 10, 9, 10] + [30, 52, 70]
    t, info = ormeggio.suggest_threshold(nights)
    check("nights around 10 h give a threshold well above a night and under two days", 14 <= t <= 20, f"{t} {info}")
    check("fewer than 14 days measured: no suggestion", ormeggio.suggest_threshold(nights[:10])[0] is None)
    check("never under 12 h", ormeggio.suggest_threshold([3] * 30)[0] == 12)
    check("never over 48 h", ormeggio.suggest_threshold([60] * 30)[0] == 48)


def test_human_detection():
    print("\nwho counts as a person")
    import ormeggio
    now = time.time()
    cases = [
        ("desktop prompt", human(now, "ciao"), True),
        ("terminal prompt", {"type": "user", "promptSource": "typed", "entrypoint": "cli",
                             "message": {"content": "hi"}}, True),
        ("older transcript without an origin", {"type": "user", "entrypoint": "claude-desktop",
                                                "message": {"content": "fai questo"}}, True),
        ("another session", peer(now), False),
        ("task notification", notification(now, "completed"), False),
        ("scheduled run", human(now, "giro", origin="scheduled"), False),
        ("claude -p automation", {"type": "user", "entrypoint": "sdk-cli", "message": {"content": "x"}}, False),
        ("tool result", result(now, "t", "ok"), False),
        ("compaction summary", {"type": "user", "message": {"content": "This session is being continued from a previous conversation"}}, False),
        ("subagent", dict(human(now, "x"), isSidechain=True), False),
    ]
    for label, r, want in cases:
        check(f"{label}: {'counts' if want else 'does not count'}", ormeggio.is_human_record(r) == want)


def test_manifest():
    print("\nplugin files")
    plugin = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
    market = json.load(open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")))
    hooks = json.load(open(os.path.join(ROOT, "hooks", "hooks.json")))
    import ormeggio
    check("plugin.json and the script agree on the version", plugin["version"] == ormeggio.VERSION)
    check("the marketplace lists this plugin from the repo root",
          market["plugins"][0]["name"] == plugin["name"] and market["plugins"][0]["source"] == "./")
    for event, groups in hooks["hooks"].items():
        for g in groups:
            for hk in g["hooks"]:
                path = hk["command"].split('"')[1].replace("${CLAUDE_PLUGIN_ROOT}", ROOT)
                check(f"the {event} hook points at a file that exists", os.path.exists(path), path)
                check(f"the {event} hook has a timeout", isinstance(hk.get("timeout"), int))
    check("the script runs on the python3 that ships with macOS",
          subprocess.run(["/usr/bin/python3", SCRIPT, "--version"], capture_output=True).returncode == 0
          if os.path.exists("/usr/bin/python3") else True)


def test_style():
    print("\nwriting")
    files = [os.path.join(ROOT, p) for p in ("README.md", "skills/ormeggio/SKILL.md",
                                              "skills/ormeggio/scripts/ormeggio.py", "install.sh")
             if os.path.exists(os.path.join(ROOT, p))]
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "stylecheck.py"), *files],
                       capture_output=True, text=True)
    check("the prose passes tools/stylecheck.py", p.returncode == 0, p.stdout[-600:])
    bad = []
    for f in files:
        text = open(f).read().lower()
        if "vibe" in text:
            bad.append(f)
    check("nowhere does it say vibe", not bad, ", ".join(bad))


FLEET = {}


def main():
    h = Home()
    try:
        t0, fleet = build_fleet(h)
        FLEET.update(fleet)
        test_human_detection()
        test_threshold()
        test_classification(h, t0)
        test_failure_hook(h)
        test_hook(h, t0)
        test_plan(h, t0)
        test_messages(h, t0)
        test_language(h)
        test_questions(h)
        test_relaunch(h, t0)
        test_manifest()
        test_style()
    finally:
        h.cleanup()
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
