# Copyright 2025 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import random
import time
import textwrap
from collections import defaultdict
from typing import Any, Callable, Optional, Union

import torch
import torch.utils.data
import transformers
from datasets import Dataset, IterableDataset
from packaging import version
from transformers import (
    AutoModelForCausalLM,
    AutoModelForSequenceClassification,
    AutoTokenizer,
    PreTrainedModel,
    PreTrainedTokenizerBase,
    Trainer,
    TrainerCallback,
    is_wandb_available,
)
from transformers.integrations.deepspeed import is_deepspeed_zero3_enabled
from transformers.utils import is_peft_available

from trl.models import create_reference_model, prepare_deepspeed, unwrap_model_for_generation
from trl.trainer.utils import generate_model_card, get_comet_experiment_url
from trl.data_utils import apply_chat_template, is_conversational, maybe_apply_chat_template

from .utils import prepare_deepspeed,create_prefix_mask
from .grpo_config import GRPOConfig


if is_peft_available():
    from peft import PeftConfig, get_peft_model

if is_wandb_available():
    import wandb

# What we call a reward function is a callable that takes a list of prompts and completions and returns a list of
# rewards. When it's a string, it's a model ID, so it's loaded as a pretrained model.
RewardFunc = Union[str, PreTrainedModel, Callable[[list, list], list[float]]]


class GRPOTrainer(Trainer):
    """
    Trainer for the Group Relative Policy Optimization (GRPO) method. This algorithm was initially proposed in the
    paper [DeepSeekMath: Pushing the Limits of Mathematical Reasoning in Open Language Models](https://huggingface.co/papers/2402.03300).

    Example:

    ```python
    from datasets import load_dataset
    from trl import GRPOTrainer

    dataset = load_dataset("trl-lib/tldr", split="train")

    trainer = GRPOTrainer(
        model="Qwen/Qwen2-0.5B-Instruct",
        train_dataset=dataset,
    )

    trainer.train()
    ```

    Args:
        model (`Union[str, PreTrainedModel]`):
            Model to be trained. Can be either:

            - A string, being the *model id* of a pretrained model hosted inside a model repo on huggingface.co, or
              a path to a *directory* containing model weights saved using
              [`~transformers.PreTrainedModel.save_pretrained`], e.g., `'./my_model_directory/'`. The model is
              loaded using [`~transformers.AutoModelForCausalLM.from_pretrained`] with the keywork arguments
              in `args.model_init_kwargs`.
            - A [`~transformers.PreTrainedModel`] object. Only causal language models are supported.
        args ([`GRPOConfig`], *optional*, defaults to `None`):
            Configuration for this trainer. If `None`, a default configuration is used.
        train_dataset ([`~datasets.Dataset`] or [`~datasets.IterableDataset`]):
            Dataset to use for training. It must include a column `"prompt"`. Any additional columns in the dataset is
            ignored. The format of the samples can be either:

            - [Standard](dataset_formats#standard): Each sample contains plain text.
            - [Conversational](dataset_formats#conversational): Each sample contains structured messages (e.g., role
              and content).
        eval_dataset ([`~datasets.Dataset`], [`~datasets.IterableDataset`] or `dict[str, Union[Dataset, IterableDataset]]`):
            Dataset to use for evaluation. It must meet the same requirements as `train_dataset`.
        processing_class ([`~transformers.PreTrainedTokenizerBase`], *optional*, defaults to `None`):
            Processing class used to process the data. The padding side must be set to "left". If `None`, the
            processing class is loaded from the model's name with [`~transformers.AutoTokenizer.from_pretrained`].
        callbacks (list of [`~transformers.TrainerCallback`], *optional*, defaults to `None`):
            List of callbacks to customize the training loop. Will add those to the list of default callbacks
            detailed in [here](https://huggingface.co/docs/transformers/main_classes/callback).

            If you want to remove one of the default callbacks used, use the [`~transformers.Trainer.remove_callback`]
            method.
        optimizers (`tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LambdaLR]`, *optional*, defaults to `(None, None)`):
            A tuple containing the optimizer and the scheduler to use. Will default to an instance of [`AdamW`] on your
            model and a scheduler given by [`get_linear_schedule_with_warmup`] controlled by `args`.
        peft_config ([`~peft.PeftConfig`], *optional*, defaults to `None`):
            PEFT configuration used to wrap the model. If `None`, the model is not wrapped.
    """

    _tag_names = ["trl", "grpo"]

    def __init__(
        self,
        model: Union[str, PreTrainedModel],
        args: GRPOConfig = None,
        train_dataset: Optional[Union[Dataset, IterableDataset]] = None,
        eval_dataset: Optional[Union[Dataset, IterableDataset, dict[str, Union[Dataset, IterableDataset]]]] = None,
        processing_class: Optional[PreTrainedTokenizerBase] = None,
        reward_processing_classes: Optional[Union[PreTrainedTokenizerBase, list[PreTrainedTokenizerBase]]] = None,
        callbacks: Optional[list[TrainerCallback]] = None,
        optimizers: tuple[Optional[torch.optim.Optimizer], Optional[torch.optim.lr_scheduler.LambdaLR]] = (None, None),
        peft_config: Optional["PeftConfig"] = None,
    ):
        # Args
        if args is None:
            model_name = model if isinstance(model, str) else model.config._name_or_path
            model_name = model_name.split("/")[-1]
            args = GRPOConfig(f"{model_name}-GRPO")
        
        # Models
        # Trained model
        model_init_kwargs = args.model_init_kwargs or {}
        if isinstance(model, str):
            model_id = model
            torch_dtype = model_init_kwargs.get("torch_dtype")
            if isinstance(torch_dtype, torch.dtype) or torch_dtype == "auto" or torch_dtype is None:
                pass  # torch_dtype is already a torch.dtype or "auto" or None
            elif isinstance(torch_dtype, str):  # it's a str, but not "auto"
                torch_dtype = getattr(torch, torch_dtype)
                model_init_kwargs["torch_dtype"] = torch_dtype
            else:
                raise ValueError(
                    "Invalid `torch_dtype` passed to `GRPOConfig`. Expected either 'auto' or a string representing "
                    f"a `torch.dtype` (e.g., 'float32'), but got {torch_dtype}."
                )
            # Disable caching if gradient checkpointing is enabled (not supported)
            model_init_kwargs["use_cache"] = (
                False if args.gradient_checkpointing else model_init_kwargs.get("use_cache")
            )
            model = AutoModelForCausalLM.from_pretrained(model, **model_init_kwargs)
        else:
            model_id = model.config._name_or_path
            if args.model_init_kwargs is not None:
                raise ValueError(
                    "You passed `model_init_kwargs` to the `GRPOConfig`, but your model is already instantiated. "
                    "This argument can only be used when the `model` argument is a string."
                )

        if peft_config is not None:
            model = get_peft_model(model, peft_config)

        # Reference model
        if is_deepspeed_zero3_enabled():
            self.ref_model = AutoModelForCausalLM.from_pretrained(model_id, **model_init_kwargs)
        elif peft_config is None:
            # If PEFT configuration is not provided, create a reference model based on the initial model.
            self.ref_model = create_reference_model(model)
        else:
            # If PEFT is used, the reference model is not needed since the adapter can be disabled
            # to revert to the initial model.
            self.ref_model = None

        # Processing class
        if processing_class is None:
            processing_class = AutoTokenizer.from_pretrained(model.config._name_or_path, padding_side="left")

        self.peft_config = peft_config

        # Data collator
        def data_collator(features):  # No data collation is needed in GRPO
            # ans = {}
            # for key in features[0]:
            #     ans[key] = [item[key] for item in features]
            return features
            # return ans

        self.beta = args.beta

        # The trainer estimates the number of FLOPs (floating-point operations) using the number of elements in the
        # input tensor associated with the key "input_ids". However, in GRPO, the sampled data does not include the
        # "input_ids" key. Instead, the available keys is "prompt". As a result, the trainer issues the warning:
        # "Could not estimate the number of tokens of the input, floating-point operations will not be computed." To
        # suppress this warning, we set the "estimate_tokens" key in the model's "warnings_issued" dictionary to True.
        # This acts as a flag to indicate that the warning has already been issued.
        model.warnings_issued["estimate_tokens"] = True

        # Initialize the metrics
        self._metrics = defaultdict(list)

        super().__init__(
            model=model,
            args=args,
            data_collator=data_collator,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset,
            processing_class=processing_class,
            callbacks=callbacks,
            optimizers=optimizers,
        )

        # Gradient accumulation requires scaled loss. Normally, loss scaling in the parent class depends on whether the
        # model accepts loss-related kwargs. Since we compute our own loss, this check is irrelevant. We set
        # self.model_accepts_loss_kwargs to False to enable scaling.
        self.model_accepts_loss_kwargs = False

        # Add tags to the model
        self.model.add_model_tags(self._tag_names)

        if self.ref_model is not None:
            if self.is_deepspeed_enabled:
                self.ref_model = prepare_deepspeed(self.ref_model, self.accelerator)
            else:
                self.ref_model = self.accelerator.prepare_model(self.ref_model, evaluation_mode=True)

    def _set_signature_columns_if_needed(self):
        # If `self.args.remove_unused_columns` is True, non-signature columns are removed.
        # By default, this method sets `self._signature_columns` to the model's expected inputs.
        # In GRPOTrainer, we preprocess data, so using the model's signature columns doesn't work.
        # Instead, we set them to the columns expected by the `training_step` method, hence the override.
        if self._signature_columns is None:
            self._signature_columns = ["prompt"]

    # # Trainer "prepares" the inputs before calling `compute_loss`. It converts to tensor and move to device.
    # # Since we preprocess the data in `compute_loss`, we need to override this method to skip this step.
    # def _prepare_inputs(self, inputs: dict[str, Union[torch.Tensor, Any]]) -> dict[str, Union[torch.Tensor, Any]]:
    #     return inputs

    def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
        """inputs = [
                {'completion': [ {'role': 'user', 'content': 'question'}, {role': 'assistant', 'content': 'answer'}], 'advantage': 0.8, 'label': '12'},
                ...   
            ]
        """

        if return_outputs:
            raise ValueError("The GRPOTrainer does not support returning outputs")
        
        prompts_text = [maybe_apply_chat_template({'messages': example['completion'] }, self.processing_class)["text"][:self.args.max_seq_length] for example in inputs]

        prompt_inputs = self.processing_class(
            prompts_text, return_tensors="pt", padding=True, padding_side="left", add_special_tokens=False
        )['input_ids']
        # only for single turn
        assistant_id = self.processing_class('<｜Assistant｜>', add_special_tokens=False)['input_ids'][0]
        prefix_mask = create_prefix_mask(prompt_inputs, assistant_id)
        prompt_completion_ids = super()._prepare_inputs(prompt_inputs)
        prefix_mask = super()._prepare_inputs(prefix_mask)[:, 1: ] # [bs, sl]
        
        device = self.accelerator.device

        # Get the per-token log probabilities for the completions for the model and the reference model
        def get_per_token_logps(model, input_ids):
            logits = model(input_ids).logits  # (B, L, V)
            logits = logits[:, :-1, :]  # (B, L-1, V), exclude the last logit: it corresponds to the next token pred
            input_ids = input_ids[:, 1:]  # (B, L-1), exclude the first input ID since we don't have logits for it
            # Compute the log probabilities for the input tokens. Use a loop to reduce memory peak.
            per_token_logps = []
            for logits_row, input_ids_row in zip(logits, input_ids):
                log_probs = logits_row.log_softmax(dim=-1)
                token_log_prob = torch.gather(log_probs, dim=1, index=input_ids_row.unsqueeze(1)).squeeze(1)
                per_token_logps.append(token_log_prob)
            return torch.stack(per_token_logps)

        per_token_logps = get_per_token_logps(model, prompt_completion_ids)
        # # Get rid of the prompt (-1 because of the shift done in get_per_token_logps)
        # per_token_logps = per_token_logps[:, prompt_length - 1 :]

        # [2 - last] logps

        with torch.inference_mode():
            if self.ref_model is not None:
                ref_per_token_logps = get_per_token_logps(self.ref_model, prompt_completion_ids)
            else:
                with self.accelerator.unwrap_model(model).disable_adapter():
                    ref_per_token_logps = get_per_token_logps(model, prompt_completion_ids)
        # ref_per_token_logps = ref_per_token_logps[:, prompt_length - 1 :]

        # Compute the KL divergence between the model and the reference model
        per_token_kl = torch.exp(ref_per_token_logps - per_token_logps) - (ref_per_token_logps - per_token_logps) - 1

        advantages = [example['advantage'] for example in inputs]
        advantages = torch.tensor(advantages, dtype=torch.float32, device=device) # batch_size

        # x - x.detach() allows for preserving gradients from x
        per_token_loss = torch.exp(per_token_logps - per_token_logps.detach()) * advantages.detach().unsqueeze(1)
        per_token_loss = -(per_token_loss - self.beta * per_token_kl)
        
        loss = ((per_token_loss * prefix_mask).sum(dim=1) / prefix_mask.sum(dim=1)).mean()
        # Log the metrics
        completion_length = self.accelerator.gather_for_metrics(prefix_mask.sum(1)).float().mean().item()
        self._metrics["completion_length"].append(completion_length)

        mean_kl = ((per_token_kl * prefix_mask).sum(dim=1) / prefix_mask.sum(dim=1)).mean()
        self._metrics["kl"].append(self.accelerator.gather_for_metrics(mean_kl).mean().item())

        self.update_model()

        return loss
        
    def get_last_time(self):
        lora_path = os.path.join(self._get_output_dir(None), 'lora')
        if self.peft_config is None:
            check_path = os.path.join(self._get_output_dir(None), 'config.json')
        else:
            check_path = os.path.join(self._get_output_dir(None), 'lora/adapter_config.json')
        if os.path.exists(check_path):
            last_time = os.path.getmtime(check_path)
        else:
            last_time = 0.0
        return last_time

    def update_model(self):
        if not hasattr(self, 'start_train_time'):
            self.start_train_time = time.time()

        # first
        save_interval_time = self.args.int_time # s
        if time.time() - self.start_train_time > save_interval_time and not hasattr(self, 'start_save'):
            self.start_save = True
            if self.peft_config is not None:
                self.save_model(os.path.join(self._get_output_dir(None), 'lora'))
            else:
                self.save_model(self._get_output_dir(None))
            self.tokenizer.save_pretrained(self._get_output_dir(None))
        # next time
        elif time.time() - self.get_last_time() > save_interval_time and hasattr(self, 'start_save'):
            if self.peft_config is not None:
                self.save_model(os.path.join(self._get_output_dir(None), 'lora'))
            else:
                self.save_model(self._get_output_dir(None))
            self.tokenizer.save_pretrained(self._get_output_dir(None))
    
    def log(self, logs: dict[str, float], start_time: Optional[float] = None) -> None:
        metrics = {key: sum(val) / len(val) for key, val in self._metrics.items()}  # average the metrics
        logs = {**logs, **metrics}
        if version.parse(transformers.__version__) >= version.parse("4.47.0.dev0"):
            super().log(logs, start_time)
        else:  # transformers<=4.46
            super().log(logs)
        self._metrics.clear()

    def create_model_card(
        self,
        model_name: Optional[str] = None,
        dataset_name: Optional[str] = None,
        tags: Union[str, list[str], None] = None,
    ):
        """
        Creates a draft of a model card using the information available to the `Trainer`.

        Args:
            model_name (`str` or `None`, *optional*, defaults to `None`):
                Name of the model.
            dataset_name (`str` or `None`, *optional*, defaults to `None`):
                Name of the dataset used for training.
            tags (`str`, `list[str]` or `None`, *optional*, defaults to `None`):
                Tags to be associated with the model card.
        """
        if not self.is_world_process_zero():
            return

        if hasattr(self.model.config, "_name_or_path") and not os.path.isdir(self.model.config._name_or_path):
            base_model = self.model.config._name_or_path
        else:
            base_model = None

        tags = tags or []
        if isinstance(tags, str):
            tags = [tags]

        if hasattr(self.model.config, "unsloth_version"):
            tags.append("unsloth")

        citation = textwrap.dedent(
            """\
            @article{zhihong2024deepseekmath,
                title        = {{DeepSeekMath: Pushing the Limits of Mathematical Reasoning in Open Language Models}},
                author       = {Zhihong Shao and Peiyi Wang and Qihao Zhu and Runxin Xu and Junxiao Song and Mingchuan Zhang and Y. K. Li and Y. Wu and Daya Guo},
                year         = 2024,
                eprint       = {arXiv:2402.03300},
            """
        )

        model_card = generate_model_card(
            base_model=base_model,
            model_name=model_name,
            hub_model_id=self.hub_model_id,
            dataset_name=dataset_name,
            tags=tags,
            wandb_url=wandb.run.get_url() if is_wandb_available() and wandb.run is not None else None,
            comet_url=get_comet_experiment_url(),
            trainer_name="GRPO",
            trainer_citation=citation,
            paper_title="DeepSeekMath: Pushing the Limits of Mathematical Reasoning in Open Language Models",
            paper_id="2402.03300",
        )

        model_card.save(os.path.join(self.args.output_dir, "README.md"))
