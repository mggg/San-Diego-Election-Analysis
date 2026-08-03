#!/bin/bash

#SBATCH --mem=32G
#SBATCH --cpus-per-task=64
#SBATCH --time=08:00:00
#SBATCH --partition=general
#SBATCH --qos=general
#SBATCH --job-name=san-diego-sim
#SBATCH --output=slurm_outputs/san-diego-sim_%j.out
#SBATCH --error=slurm_logs/san-diego-sim_%j.log

# NOTE: --time above assumes ALL configs in configs/*.json get run in one job
#       (main() now loops over every config file it finds there, not just one).
#       Check how many config files are in configs/ and roughly how long a single
#       run takes before trusting this time budget on a big batch.
#
# NOTE: Consider --qos=protected once you've benchmarked and are ready to run for
#       real — it just means the job won't get pre-empted mid-run. Don't burn that
#       budget on test runs.
#
# NOTE: --requeue could make sense here since the pipeline is staged and checks
#       for already-completed outputs (has_valid_district_outputs, has_valid_settings,
#       etc. all skip stages that are already done) — but if you add --requeue you
#       should double check those "has_valid_*" checks actually catch a job that died
#       mid-write, not just one that never started.

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
"$UV" run python run.py
