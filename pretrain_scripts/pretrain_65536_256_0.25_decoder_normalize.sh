#! /bin/bash
##SBATCH --output=/checkpoint/chuntinz/fairseq/logs/slurm-%A.out
##SBATCH --error=/checkpoint/chuntinz/fairseq/logs/slurm-%A.err
#SBATCH --job-name=pretrain.v4.doc.c0.25.65536.chunk.512.no.shard
#SBATCH --partition=priority
#SBATCH --comment="2.6 ICML"
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=8
#SBATCH --gres=gpu:8
#SBATCH --mem=480g
#SBATCH -C volta32gb
#SBATCH --cpus-per-task=10
##SBATCH --signal=B:USR1@60 #Signal is sent to batch script itself
##SBATCH --open-mode=append
#SBATCH --time=4320

trap_handler () {
   echo "Caught signal: " $1
   # SIGTERM must be bypassed
   if [ "$1" = "TERM" ]; then
       echo "bypass sigterm"
   else
     # Submit a new job to the queue
     echo "Requeuing " $SLURM_ARRAY_JOB_ID $SLURM_ARRAY_TASK_ID
     # SLURM_JOB_ID is a unique representation of the job, equivalent
     # to above
     scontrol requeue $SLURM_JOB_ID
   fi
}


# Install signal handler
trap 'trap_handler USR1' USR1
trap 'trap_handler TERM' TERM

module load cuda/10.0
source activate py36

# The ENV below are only used in distributed training with env:// initialization
#export MASTER_ADDR=${SLURM_NODELIST:0:9}${SLURM_NODELIST:10:3}
#export MASTER_PORT=15213

DATE=`date +%Y%m%d`

SHARD_ROOT_PATH='/checkpoint/chuntinz/work/data/data-bin/shard-doc-ende19'
NUM_SHARDS=10
DATA=${SHARD_ROOT_PATH}/shard0

for (( SHARD_NUM=1; SHARD_NUM<=$NUM_SHARDS; SHARD_NUM++ ))
do
      DATA=$DATA:${SHARD_ROOT_PATH}/shard${SHARD_NUM}/
done
echo $DATA

DATA='/checkpoint/chuntinz/work/data/data-bin/doc-ende19-v2'
SAVE_ROOT=/checkpoint/chuntinz/work/fairseq/saved_models

PORT=15213
model=vqvae_lm_base
run_name="v4_pretrain_c0.25_doc19_soft_15_chunk_256_65536_no_shard_exp_10k"
SAVE=${SAVE_ROOT}/$model_${run_name}
mkdir -p ${SAVE}

cp $0 ${SAVE}/run.sh

srun --label python -u train.py ${DATA} \
    --arch ${model} --distributed-port $PORT --distributed-world-size 16 \
    --task VQVAE_language_modeling \
    --criterion vqvae_label_smoothed_cross_entropy \
    --save-dir $SAVE \
    --seed 1 --use-stride-first 1 \
    --use-context-dataset 0 --context-mode doc --window-size 0 \
    --decoder-layers 6 \
    --use-deconv 1 \
    --quantize-explore-steps 10000 \
    --no-cross-attention \
    --bottom-latent-k 65536 \
    --tensorboard-logdir ./tb-logs/$run_name \
    --add-latent-positions 0 \
    --encoder-form 'conv' \
    --bottom-conv-stride '2,2' \
    --bottom-conv-kernel-size '5,5' \
    --soft-em 1 --soft-max-temp 15.0 --soft-min-temp 15.0 \
    --soft-temp-anneal-steps 0 --soft-samples 10 \
    --commitment-cost 0.25 \
    --max-update 700000 \
    --warmup-updates 6000 --warmup-init-lr 1e-07 \
    --optimizer adam --lr 0.0003 --min-lr '1e-09' --lr-scheduler inverse_sqrt --weight-decay 0.0001 --adam-betas '(0.9, 0.98)' \
    --update-freq 1  --save-interval-updates 20000 \
    --tokens-per-sample 256 --max-tokens 6084 --max-target-positions 1024 \
    --sample-break-mode 'complete_doc' --skip-invalid-size-inputs-valid-test --ddp-backend=no_c10d \
    --label-smoothing 0.1 --decoder-normalize-before \
    --keep-last-epochs 5 \
    --dataset-impl mmap --num-workers 0 \
    --log-format simple --log-interval 500 \
    --share-all-embeddings | tee ${SAVE}/log.txt

