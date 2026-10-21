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
| Data-build tag | the UTC date NCBI posted the last file the build loaded, its cutoff (step 4); unknown until the download, so write `2026sepNN` in anything drafted before the run | `2026sep24` |
| Version tag and GitHub release | `v<version>.<patch>` / "pubmed2db v<version>.<patch>" | `v1.1.0` |

**Two version numbers, for two audiences.** The code version says how the *code* changed; the data model version says how *consumers* are affected. They move independently: a big code release can leave the data model alone, and a one-line fix can bump it.

**Code versions** (`vX.Y.Z`, the release tags). Tags always carry all three components (`v1.1.0`, not `v1.1`), the same form as `pyproject.toml`'s `version`, while the milestone, release branch and release PR name only the first two. `v1.0` predates this and keeps its name.

- **X** for a rewrite: a new language, framework or code organization.
- **Y** for a release with substantial changes or improvements. This is the usual release, one per milestone (`v1.1.0`, `v1.2.0`).
- **Z** for smaller improvements and fixes (`v1.1.1`). A Z release can still change output values, since a value fix never moves the data model version; its release notes say which values change and whether an existing database needs a reload, the same as a Y release's.

**The data model version** (`datamodel.DATA_MODEL_VERSION`, 1.0.0 since the build after `2026sep26`) is what consumers pin. It is standard semantic versioning, defined in [`docs/data-model/README.md`](./data-model/README.md#versioning-and-stability):

- **minor:** only additions, including a guarantee that leaves every old reading correct;
- **major:** anything renamed, removed, or changed in meaning so that an old reading is now wrong, however mildly;
- **patch:** not used. The version describes how to read the outputs, not what they contain, so a fix that changes values without changing how to read them is a code change, listed in the release notes with whether a database needs a reload.

A major bump is often breaking for one output and harmless for another (1.0.0 renamed Parquet tables and left the NDJSON untouched). So rather than a separate level for "breaks some consumers", every bump gets an entry in [the data model changelog](./data-model/Changelog.md) saying, output by output, what a consumer must do. A test fails until the entry exists. A release that changes the data model says so in its release PR and GitHub release notes, and links that entry.

## Steps

1. **Plan.**
   - Open a milestone named for the version, with the planned build date as its due date. It is named for the version, not the date, so a build that slips does not leave it misnamed; the data-build tag records the actual date.
   - Put the build's issues and PRs on it. An item the run only has to *observe*, such as a measurement read from its logs, goes on the next milestone, with a line in the release PR's post-run checklist to record what the run showed.
   - Decide whether the release changes the data model, and at which level. `tests/test_data_model.py` fails on any shape change that was not bumped, but only a person can spot a change of meaning, so ask of every merged PR: does a consumer need to read anything differently? If code reading it the old way is now wrong, the bump is major; if the old reading still holds and the PR only promises more, it is minor. A PR that only changes values (a parsing fix, an upstream library that parses differently) does not bump the version; list it in the release notes, with whether it needs a reload.
   - Feature PRs may be stacked on each other, and each is reviewed on its own.

2. **Cut the release branch.**
   - Branch from the tip of the last PR in the stack, or from `main` if there is no stack.
   - Set `version` in `pyproject.toml` to the release's version (`1.2.0` for v1.2) and run `uv lock`. It is the only place the code version is written: `pubmed2db --version`, and a database built outside a git checkout, read it from there.
   - Open the release PR on the milestone, with that same branch as its base, so its diff shows only what the release branch adds. GitHub retargets it onto `main` once the feature PRs beneath it merge (step 5).
   - Its description lists the PRs it carries, anything the build needs that a routine run does not (a reload, new outbound hosts, new settings), and a checklist to clear before the run.
   - Every item on that checklist is required. Something worth doing only if convenient goes on the next release's checklist instead: v1.1's one "optionally" item, the DuckDB cgroup probe, was the one skipped.

3. **Build on the cluster from the release branch** ([`slurm/README.md`](../slurm/README.md)).
   - Copy this build's lines from `data/logs/submit.log` into the release PR: `submit.sh` appends the command and commit behind every job it submits. The step logs carry the rest: each opens with the commit, the allocation and every setting it ran with, overrides included, and each command then logs DuckDB's effective limits ([`slurm/README.md`](../slurm/README.md#monitoring-memory-and-runtime)). The v1.1 build's logs had none of this, so its limits had to be confirmed from memory.

   Fix what the run turns up:
   - Put the fixes in a PR against the release branch (#58 for v1.1), so they are reviewed and described like any other change, and merge it into the release branch.
   - GitHub links a closing keyword only while the PR's base is the default branch, so one in that PR does nothing. An issue the run answers gets `Closes #N` in the **release** PR. But the release PR is stacked on a feature branch too, so those keywords need checking once it is retargeted onto `main` (step 5).
   - Fix anything that belongs to a feature PR in that PR, then **merge** its branch into the release branch.
   - Merge rather than rebase: the commit the cluster ran has to stay reachable.

4. **Archive, publish and tag the data build.** Once `validate` has passed, move the export out of `data/json` as a build directory, copy it (and the database, if it is published) to stars, and tag the exact commit the cluster ran, recording that commit in the release PR:

   ```bash
   uv run python scripts/archive_build.py                # data/json -> data/builds/2026sep24/, prints the rsync lines
   git tag -a 2026sep24 <sha> -m "pubmed2db used to create 2026sep24"
   git push origin 2026sep24
   ```

   The script prints the build name; it is `corpus.build_name` in the build's `metadata.json`. The README's ["Between runs"](../README.md#between-runs-what-to-keep-archive-and-delete) says what else the next run needs.

   The name is the build's **cutoff**: the UTC date NCBI posted the last file the build loaded. Every update file before it is in the build and every one after belongs to the next, so the name says what the build contains. `status` prints the same name, and `metadata.json` carries the file and the exact time (`corpus.cutoff_file`, `corpus.cutoff_published_at`).

   The builds before data model 1.0 were named for the day their export finished (`2026aug21`, `2026sep26`), which depended on how long the run took: `2026sep26`'s cutoff was `pubmed26n1636`, posted on 2026-09-24. Those tags keep their names.

   Once the tag exists, a bare `git describe` names the *build*, not the version: `2026sep26` on the tagged commit, `2026sep26-7-gaa2b666` seven commits later. Before it exists, it names the nearest version tag instead (`v1.0-26-gfca8601`), which reads like a version and is not one. Ask for the one you mean:

   ```bash
   git describe --match 'v*'    # the code version:  v1.0-26-gfca8601
   git describe --match '20*'   # the data build:    2026sep26
   ```

5. **Merge the feature PRs into `main`**, in stack order, with **merge commits** (see below). The repository deletes a merged branch, and GitHub then retargets the next PR in the stack onto `main`.

   GitHub does not re-read a PR's closing keywords when it retargets the PR. After each retarget, check `gh pr view <N> --json closingIssuesReferences`, and if an issue is missing, edit the description. Saving it unchanged is not enough: in the v1.1 cycle, #58's `Closes #11. Closes #37.` stayed unlinked through its retarget and through an unchanged re-save, and linked as soon as one of its sentences changed.

6. **Wrap up the release PR.**
   - Bring its description up to date: what shipped, what was deferred, and how the run went.
   - Record the run's measurements in the repo, not only in the description: `slurm/README.md`'s run tables take the timings and peaks, and each issue the run answered gets its numbers in a comment. The description links to them. It is read at review and at changelog time and never again, so it cannot be the only copy.
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

   Value fixes are not in the data model changelog, so these notes are the only place consumers find them ([`docs/data-model/README.md`](./data-model/README.md#versioning-and-stability) sends them here). Above the generated list, name every PR that changes output values, say for each whether an existing database needs a reload, and say whether this release's data build has it: a build on a fresh database does, one that continued an older database does only if it reloaded the files.

8. **Close the milestone.** Move anything still open on it to the next version's milestone or to one of the undated ones ("Needed soon", "Needed later", "Not urgent"). Never rename a milestone to reuse it for the next version: its closed items are the record of what that version shipped.

## Why merge commits, not squash

Squashing replaces a branch's commits with a new one on `main`. That breaks two things this process relies on:

- **Stacked PRs.** A PR stacked on a squashed branch shows its parent's commits again, and has to be rebased before it can merge.
- **Tags.** The data-build tag points at the commit the cluster actually ran. A squash leaves that commit off `main`'s history, and `git describe` and the release's changelog comparison lose track of it.

Merge commits keep every commit that was built, tagged or reviewed on `main`.
