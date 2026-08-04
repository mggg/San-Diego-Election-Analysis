### 1. `generate_profiles` reproducibility
- Still not fully reproducible across runs
- Tried:
  - setting the seed once globally  
  - setting the seed inside each call to `process_settings_file`  
  - deriving deterministic per-task seeds from the settings file name/path  

### 2. CLI autocomplete (Windows)
- Works on MacOS  
- Needs to be tested on Windows  

### 3. Intermediate output validation
- Currently only checks that the expected number of files are produced  
- Does not validate file contents  
- This was added to allow restarting mid-pipeline, but could be strengthened

### 4. `primary_turnout` needs its own profile archive
- `profiles.zip` holds one profile per (district, mode, replicate) and *every*
  voting rule runs on it, so a two-round rule whose narrowing round has a
  different electorate cannot share those ballots
- Ballots can't be reweighted after sampling — the profile CSV records rankings,
  not which bloc each ballot came from — so the primary round has to be sampled
  fresh into `primary_profiles.zip`, keyed by the same entry names
- Both two-round rules read the same primary archive, so they narrow on the same
  ballots (as the single-round rules already do with `profiles.zip`); the cost is
  one extra sample per district-profile, not one per rule
- `primary_turnout` is deliberately **not** in `PROFILE_SIGNATURE_KEYS`: it
  doesn't change `profiles.zip`'s contents, so folding it in would invalidate
  every standard profile for nothing. It has its own
  `primary_profiles_signature`, and is folded into
  `election_results_signature` because it does change two-round winners

### 5. Primary and general are recorded separately
- A two-round rule produces two outcomes worth keeping: who the primary advanced
  and who the general elected. Only the second used to be written, so the
  finalists could not be recovered without re-running the primary
- `primary_results/` mirrors `election_results/` file for file, sharing its
  `signature` and `profile_files` order, so the two join on row index
- `has_valid_election_results` checks for the primary file explicitly: results
  simulated before it existed look complete from the general's side alone


### 6. Voter models fix the ballot type
- `profile_class_for_mode` reads each generator's return annotation rather than a
  hand-kept table, so adding a generator to `generator_name_to_function` is
  enough for `simulate_elections` to know what it yields
- `ElectionPlanEntry.accepted_profiles` comes from the election class's own
  `profile` annotation, so union rules (BlockPlurality) run under both families
  instead of being forced into one
- Score ballots are only valid for the budget they were generated with, so the
  budget is part of the archive path: `<mode>/<budget>/<district_num>/<file>`
  (ranked models keep the original `<mode>/<district_num>/<file>`). Budgets come
  from the rules themselves via `score_rule_budgets`, so Cumulative and Limited
  with different budgets coexist in one run
- The budgets are folded into `profiles_signature`. They come from
  `voting_configs`, which is otherwise not profile-determining, but changing a
  budget changes which ballots must exist
