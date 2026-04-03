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