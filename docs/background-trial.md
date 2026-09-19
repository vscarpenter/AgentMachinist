# Background pilot trial worksheet

Use this worksheet with the [background pilot](background-pilot.md) to decide
whether AgentMachinist saves time across your own projects. The feature is an
unreleased source-checkout experiment. This blank worksheet records no live
results and makes no claim about time savings.

## Set the comparison before starting

Choose 10–15 real, bounded Tasks on a project other than AgentMachinist. Favor
reproducible bugs or maintenance with reliable tests. Do not create extra work
just to feed the bot. Split comparable tasks between background operation and
your usual direct-agent workflow, keeping the model and acceptance standard as
similar as practical. Avoid implementing the same task twice: the second attempt
benefits from knowledge gained on the first.

| Trial choice | Record |
| --- | --- |
| Repository and starting date | |
| Task class and excluded work | |
| Direct-agent comparison workflow | |
| Harness/model for each workflow | |
| Required local Gates and remote checks | |
| Worker image/configuration revision | |
| Runtime, repair, and open-PR limits | |
| Trial setup and maintenance budget | |
| Date to review results | |

## Record each Task

Copy this table once per Task. Measure active human time in minutes. Record
machine elapsed time separately; waiting overnight costs attention differently
from repeatedly diagnosing a stuck run. Include abandoned attempts and rejected
PRs rather than measuring only successes.

| Field | Record |
| --- | --- |
| Task/issue and PR link | |
| Workflow: background or direct | |
| Task class and rough complexity | |
| Preparation: issue, context, acceptance criteria | |
| Active review: diff, checks, findings | |
| Intervention: diagnosis, retry, manual fixes | |
| Cleanup and follow-up | |
| Rework discovered within one week | |
| Total active minutes | |
| Machine elapsed minutes | |
| Number of human interruptions | |
| Result: accepted, rejected, abandoned, still open | |
| Local Gates and exact candidate SHA | |
| Remote CI result on that SHA | |
| Review findings and human disposition | |
| Provider usage/cost, or unknown | |

## Account for the tool itself

Track setup, image rebuilds, authentication repairs, worker supervision,
AgentMachinist changes, and documentation/debugging time separately from Task
work. Count all of it when deciding whether to keep investing.

| Date | Maintenance activity | Active minutes | One-time setup or recurring |
| --- | --- | --- | --- |
| | | | |

## Make the decision

Compare median active minutes for comparable accepted changes, interruption
counts, rejected or abandoned work, and rework after a week. Report how many
Tasks are still open. Do not call an unreviewed PR a delivered saving or a passing
Phase an accepted change. A small sample guides a personal decision; it does
not establish a general productivity benchmark.

As an initial continuation rule, look for roughly 25–30% less active time on a
recurring Task class without a quality regression. Weekly time saved must also
exceed recurring maintenance. Keep one-time setup visible and estimate how many
weeks of actual use would recover it. Adjust the threshold before the trial if
your priorities differ, rather than changing it to fit the result.

| Decision question | Answer and evidence |
| --- | --- |
| Which recurring Task class saved attention? | |
| How much active time did each workflow require? | |
| What did setup and maintenance cost? | |
| Did defects, rejected PRs, or review backlog increase? | |
| Would I naturally choose the background workflow next week? | |
| Continue narrowly, revise the experiment, or stop investment? | |

Keep the decision small. A successful trial justifies continuing the useful
workflow; it does not by itself justify additional Harnesses, a hosted fleet,
automatic task discovery, or automatic merge.

Return to the [pilot guide](background-pilot.md) or [documentation index](README.md).
