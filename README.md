# ormeggio

A Claude Code plugin for coming back after a few days away.

You leave a dozen sessions working and go. They carry on until something stops
them, and nothing stops them loudly. When you come back every session looks
idle, and telling the finished ones from the stalled ones and from the ones
waiting on a question from you means opening them one at a time and scrolling
to the bottom.

ormeggio does that reading for you, the moment you send your first message.

## What stops a session while you are away

Each of these happened on one machine in the autumn of 2026, more than once.

**The usage limit.** The turn ends with "You've hit your 5-hour limit" and the
session sits there. When the limit resets, nobody tells it.

**A login that expires.** Every session fails at once with a 401, and stays
failed after you log in again.

**A battery that runs out.** The machine shuts down in the middle of a turn. A
tool call never returns. The restart also empties `/private/tmp`, so whatever a
session left there for later is gone.

**An app that freezes.** Twenty sessions running at once kept the desktop app's
main thread busy for hours, and every tool call in every session waited ten
minutes for it.

**Background agents that never report back.** The session restarts, its agents
are reported as stopped, and nobody is there to answer the notification.

And the one that is not a failure at all: **a session that finished with a
question for you**, and has been waiting on it since.

## What you get

**A hook that notices you are back.** It runs on every message you send. Almost
always it reads one small file and says nothing. When the pause before your
message is longer than your usual nights, it reads the end of every transcript
that was active around the absence and gives Claude one paragraph: how long you
were away, whether the machine restarted, which sessions stalled and why, which
ones are waiting on you. Claude offers to resume them, and you say yes or no.

Only your own messages count. The hook also fires for background notifications,
for messages from other sessions, for scheduled runs and for `claude -p`
scripts, and its input has no field that says who typed. The transcript has
one, so the hook looks the prompt up there before deciding. Without that, a
night of agents talking to each other would look like you never left.

**A second hook that writes down failures.** When a turn ends on an API error,
Claude Code says which one. ormeggio keeps that line, so a usage limit or an
expired login is known later instead of guessed from the transcript.

**A skill, `/ormeggio`, that does the resume.** It sorts the sessions into the
ones that can carry on alone, the ones that need you, and the ones that are
fine. Then:

- In the desktop app, it sends each stalled session a message through the app's
  own session tools: pick up where you were, check what is already on disk,
  do not start anything twice. At most twelve at once, counting the sessions
  already running, and nothing starts while free memory is under 30%. The rest
  go in later waves, as the first ones finish.
- In a terminal, it resumes them with `claude -p --resume`, each in its own
  folder and permission mode, with the same limits, from a runner that keeps
  going after you close the window.
- Every open question goes in one place, numbered, each with a link back to its
  session. You answer them in one message and each answer is forwarded to the
  session that asked.

A session stopped by a usage limit is held while the limit is still used up,
because resuming it then only burns the next window.

## The threshold is yours

What counts as "away" depends on the person. A fixed 24 hours fires after every
long Saturday for one person and misses a real absence for another, so
ormeggio measures yours from your transcripts:

```bash
python3 skills/ormeggio/scripts/ormeggio.py threshold
```

On the machine it was built on, over the last 60 days:

```
1122 messages from you between Thu 10 Sep 01:01 and Sun 08 Nov 22:32 (8.1 s to read).

Longest pause of each day: median 10.9 h, under 16.9 h on three days out of four.
Longest pauses: 54.3 h, 51.9 h, 50.1 h, 49.2 h, 35.4 h, 31.4 h, 30.2 h, 26.1 h

Suggested threshold: 22 h. A pause longer than that is not one of your nights.
```

The rule: for every day, take the longest pause between two of your messages.
On three days out of four it stays under some length, and ormeggio waits a
quarter longer than that. The absences themselves sit in the top quarter, so
they do not drag the number up the way an average would. The same rule gave 22
hours on 30, 60 and 120 days of history. It never goes under 12 hours or over
48. Add `--save` to use it.

## Installing

One line:

```bash
claude plugin marketplace add nerln/ormeggio && claude plugin install ormeggio@ormeggio
```

Or load it for one session, which is the quickest way to see whether you want
it:

```bash
git clone https://github.com/nerln/ormeggio.git
claude --plugin-dir ./ormeggio
```

Or keep the clone and wire it in yourself:

```bash
git clone https://github.com/nerln/ormeggio.git
cd ormeggio
./install.sh           # says what it would do, writes nothing
./install.sh --apply
```

The skill goes in as a symlink, so a `git pull` updates it. The two hooks are
entries in `settings.json`, which other tools share, so the script adds only
what is missing, copies the file first and says where the copy went.

No dependencies past Python 3. The one macOS ships is enough.

It costs about 173 tokens in every session, which is the skill announcing that
it exists, and about 1,800 more when the skill runs. The hooks run in the
harness and add nothing to the model's context, except the one paragraph on the
day you come back. Numbers from `claude plugin details ormeggio@ormeggio`, not
estimated by hand.

## What it reads

- The end of each transcript under `~/.claude/projects` that changed around the
  absence. It reads the last 600 KB of a file, not the whole file.
- Its own state, in `~/.claude/ormeggio`.
- The Claude desktop app's session index, if there is one, to put the app's
  session id next to each transcript. The format is not documented and may
  change; when it does, ormeggio falls back to the transcripts alone and loses
  only the links.
- The machine's boot time and free memory.

It writes only to `~/.claude/ormeggio`, and it sends nothing over the network.
Messages to sessions are sent by Claude, through the app's own tools, after you
said yes.

## What it does not do

It does not resume a session you stopped yourself, an archived one, a scheduled
one, or one that stopped on a request the API refused. A "carry on" message
would only repeat the refusal, so those go in the list for you.

It does not answer the questions sessions left for you. It puts them in front
of you.

It does not restart a frozen desktop app. Closing the app ends every session
inside it, including the one you are talking to, and that is a decision for a
person at the keyboard.

## Checks

```bash
python3 tools/prova.py
```

Ninety of them, about fifteen seconds, all against a throwaway Claude home
built on the spot: a transcript written by hand for every way a session can
stop, a fake app index, a fake clock, a fake boot time, and a fake `claude`
binary for the terminal runner, which must never have more than its limit
running at once. They also run the prose through `tools/stylecheck.py`.

Then it was tried for real. Loaded with `--plugin-dir` into a live session with
the clock set three days ahead, the first reply opened by offering the resume.
The second message, two minutes later on the same clock, got nothing from the
hook, which is the point.

## Licence

GPL-3.0.
