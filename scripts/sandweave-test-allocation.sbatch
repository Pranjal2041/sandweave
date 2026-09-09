#!/bin/bash
# Disposable resources for SDK acceptance; no user workloads are started here.
#SBATCH --job-name=sandweave-e2e
#SBATCH --partition=preempt
#SBATCH --qos=preempt_qos
#SBATCH --gres=gpu:L40S:1
#SBATCH --cpus-per-task=12
#SBATCH --mem=96G
#SBATCH --time=03:00:00
#SBATCH --output=runs/sandweave-slurm-%j.log
set -euo pipefail
hostname
sleep infinity
