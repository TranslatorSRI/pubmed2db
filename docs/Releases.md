# Releasing pubmed2db

Every release pairs a **code version** (`v1.1.0`) with the **data build** it
produced (`2026sep26`). The release branch is where the two meet: the cluster
runs from it, the bugs that run turns up are fixed on it or in the PRs feeding
it, and the data-build tag is cut from it. The version tag is cut from `main`
once the release PR has merged.

## Names

| Thing | Form | Example |
| --- | --- | --- |
| Milestone | `pubmed2db v<version>`, due on the planned build date | "pubmed2db v1.1" |
| Release branch | `pubmed2db-v<version>` | `pubmed2db-v1.1` |
| Release PR | `pubmed2db v<version>`, stacked on the last feature PR | "pubmed2db v1.1" |
| Data-build tag | the date the outputs were produced | `2026aug21` |
| Version tag and GitHub release | `v<version>.<patch>` / "pubmed2db v<version>.<patch>" | `v1.1.0` |

**Versions.**
- Raise the **minor** version for anything a consumer can safely ignore, such as a new JSON field or a new table or column.
- Raise the **major** version when an existing JSON field or Parquet column is renamed, removed or changes meaning.
- Raise the third component (`v1.1.1`) for a re-release that only fixes something. Version tags always carry all three (`v1.1.0`, not `v1.1`), the same form as the package version in `pyproject.toml`, while the milestone, release branch and release PR name only the first two. `v1.0` predates this and keeps its name.

## Steps

1. **Plan.**
   - Open a milestone named for the version, with the planned build date as its due date. It is named for the version, not the date, so a build that slips does not leave it misnamed; the data-build tag records the actual date.
   - Put the build's issues and PRs on it. An item the run only has to *observe*, such as a measurement read from its logs, goes on the next milestone, with a line in the release PR's post-run checklist to record what the run showed.
   - Feature PRs may be stacked on each other, and each is reviewed on its own.

2. **Cut the release branch.**
   - Branch from the tip of the last PR in the stack, or from `main` if there is no stack.
   - Open the release PR on the milestone, with that same branch as its base, so its diff shows only what the release branch adds. GitHub retargets it onto `main` once the feature PRs beneath it merge (step 5).
   - Its description lists the PRs it carries, anything the build needs that a routine run does not (a reload, new outbound hosts, new settings), and a checklist to clear before the run.

3. **Build on the cluster from the release branch** ([`slurm/README.md`](../slurm/README.md)). Fix what the run turns up:
   - Commit a small fix directly on the release branch.
   - Fix anything that belongs to a feature PR in that PR, then **merge** its branch into the release branch.
   - Merge rather than rebase: the commit the cluster ran has to stay reachable.

4. **Tag the data build** on the exact commit the cluster ran, and record that commit in the release PR:

   ```bash
   git tag -a 2026sep26 <sha> -m "pubmed2db used to create 2026sep26"
   git push origin 2026sep26
   ```

   Use the date the outputs were produced, which need not be the milestone's date.

5. **Merge the feature PRs into `main`**, in stack order, with **merge commits** (see below). The repository deletes a merged branch, and GitHub then retargets the next PR in the stack onto `main`.

   GitHub does not re-read a PR's closing keywords when it retargets the PR. After each retarget, check `gh pr view <N> --json closingIssuesReferences`, and if an issue is missing, edit the description. Saving it unchanged is not enough: in the v1.1 cycle, #58's `Closes #11. Closes #37.` stayed unlinked through its retarget and through an unchanged re-save, and linked as soon as one of its sentences changed.

6. **Wrap up the release PR.**
   - Bring its description up to date: what shipped, what the run measured, and what was deferred.
   - Merge it with a merge commit.

7. **Tag the version on `main` and publish it.** Pull `main` and tag the release PR's merge commit, by its id rather than `HEAD` in case something else has merged since:

   ```bash
   git switch main && git pull
   git tag -s v1.1.0 "$(gh pr view 55 --json mergeCommit -q .mergeCommit.oid)" -m "pubmed2db v1.1.0"
   git push origin v1.1.0
   gh release create v1.1.0 --title "pubmed2db v1.1.0" --generate-notes --notes-start-tag v1.0
   ```

   Tag `main`, not the release branch's last commit. If `main` moved on while the release was being built and was never merged into the release branch, the merge commit is the only commit holding exactly what shipped; the branch's last commit lacks `main`'s changes. Tagging `main` also puts the tag on its first-parent history, where `git describe` finds it.

   `--generate-notes` lists the merged PRs by title, which is why PR titles are written as changelog lines.

8. **Close the milestone.** Move anything still open on it to the next version's milestone or to one of the undated ones ("Needed soon", "Needed later", "Not urgent"). Never rename a milestone to reuse it for the next version: its closed items are the record of what that version shipped.

## Why merge commits, not squash

Squashing replaces a branch's commits with a new one on `main`. That breaks two things this process relies on:

- **Stacked PRs.** A PR stacked on a squashed branch shows its parent's commits again, and has to be rebased before it can merge.
- **Tags.** The data-build tag points at the commit the cluster actually ran. A squash leaves that commit off `main`'s history, and `git describe` and the release's changelog comparison lose track of it.

Merge commits keep every commit that was built, tagged or reviewed on `main`.
