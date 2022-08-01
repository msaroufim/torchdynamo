#!/usr/bin/env python

"""
A wrapper over the benchmark infrastructure to generate commonly used commands,
parse results and generate csv/graphs.

The script works on manually written TABLE (see below). We can add more commands
in the future.

One example usage is
-> python benchmarks/runner.py --suites=torchbench --inference
This command will generate the commands for the default compilers (see DEFAULTS
below) for inference, run them and visualize the logs.

If you want to just print the commands, you could use the following command
-> python benchmarks/runner.py --print_run_commands --suites=torchbench --inference

Similarly, if you want to just visualize the already finished logs
-> python benchmarks/runner.py --visualize_logs --suites=torchbench --inference

If you want to test float16
-> python benchmarks/runner.py --suites=torchbench --inference --dtypes=float16

"""

import argparse
import io
import itertools
import os
from os.path import exists

import matplotlib.pyplot as plt
import pandas as pd
import torch
from matplotlib import rcParams
from tabulate import tabulate

import torchdynamo

rcParams.update({"figure.autolayout": True})
plt.rc("axes", axisbelow=True)

DEFAULT_OUTPUT_DIR = "benchmark_logs"


TABLE = {
    "training": {
        "ts_nnc": "--training --speedup-ts --use-eval-mode --isolate",
        "ts_nvfuser": "--training --nvfuser --speedup-dynamo-ts --use-eval-mode --isolate",
        "aot_eager": "--training --accuracy-aot-nop --generate-aot-autograd-stats --use-eval-mode --isolate",
        "aot_nnc": "--training --accuracy-aot-ts-mincut --use-eval-mode --isolate",
        "aot_nvfuser": "--training --nvfuser --accuracy-aot-ts-mincut --use-eval-mode --isolate",
        "inductor_cudagraphs": "--training --inductor --use-eval-mode --isolate",
    },
    "inference": {
        "ts_nnc": "-dcuda --isolate --speedup-ts",
        "ts_nvfuser": "-dcuda --isolate -n100 --speedup-ts --nvfuser",
        "trt": "-dcuda --isolate -n100 --speedup-trt",
        "eager_cudagraphs": "-dcuda --inductor-settings --float32 -n50 --backend=cudagraphs",
        "nnc_cudagraphs": "-dcuda --inductor-settings --float32 -n50 --backend=cudagraphs_ts --nvfuser",
        "ts_nvfuser_cudagraphs": "-dcuda --inductor-settings --float32 -n50 --backend=cudagraphs_ts",
        "inductor_cudagraphs": "-dcuda --inductor-settings --float32 -n50 --inductor",
    },
}

INFERENCE_COMPILERS = tuple(TABLE["inference"].keys())
TRAINING_COMPILERS = tuple(TABLE["training"].keys())

DEFAULTS = {
    "training": ["ts_nvfuser", "aot_nvfuser", "inductor_cudagraphs"],
    "inference": ["ts_nvfuser_cudagraphs", "inductor_cudagraphs"],
    "dtypes": [
        "float32",
    ],
    "suites": ["torchbench", "huggingface", "timm_models"],
    "devices": [
        "cuda",
    ],
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--devices", action="append", help="cpu or cuda")
    parser.add_argument("--dtypes", action="append", help="float16/float32/amp")
    parser.add_argument("--suites", action="append", help="huggingface/torchbench/timm")
    parser.add_argument(
        "--compilers",
        action="append",
        help=f"For --inference, options are {INFERENCE_COMPILERS}. For --training, options are {TRAINING_COMPILERS}",
    )
    parser.add_argument(
        "--quick", action="store_true", help="Just runs one model. Helps in debugging"
    )
    parser.add_argument(
        "--output-dir", help="Choose the output directory to save the logs"
    )

    # Choose either generation of commands, pretty parsing or e2e runs
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        "--print_run_commands",
        action="store_true",
        help="Generate commands and saves them to run.sh",
    )
    group.add_argument(
        "--visualize_logs",
        action="store_true",
        help="Pretty print the log files and draw graphs",
    )
    group.add_argument(
        "--run",
        action="store_true",
        default=True,
        help="Generate commands, run and parses the files",
    )

    # Choose either inference or training
    group_mode = parser.add_mutually_exclusive_group(required=True)
    group_mode.add_argument(
        "--inference", action="store_true", help="Only run inference related tasks"
    )
    group_mode.add_argument(
        "--training", action="store_true", help="Only run training related tasks"
    )
    group_mode.add_argument(
        "--coverage", action="store_true", help="Run coverage experiments"
    )

    args = parser.parse_args()
    return args


def generate_commands(args, dtypes, suites, devices, compilers, output_dir):
    if args.inference:
        mode = "inference"
    elif args.training:
        mode = "training"
    else:
        assert args.coverage
        mode = "coverage"
    with open("run.sh", "w") as runfile:
        lines = []

        lines.append("# Setup the output directory")
        lines.append(f"rm -rf {output_dir}")
        lines.append(f"mkdir {output_dir}")
        lines.append("")

        for iter in itertools.product(suites, devices, dtypes):
            suite, device, dtype = iter
            lines.append(
                f"# Commands for {suite} for device={device}, dtype={dtype} for {mode}"
            )

            if args.coverage:
                output_filename = f"{output_dir}/{suite}_{dtype}_{mode}_{device}.csv"
                cmd = f"python benchmarks/{suite}.py --{dtype} -d{device} --output={output_filename} --coverage"
                lines.append(cmd)
            else:
                info = TABLE[mode]
                for compiler in compilers:
                    base_cmd = info[compiler]
                    output_filename = (
                        f"{output_dir}/{compiler}_{suite}_{dtype}_{mode}_{device}.csv"
                    )
                    cmd = f"python benchmarks/{suite}.py --{dtype} -d{device} --output={output_filename} {base_cmd}"
                    if args.quick:
                        if suite == "torchbench":
                            cmd = f"{cmd} --only=resnet18"
                        elif suite == "huggingface":
                            cmd = f"{cmd} --only=BertForPreTraining_P1_bert"
                        else:
                            raise NotImplementedError(
                                f"Quick not implemented for {suite}.py"
                            )
                    lines.append(cmd)
            lines.append("")
        runfile.writelines([line + "\n" for line in lines])


def pp_dataframe(df, title, output_dir, out_io=None):
    # Pretty print
    if out_io is not None:
        out_io.write("\n")
        out_io.write("~~~\n")
        out_io.write(f"Results for {title}\n")
        out_io.write(tabulate(df, headers="keys", tablefmt="pretty", showindex="never"))
        out_io.write("\n")
        out_io.write("~~~\n")

    # Save to csv, can be copy pasted in google sheets
    df.to_csv(f"{output_dir}/{title}.csv", index=False)

    # Graph
    labels = df.columns.values.tolist()
    labels = labels[2:]
    df.plot(
        x="name",
        y=labels,
        kind="bar",
        title=title,
        ylabel="Speedup over eager",
        xlabel="",
        grid=True,
        figsize=(max(len(df.index) / 4, 5), 10),
        edgecolor="black",
    )
    plt.tight_layout()
    plt.savefig(f"{output_dir}/{title}.png")


def build_summary(out_io):
    import git

    def print_commit_hash(path, name):
        if exists(path):
            repo = git.Repo(path, search_parent_directories=True)
            sha = repo.head.object.hexsha
            out_io.write(f"{name} commit: {sha}\n")
        else:
            out_io.write(f"{name} Absent\n")

    def env_var(name):
        out_io.write(f"{name} = {os.environ[name]}\n")

    out_io.write("## Commit hashes ##\n")
    print_commit_hash(".", "torchdynamo")
    print_commit_hash("../pytorch", "pytorch")
    print_commit_hash("../functorch", "functorch")
    print_commit_hash("../torchbenchmark", "torchbench")

    out_io.write("\n")
    out_io.write("## TorchDynamo config flags ##\n")
    for key in dir(torchdynamo.config):
        val = getattr(torchdynamo.config, key)
        if not key.startswith("__") and isinstance(val, bool):
            out_io.write(f"torchdynamo.config.{key} = {val}\n")

    out_io.write("\n")
    out_io.write("## Torch version ##\n")
    out_io.write(f"torch: {torch.__version__}\n")

    out_io.write("\n")
    out_io.write("## Environment variables ##\n")
    env_var("TORCH_CUDA_ARCH_LIST")
    env_var("CUDA_HOME")
    env_var("USE_LLVM")

    out_io.write("\n")
    out_io.write("## GPU details ##\n")
    out_io.write(f"CUDNN VERSION: {torch.backends.cudnn.version()}\n")
    out_io.write(f"Number CUDA Devices: {torch.cuda.device_count()}\n")
    out_io.write(f"Device Name: {torch.cuda.get_device_name(0)}\n")
    out_io.write(
        f"Device Memory [GB]: {torch.cuda.get_device_properties(0).total_memory/1e9}\n"
    )


def read_csv(output_filename):
    has_header = False
    n_cols = 3
    with open(output_filename, "r") as f:
        line = f.readline()
        if "dev" in line:
            has_header = True
            n_cols = len(line.rstrip().split())

    if has_header:
        return pd.read_csv(output_filename)
    else:
        assert n_cols == 3
        return pd.read_csv(
            output_filename, names=["dev", "name", "speedup"], header=None
        )


def parse_logs(args, dtypes, suites, devices, compilers, output_dir):
    if args.inference:
        mode = "inference"
    elif args.training:
        mode = "training"
    else:
        assert args.coverage
        mode = "coverage"
    out_io = io.StringIO()
    if mode == "coverage":
        out_io.write("\n")
        out_io.write("## Graph results ##\n")
        for iter in itertools.product(suites, devices, dtypes):
            suite, device, dtype = iter
            frames = []
            # Collect results from all the files
            output_filename = f"{output_dir}/{suite}_{dtype}_{mode}_{device}.csv"

            df = read_csv(output_filename)
            frames.append(df)

            # Merge the results
            if len(frames) == 1:
                df = frames[0]
            else:
                df = pd.merge(frames[0], frames[1], on=["dev", "name"])
                for idx in range(2, len(frames)):
                    df = pd.merge(df, frames[idx], on=["dev", "name"])

            # Pretty print and also write to a bargraph
            title = f"{suite}_{dtype}_{mode}_{device}"
            pp_dataframe(df, title, output_dir)

            # Sort the dataframe and pretty print
            sorted_df = df.sort_values(by="graphs", ascending=False)
            pp_dataframe(sorted_df, f"sorted_{title}", output_dir, out_io=out_io)
        print(out_io.getvalue())
        with open(f"{output_dir}/github_comment.txt", "w") as gh_fh:
            gh_fh.write(out_io.getvalue())
    else:
        build_summary(out_io)
        out_io.write("\n")
        out_io.write("## Performance results ##\n")
        for iter in itertools.product(suites, devices, dtypes):
            suite, device, dtype = iter
            frames = []
            # Collect results from all the files
            for compiler in compilers:
                output_filename = (
                    f"{output_dir}/{compiler}_{suite}_{dtype}_{mode}_{device}.csv"
                )

                df = read_csv(output_filename)
                df.rename(
                    columns={
                        "speedup": compiler,
                        "ts": compiler,
                        "ofi": f"ofi_{compiler}",
                    },
                    inplace=True,
                )
                frames.append(df)

            # Merge the results
            if len(compilers) == 1:
                df = frames[0]
            else:
                df = pd.merge(frames[0], frames[1], on=["dev", "name"])
                for idx in range(2, len(frames)):
                    df = pd.merge(df, frames[idx], on=["dev", "name"])

            # Pretty print and also write to a bargraph
            title = f"{suite}_{dtype}_{mode}_{device}"
            pp_dataframe(df, title, output_dir)

            # Sort the dataframe and pretty print
            sorted_df = df.sort_values(by=list(reversed(compilers)), ascending=False)
            pp_dataframe(sorted_df, f"sorted_{title}", output_dir, out_io=out_io)
        print(out_io.getvalue())
        with open(f"{output_dir}/github_comment.txt", "w") as gh_fh:
            gh_fh.write(out_io.getvalue())


if __name__ == "__main__":
    args = parse_args()

    def extract(key):
        return DEFAULTS[key] if getattr(args, key, None) is None else getattr(args, key)

    dtypes = extract("dtypes")
    suites = extract("suites")
    devices = extract("devices")

    if args.inference:
        compilers = DEFAULTS["inference"] if args.compilers is None else args.compilers
    else:  # args.training
        compilers = DEFAULTS["training"] if args.compilers is None else args.compilers

    output_dir = args.output_dir if args.output_dir is not None else DEFAULT_OUTPUT_DIR

    if args.print_run_commands:
        generate_commands(args, dtypes, suites, devices, compilers, output_dir)
    elif args.visualize_logs:
        parse_logs(args, dtypes, suites, devices, compilers, output_dir)
    elif args.run:
        generate_commands(args, dtypes, suites, devices, compilers, output_dir)
        # TODO - Do we need to worry about segfaults
        try:
            os.system("bash run.sh")
        except Exception as e:
            print(
                "Running commands failed. Please run manually (bash run.sh) and inspect the errors."
            )
            raise e
        parse_logs(args, dtypes, suites, devices, compilers, output_dir)
