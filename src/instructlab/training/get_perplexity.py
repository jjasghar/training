# SPDX-License-Identifier: Apache-2.0

# Standard
from copy import deepcopy
from pathlib import Path
import argparse
import math
import os
import re
import subprocess
import time

# Third Party
from accelerate import Accelerator
from deepspeed.ops.adam import DeepSpeedCPUAdam, FusedAdam
from deepspeed.runtime.zero.utils import ZeRORuntimeException

# pylint: disable=no-name-in-module
from instructlab.dolomite.hf_models import GPTDolomiteForCausalLM
from tqdm import tqdm
from transformers import AutoModelForCausalLM, get_scheduler
import torch
import torch.distributed

# First Party
from instructlab.training import config
from instructlab.training.async_logger import AsyncStructuredLogger
from instructlab.training.config import (
    DataProcessArgs,
    DistributedBackend,
    TorchrunArgs,
    TrainingArgs,
)
from instructlab.training.multipack_sampler import (
    find_packing_max_batch_len_and_grad_accum,
)
from instructlab.training.setup_accelerator import setup_accelerator
from instructlab.training.token_dataset import setup_dataloader, setup_dataset
from instructlab.training.tokenizer_utils import setup_tokenizer
from instructlab.training.utils import (
    StreamablePopen,
    add_noisy_embeddings,
    apply_gradient_checkpointing,
    check_flash_attn_enabled,
    convert_loss_to_reduce_sum,
    ensure_loadable_dolomite_checkpoint,
    get_projection_layer_names,
    load_latest_full_state,
    prepare_peft_model,
    prepare_universal_checkpoint_from_latest,
    retrieve_chat_template,
    save_checkpoint,
    save_hf_format_accelerate,
    set_random_seed,
    setup_logger,
)
import instructlab.training.data_process as dp


def setup_optimizer(args, model):
    if args.distributed_training_framework == DistributedBackend.FSDP.value:
        optimizer = torch.optim.AdamW(
            model.parameters(),
            lr=args.learning_rate,
            betas=(0.9, 0.95),
            weight_decay=0.0,
        )
    elif args.distributed_training_framework == DistributedBackend.DEEPSPEED.value:
        # need to use this only when the CPU offload optimizer is enabled
        if args.cpu_offload_optimizer:
            print(
                "\033[33m!!! CPU offload optimizer enabled, using DeepSpeedCPUAdam !!!\033[0m"
            )
            optimizer = DeepSpeedCPUAdam(
                model.parameters(), lr=args.learning_rate, betas=(0.9, 0.95)
            )
        else:
            optimizer = FusedAdam(
                model.parameters(), lr=args.learning_rate, betas=(0.9, 0.95)
            )
    else:
        raise ValueError(
            f"Sharding framework {args.distributed_training_framework} is not supported."
        )
    return optimizer


def setup_model(args, tokenizer, train_loader, grad_accum, flash_enabled):
    bnb_config = None
    if args.lora_r > 0 and args.lora_quant_bits == 4:
        # Third Party
        from transformers import BitsAndBytesConfig

        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.float16,  # if not set will throw a warning about slow speeds when training
        )

    base_model_args = {
        "pretrained_model_name_or_path": args.model_name_or_path,
        "torch_dtype": torch.bfloat16,
        "quantization_config": bnb_config,
    }
    if flash_enabled:
        base_model_args["attn_implementation"] = "flash_attention_2"

    if args.use_dolomite:
        with ensure_loadable_dolomite_checkpoint(
            args.model_name_or_path, args.output_dir
        ) as path:
            base_model_args["pretrained_model_name_or_path"] = path
            model = GPTDolomiteForCausalLM.from_pretrained(
                **base_model_args,
                use_padding_free_transformer=True,
            )
    else:
        model = AutoModelForCausalLM.from_pretrained(**base_model_args)

    if len(tokenizer) > model.config.vocab_size:
        print(
            f"WARNING: tokenizer has {len(tokenizer)} tokens but model has {model.config.vocab_size} vocab size"
        )
        model.resize_token_embeddings(
            int(8 * math.ceil(len(tokenizer) / 8.0))
        )  # make the vocab size multiple of 8 for sharding the embedding layer.

    # Fix any discrepancy between model and tokenizer
    if (
        model.config.pad_token_id is not None
        and tokenizer.pad_token_id is not None
        and model.config.pad_token_id != tokenizer.pad_token_id
    ):
        print(
            f"WARNING: There is a mismatch between pad token id of model ({model.config.pad_token_id}) and tokenizer({tokenizer.pad_token_id}). Fixing model pad token id to be same as tokenizer's pad token id"
        )
        model.config.pad_token_id = tokenizer.pad_token_id
    if (
        model.config.bos_token_id is not None
        and tokenizer.bos_token_id is not None
        and model.config.bos_token_id != tokenizer.bos_token_id
    ):
        print(
            f"WARNING: There is a mismatch between bos token id of model({model.config.bos_token_id}) and tokenizer({tokenizer.bos_token_id}). Fixing model bos token id to be same as tokenizer's bos token id"
        )
        model.config.bos_token_id = tokenizer.bos_token_id
    if (
        model.config.eos_token_id is not None
        and tokenizer.eos_token_id
        and model.config.eos_token_id != tokenizer.eos_token_id
    ):
        print(
            f"WARNING: There is a mismatch between eos token id of model({model.config.eos_token_id}) and tokenizer({tokenizer.eos_token_id}). Fixing model eos token id to be same as tokenizer's eos token id"
        )
        model.config.eos_token_id = tokenizer.eos_token_id

    assert model.__class__.__name__ in [
        "MistralForCausalLM",
        "GPTDolomiteForCausalLM",
        "LlamaForCausalLM",
        "Starcoder2ForCausalLM",
        "GemmaForCausalLM",
        "MixtralForCausalLM",
        "GraniteForCausalLM",
    ], f"Model class name: {model.__class__.__name__} is not supported."

    model = convert_loss_to_reduce_sum(model, use_dolomite=args.use_dolomite)
    model = add_noisy_embeddings(model, noise_alpha=args.NEFTune_alpha)

    # handling of gradient checkpointing
    # it is handled differently for lora and full
    # - with the exception of granite, which handles it
    #   in the later stanza
    if args.lora_r > 0:
        # if lora
        # Third Party
        from peft import LoraConfig

        # ensure we select only the modules that exist in the model
        proj_layers = get_projection_layer_names(model)
        if not args.lora_target_modules:
            print(
                f"WARNING: lora_target_modules was not specified, defaulting to all of the model's projection modules"
            )
            if not proj_layers:
                raise RuntimeError("could not find any projection layers in the model")
            args.__dict__["lora_target_modules"] = proj_layers
        else:
            # when the user specifies the module, we should verify that they align with what's in the model
            lora_target_modules_set = set(args.lora_target_modules)
            diff = lora_target_modules_set - set(proj_layers)
            layers_to_target = lora_target_modules_set - diff
            if len(diff) == len(args.lora_target_modules):
                raise ValueError(
                    f"None of the modules you requested exist in the model.\nRequested modules: {args.lora_target_modules}; Available modules: {proj_layers}.\nThis is usually a misconfiuration error. Consider omitting your `lora_target_modules` list to have these discovered automatically."
                )
            if diff:
                print(
                    f"\033[33mWARNING: the following modules were targeted for LoRA but are not present in the model: {list(diff)}. Applying LoRA only to {list(layers_to_target)} modules.\033[0m"
                )
            args.__dict__["lora_target_modules"] = list(layers_to_target)

        peft_config = LoraConfig(
            lora_alpha=args.lora_alpha,
            lora_dropout=args.lora_dropout,
            r=args.lora_r,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=args.lora_target_modules,
        )
        model = prepare_peft_model(
            model, peft_config, gradient_checkpointing=not args.use_dolomite
        )

    elif not args.use_dolomite:
        model.gradient_checkpointing_enable()

    # granite gradient checkpointing is handled uniformly
    # for both lora and full here
    if args.use_dolomite:
        block_name = model._no_split_modules[0]
        apply_gradient_checkpointing(
            model,
            block_name=block_name,
            use_reentrant=True,  # this should be the HF default mode
        )

        if args.lora_r > 0:

            def make_inputs_require_grad(module, input, output):
                output.requires_grad_(True)

            model.get_input_embeddings().register_forward_hook(make_inputs_require_grad)

    accelerator = setup_accelerator(args, model, grad_accum)
    if args.distributed_training_framework == DistributedBackend.FSDP.value:
        model = accelerator.prepare(model)
    optimizer = setup_optimizer(args, model)

    lr_scheduler = get_scheduler(
        name=args.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=args.num_warmup_steps,
        num_training_steps=args.num_epochs * len(train_loader) // grad_accum,
    )
    model, optimizer, _, lr_scheduler = accelerator.prepare(
        model,
        optimizer,
        deepcopy(train_loader),
        lr_scheduler,
    )
    # Necessary so that Accelerate does not step once per GPU
    # see https://github.com/huggingface/accelerate/blob/127818fc27ebe5cb236357fff59ff1748326d643/src/accelerate/scheduler.py#L69
    lr_scheduler.split_batches = True
    return model, lr_scheduler, optimizer, accelerator


# this function is to check if the checkpoint provided can be resumed
def maybe_resume_training(args, model):
    local_rank = int(os.environ["LOCAL_RANK"])

    # DS's loading function will not raise if fails to reload a checkpoint
    # - if lora is used, then the checkpoints will only be for the adapters
    #   so we need to disable load_module_strict
    # - load checkpoint will find the latest checkpoint
    # - it will also load the optimizer and scheduler states by default
    load_module_strict = args.lora_r == 0  # can only be true if lora is not used
    output_dir = Path(args.output_dir) / "ds_native"

    try:
        # attempt to load a regular checkpoint first
        model.load_checkpoint(output_dir, load_module_strict=load_module_strict)
    except ZeRORuntimeException as e:
        if str(e).startswith("The checkpoint being loaded used a DP world size of"):
            # if it fails with the above exception, then a universal
            # checkpoint is required

            # prepare the universal checkpoint
            # - by reading 'latest' to get the resumable checkpoint
            prepare_universal_checkpoint_from_latest(output_dir)

            # need to do this to trigger the universal checkpoint
            # loading
            model._config.load_universal_checkpoint = True

            # then attempt to load again
            model.load_checkpoint(output_dir, load_module_strict=load_module_strict)

            # reset to regular checkpoint loading
            model._config.load_universal_checkpoint = False
        else:
            raise e  # reraise

    # do this to figure out the last_step
    latest_file = output_dir / "latest"
    try:
        with open(latest_file) as f:
            # there is some assumption here that the ds_native
            # checkpoints are tagged as <something>_(samples_seen)
            step_folder = f.read()
            (samples_seen,) = re.match("\w+_(\d+)", step_folder).groups()
            samples_seen = int(samples_seen)

            last_step = samples_seen // args.effective_batch_size
            args.__dict__["last_step"] = last_step
        (
            print(f"\033[93mStarting from: {last_step}\033[0m")
            if local_rank == 0
            else None
        )
    except FileNotFoundError:
        pass

    # we will update the start step here
    return model

def get_perplexity(args, model, tokenizer, eval_loader, accelerator, metric_logger):
    model.eval()

    local_rank = int(os.environ["LOCAL_RANK"])
    world_size = int(os.environ["WORLD_SIZE"])
    total_num_tokens = 0
    total_log_perplexity = 0.0
    total_samples = 0
    
    with torch.no_grad():
        torch.distributed.barrier()  # Ensure all processes are synchronized before starting evaluation
        if local_rank == 0:
            pb = tqdm(range(len(eval_loader)), desc="Calculating Perplexity")
        
        for batch in eval_loader:
            # Move data to appropriate device
            num_loss_counted_tokens = batch.pop("num_loss_counted_tokens")
            num_samples = batch.pop("num_samples")
            if not args.use_dolomite:
                for k in batch:
                    batch[k] = batch[k].to(local_rank)

            # Forward pass to calculate the loss
            output = model(**batch, use_cache=False)
            loss = output.loss

            log_perplexity = loss.item()

            log_perplexity, num_loss_counted_tokens, num_samples = map(float,
                accelerator.reduce(
                    torch.tensor(
                        [log_perplexity, num_loss_counted_tokens, num_samples],
                        dtype=torch.float32,
                        device=accelerator.device,
                    ),
                    reduction="sum",
                ),
            )
            total_log_perplexity += log_perplexity
            total_num_tokens += int(num_loss_counted_tokens)
            total_samples += int(num_samples)

            if local_rank == 0:
                pb.update(1)
            
                metric_logger.log_sync(
                        {
                            "log_perplexity": log_perplexity,
                            "num_loss_counted_tokens": num_loss_counted_tokens,
                            "total_log_perplexity": total_log_perplexity,
                            "total_num_tokens": total_num_tokens,
                            "num_samples": num_samples,
                            "total_samples": total_samples,
                        }
                    )

        # Calculate final perplexity
        average_log_perp = total_log_perplexity/total_num_tokens
        avg_perplexity = math.exp(average_log_perp)

        if local_rank == 0:
            metric_logger.log_sync(
                        {
                            "average_log_perp": average_log_perp,
                            "avg_perplexity": avg_perplexity,
                            "total_log_perplexity": total_log_perplexity,
                            "total_num_tokens": total_num_tokens,
                            "total_samples": total_samples,
                        }
                    )

    return avg_perplexity



def main(args):
    # Third Party
    import yaml

    metric_logger = AsyncStructuredLogger(
        args.output_dir
        + f"/perplexity_log_{os.environ['RANK']}.jsonl"
    )
    if os.environ["LOCAL_RANK"] == "0":
        print(f"\033[38;5;120m{yaml.dump(vars(args), sort_keys=False)}\033[0m")
        metric_logger.log_sync({"script_params": vars(args)})

    setup_logger(args.log_level)
    CHAT_TEMPLATE, SPECIAL_TOKENS = retrieve_chat_template(args.chat_tmpl_path)
    tokenizer = setup_tokenizer(args.model_name_or_path, SPECIAL_TOKENS, CHAT_TEMPLATE)
    # device = torch.device("cuda", args.local_rank)

    #### distributed init #####
    torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))
    args.local_rank = int(os.environ["LOCAL_RANK"])
    torch.distributed.init_process_group("nccl")
    args.global_rank = torch.distributed.get_rank()
    tensor = torch.ByteTensor([False]).cuda()
    torch.distributed.all_reduce(tensor)
    torch.distributed.barrier()

    flash_enabled = check_flash_attn_enabled(args.disable_flash_attn, args.use_dolomite)

    dataset = setup_dataset(
        args.data_path,
        mock=args.mock_data,
        mock_len=args.mock_len,
    )

    args.sampler = "distributed"
    args.samples_per_gpu = 100
    grad_accum = 1
    packing_max_batch_len  = None

    train_loader = setup_dataloader(
        dataset,
        tokenizer.pad_token_id,
        num_workers=8,
        use_dolomite=args.use_dolomite,
        flash_enabled=flash_enabled,
        max_batch_len=args.max_batch_len,
        packing_max_batch_len=packing_max_batch_len,
        samples_per_gpu=args.samples_per_gpu,
        sampler=args.sampler,
        seed=args.seed,
    )

    if args.local_rank == 0:
        metric_logger.log_sync(
            {
                "num_gpus": torch.distributed.get_world_size(),
                "avg_sample_len": dataset.get_lengths().mean(),
                "effective_batch_size": args.effective_batch_size,
                "max_batch_len_per_gpu": args.max_batch_len,
                "packing_max_batch_len": packing_max_batch_len,
                "grad_accum": grad_accum,
                "num_batches": len(train_loader),
                "avg_samples_per_batch": len(dataset) / len(train_loader),
                "samples_per_gpu": args.samples_per_gpu,
            }
        )

    model, lr_scheduler, optimizer, accelerator = setup_model(
        args, tokenizer, train_loader, grad_accum, flash_enabled
    )

    # train(
    #     args,
    #     model,
    #     optimizer,
    #     lr_scheduler,
    #     accelerator,
    #     tokenizer,
    #     train_loader,
    #     grad_accum,
    #     metric_logger,
    # )


    get_perplexity(args, model, tokenizer, train_loader, accelerator, metric_logger)


    torch.distributed.barrier()
    torch.distributed.destroy_process_group()


# public API
def run_training(torch_args: TorchrunArgs, train_args: TrainingArgs) -> None:
    """
    Wrapper around the main training job that calls torchrun.
    """
    # early validation logic here
    if train_args.max_batch_len < train_args.max_seq_len:
        raise ValueError(
            f"the `max_batch_len` cannot be less than `max_seq_len`: {train_args.max_batch_len=} < {train_args.max_seq_len=}"
        )

    # process the training data
    if not os.path.exists(train_args.data_output_dir):
        os.makedirs(train_args.data_output_dir, exist_ok=True)
    dp.main(
        DataProcessArgs(
            # XXX(osilkin): make a decision here, either:
            #   1. the CLI is fully responsible for managing where the data is written
            #   2. we never cache it and simply write it to a tmp file every time.
            #
            # An important reason for why #1 would be preferable is in the case of OpenShift/SELinux
            # where the user has a defined place for new temporary data to be written.
            data_output_path=train_args.data_output_dir,
            model_path=train_args.model_path,
            data_path=train_args.data_path,
            max_seq_len=train_args.max_seq_len,
            chat_tmpl_path=train_args.chat_tmpl_path,
        )
    )

    if not os.path.exists(train_args.ckpt_output_dir):
        os.makedirs(train_args.ckpt_output_dir, exist_ok=True)
    command = [
        "torchrun",
        f"--nnodes={torch_args.nnodes}",
        f"--node_rank={torch_args.node_rank}",
        f"--nproc_per_node={torch_args.nproc_per_node}",
        f"--rdzv_id={torch_args.rdzv_id}",
        f"--rdzv_endpoint={torch_args.rdzv_endpoint}",
        __file__,
        f"--model_name_or_path={train_args.model_path}",
        f"--data_path={train_args.data_output_dir}/data.jsonl",
        f"--output_dir={train_args.ckpt_output_dir}",
        f"--num_epochs={train_args.num_epochs}",
        f"--effective_batch_size={train_args.effective_batch_size}",
        f"--learning_rate={train_args.learning_rate}",
        f"--num_warmup_steps={train_args.warmup_steps}",
        f"--save_samples={train_args.save_samples}",
        f"--log_level=INFO",
        f"--max_batch_len={train_args.max_batch_len}",
        f"--seed={train_args.random_seed}",
        f"--chat-tmpl-path={train_args.chat_tmpl_path}",
    ]

    if train_args.checkpoint_at_epoch:
        command.append("--checkpoint_at_epoch")

    if train_args.accelerate_full_state_at_epoch:
        command.append("--accelerate_full_state_at_epoch")

    if train_args.mock_data:
        command.append("--mock_data")
        if train_args.mock_len:
            command.append(f"--mock_len={train_args.mock_len}")

    if train_args.use_dolomite:
        command.append("--use_dolomite")

    if train_args.disable_flash_attn:
        if train_args.use_dolomite:
            raise RuntimeError(
                "ERROR: Trying to use padding-free transformer without flash attention is not supported"
            )
        command.append("--disable_flash_attn")

    if train_args.lora:
        command.extend(
            [
                f"--lora_r={train_args.lora.rank}",
                f"--lora_alpha={train_args.lora.alpha}",
                f"--lora_dropout={train_args.lora.dropout}",
                "--lora_target_modules",
            ]
        )
        if train_args.lora.target_modules:
            command.extend(train_args.lora.target_modules)
        # hard-code 4-bit quantization for now, change this when we add more
        quant_dtype = train_args.lora.quantize_data_type
        quantization_is_enabled = quant_dtype in (
            config.QuantizeDataType.NF4,
            config.QuantizeDataType.NF4.value,
        )
        if quantization_is_enabled:
            command.append("--lora_quant_bits=4")

    # specify which distributed training backend we use
    command.append(
        f"--distributed_training_framework={train_args.distributed_backend.value}"
    )

    # deepspeed options
    if train_args.deepspeed_options.save_samples:
        command.append(f"--save_samples_ds={train_args.deepspeed_options.save_samples}")
    if train_args.deepspeed_options.cpu_offload_optimizer:
        command.extend(
            [
                "--cpu_offload_optimizer",
                f"--cpu_offload_optimizer_ratio={train_args.deepspeed_options.cpu_offload_optimizer_ratio}",
            ]
        )
        if train_args.deepspeed_options.cpu_offload_optimizer_pin_memory:
            command.append("--cpu_offload_optimizer_pin_memory")

    # FSDP Options
    if train_args.fsdp_options.cpu_offload_params:
        command.extend(
            [
                "--cpu_offload_params_fsdp",
            ]
        )

    # specify the sharding strategy
    command.append(
        f"--fsdp_sharding_strategy={train_args.fsdp_options.sharding_strategy.value}"
    )

    print(f"\033[92mRunning training command as subprocess: {' '.join(command)}\033[0m")
    process = None
    interrupt: KeyboardInterrupt | Exception | None = None
    try:
        process = StreamablePopen(
            f"{train_args.ckpt_output_dir}/full_logs_global{torch_args.node_rank}.log",
            command,
        )
        process.listen()
    except KeyboardInterrupt as e:
        print("Training subprocess interrupted by user.")
        interrupt = e
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        interrupt = e
    finally:
        if "process" not in locals() or process is None:
            return
        if process.poll() == 0:
            print("\033[92mTraining subprocess exited successfully! 🎉\033[0m")
        else:
            print(
                "\033[91mTraining subprocess has not exited yet. Sending SIGTERM.\033[0m"
            )

        print("Sending interrupt signal to Training subprocess.")
        process.terminate()
        try:
            print("Waiting for process to exit, 60s...")
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            print(
                "\033[91mTraining subprocess did not terminate before timeout, sending SIGKILL.\033[0m"
            )
            process.kill()

        if interrupt:
            print(f"Error caught from training subprocess.: {interrupt}")
            raise interrupt


if __name__ == "__main__":
    # TODO(osilkin): Configure a type that these args must adhere to for the sake of type checking
    #               Maybe switch out from argparse to something smarter
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_name_or_path", type=str)
    parser.add_argument("--data_path", type=str)
    parser.add_argument("--output_dir", type=str)
    parser.add_argument("--num_epochs", type=int, default=1)
    parser.add_argument(
        "--current_epoch",
        type=int,
        default=0,
        help="Helpful flag for resuming on a later epoch. Sets dataloader correctly.",
    )
    parser.add_argument(
        "--last_step",
        type=int,
        default=0,
        help="understand this as the last completed step. "
        "The default is 0, since global_step starts from 1 by default.",
    )
    # parser.add_argument("--samples_per_gpu", type=int, default=8)
    parser.add_argument("--effective_batch_size", type=int, default=3840)
    parser.add_argument("--learning_rate", type=float, default=1e-4)
    parser.add_argument(
        "--lr_scheduler",
        type=str,
        default="cosine",
        help="The scheduler type to use.",
        choices=[
            "linear",
            "cosine",
            "cosine_with_restarts",
            "polynomial",
            "constant",
            "constant_with_warmup",
        ],
    )
    parser.add_argument("--num_warmup_steps", type=int, default=1000)
    # parser.add_argument("--gradient_accumulation_steps", type=int, default=1)
    parser.add_argument(
        "--save_samples",
        type=int,
        help="The number of samples seen between each checkpoint save. If --save_samples<=0, this feature is disabled.",
    )
    parser.add_argument(
        "--save_samples_ds",
        type=int,
        help="for saving in ds native format",
        default=None,
    )
    parser.add_argument(
        "--save_last", action="store_true", help="save after finishing training"
    )
    parser.add_argument(
        "--checkpoint_at_epoch",
        action="store_true",
        help="Save a model checkpoint after finishing an epoch.",
    )
    parser.add_argument(
        "--accelerate_full_state_at_epoch",
        action="store_true",
        help="Save full model state using Accelerate after finishing an epoch.",
    )
    parser.add_argument("--log_level", type=str, default="INFO")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--mock_data", action="store_true")
    parser.add_argument("--mock_len", type=int, default=2600)
    parser.add_argument(
        "--distributed_training_framework",
        type=str,
        choices=[
            DistributedBackend.DEEPSPEED.value,
            DistributedBackend.FSDP.value,
        ],
        default=DistributedBackend.DEEPSPEED.value,
    )
    parser.add_argument(
        "--fsdp_sharding_strategy",
        type=str,
        # choices=[e.name for e in ShardingStrategy],
        default="SHARD_GRAD_OP",
        help="Sharding strategy to be used for FSDP distributed training.",
    )
    parser.add_argument("--use_dolomite", action="store_true")
    parser.add_argument("--lora_r", type=int, default=0)  # set to > 0 to activate lora
    parser.add_argument("--lora_alpha", type=int, default=32)
    parser.add_argument("--lora_dropout", type=float, default=0.1)
    parser.add_argument("--lora_quant_bits", type=int, default=None)
    parser.add_argument(
        "--lora_target_modules",
        nargs="*",
        default=None,
        help="Which modules we should target for injecting LoRA layers. Defaults to selecting all projection layers when no values are provided.",
    )
    parser.add_argument("--max_batch_len", type=int, default=60000)
    parser.add_argument(
        "--cpu_offload_optimizer",
        action="store_true",
        default=False,
        help="Offload optimizer to CPU when using DeepSpeed. This configures it to use ZeRO stage 2.",
    )
    parser.add_argument(
        "--cpu_offload_params_fsdp",
        action="store_true",
        default=False,
        help="Offload to CPU when using FSDP.",
    )
    parser.add_argument(
        "--cpu_offload_optimizer_pin_memory",
        action="store_true",
        default=False,
        help="Pin memory when offloading optimizer to CPU. This allows for faster transfers between CPU and GPU. Comes at the cost of higher memory usage and CPU overhead.",
    )
    parser.add_argument(
        "--cpu_offload_optimizer_ratio",
        type=float,
        default=1.0,
        help="Ratio of the optimizer to be offloaded to CPU. The rest will be on GPU(s).",
    )
    parser.add_argument("--NEFTune_alpha", type=float, default=None)
    parser.add_argument(
        "--chat-tmpl-path",
        type=str,
        default=os.path.join(
            os.path.dirname(__file__), "chat_templates/ibm_generic_tmpl.py"
        ),
    )
    parser.add_argument("--disable_flash_attn", action="store_true")
    args = parser.parse_args()
    set_random_seed(args.seed)
    main(args)

"""
pkill python
git reset --hard
git pull
export WORLD_SIZE=1
sleep 3
mkdir -p /new_data/experiments/ap-fsdp-p00-old-m-ds-2t
cd /app/fsdp
export WORLD_SIZE=1
torchrun --nnodes=$WORLD_SIZE --node_rank=$RANK \
--nproc_per_node=8 --rdzv_id=101 \
--rdzv_endpoint="$MASTER_ADDR:$MASTER_PORT" main_ds.py \
--model_name_or_path=mistralai/Mistral-7B-v0.1 \
--data_path="/dev/shm/data.jsonl" \
--output_dir="/new_data/experiments/ap-fsdp-p00-old-m-ds-2t" \
--num_epochs=100 \
--samples_per_gpu=24 \
--learning_rate=1e-06 \
--num_warmup_steps=800 \
--gradient_accumulation_steps=2 \
--save_samples=12000 \
--log_level="INFO" \
--mock_data \
--mock_len=2048 \
--seed=42 | tee /new_data/experiments/ap-fsdp-p00-old-m-ds-2t/$RANK.log
export WORLD_SIZE=1
torchrun --nnodes=$WORLD_SIZE --node_rank=$RANK \
--nproc_per_node=8 --rdzv_id=101 \
--rdzv_endpoint="$MASTER_ADDR:$MASTER_PORT" main_ds.py \
--model_name_or_path=/new_data/models/granite7b/ibm_models_version/ \
--data_path="/dev/shm/data.jsonl" \
--output_dir="/new_data/experiments/ap-granite-4t" \
--num_epochs=100 \
--samples_per_gpu=240 \
--learning_rate=2e-05 \
--num_warmup_steps=385 \
--gradient_accumulation_steps=2 \
--save_samples=250000 \
--log_level="INFO" \
--fsdp_sharding_strategy="SHARD_GRAD_OP" \
--use_dolomite \
--max_batch_len 70000 \
--seed=42
"""