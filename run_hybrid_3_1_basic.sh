#!/bin/bash

#SBATCH --mem=32G
#SBATCH --cpus-per-task=64
#SBATCH --time=08:00:00
#SBATCH --partition=general
#SBATCH --qos=general
#SBATCH --job-name=san-diego-sim-hybrid31-basic35
#SBATCH --output=slurm_outputs/san-diego-sim-hybrid31-basic35_%j.out
#SBATCH --error=slurm_logs/san-diego-sim-hybrid31-basic35_%j.log

# Runs only configs/hybrid_3_1.json and configs/basic_3_5.json, one after the
# other -- unlike run.sh, which loops over every config in configs/. Each
# run.py invocation is a single named config (see README: "Run one config"),
# so this does NOT use --run-all.
#
# NOTE: --time above is a guess for two runs, not the whole-directory budget
#       run.sh assumes. Benchmark one run and adjust before trusting it.
#
# NOTE: The pipeline stages are skip-if-already-valid (has_valid_district_outputs,
#       has_valid_settings, etc.), so re-running this script after a partial
#       failure resumes rather than restarting from scratch.

mkdir -p slurm_outputs slurm_logs

echo "Job Name: $SLURM_JOB_NAME"
echo "Job ID: $SLURM_JOB_ID"
echo "Partition: $SLURM_JOB_PARTITION"
echo "CPUs allocated: $SLURM_CPUS_PER_TASK"
echo "Mem allocated: $SLURM_MEM_PER_NODE"

# Uses uv to run inside the project's managed environment (needs pyproject.toml / uv.lock
# in this directory). If this project doesn't use uv, swap this line for your usual
# venv/conda activation + `python run.py`.

UV=/home/alexandramarcos1/miniconda3/bin/uv

echo "=== Running configs/hybrid_3_1.json ==="
"$UV" run python run.py hybrid_3_1

echo "=== Running configs/basic_3_5.json ==="
"$UV" run python run.py basic_3_5

echo "=== Running configs/plurality_9_1.json ==="
"$UV" run python run.py plurality_9_1
