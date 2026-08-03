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