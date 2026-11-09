---
name: ormeggio
description: >-
  Bring back the sessions that stalled while the user was away: find the ones
  interrupted by a usage limit (429), an expired login (401), a shutdown or
  restart, a tool left halfway or background agents reported stopped, resume
  them a dozen at a time after a memory check, and gather the questions that
  finished sessions left for the user in one place. Use it when the ormeggio
  hook says the user is back, when they invoke /ormeggio, and when they ask to
  restart, resume or wake up their sessions after an absence, a dead battery,
  a frozen app or an expired login. Also for "fai ripartire le sessioni",
  "riprendi le sessioni", "sono tornato", "what stalled while I was away".
---

# ormeggio

The user was away. Sessions kept working until something stopped them, and
nothing stopped them loudly. The job is to find out which ones stalled and why,
start the ones that can carry on by themselves, and hand the user the few
decisions only they can make, all at once.

Everything below goes through one script. In the commands, `ORMEGGIO` stands
for:

```bash
python3 "<this skill's base directory>/scripts/ormeggio.py"
```

Use the absolute base directory shown when this skill loaded. The script reads
transcripts and its own state, prints JSON with `--json`, and never sends
anything anywhere. Sending is your part.

## 1. Read the situation, without spending tokens on it

```bash
ORMEGGIO plan --json
```

It returns the absence (from the hook's record, or the last 24 hours), the free
memory, how many sessions are working right now, and the sessions sorted into:

- `first_wave`: interrupted, can carry on alone, and there is room for them now
- `later_waves`: the same, after the first wave has made room
- `need_review`: interrupted on something a "carry on" would only repeat (a
  refused request, a conversation too long, an account problem, or a session
  already resumed twice that stopped again)
- `waiting_for_you`: finished with a question for the user, with the question
- `stopped_by_you`, `done`, `running`, `resumed_already`: left alone

Runs that were never the user's sessions are already out of every list: a
`claude -p` started from inside another session, anything in a temporary
folder or a scratchpad, a session that only ran a slash command.

Each entry has the transcript `session_id`, the desktop app's `app_id` when
there is one, the `cwd`, the cause and a `resume_command` for a terminal.

The classification comes from the end of each transcript and is right far more
often than not, but it is a reading, not a fact. When one entry looks odd (a
session marked done that the user asks about, a cause that does not fit), check
that one session before acting on it, not all of them.

## 2a. In the desktop app

You are in the app when the `mcp__ccd_session_mgmt__*` tools exist (they may be
deferred: load `list_sessions`, `list_events`, `send_message` and `get_usage` in
one ToolSearch call).

1. `list_sessions` with `limit: 40`. Use `isRunning` as the truth about what is
   busy right now, and match entries without an `app_id` by title and cwd.
2. If any session in the waves stopped on the usage limit, call `get_usage`.
   While the limit is still used up, resuming only burns the next window: hold
   those sessions and tell the user when it resets.
3. If `memory_ok` is false, send nothing. Say how much memory is free, then run
   `ORMEGGIO wait --below <max_parallel>` in the background and continue when it
   returns.
4. For each session in `first_wave`, get its message and send it:

   ```bash
   ORMEGGIO message <session_id>
   ```

   Send that text to the session's `app_id` with `SendMessage` (or
   `send_message`). If the user said something about the resume in their own
   words, add it, quoted. Then record what you sent, so a second run does not
   send it again:

   ```bash
   ORMEGGIO mark-sent <session_id>,<session_id>,...
   ```

5. If `later_waves` is not empty, run
   `ORMEGGIO wait --below <max_parallel> --timeout 3600` with
   `run_in_background`. When it reports `ready`, run `plan --json` again and send
   the new `first_wave`. Repeat until nothing is left. Never more than
   `max_parallel` at once, counting the ones that were already running: twenty
   sessions at once froze the app for hours, twelve did not.

## 2b. In a terminal

There is no app to send messages through, so the sessions are resumed with
`claude -p --resume`, each in its own folder and permission mode:

```bash
ORMEGGIO relaunch            # says what it would do
ORMEGGIO relaunch --apply    # starts a detached runner
```

The runner keeps at most `max_parallel` going, starts nothing while free memory
is under the floor, writes one log per session under
`~/.claude/ormeggio/rilanci/<time>/`, and stops starting new ones when a `STOP`
file appears there. If the desktop app is open, it leaves the app's sessions
alone, because two writers on one session is worse than a stalled one.

## 3. The questions, in one place

```bash
ORMEGGIO questions
```

writes every open question and every `need_review` session to one Markdown file
under `~/.claude/ormeggio/` and prints its path. Then show them in your reply
as a numbered list: the session as a link, one line of context, the question.
In the app a session link is `[title](#<app_id>)`; in a terminal give the
`resume_command`.

Ask the user to answer by number, in one message. When they do, forward each
answer to its session, quoting their words, and record it with `mark-sent`.
That is the point of gathering them: one message from the user restarts every
session that was waiting on them.

If the user would rather answer inside each session, send each waiting session
one line asking it to put its open questions to the user again, briefly, and
stop.

## 4. Report

End with a short summary, in the user's language:

- resumed now, with links
- queued for a later wave, and what they are waiting for (room, memory, the
  usage limit's reset time)
- waiting for the user: the numbered questions, or the file
- need the user first, with the cause in a few words
- finished, as one line with the count

Say plainly what you could not check. A session resumed is not a session that
finished: the report says "sent", not "done".

## Rules that came from real incidents

- **Twelve at most, counting the ones already running.** Twenty sessions at once
  kept the desktop app's main thread busy for hours, and every tool call in every
  session waited ten minutes for it.
- **Memory before every wave.** A wave that starts at 15% free memory ends in
  swap, and then nothing finishes.
- **Heavy jobs one at a time.** Several sessions waking together will all want to
  build or render. The message tells them to queue heavy work; if the machine has
  a queue command, the message names it.
- **A restart empties `/private/tmp`.** Scratchpads and anything left there for
  later are gone. The message says so when the machine booted during the absence.
- **No duplicates.** The message tells each session to check what is already on
  disk before redoing it, and not to start again the agents or sessions it was
  starting when it stopped.
- **Never resume** the session you are in, an archived one, a scheduled one (it
  runs on its own schedule), one the user stopped, or one in `need_review`.

## Settings

`~/.claude/ormeggio/config.json`, all optional:

| Setting | Default | Meaning |
|---|---|---|
| `threshold_hours` | 24 | pause that counts as an absence |
| `window_hours` | 24 | how long before the absence a session must have been active |
| `max_parallel` | 12 | sessions working at once, at most |
| `min_free_memory_pct` | 30 | no new wave below this |
| `on_return` | `propose` | `run` resumes without asking first |
| `heavy_queue` | `auto` | command that serialises heavy jobs |
| `language` | `auto` | language of the resume messages; `auto` follows the user's recent messages |
| `exclude_paths` | `[]` | more folders whose sessions are never counted, as shell patterns |

`ORMEGGIO threshold` measures the user's own pauses in their transcripts and
suggests a threshold; `ORMEGGIO threshold --save` writes it. `ORMEGGIO status`
shows when they last wrote and what the hook knows.
