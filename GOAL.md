# GOAL: Daily FargoRate pull runs on GitHub Actions, not locally

- Set: 2026-07-14
- Last updated: 2026-07-14 (PRIMARY GOAL DONE: Actions pull verified green; local retired)
- Autonomy: execute local/reversible work; gate outward/irreversible actions.

## End state
The daily rating pull is produced solely by the GitHub Actions workflow
(`.github/workflows/pull.yml`) on its schedule, committing baseline/change rows
to `data/history.csv`. The local Windows Scheduled Task stopgap (`FargoDailyPull`)
is retired. Project docs and memory no longer claim a billing outage / local
stopgap. The repo tests pass.

## Definition of Done (whole goal)
- GitHub Actions `pull.yml` runs daily and succeeds (verified against `gh run list`).
- The local Scheduled Task `FargoDailyPull` no longer runs the pull (unregistered).
- No dual-writer race: only Actions commits pull data to `main`.
- Stale "billing-blocked / local stopgap" claims updated (memory + project notes).
- `pytest` green.

## Context (verified 2026-07-14)
- GitHub Actions billing outage (memory `local-pull-stopgap`, 2026-06-20) is OVER:
  scheduled `fargo-pull` runs succeed daily (07-12, 07-13 green at ~70 min).
- Runs 07-08..07-11 were cancelled at exactly ~45 min = the OLD timeout; commit
  `230b439` raised it 45->100 for roster growth, and runs recovered.
- The local task has been BROKEN since 07-08: every run logs only `=== run start ===`
  with no completion (terminated; `LastTaskResult=267014` = SCHED_S_TASK_TERMINATED,
  `last-success.txt` frozen at 07-07). It also caused `.git/index.lock` + rebase
  errors racing the Actions push. It is dead weight and a hazard.
- `pytest -q`: 78 passed.

## Steps (status: [x] done, [~] in progress, [ ] todo)
- [x] Retire the local stopgap
      DoD: `FargoDailyPull` scheduled task unregistered; `Get-ScheduledTask *Fargo*`
      returns nothing; `Fargo-local-runner\` left as an inert log archive (outside repo).
      DONE 2026-07-14: unregistered; no `*Fargo*` tasks remain.
- [x] Confirm Actions carries the daily pull alone
      DoD: today's scheduled run (13:17 UTC) or a manual `workflow_dispatch` completes
      green and commits data (or a clean no-op); schedule remains enabled.
      DONE 2026-07-14: manual run 29332370560 succeeded (~69 min), committed 46d23cf
      "data: rating/robustness changes 2026-07-14". Actions is the sole daily-pull writer.
- [~] Refresh stale stopgap claims
      DoD: memory `local-pull-stopgap` updated to "billing restored, local task
      removed"; project local-skill notes that reference the stopgap reconciled.
      DONE: memory + MEMORY.md index updated. Repo local-skill notes still reference
      the stopgap; deferred (CLAUDE.md carries foreign drift - avoid entangling).

## Hard boundaries / gotchas
- CLAUDE.md carries uncommitted drift from another session (capabilities-block
  sync). Do NOT sweep it into any commit; stage only this-session paths.
- Removing the local task is reversible (re-register from `Fargo-local-runner\run-pull.ps1`).
- Watch item (not in scope): pull takes ~70 min vs the 100 min timeout; roster growth
  will eventually need parallelized fetches or a higher timeout.

## Related workstream: roster/crosswalk completeness (approved 2026-07-14)
Prompted by "are we tracking all players across the pool repos?". The daily pull
covers all 3,847 rostered ids; completeness gaps + downstream bugs found:
- Person/crosswalk layer was stale (Jun 27); regen lifts persons 2,346 -> 3,842.
- BUG: `people.py` BCA crosswalk reads `bca_division_id`+`name` that don't exist on
  the link (name is on the parent entry), so all 1,571 BCA links collapse to bca=1.
  PoolPredict gets ~0 BCA cross-links. Fix delegated to entity-resolution-engineer.
- Staged-but-not-admitted (precision-first, need review): APA ~319, NAPA ~144, BCA ~74.
- Unfound (no FargoRate identity, untrackable now): APA 1,175, NAPA 420, BCA 37.
- Task-2 blocker: siblings NAPA/APA-Scraper/bca are all PRIVATE and `REF_TOKEN` is
  NOT set, so NAPA/full-APA/BCA resolves can't clone `_ref` on Actions. APA-queue
  re-resolve runs without a token. No `resolve-bca.yml` exists at all.
- Sequencing: land the BCA code fix on main FIRST (resolve-napa.yml regenerates the
  crosswalk on the runner with repo code), THEN run resolves, THEN review staged.

### Completeness steps
- [x] Fix BCA crosswalk bug (people.py/audit.py + tests) -- DONE, pushed (c16a8e5).
      crosswalk bca 1->1,659; ambiguous id-less keys flagged; collision_allowlist.json
      accepts reviewed same-name-same-league cases (BCA Patrick Riley).
- [~] Re-run resolves on Actions -- APA-queue DONE on Actions (d64ac23; +players,
      re-staged). It surfaced 2 collisions (auto-add re-added reassigned/duplicate APA
      links); RESOLVED (e9be9d0): merged Bryson Ford; unlinked Andrew Tran TX namesake via
      apa_unlink.json. NAPA/full-APA/BCA resolves still need a REF_TOKEN PAT (deferred by
      user choice); no resolve-bca.yml exists.
- [~] Review remaining staged candidates -- COLLISIONS resolved (the urgent part): the
      full-APA crossref surfaced 5 more (4 wrong-person links unlinked + 1 Gonzales merge,
      63615cf). The bulk variant/ambiguous candidates (APA ~75/~193, NAPA ~25/~77) remain
      STAGED for review -- medium/low confidence, precision-first, not urgent.
- [x] Update stale docs -- DONE, pushed. CLAUDE.md (492a922), design doc
      cross-league-identity.md (16edaa0) synced to "BCA built / <league> key / ambiguity";
      fargo-pull-doctor + fargo-resolve-local SKILL.md updated (local/gitignored).
- [x] Harden importer `add` against cross-id dup links -- DONE, pushed. import_apa
      (c4d7ffc) + import_napa (0f54b6c) now skip + record a member already on a different
      Fargo id to `<source>_add_conflicts.json`; import_bca intentionally excluded
      (its <league>:<norm name> key legitimately maps 2 people -> allowlist handles it).
- [x] NAPA + full-APA re-resolves -- DONE (local, 63615cf). No token needed: the
      authenticated `gh` login clones the private siblings directly (nothing stored). NAPA
      +13 (clean); APA full crossref +1 + 5 collisions resolved.
- [ ] BCA re-resolve -- DEFERRED. `basket/bca_players.json` must be rebuilt by aggregating
      the bca repo's 19 per-league `data/exports/*` dirs (no transform in this repo), for
      near-zero yield (BCA backfilled ~1 week ago via #15). Not worth it now; revisit if a
      BCA export transform is written.
- [x] CODE follow-up DONE (d75a2bb): moved the cross-id guard into the shared
      `_attach_crosslink` choke point so `crossref()` is guarded too (apa + napa); it now
      records a member already on another Fargo id to `<source>_add_conflicts.json` instead
      of creating a collision. +8 tests (95 pass).

## Log
- 2026-07-14: Diagnosed. Actions healthy (billing outage over, timeout bumped);
  local task broken since 07-08 and racing Actions. Tests green. Charter set.
- 2026-07-14: Unregistered `FargoDailyPull` (no `*Fargo*` tasks remain). Updated
  memory `local-pull-stopgap` + MEMORY.md index to "retired / Actions healthy".
- 2026-07-14: PRIMARY GOAL DONE -- manual pull run 29332370560 green (~69 min),
  committed 46d23cf. Actions is the sole daily-pull writer; local stopgap retired.
- 2026-07-14: Completeness -- fixed+pushed BCA crosswalk (bca 0->1,659) + audit +
  allowlist (c16a8e5); ran APA re-resolve on Actions (d64ac23); resolved the 2 collisions
  it surfaced -- merged Bryson Ford, unlinked Andrew Tran TX namesake (e9be9d0). Pushed.
