#!/usr/bin/env bash

datasets="-k hf_Albert -k hf_GPT2 -k mnasnet1_0 -k mobilenet_v2 -k resnet50 -k shufflenet_v2_x1_0 -k squeezenet1_1 -k timm_efficientnet -k timm_regnet -k timm_vovnet" #  
repeat=50
fp="float16"
batch_size_file="\$(realpath benchmarks/torchbench_models_list.txt)"

EXP="$1"


methods=("eager")
for method in "${methods[@]}"; do
    cmd="AOT_PARTITIONER_DEBUG=1 PYTORCH_NVFUSER_DISABLE_FALLBACK=1 python benchmarks/torchbench.py --training --devices=cuda --isolate --skip-accuracy-check --${fp} --peak-memory-for-backend=${method} --output=exp.csv --batch_size_file ${batch_size_file} ${datasets}"
    echo "$cmd"
    eval "$cmd"
done
exit 1

if [[ "inductor" == "$EXP" ]]; then
    cmd="AOT_PARTITIONER_DEBUG=1 PYTORCH_NVFUSER_DISABLE_FALLBACK=1 python benchmarks/torchbench.py --training --devices=cuda --inductor --isolate --skip-accuracy-check --${fp} --batch_size_file ${batch_size_file} --repeat $repeat ${datasets}"
    echo "$cmd"
    eval "$cmd"
    exit 1
fi

if [[ "mem" != "$EXP" ]]; then
    methods=("accuracy-aot-ts-mincut" "accuracy-aot-ts" "accuracy-ts") #
    for method in "${methods[@]}"; do
        cmd="AOT_PARTITIONER_DEBUG=1 PYTORCH_NVFUSER_DISABLE_FALLBACK=1 python benchmarks/torchbench.py --training --devices=cuda --nvfuser --${method} --use-eval-mode --isolate --${fp} --batch_size_file ${batch_size_file} --repeat $repeat ${datasets}"
        echo "$cmd"
        eval "$cmd"
    done
else
    cmd="AOT_PARTITIONER_DEBUG=1 PYTORCH_NVFUSER_DISABLE_FALLBACK=1 python benchmarks/torchbench.py --training --devices=cuda --isolate --skip-accuracy-check --${fp} --peak-memory-for-backend=inductor --output=exp.csv --batch_size_file ${batch_size_file} ${datasets}"
    echo "$cmd"
    eval "$cmd"
    methods=("aot_nvfuser_nop" "aot_nop" "eager")
    for method in "${methods[@]}"; do
        cmd="AOT_PARTITIONER_DEBUG=1 PYTORCH_NVFUSER_DISABLE_FALLBACK=1 python benchmarks/torchbench.py --training --devices=cuda --use-eval-mode --isolate --skip-accuracy-check --${fp} --peak-memory-for-backend=${method} --output=exp.csv --batch_size_file ${batch_size_file} ${datasets}"
        echo "$cmd"
        eval "$cmd"
    done
fi