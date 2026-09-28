# Releasing pubmed2db

Every release pairs a **code version** (`v1.1`) with the **data build** it
produced (`2026sep26`). The release branch is where the two meet: the cluster
runs from it, the bugs that run turns up are fixed on it or in the PRs feeding
it, and both tags are cut from it.

## Names

| Thing | Form | Example |
| --- | --- | --- |
| Milestone | `pubmed2db v<version>`, due on the planned build date | "pubmed2db v1.1" |
| Release branch | `pubmed2db-v<version>` | `pubmed2db-v1.1` |
| Release PR | `pubmed2db v<version>`, stacked on the last feature PR | "pubmed2db v1.1" |
| Data-build tag | the day the export finished (step 4) — unknown until then, so write `2026sepNN` in anything drafted before the run | `2026sep26` |
| Version tag and GitHub release | `v<version>` / "pubmed2db v<version>" | `v1.0` |

**Versions.**
- Raise the **minor** version for anything a consumer can safely ignore, such as a new JSON field or a new table or column.
- Raise the **major** version when an existing JSON field or Parquet column is renamed, removed or changes meaning.
- Add a third component (`v1.1.1`) for a re-release that only fixes something.

## Steps

1. **Plan.**
   - Open a milestone named for the version, with the planned build date as its due date. It is named for the version, not the date, so a build that slips does not leave it misnamed; the data-build tag records the actual date.
   - Put the build's issues and PRs on it. An item the run only has to *observe*, such as a measurement read from its logs, goes on the next milestone, with a line in the release PR's post-run checklist to record what the run showed.
   - Feature PRs may be stacked on each other, and each is reviewed on its own.

2. **Cut the release branch.**
   - Branch from the tip of the last PR in the stack, or from `main` if there is no stack.
   - Open the release PR on the milestone, with that same branch as its base, so its diff shows only what the release branch adds. GitHub retargets it onto `main` once the feature PRs beneath it merge (step 5).
   - Its description lists the PRs it carries, anything the build needs that a routine run does not (a reload, new outbound hosts, new settings), and a checklist to clear before the run.
   - Every item on that checklist is required. Something worth doing only if convenient goes on the next release's checklist instead: v1.1's one "optionally" item, the DuckDB cgroup probe, was the one skipped.

3. **Build on the cluster from the release branch** ([`slurm/README.md`](../slurm/README.md)).
   - Record the `submit.sh` command in the release PR. The logs carry the rest: each step's log opens with the commit, the allocation and every setting it ran with, overrides included, and each command then logs DuckDB's effective limits ([`slurm/README.md`](../slurm/README.md#monitoring-memory-and-runtime)). The v1.1 build's logs had neither, so its limits had to be confirmed from memory.

   Fix what the run turns up:
   - Put the fixes in a PR against the release branch (#58 for v1.1), so they are reviewed and described like any other change, and merge it into the release branch.
   - GitHub links a closing keyword only while the PR's base is the default branch, so one in that PR does nothing. An issue the run answers gets `Closes #N` in the **release** PR. But the release PR is stacked on a feature branch too, so GitHub does not link those keywords either until it is retargeted onto `main` (step 5). After the retarget, check with `gh pr view <N> --json closingIssuesReferences`. If any are missing, re-save the description or link them from the PR's *Development* sidebar. Whether GitHub re-reads the keywords on retarget has not been tested. On v1.1, #55 linked `closes #46`, written while it briefly targeted `main`, but not #11 and #37, which were added after it was retargeted.
   - Fix anything that belongs to a feature PR in that PR, then **merge** its branch into the release branch.
   - Merge rather than rebase: the commit the cluster ran has to stay reachable.

4. **Tag the data build** on the exact commit the cluster ran, and record that commit in the release PR:

   ```bash
   git tag -a 2026sep26 <sha> -m "pubmed2db used to create 2026sep26"
   git push origin 2026sep26
   ```

   Use the day the export finished, which need not be the milestone's date
   (`2026aug21` was a run that started on the 20th). The better name is the
   date of the last update file the build loaded, since that is the cutoff —
   the next file belongs to the next build — but nothing reports it yet (#57).

   Once the tag exists, a bare `git describe` names the *build*, not the version: `2026sep26` on the tagged commit, `2026sep26-7-gaa2b666` seven commits later. Before it exists, it names the nearest version tag instead (`v1.0-26-gfca8601`), which reads like a version and is not one. Ask for the one you mean:

   ```bash
   git describe --match 'v*'    # the code version:  v1.0-26-gfca8601
   git describe --match '20*'   # the data build:    2026sep26
   ```

5. **Merge the feature PRs into `main`**, in stack order, with **merge commits** (see below). The repository deletes a merged branch, and GitHub then retargets the next PR in the stack onto `main`.

6. **Wrap up the release PR.**
   - Bring its description up to date: what shipped, what was deferred, and how the run went.
   - Record the run's measurements in the repo, not only in the description: `slurm/README.md`'s run tables take the timings and peaks, and each issue the run answered gets its numbers in a comment. The description links to them. It is read at review and at changelog time and never again, so it cannot be the only copy.
   - Merge it with a merge commit.

7. **Tag the version and publish it.**
   - Tag the release branch's final commit, as `v1.0` was.
   - The merge commit keeps that commit reachable from `main`, and its tree is the same as the merge result.

   ```bash
   git tag -a v1.1 <sha> -m "pubmed2db v1.1"
   git push origin v1.1
   gh release create v1.1 --title "pubmed2db v1.1" --generate-notes --notes-start-tag v1.0
   ```

   `--generate-notes` lists the merged PRs by title, which is why PR titles are written as changelog lines.

8. **Close the milestone.** Move anything still open on it to the next version's milestone or to one of the undated ones ("Needed soon", "Needed later", "Not urgent"). Never rename a milestone to reuse it for the next version: its closed items are the record of what that version shipped.

## Why merge commits, not squash

Squashing replaces a branch's commits with a new one on `main`. That breaks two things this process relies on:

- **Stacked PRs.** A PR stacked on a squashed branch shows its parent's commits again, and has to be rebased before it can merge.
- **Tags.** The data-build tag points at the commit the cluster actually ran. A squash leaves that commit off `main`'s history, and `git describe` and the release's changelog comparison lose track of it.

Merge commits keep every commit that was built, tagged or reviewed on `main`.
